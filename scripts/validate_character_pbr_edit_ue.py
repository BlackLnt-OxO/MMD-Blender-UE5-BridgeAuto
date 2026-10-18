"""Disk readback, one-slot apply/undo and portrait MRQ preset verification."""
import json
import os
from pathlib import Path
import sys
import unreal as u

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.ue_character_pbr_edit import apply, undo

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
receipt=read_json(folder/'ue_receipt.json')
assert receipt['status']=='ue_assets_created',receipt.get('error')
manifest=read_json(folder/'manifest.json')
editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
assert editor.load_level(receipt['render_assets']['level'])
actors=u.get_editor_subsystem(u.EditorActorSubsystem)
actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
actors.set_selected_level_actors([actor])
component=actor.skeletal_mesh_component
assert list(actors.get_selected_level_actors())==[actor], 'Run with -ExecutePythonScript in editor mode to retain actor selection'
before=[m.get_path_name() for m in component.get_materials()]
before_hashes={path:file_hash(Path(u.Paths.project_content_dir())/(path.split('.')[0][6:]+'.uasset')) for path in before}
hair=next(r for r in manifest['materials'] if r['name']=='Hair02')
cfg={'character_pbr_options':{},'character_material_overrides':{manifest['source_sha256']:{
    str(hair['pmx_index']):{'matcap_mode':'off','params':{'Roughness':.41}}}}}
result=apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
              'pmx_index':hair['pmx_index'],'settings':cfg})
after=[m.get_path_name() for m in component.get_materials()]
changed=[i for i,(a,b) in enumerate(zip(before,after)) if a!=b]
assert len(changed)==1,(changed,result)
material=component.get_material(changed[0])
assert u.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(material,'UseMatCap')==0.
assert abs(u.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(material,'Roughness')-.41)<1e-5
undone=undo()
assert [m.get_path_name() for m in component.get_materials()]==before
for path,digest in before_hashes.items():
    assert file_hash(Path(u.Paths.project_content_dir())/(path.split('.')[0][6:]+'.uasset'))==digest
preset=u.load_asset(receipt['render_assets']['preset'])
portrait=u.load_asset(receipt['root']+'/MRQ_PBR_Portrait2K')
if not portrait:
    portrait=u.EditorAssetLibrary.duplicate_asset(preset.get_path_name(),receipt['root']+'/MRQ_PBR_Portrait2K')
    output=portrait.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution=u.IntPoint(2560,1440)
    output.use_custom_playback_range=True;output.custom_start_frame=0;output.custom_end_frame=1
    aa=portrait.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.spatial_sample_count=4;aa.temporal_sample_count=1;aa.engine_warm_up_count=48;aa.render_warm_up_count=32
    u.EditorAssetLibrary.save_loaded_asset(portrait,only_if_is_dirty=False)
assert editor.load_level(receipt['render_assets']['level'])
restored=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
assert [m.get_path_name() for m in restored.skeletal_mesh_component.get_materials()]==before
receipt['portrait_preset']=portrait.get_path_name()
receipt['checks']['one_slot_apply_undo']={'changed_slots':changed,'apply':result['count'],'undo':undone['count'],
                                        'original_assets_preserved':True,'reload_binding_restored':True}
atomic_json(folder/'ue_receipt.json',receipt)
atomic_json(folder/'edit_receipt.json',receipt['checks']['one_slot_apply_undo'])
print('PBR_EDIT_UNDO_COMPLETE',changed)
