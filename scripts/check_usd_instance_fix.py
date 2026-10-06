"""Run with Blender 4.3: blender --background --factory-startup --python scripts/check_usd_instance_fix.py."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Matrix
from pxr import Usd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.blender_usd import _mesh_data, _mesh_span, _scene_wide_instance_array, _write_chunk

bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
source.modifiers.new("Array", "ARRAY")
wide = source.data.copy()
for vertex in wide.vertices:
    vertex.co.x *= 100
    vertex.co.y *= 100
assert _scene_wide_instance_array(SimpleNamespace(is_instance=True), source, wide)
assert not _scene_wide_instance_array(SimpleNamespace(is_instance=False), source, wide)
assert _mesh_span(source.data) == (2.0, 2.0, 2.0)

group = {"id": "cube", "geometry": _mesh_data(source.data), "materials": [],
         "polygon_materials": [0] * 6, "collision": True,
         "instances": [{"index": 0, "name": "Cube", "source_id": "cube",
                        "matrix": [list(row) for row in Matrix.Identity(4)]}]}
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "chunk.usdc"
    payload_root=_write_chunk(path, 0, [group], 1, True, "/Game/Test", "test")
    stage = Usd.Stage.Open(str(path))
    assert stage.GetPrimAtPath(payload_root+"/Instances/I_0000000_Cube").IsInstance()
    assert not any("__Prototypes" in str(prim.GetPath()) for prim in stage.Traverse())
    assert stage.GetPrimAtPath(group['prim_path']+"/Mesh").IsValid()
print("USD_INSTANCE_FIX_OK")

# UE stores TRS: scaled parents plus rotated children must never discard shear.
import math
import numpy as np
from mathutils import Vector
from pxr import Gf, UsdGeom
from mmd_bridge.blender_usd import _rigid_matrix, _instance_geometry, _write_geometry_index, _matrix, _bounds
from mmd_bridge.core import atomic_json

output=Path(__file__).resolve().parents[1]/'work/usd_affine_smoke'
(output/'Geometry').mkdir(parents=True,exist_ok=True)
parent=Matrix.Translation((2,4,1)) @ Matrix.Rotation(.7,4,'Z') @ Matrix.Diagonal((2.,7.,.3,1.))
owner=_rigid_matrix(parent)
worlds=[Matrix.Translation((1,2,.03)) @ Matrix.Rotation(math.pi/2,4,'X') @ Matrix.Diagonal((.1,.1,.1,1.)),
        parent @ Matrix.Rotation(.9,4,'Y'),
        Matrix.Translation((-2,1,0)) @ Matrix.Rotation(.4,4,'Z') @ Matrix.Diagonal((-2.,.3,1.,1.))]
groups={}
parts=[]
expected=[]
for number,world in enumerate(worlds):
    original=dict(group,source_types={'MESH'})
    converted,trs=_instance_geometry(original,world,groups)
    cached=groups.setdefault(converted['id'],dict(converted,instances=[]))
    local=owner.inverted() @ trs
    loc,rot,scale=local.decompose()
    assert np.allclose(local,Matrix.LocRotScale(loc,rot,scale),atol=1e-5)
    # Check actual vertices, not merely decomposed transforms or an AABB.
    for before,after in zip(original['geometry']['points'],cached['geometry']['points']):
        assert np.allclose(world @ Vector(before),owner @ local @ Vector(after),atol=2e-5)
    if number==1:
        assert converted is not original
        normals=np.asarray(cached['geometry']['normals'])
        assert np.allclose(np.linalg.norm(normals,axis=1),1,atol=1e-6)
        assert np.allclose(cached['geometry']['uvs'][0]['values'],original['geometry']['uvs'][0]['values'])
    path='/World/Objects/Emitter/Piece_'+str(number)
    parts.append({'prim_path':path,'label':'Piece '+str(number),'local_matrix':[list(row) for row in local],
                  'chunk':0,'prototype_id':cached['id']})
    points=np.asarray(cached['geometry']['points'])
    low,high=points.min(axis=0),points.max(axis=0)
    box=[(x,y,z) for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])]
    expected.append({'prim_path':path,'bounds':_bounds(box,trs),
                     'source_bounds':_bounds(original['geometry']['points'],world),
                     'affine_baked':converted is not original})
chunk=output/'Geometry/Payload_01.usdc'
_write_chunk(chunk,0,list(groups.values()),1,True,'/Game/Test','affine',definitions_only=True)
for part in parts:
    part['source_prim']=groups[part.pop('prototype_id')]['prim_path']
root=output/'Scene.usda'
_write_geometry_index(root,[{'path':str(chunk)}],1,hierarchy=[{'name':'Emitter','prim_path':'/World/Objects/Emitter',
                       'local_matrix':[list(row) for row in owner],'parts':parts}])
stage=Usd.Stage.Open(str(root))
cache=UsdGeom.XformCache()
for item in expected:
    prim = stage.GetPrimAtPath(item['prim_path'])
    assert prim.IsInstance(), item['prim_path']
    assert stage.GetPrimAtPath(item['prim_path']+'/Mesh').IsInstanceProxy()
    world=cache.GetLocalToWorldTransform(stage.GetPrimAtPath(item['prim_path']))
    assert np.allclose(world,_matrix(owner @ Matrix(next(p['local_matrix'] for p in parts if p['prim_path']==item['prim_path']))),atol=1e-5)
atomic_json(output/'expected.json',{'root_layer':str(root),'instances':expected})
print('USD_AFFINE_NATIVE_PASS',flush=True)

# Sharing geometry must not turn an editable parent/child hierarchy into an
# instance subtree. Independent leaves should reference one native prototype.
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    (folder/'Geometry').mkdir()
    shared_chunk = folder/'Geometry/Payload_01.usdc'
    shared = dict(group, instances=[])
    _write_chunk(shared_chunk,0,[shared],1,True,'/Game/Test','sharing',definitions_only=True)
    paths = ['/World/Objects/Parent', '/World/Objects/Parent/Child',
             '/World/Objects/Other', '/World/Objects/Other2']
    hierarchy = []
    for path in paths:
        hierarchy.append({'name':path.rsplit('/',1)[-1], 'prim_path':path,
            'local_matrix':[list(row) for row in Matrix.Identity(4)],
            'parts':[{'prim_path':path,'label':'Mesh','chunk':0,
                      'source_prim':shared['prim_path']}]})
    index = folder/'Geometry.usda'
    _write_geometry_index(index,[{'path':str(shared_chunk)}],1,hierarchy=hierarchy)
    sharing = Usd.Stage.Open(str(index))
    assert not sharing.GetPrimAtPath(paths[0]).IsInstance()
    prototypes = []
    for path in paths[1:]:
        prim = sharing.GetPrimAtPath(path)
        assert prim.IsInstance(), path
        assert sharing.GetPrimAtPath(path+'/Mesh').IsInstanceProxy()
        prototypes.append(str(prim.GetPrototype().GetPath()))
    assert len(set(prototypes)) == 1, prototypes
print('USD_SHARED_HIERARCHY_NATIVE_PASS',flush=True)
