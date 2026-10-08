"""Collection lights survive new export, frozen resume and old checkpoint migration."""
import sys
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Settings,Job,read_json,atomic_json
from mmd_bridge.blender_usd import export_usd_scene
OUT=ROOT/'work/world_appearance/instanced_light_native';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene;scene.render.engine='CYCLES'
bpy.ops.mesh.primitive_cube_add(size=1)
material=bpy.data.materials.new('OpaqueControl');material.use_nodes=True;bpy.context.object.data.materials.append(material)
camera=bpy.data.objects.new('Camera',bpy.data.cameras.new('Camera'));scene.collection.objects.link(camera);scene.camera=camera
collection=bpy.data.collections.new('LightPrototype');light=bpy.data.lights.new('SourcePoint','POINT');light.energy=100
point=bpy.data.objects.new('SourcePoint',light);collection.objects.link(point)
for i in range(3):
 obj=bpy.data.objects.new('LampInstance'+str(i),None);obj.instance_type='COLLECTION';obj.instance_collection=collection;obj.location=(i*2,0,2);scene.collection.objects.link(obj)
source=OUT/'fixture.blend';bpy.ops.wm.save_as_mainfile(filepath=str(source))
(OUT/'Transfer').mkdir(exist_ok=True)
settings=Settings(scene=str(source),flow='scene',scene_format='usd_stage',material_workflow='legacy_main_v1',
                  blender=bpy.app.binary_path,
                  character_output_dir=str(OUT/'Transfer'),restore_scene_appearance=True,scene_import_profile='render_optimized')
job=Job.create(OUT/'Jobs',settings)
def export():
 export_usd_scene(job)
 return [o for o in read_json(job.folder/'manifest.json')['scene']['objects'] if o['type']=='LIGHT']
first=export();second=export()
checkpoint=read_json(job.folder/'scene_checkpoint.json');checkpoint.pop('instanced_lights',None);checkpoint.pop('light_export_revision',None);atomic_json(job.folder/'scene_checkpoint.json',checkpoint)
third=export()
assert len(first)==len(second)==len(third)==3
assert first==second==third,(first,second,third)
atomic_json(OUT/'result.json',dict(complete=True,new_lights=len(first),resumed_lights=len(second),migrated_lights=len(third),records_unchanged=True))
print('INSTANCED_LIGHT_RESUME_NATIVE_PASS',flush=True)
