"""Blender-hosted USD content, cancellation and instance-authoring benchmark."""
import importlib.util
import json
import sys
import time
from pathlib import Path
import bpy
from mathutils import Matrix
from pxr import Usd, UsdGeom, UsdPhysics
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge import blender_usd as new
from mmd_bridge.core import Job, Cancelled
out = root / 'work/upstream58/usd_perf'
out.mkdir(parents=True, exist_ok=True)
bpy.ops.mesh.primitive_cube_add()
mesh = bpy.context.object.data
data = new._mesh_data(mesh)
assert len(data['points']) == 8 and len(data['indices']) == 24
assert list(data['counts']) == [4] * 6
assert len(data['normals']) == 24 and len(data['uvs'][0]['values']) == 24

def groups(count, instances):
    result = []
    for i in range(count):
        result.append({'id': str(i), 'geometry': data, 'materials': ['a','b'],
                       'polygon_materials': [0,1,0,1,0,1], 'collision': True,
                       'instances': [{'index': i*instances+j, 'name': 'fixture', 'source_id': str(i),
                                      'matrix': [list(row) for row in Matrix.Translation((i,j,3))]}
                                     for j in range(instances)]})
    return result

results = {}
previous = root / 'work/upstream58/blender_usd_before_perf.py'
if previous.exists():
    spec = importlib.util.spec_from_file_location('mmd_bridge._old_usd', previous)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    start = time.perf_counter()
    old._write_chunk(out/'old.usdc', 0, groups(8,50), 1, True)
    results['old_400_instances_seconds'] = time.perf_counter()-start
for label, count, per in [('new_400',8,50), ('new_1800',600,3)]:
    job = Job(out/label)
    start = time.perf_counter()
    new._write_chunk(out/(label+'.usdc'), 0, groups(count,per), 1, True, job)
    results[label+'_instances_seconds'] = time.perf_counter()-start
    stage = Usd.Stage.Open(str(out/(label+'.usdc')))
    instance = stage.GetPrimAtPath('/World/Payload_000/Instances/I_0000000_fixture')
    assert instance.IsInstance() and instance.GetCustomDataByKey('MMDBridge:sourceId') == '0'
    assert UsdGeom.Xformable(instance).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation() == (0,0,3)
    exported = UsdGeom.Mesh(stage.GetPrimAtPath('/World/Payload_000/__Prototypes/P_0/Mesh'))
    assert list(exported.GetPointsAttr().Get()) == list(data['points'])
    assert list(exported.GetFaceVertexIndicesAttr().Get()) == list(data['indices'])
    assert list(exported.GetNormalsAttr().Get()) == list(data['normals'])
    assert list(UsdGeom.PrimvarsAPI(exported).GetPrimvar('st').Get()) == list(data['uvs'][0]['values'])
    assert UsdPhysics.CollisionAPI(exported).GetCollisionEnabledAttr().Get()
    subset = stage.GetPrimAtPath(str(exported.GetPath())+'/Material_1')
    assert list(UsdGeom.Subset(subset).GetIndicesAttr().Get()) == [1,3,5]
    assert str(subset.GetRelationship('material:binding').GetTargets()[0]) == '/World/Materials/M_b'
    assert len(list(stage.GetPrimAtPath('/World/Payload_000/Instances').GetChildren())) == count*per
    del stage
job = Job(out/'cancel')
job.folder.mkdir(parents=True, exist_ok=True)
target = out/'cancel.usdc'
target.write_bytes(b'previous-complete-payload')
def cancel(state, message, **extra):
    if '写入实例' in message:
        (job.folder/'stop').touch()
job.status = cancel
try:
    new._write_chunk(target, 0, groups(2,60), 1, True, job)
    raise AssertionError('cancellation not checked')
except Cancelled:
    assert target.read_bytes() == b'previous-complete-payload'
results['content_and_cancellation_passed'] = True
results['peak_working_set_mb'] = new._peak_working_set_mb()
(out/'result.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results), flush=True)
