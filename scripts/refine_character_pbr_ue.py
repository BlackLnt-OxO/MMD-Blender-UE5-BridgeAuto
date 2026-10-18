"""Compile the corrected PBR family using the existing GenZ mesh and animation."""
import os
import sys
import copy
import json
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_pbr import build
from mmd_bridge.ue_host import bind_float
from mmd_bridge.character_pbr import VERSION

folder=Path(os.environ.get('MMDBRIDGE_PBR_FIXTURE',str(ROOT/'work/character_pbr_20261018'/('Refined_v'+str(VERSION)))))
manifest=read_json(folder/'manifest.json')
animation_base=read_json(ROOT/'work/character_pbr_20261017/GenZ_Flow/ue_receipt.json')
base=read_json(Path(os.environ['MMDBRIDGE_PBR_BASE_RECEIPT'])) if os.environ.get('MMDBRIDGE_PBR_BASE_RECEIPT') else animation_base
root=os.environ.get('MMDBRIDGE_PBR_ASSET_ROOT','/Game/MMDBridgePBR/GenZ_Lookdev_1018/Refined_v'+str(VERSION))
report={'warnings':[],'failures':[],'checks':{},'assets':{},'root':root}
try:
    manager=Materials(root,'pbr_1018_'+root.rsplit('/',1)[-1],report,manifest['settings'])
    for record in manifest['materials']:
        if not manager.reuse(record):build(manager,record)
    assert not report['failures'],report['failures']
    mesh_path=root+'/Meshes/SK_GenZ'
    mesh=u.load_asset(mesh_path)
    if mesh:manager.verify_asset(mesh)
    else:mesh=u.EditorAssetLibrary.duplicate_asset(base['mesh'],mesh_path)
    manager.managed(mesh)
    manager.assign(mesh,manifest['materials'])
    report['assets']['materials']=manager.assets
    report['mesh']=mesh.get_path_name()
    report['source_mesh']=base['mesh']
    report['checks']['slots']=len(mesh.materials)
    report['matcap_off_overrides']={}
    for record in manifest['materials']:
        if not record['character_material']['pbr']['matcap']['enabled']:continue
        original=manager.asset(record['id'])
        target=original.get_path_name().split('.')[0]+'_MatCapOff'
        off=u.load_asset(target)
        if off:manager.verify_asset(off)
        else:off=u.EditorAssetLibrary.duplicate_asset(original.get_path_name(),target)
        manager.managed(off)
        manager.lib.set_material_instance_scalar_parameter_value(off,'UseMatCap',0.)
        manager.lib.update_material_instance(off)
        u.EditorAssetLibrary.save_loaded_asset(off,only_if_is_dirty=False);manager.protect_asset(off)
        report['matcap_off_overrides'][original.get_path_name()]=off.get_path_name()
    editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
    level=root+'/L_PBR_Review'
    if u.EditorAssetLibrary.does_asset_exist(level):
        assert editor.load_level(level)
    else:
        # Level duplication through AssetLibrary can leave a standalone UWorld
        # which LoadLevel then rejects as a world leak. Use the level subsystem.
        assert editor.new_level_from_template(level,base['render_assets']['level'])
    actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actor.modify();actor.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)
    # A copied review level can retain explicit overrides left by an earlier
    # apply/undo test. Rebind this generated candidate to its assigned family.
    for index, slot in enumerate(mesh.materials):
        old=actor.skeletal_mesh_component.get_material(index)
        if old and old!=slot.material_interface:
            text=u.EditorAssetLibrary.get_metadata_tag(old,'MMDBridge.PBRRecord')
            if not text or json.loads(text)['character_material']['pbr']['model_sha256']!=manifest['source_sha256']:
                raise RuntimeError('候选关卡存在其它来源的材质覆盖，保留现有绑定')
        actor.skeletal_mesh_component.set_material(index,slot.material_interface)
    sun=next(a for a in actors.get_all_level_actors() if isinstance(a,u.DirectionalLight))
    sky=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkyLight))
    camera=next(a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor)
                and not a.get_actor_label().startswith('PBR_'))
    sun.set_actor_rotation(u.Rotator(pitch=-25,yaw=180,roll=0),False)
    sun.light_component.set_editor_property('light_source_angle',6.)
    camera.modify()
    post=camera.get_cine_camera_component().get_editor_property('post_process_settings')
    post.set_editor_property('auto_exposure_bias',-1.)
    camera.get_cine_camera_component().set_editor_property('post_process_settings',post)
    animation=u.load_asset(animation_base['root']+'/Animations/A_HeadEyes')
    assert isinstance(animation,u.AnimSequence)
    report['animation_asset']=animation.get_path_name()
    seq=u.load_asset(root+'/S_PBR_TurnLight') or u.AssetToolsHelpers.get_asset_tools().create_asset(
        'S_PBR_TurnLight',root,u.LevelSequence,u.LevelSequenceFactoryNew())
    for binding in list(seq.get_bindings()):binding.remove()
    for track in list(seq.get_tracks()):seq.remove_track(track)
    seq.set_display_rate(u.FrameRate(30,1));seq.set_playback_start(0);seq.set_playback_end(241)
    section=seq.add_possessable(actor).add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
    section.set_range(0,241)
    params=section.get_editor_property('params');params.animation=animation
    params.force_custom_mode=True
    params.play_rate=u.MovieSceneTimeWarpExtensions.make_time_warp(.25)
    section.set_editor_property('params',params)
    camera_binding=seq.add_possessable(camera)
    cut=seq.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,241)
    camera_id=u.MovieSceneObjectBindingID();camera_id.set_editor_property('guid',camera_binding.get_id())
    cut.set_camera_binding_id(camera_id)
    light_binding=seq.add_possessable(sun)
    transform=light_binding.add_track(u.MovieScene3DTransformTrack).add_section();transform.set_range(0,241)
    channels=transform.get_all_channels()
    for channel,value in zip(channels,(0.,0.,300.,0.,-25.,180.,1.,1.,1.)):channel.set_default(value)
    for frame,yaw in ((0,180),(60,180),(90,130),(120,230),(150,0),(211,0),(240,180)):
        channels[5].add_key(u.FrameNumber(frame),yaw)
    for light,intensity in ((sun,10.),(sky,1.)):
        bind_float(seq,light_binding if light==sun else seq.add_possessable(sky),light.light_component,
            'Intensity','Intensity',[(0,intensity),(150,intensity),(151,0.),(210,0.),(211,intensity),(240,intensity)])
    u.EditorAssetLibrary.save_loaded_asset(seq,only_if_is_dirty=False)
    assert editor.save_current_level()
    for label,resolution,samples in (('MRQ_PBR',u.IntPoint(1920,1080),4),
                                      ('MRQ_PBR_Portrait2K',u.IntPoint(2560,1440),16)):
        path=root+'/'+label
        preset=u.load_asset(path)
        if preset:manager.verify_asset(preset)
        else:preset=u.EditorAssetLibrary.duplicate_asset(base['render_assets']['preset'],path)
        manager.managed(preset)
        output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
        output.output_resolution=resolution;output.use_custom_playback_range=False
        aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
        aa.spatial_sample_count=samples;aa.temporal_sample_count=1
        aa.engine_warm_up_count=64;aa.render_warm_up_count=32;aa.render_warm_up_frames=True
        console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
        for key,value in {'r.TemporalAASamples':16.,'r.SSS.Burley.NumSamplesOverride':128.,
                          'r.SSS.Checkerboard':0.,'r.SSS.SampleSet':2.,
                          'r.RayTracing.Shadows.SamplesPerPixel':4.}.items():
            console.add_or_update_console_variable(key,value)
        u.EditorAssetLibrary.save_loaded_asset(preset,only_if_is_dirty=False);manager.protect_asset(preset)
    report['render_assets']={'level':level,'sequence':seq.get_path_name(),'preset':root+'/MRQ_PBR.MRQ_PBR'}
    report['portrait_preset']=root+'/MRQ_PBR_Portrait2K.MRQ_PBR_Portrait2K'
    report['status']='ue_assets_created'
    assert editor.load_level(level)
    report['checks']['reload_slots']=len(u.load_asset(mesh_path).materials)
    print('PBR_REFINED_READY',len(mesh.materials))
except Exception:
    import traceback
    report['status']='failed';report['error']=traceback.format_exc()
    raise
finally:atomic_json(folder/'ue_receipt.json',report)
