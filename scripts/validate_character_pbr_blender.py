"""Export one real PMX through the production PBR preparation and FBX path."""
import sys
from pathlib import Path
from types import SimpleNamespace
import json
import argparse

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT/'vendor/python', ROOT/'vendor/blender_mmd_tools-4.5.13'):
    sys.path.insert(0, str(path))

import bpy
from mmd_bridge.core import atomic_json, file_hash, stable_id
from mmd_bridge.blender_host import enable_mmd, select, FBX_DEFAULTS
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_character_materials import prepare
from mmd_bridge.blender_character_pbr import analyze

args = sys.argv[sys.argv.index('--')+1:]
parser=argparse.ArgumentParser()
parser.add_argument('model',type=Path);parser.add_argument('output',type=Path)
parser.add_argument('config',type=Path,nargs='?')
parser.add_argument('--fixture-kind',choices=('character','prop'),default='character')
parser.add_argument('--mesh-finish',choices=('none','weld_weighted'),default='none')
parser.add_argument('--weld-distance',type=float,default=.0001,help='Weld distance in imported Blender metres')
options=parser.parse_args(args)
model, output = options.model.resolve(), options.output.resolve()
output.mkdir(parents=True, exist_ok=True)
cfg = json.loads(options.config.read_text(encoding='utf-8')) if options.config else {}
cfg.update(model=str(model), character_material_profile='character_pbr', character_backend='mmdbridge_fbx')
source_hash = file_hash(model)
enable_mmd()
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'; scene.unit_settings.scale_length = 1
scene.render.fps = 30; scene.frame_start = 1; scene.frame_end = 61; scene.frame_set(1)
result = bpy.ops.mmd_tools.import_model(filepath=str(model), scale=.08,
    types={'MESH','ARMATURE','MORPHS','DISPLAY'}, rename_bones=False,
    clean_model=False, remove_doubles=False, save_log=False, log_level='WARNING')
assert 'FINISHED' in result
armature = next(o for o in scene.objects if o.type == 'ARMATURE')
meshes = [o for o in scene.objects if o.type == 'MESH' and o.find_armature() == armature]
worker = SimpleNamespace(cfg=cfg, data={'settings':cfg}, bundle=output/'Data', meshes=meshes,
                         manifest={'bones':[]}, armature=armature)
worker.bundle.mkdir(exist_ok=True)
worker.materials = Materials(worker.bundle, str(model))
for index, bone in enumerate(list(armature.data.bones)):
    original = bone.name; bone.name = f'b_{index:04d}_{stable_id(original)[:8]}'
    worker.manifest['bones'].append({'original':original, 'exported':bone.name})
for obj in meshes:
    for index, material in enumerate(obj.data.materials):
        if material: analyze(worker.materials, material, index)
prepare(worker)
records = list(worker.materials.records.values())
atomic_json(output/'materials.json', records)
mesh_finish = {'mode': 'none'}
if options.mesh_finish == 'weld_weighted':
    from mmd_bridge.blender_mesh_finish import prepare_comparison
    meshes, mesh_finish = prepare_comparison(meshes, options.weld_distance)
    worker.meshes = meshes
atomic_json(output/'mesh_finish_receipt.json', mesh_finish)
morphs = []
for obj in meshes:
    if not obj.data.shape_keys: continue
    for key in list(obj.data.shape_keys.key_blocks)[1:]:
        original = key.name; key.name = 'm_'+stable_id(original)
        if original.lower() in ('まばたき','blink','闭眼','閉眼'):
            key.driver_remove('value')
            for frame, value in ((1,0.),(12,0.),(15,1.),(18,0.),(61,0.)):
                key.value=value; key.keyframe_insert('value',frame=frame)
            morphs.append({'original':original,'exported':key.name,'kind':'blink'})
        elif options.fixture_kind=='character' and original in ('あ','a','A','aa','Aa','Ah','ah','mouth_a','Mouth_A'):
            key.driver_remove('value')
            for frame,value in ((1,0.),(20,0.),(30,1.),(42,1.),(52,0.),(61,0.)):
                key.value=value;key.keyframe_insert('value',frame=frame)
            morphs.append({'original':original,'exported':key.name,'kind':'mouth_open'})
armature.name = 'MMD_Rig'
select(meshes+[armature])
model_path = output/'model.fbx'
bpy.ops.export_scene.fbx(filepath=str(model_path), **{**FBX_DEFAULTS,'bake_anim':False,'object_types':{'MESH','ARMATURE'}})
for bone in worker.manifest['bones'] if options.fixture_kind=='character' else []:
    if bone['original'] in ('頭','头','Head','head','左目','右目'):
        pose = armature.pose.bones[bone['exported']]; pose.rotation_mode='XYZ'
        for frame, rotation in ((1,(0,0,0)),(20,(0,0,0)),(30,(0,.15,0)),
                                (42,(0,-.15,0)),(52,(.08,0,0)),(61,(0,0,0))):
            pose.rotation_euler=rotation; pose.keyframe_insert('rotation_euler',frame=frame)
animation_path=output/'animation.fbx'
if options.fixture_kind=='character':
    bpy.ops.export_scene.fbx(filepath=str(animation_path), **{**FBX_DEFAULTS,'bake_anim':True,'object_types':{'MESH','ARMATURE'}})
scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(output/'Character.blend'))
files=[('model',model_path)]
if options.fixture_kind=='character':files.append(('animation',animation_path))
bounds={}
for record in records:
    points=[obj.matrix_world @ obj.data.vertices[index].co for obj in meshes
            for polygon in obj.data.polygons
            if obj.data.materials[polygon.material_index] and obj.data.materials[polygon.material_index].name==record['asset_name']
            for index in polygon.vertices]
    if points:
        bounds[record['id']]={'min':[min(point[axis] for point in points) for axis in range(3)],
                              'max':[max(point[axis] for point in points) for axis in range(3)],'space':'blender_world_m'}
manifest = {'schema':2,'model':str(model),'source_sha256':source_hash,'name':model.stem,
    'fixture_kind':options.fixture_kind,'material_bounds':bounds,'mesh_finish':mesh_finish,
    'settings':cfg,'bones':worker.manifest['bones'],'morph_probe':morphs,
    'materials':records,'files':{key:{'path':str(path),'sha256':file_hash(path)}
                              for key,path in files}}
assert source_hash == file_hash(model), 'Source PMX changed'
atomic_json(output/'manifest.json', manifest)
atomic_json(output/'blender_receipt.json', {'status':'complete','material_count':len(records),
    'white_materials':[r['name'] for r in records if r['character_material']['pbr']['status']=='white'],
    'source_preserved':True})
print('CHARACTER_PBR_BLENDER_COMPLETE',len(records),flush=True)
