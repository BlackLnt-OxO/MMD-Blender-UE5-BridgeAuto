"""Read only linked volume objects named by an existing export manifest."""
import os,sys
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.blender_appearance import uniform_volume,output
job=Path(os.environ['MMDBRIDGE_FOG_SOURCE_JOB']);manifest=read_json(job/'manifest.json')
source=read_json(job/'job.json')['settings']['scene']
names={v['name'] for v in manifest['scene'].get('volumes',[]) if v.get('kind') in ('grid','local_grid')}
with bpy.data.libraries.load(source,link=True) as (available,target):
    target.objects=[name for name in available.objects if name in names]
records=[]
for obj in target.objects:
    if obj is None or obj.type!='MESH':continue
    coords=[tuple(v.co) for v in obj.data.vertices]
    corners=set(tuple(float(x) for x in p) for p in obj.bound_box)
    box=len(coords)==8 and set(coords)==corners and len(obj.data.polygons) in (6,12)
    materials=[]
    for mat in obj.data.materials:
        if not mat or not mat.use_nodes:continue
        end=output(mat.node_tree,'ShaderNodeOutputMaterial','CYCLES')
        if not end or not end.inputs['Volume'].is_linked:continue
        try:coefficients=uniform_volume(end.inputs['Volume']);error=None
        except (ValueError,TypeError) as exc:coefficients=None;error=str(exc)
        materials.append(dict(name=mat.name,uniform_coefficients=coefficients,error=error))
    records.append(dict(name=obj.name,vertices=len(coords),faces=len(obj.data.polygons),
        box_proven=box,bounds=[list(p) for p in obj.bound_box],modifiers=[m.type for m in obj.modifiers],
        materials=materials))
atomic_json(Path(os.environ['MMDBRIDGE_FOG_SOURCE_OUT']),dict(source=source,linked_only=True,objects=records))
print('FOG_SOURCE_ANALYSIS_READY')
