"""Read railway and verify generic sheet inference for its two grass materials."""
import sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_materials import Materials
from mmd_bridge.material_policy import cutout_transmission
from mmd_bridge.blender_mesh_metrics import sheet_surface_evidence
from mmd_bridge.core import atomic_json

out=ROOT/'work/material_functions_implementation/grass_shadow';out.mkdir(parents=True,exist_ok=True)
start=time.perf_counter()
bpy.ops.wm.open_mainfile(filepath=r'F:\MMD_Scene\Blender\SableFiSh\railway\railway.blend',load_ui=False,use_scripts=False)
# Test closed-solid material subsets and non-manifold rejection in the native
# NumPy runtime too; the desktop test runtime may not have NumPy installed.
cube=[[0,1,2,3],[4,7,6,5],[0,4,5,1],[1,5,6,2],[2,6,7,3],[3,7,4,0]]
fixture=bpy.data.meshes.new('MMDBridge_SheetTopologyFixture')
fixture.from_pydata([(i%3,(i//3)%3,i//9) for i in range(12)],[],cube+[[8,9,10,11]])
for polygon,slot in zip(fixture.polygons,[0,1,1,1,1,1,2]):polygon.material_index=slot
assert not sheet_surface_evidence(fixture,[0])['thin_open_surface']
assert not sheet_surface_evidence(fixture)['thin_open_surface']
assert sheet_surface_evidence(fixture,[2])['thin_open_surface']
bpy.data.meshes.remove(fixture)
fixture=bpy.data.meshes.new('MMDBridge_NonmanifoldFixture')
fixture.from_pydata([(i,i%2,0) for i in range(5)],[],[[0,1,2],[1,0,3],[0,1,4]])
assert not sheet_surface_evidence(fixture)['thin_open_surface']
bpy.data.meshes.remove(fixture)
converter=Materials(out/'export','railway_grass_shadow',resolution=16,strategy='nodes')
converter.restore_scene_appearance=True
records=[];metrics=[]
for name in ('Grass_smdrailr','Wild_Grass_vlkhcbxia'):
    source=bpy.data.materials[name]
    original=next(o for o in bpy.data.objects if o.type=='MESH' and source in list(o.data.materials))
    obj=original.copy();obj.data=original.data.copy();bpy.context.scene.collection.objects.link(obj)
    material=source.copy()
    for index,candidate in enumerate(obj.data.materials):
        if candidate==source:obj.data.materials[index]=material
    metrics.append(dict(name=name,object=original.name,mesh=sheet_surface_evidence(obj.data)))
    record=converter.analyze(material,obj,source_name=name)
    assert record['surface_policy']['mesh_surface']['thin_open_surface'],metrics[-1]
    assert cutout_transmission(record),name
    assert record['channels']['opacity']['op']!='constant' and not record['baked_channels']
    records.append(record)
atomic_json(out/'blender_result.json',dict(complete=True,materials=records,metrics=metrics,
    seconds=time.perf_counter()-start,source_modified=False,bake_stats=converter.bake_stats,
    topology_fixtures={'closed_material_subset':True,'mixed_closed_open':True,'nonmanifold_rejected':True},
    ue_visual_validation='pending; user editor and modified materials preserved'))
print('GRASS_SHADOW_SOURCE_PASS',[(m['name'],m['mesh']['open_components']) for m in metrics],flush=True)
