"""Inspect live bindings and evaluate the saved sequence before accepting motion."""
import sys
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/character_material_20261014';folder=OUT/'full/橘シェリー'
r=read_json(folder/'ue_receipt.json');m=read_json(folder/'manifest.json')
editor=u.get_editor_subsystem(u.LevelEditorSubsystem);assert editor.load_level(r['render_assets']['level'])
actor=next(a for a in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
component=actor.skeletal_mesh_component
result={'materials':{},'sequence_samples':[]}
function=u.load_asset('/Game/Character/MasterMaterial/RenderTools/MF_RGB_Mapped')
task=u.AssetExportTask();task.object=function;task.filename=str(OUT/'MF_RGB_Mapped.t3d');task.automated=True;task.prompt=False;task.replace_identical=True
result['function_export']=u.Exporter.run_asset_export_task(task)
manager=Materials('/Game/CM_1014/RawProbe','cm_raw_probe',{'warnings':[],'checks':{},'assets':{},'failures':[]})
raw=u.load_asset('/Game/CM_1014/RawProbe/Materials/M_Raw')
if not raw:
 raw=manager.create('M_Raw',u.Material,u.MaterialFactoryNew());manager.material=raw
 tex=manager.node('TextureSampleParameter2D',parameter_name='BaseTex',texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
 manager.lib.connect_material_property(tex[0],'RGB',u.MaterialProperty.MP_EMISSIVE_COLOR)
 manager.lib.recompile_material(raw);u.EditorAssetLibrary.save_loaded_asset(raw);manager.protect_asset(raw)
raw.set_editor_property('two_sided',True)
manager.lib.set_material_usage(raw,u.MaterialUsage.MATUSAGE_SKELETAL_MESH)
manager.lib.recompile_material(raw);u.EditorAssetLibrary.save_loaded_asset(raw);manager.protect_asset(raw)
result['raw_overrides']={}
for record in m['materials']:
 if record.get('character_material',{}).get('role')=='face':
  slot=next(s for s in r['slots'] if s['name']==record['slot_name']);mi=u.load_asset(slot['material'])
  for asset,label in ((mi,'FaceMI'),(mi.parent,'AdaptedMaster')):
   task=u.AssetExportTask();task.object=asset;task.filename=str(OUT/(label+'.t3d'));task.automated=True;task.prompt=False;task.replace_identical=True
   u.Exporter.run_asset_export_task(task)
  result['materials'][record['name']]={}
  for parameter in ('BaseTex','Lightmap','MN','SDF_Atlas','SDF_Coverage'):
   image=u.MaterialEditingLibrary.get_material_instance_texture_parameter_value(mi,parameter)
   result['materials'][record['name']][parameter]=image.get_path_name() if image else None
  for parameter in ('BaseColor','ShadowColor','TransitionColor','MSRE','Matcap_G','Matcap_B','Matcap_Globe'):
   color=u.MaterialEditingLibrary.get_material_instance_vector_parameter_value(mi,parameter)
   result['materials'][record['name']][parameter]=[color.r,color.g,color.b,color.a]
  variant=manager.create('MI_Raw',u.MaterialInstanceConstant,u.MaterialInstanceConstantFactoryNew())
  manager.lib.set_material_instance_parent(variant,raw)
  manager.lib.set_material_instance_texture_parameter_value(variant,'BaseTex',manager.lib.get_material_instance_texture_parameter_value(mi,'BaseTex'))
  manager.lib.update_material_instance(variant);u.EditorAssetLibrary.save_loaded_asset(variant);manager.protect_asset(variant)
  result['raw_overrides'][mi.get_path_name()]=variant.get_path_name()
sequence=u.load_asset(r['render_assets']['sequence'])
world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
player,sequence_actor=u.LevelSequencePlayer.create_level_sequence_player(world,sequence,u.MovieSceneSequencePlaybackSettings())
face=actor.get_component_by_class(u.MMDBridgeFaceDirectionComponent)
for frame in range(9):
 params=u.MovieSceneSequencePlaybackParams();params.set_editor_property('time',frame/30);params.set_editor_property('position_type',u.MovieScenePositionType.TIME)
 player.set_playback_position(params)
 face.update_directions()
 state=dict(u.MMDBridgeCharacterLibrary.face_direction_state(face))
 animation=component.get_anim_instance()
 result['sequence_samples'].append({'frame':frame,'animation_class':animation.get_class().get_name() if animation else None,
                                    'directions':{k:[v.x,v.y,v.z] for k,v in state.items()}})
atomic_json(OUT/'runtime_probe.json',result)
