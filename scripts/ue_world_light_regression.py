"""Create a tiny owned scene for real IES/Light Function runtime validation."""
import sys,time,uuid
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.ue_materials import Materials,prop
from mmd_bridge.ue_appearance import configure_directional_light
OUT=ROOT/'work/world_appearance/ue_light_regression';OUT.mkdir(parents=True,exist_ok=True)
owner='WorldAppearanceValidation_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:4];root='/Game/'+owner
level=u.get_editor_subsystem(u.LevelEditorSubsystem);assert level.new_level(root+'/Maps/LightValidation')
actors=u.get_editor_subsystem(u.EditorActorSubsystem)
world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
report={'checks':{},'assets':{}}
materials=Materials(root,owner,report)
ground=actors.spawn_actor_from_class(u.StaticMeshActor,u.Vector(0,0,0));ground.set_actor_scale3d(u.Vector(8,8,1));ground.static_mesh_component.set_static_mesh(u.load_asset('/Engine/BasicShapes/Plane'))
camera=actors.spawn_actor_from_class(u.CineCameraActor,u.Vector(-650,0,450));rotation=u.Rotator();rotation.pitch=-35;camera.set_actor_rotation(rotation,False)
component=camera.get_cine_camera_component();component.set_editor_property('current_focal_length',35)
focus=component.get_editor_property('focus_settings');focus.focus_method=u.CameraFocusMethod.DISABLE;component.set_editor_property('focus_settings',focus)
light=actors.spawn_actor_from_class(u.PointLight,u.Vector(0,0,250));light.set_actor_label('Angular validation light');rotation=u.Rotator();rotation.pitch=-90;light.set_actor_rotation(rotation,False)
lc=light.point_light_component;lc.set_mobility(u.ComponentMobility.MOVABLE);lc.set_intensity(5000);prop(lc,'intensity_units',u.LightUnits.LUMENS)
graph={'op':'math','mode':'ADD','args':[{'op':'constant','value':.15},{'op':'math','mode':'MULTIPLY','args':[{'op':'constant','value':.85},{'op':'math','mode':'MAXIMUM','args':[{'op':'constant','value':0},{'op':'component','axis':0,'value':{'op':'incoming'}}],'clamp':False}],'clamp':False}],'clamp':False}
configure_directional_light(lc,graph,materials)
lf=lc.get_editor_property('light_function_material');assert lf is not None
ies=OUT/'profile.ies'
ies.write_text('IESNA:LM-63-2002\n[TEST] MMDBridge world lighting\nTILT=NONE\n1 1000 1 5 1 1 2 .1 .1 .1\n1 1 10\n0 45 90 135 180\n0\n100 80 50 10 0\n',encoding='ascii')
from mmd_bridge.core import file_hash
configure_directional_light(lc,dict(op='ies',path=str(ies),sha256=file_hash(ies)),materials)
profile=lc.get_editor_property('ies_texture');assert isinstance(profile,u.TextureLightProfile)
lc.set_editor_property('ies_texture',None)
post=actors.spawn_actor_from_class(u.PostProcessVolume,u.Vector());post.set_editor_property('unbound',True)
settings=post.get_editor_property('settings')
for n,v in dict(auto_exposure_method=u.AutoExposureMethod.AEM_MANUAL,auto_exposure_apply_physical_camera_exposure=False,auto_exposure_bias=-3.,bloom_intensity=0.,vignette_intensity=0.).items():setattr(settings,'override_'+n,True);setattr(settings,n,v)
post.set_editor_property('settings',settings)
sequence=materials.create('LS_LightValidation',u.LevelSequence,u.LevelSequenceFactoryNew());sequence.set_display_rate(u.FrameRate(30,1));sequence.set_playback_start(0);sequence.set_playback_end(2)
binding=sequence.add_possessable(camera);cut=sequence.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,2)
bid=u.MovieSceneObjectBindingID();bid.set_editor_property('guid',binding.get_id());cut.set_camera_binding_id(bid)
u.EditorAssetLibrary.save_loaded_asset(sequence);materials.protect_asset(sequence)
preset=materials.create('MRQ_LightValidation',u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
out=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting);out.output_resolution=u.IntPoint(640,360)
preset.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase);preset.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting);aa.engine_warm_up_count=60;aa.render_warm_up_count=8
u.EditorAssetLibrary.save_loaded_asset(preset);materials.protect_asset(preset)
assert level.save_current_level()
report['assets'].update(level=root+'/Maps/LightValidation',sequence=sequence.get_path_name(),render_preset=preset.get_path_name(),light_function=lf.get_path_name(),ies=profile.get_path_name())
atomic_json(OUT/'ue_report.json',report)
atomic_json(OUT/'job.json',dict(settings=dict(project=u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path()),unreal='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe',width=640,height=360,ue_vram_pause_percent=90,ue_vram_resume_percent=80)))
print('WORLD_LIGHT_NATIVE_SCENE_READY')
