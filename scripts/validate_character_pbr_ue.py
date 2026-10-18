"""Real-RHI skeletal import, native PBR compilation and render fixtures."""
import copy
import json
import os
import sys
import traceback
from pathlib import Path
import unreal as u

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_host import import_fbx, bind_float
from mmd_bridge.ue_character_pbr import build

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
asset_root=os.environ.get('MMDBRIDGE_PBR_ASSET_ROOT','/Game/MMDBridgePBR/GenZ_v1')
manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
fixture_kind=manifest.get('fixture_kind','character')
fixture_name=asset_root.rsplit('/',1)[-1]
allowed_white=set(json.loads(os.environ.get('MMDBRIDGE_PBR_EXPECTED_RESOURCE_SLOTS','[]')))
owner='pbr_1017_'+asset_root.rsplit('/',1)[-1]
report={'warnings':[],'failures':[],'checks':{},'assets':{},'root':asset_root}

try:
    u.SystemLibrary.execute_console_command(None,'Interchange.FeatureFlags.Import.FBX 0')
    report['checks']['renderer']={key:u.SystemLibrary.get_console_variable_int_value(key) for key in (
        'r.Substrate','r.Substrate.ProjectGBufferFormat','r.DynamicGlobalIlluminationMethod','r.ReflectionMethod','r.AnisotropicMaterials')}
    imported=import_fbx(manifest['files']['model']['path'],asset_root+'/Meshes','SK_'+fixture_name,'model',owner=owner)
    mesh=next(a for a in imported if isinstance(a,u.SkeletalMesh))
    cfg=dict(manifest['settings'],bake_resolution=2048)
    manager=Materials(asset_root,owner,report,cfg)
    prepared_white={r['id'] for r in manifest['materials'] if r['character_material']['pbr']['status']=='white'}
    assert {r['pmx_index'] for r in manifest['materials'] if r['id'] in prepared_white}<=allowed_white,'Unexpected source material failure'
    for record in manifest['materials']:
        instance=manager.asset(record['id']) if manager.reuse(record) else build(manager,record)
        assert manager.lib.has_material_usage(instance,u.MaterialUsage.MATUSAGE_MORPH_TARGETS)
    assert {r['material_id'] for r in report.get('white_materials',[])}<=prepared_white,report['failures']
    report['expected_resource_failures']=[r for r in report['failures'] if r['material_id'] in prepared_white]
    report['review_status']='needs_review' if prepared_white or report.get('needs_review_materials') else 'awaiting_visual_review'
    manager.assign(mesh,manifest['materials'])
    report['assets']['materials']=manager.assets
    report['mesh']=mesh.get_path_name()
    report['checks']['slots']=len(mesh.materials)
    report['matcap_off_overrides']={}
    for record in manifest['materials']:
        if not record['character_material']['pbr']['matcap']['enabled']:continue
        original=u.load_asset(manager.assets[record['id']])
        target=original.get_path_name().split('.')[0]+'_MatCapOff'
        off=u.load_asset(target) or u.EditorAssetLibrary.duplicate_asset(original.get_path_name(),target)
        manager.lib.set_material_instance_scalar_parameter_value(off,'UseMatCap',0.)
        manager.lib.update_material_instance(off)
        u.EditorAssetLibrary.set_metadata_tag(off,'MMDBridge.PBRBinding','matcap_off_comparison')
        u.EditorAssetLibrary.save_loaded_asset(off);manager.protect_asset(off)
        report['matcap_off_overrides'][original.get_path_name()]=off.get_path_name()
    # The white fallback must compile and keep originally hidden material opacity.
    failed=copy.deepcopy(manifest['materials'][0])
    failed['id']='white_probe';failed['asset_name']='WhiteProbe'
    failed['character_material']['pbr'].update(status='white',issues=['controlled missing-resource probe'])
    failed['character_material']['pbr']['params']['Opacity']=0.
    failed['character_material']['pbr']['alpha_mode']='masked'
    fallback_report={'warnings':[],'failures':[],'checks':{}}
    fallback=Materials(asset_root+'/WhiteProbe',owner+'_white',fallback_report,cfg)
    white=build(fallback,failed)
    assert fallback.lib.get_material_instance_scalar_parameter_value(white,'Opacity')==0.
    report['checks']['white_hidden_opacity']=True
    animation=None
    if fixture_kind=='character':
        animations=import_fbx(manifest['files']['animation']['path'],asset_root+'/Animations','A_HeadEyes','animation',
                              skeleton=mesh.skeleton,owner=owner)
        animation=next(a for a in animations if isinstance(a,u.AnimSequence))
        report['animation_asset']=animation.get_path_name()
    editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
    preview=asset_root+'/L_PBR_Review'
    assert (editor.load_level(preview) if u.EditorAssetLibrary.does_asset_exist(preview)
            else editor.new_level(preview))
    actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    for previous in list(actors.get_all_level_actors()):
        if 'MMDBridgePBRFixture' in [str(tag) for tag in previous.tags]:
            actors.destroy_actor(previous)
    actor=actors.spawn_actor_from_class(u.SkeletalMeshActor,u.Vector())
    actor.set_actor_label('PBR_'+fixture_name+'_Review')
    component=actor.skeletal_mesh_component
    component.set_skeletal_mesh_asset(mesh)
    component.set_editor_property('render_static',False)
    component.set_editor_property('visibility_based_anim_tick_option',u.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
    component.set_editor_property('enable_update_rate_optimizations',False)
    sun=actors.spawn_actor_from_class(u.DirectionalLight,u.Vector(0,0,300),u.Rotator(pitch=-25,yaw=180,roll=0))
    sun.light_component.set_editor_property('intensity',10.)
    sun.light_component.set_editor_property('light_color',u.Color(255,255,255,255))
    sky=actors.spawn_actor_from_class(u.SkyLight,u.Vector())
    sky.light_component.set_editor_property('intensity',1.)
    sky.light_component.set_editor_property('source_type',u.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    sky.light_component.set_editor_property('cubemap',u.load_asset('/Engine/MapTemplates/Sky/DaylightAmbientCubemap'))
    sky.light_component.set_editor_property('real_time_capture',False)
    origin,extent=actor.get_actor_bounds(False)
    report['bounds']={'origin':[origin.x,origin.y,origin.z],'extent':[extent.x,extent.y,extent.z]}
    if fixture_kind=='character':
        head=next(b['exported'] for b in manifest['bones'] if b['original'] in ('頭','头','Head','head'))
        center=component.get_socket_location(head)+u.Vector(0,0,-1)
        distance=max(50.,extent.z*1.05)
    else:
        center=origin;distance=max(extent.z*6.,extent.y*4.,extent.x*4.,25.)
    camera=actors.spawn_actor_from_class(u.CineCameraActor,center+u.Vector(distance,0,0))
    for fixture_actor in (actor,sun,sky,camera):
        fixture_actor.set_editor_property('tags',[u.Name('MMDBridgePBRFixture')])
    camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(camera.get_actor_location(),center),False)
    cine=camera.get_cine_camera_component();cine.set_editor_property('current_focal_length',50.)
    focus=cine.get_editor_property('focus_settings');focus.focus_method=u.CameraFocusMethod.DISABLE
    cine.set_editor_property('focus_settings',focus)
    post=cine.get_editor_property('post_process_settings')
    for key,value in {'override_auto_exposure_method':True,'auto_exposure_method':u.AutoExposureMethod.AEM_MANUAL,
        'override_auto_exposure_bias':True,'auto_exposure_bias':-1.5,
        'override_auto_exposure_apply_physical_camera_exposure':True,'auto_exposure_apply_physical_camera_exposure':False,
        'override_motion_blur_amount':True,'motion_blur_amount':0.,
        'override_bloom_intensity':True,'bloom_intensity':0.}.items():
        post.set_editor_property(key,value)
    cine.set_editor_property('post_process_settings',post)
    seq=u.load_asset(asset_root+'/S_PBR_TurnLight') or u.AssetToolsHelpers.get_asset_tools().create_asset('S_PBR_TurnLight',asset_root,u.LevelSequence,u.LevelSequenceFactoryNew())
    manager.managed(seq)
    for binding in list(seq.get_bindings()): binding.remove()
    for track in list(seq.get_tracks()): seq.remove_track(track)
    seq.set_display_rate(u.FrameRate(30,1));seq.set_playback_start(0);seq.set_playback_end(61)
    if animation:
        actor_binding=seq.add_possessable(actor)
        section=actor_binding.add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
        section.set_range(0,61)
        params=section.get_editor_property('params');params.animation=animation;params.force_custom_mode=True
        section.set_editor_property('params',params)
    camera_binding=seq.add_possessable(camera)
    cut=seq.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,61)
    camera_id=u.MovieSceneObjectBindingID()
    camera_id.set_editor_property('guid',camera_binding.get_id())
    cut.set_camera_binding_id(camera_id)
    light_binding=seq.add_possessable(sun)
    transform=light_binding.add_track(u.MovieScene3DTransformTrack).add_section();transform.set_range(0,61)
    channels=transform.get_all_channels()
    for channel,value in zip(channels,(0.,0.,300.,0.,-25.,180.,1.,1.,1.)):channel.set_default(value)
    for frame,pitch,yaw in ((0,-25,180),(20,-25,180),(30,-25,130),(42,-25,230),(52,-25,0),(60,-25,180)):
        channels[4].add_key(u.FrameNumber(frame),pitch)
        channels[5].add_key(u.FrameNumber(frame),yaw)
    # A real zero-direct-light + zero-skylight segment, sampled by the renderer.
    bind_float(seq,light_binding,sun.light_component,'Intensity','Intensity',[(0,10.),(53,10.),(54,0.),(58,0.),(59,10.),(60,10.)])
    bind_float(seq,seq.add_possessable(sky),sky.light_component,'Intensity','Intensity',[(0,1.),(53,1.),(54,0.),(58,0.),(59,1.),(60,1.)])
    u.EditorAssetLibrary.save_loaded_asset(seq);manager.protect_asset(seq)
    preset=u.load_asset(asset_root+'/MRQ_PBR') or u.AssetToolsHelpers.get_asset_tools().create_asset('MRQ_PBR',asset_root,u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    manager.managed(preset)
    preset.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase)
    preset.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution=u.IntPoint(1920,1080);output.file_name_format='{frame_number}'
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.spatial_sample_count=1;aa.temporal_sample_count=1;aa.engine_warm_up_count=24;aa.render_warm_up_count=16
    console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for key,value in {'r.MotionBlurQuality':0.,'r.AnisotropicMaterials':1.,'r.SSS.Quality':1.,
                      'r.SSS.HalfRes':0.,'r.TextureStreaming':1.}.items():
        console.add_or_update_console_variable(key,value)
    u.EditorAssetLibrary.save_loaded_asset(preset);manager.protect_asset(preset)
    assert editor.save_current_level()
    # Force disk readback and verify bindings survive a level reload.
    assert editor.load_level(preview)
    loaded=u.load_asset(mesh.get_path_name())
    assert len(loaded.materials)==len(manifest['materials'])
    report['checks']['reload_slots']=len(loaded.materials)
    report['render_assets']={'level':preview,'sequence':seq.get_path_name(),'preset':preset.get_path_name()}
    report['render_frame_count']=61
    report['capture_frames']=[0,15,20,30,42,52,57,60]
    report['status']='ue_assets_created'
except Exception:
    report['status']='failed';report['error']=traceback.format_exc()
    raise
finally:
    atomic_json(folder/'ue_receipt.json',report)
