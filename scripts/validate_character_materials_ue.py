"""Import the real Blender fixture and exercise the production UE adapter."""
import json
import os
import sys
import traceback
from pathlib import Path

import unreal as u

ROOT=Path(r'D:/Codex_Projects/MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_host import import_fbx,bind_float
from mmd_bridge.ue_character_materials import attach_runtime,enable_character_usage,character_blueprint
from mmd_bridge.core import atomic_json

tag=os.environ.get('MMDBRIDGE_CHARACTER_TEST_TAG','smoke')
eye_lighting=os.environ.get('MMDBRIDGE_CHARACTER_TEST_EYE_LIGHTING')=='1'
model=os.environ.get('MMDBRIDGE_CHARACTER_TEST_MODEL','Barbara')
folder=ROOT/'work/character_material_20261014'/tag/model
manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
asset_root=('/Game/Character/MMDBridge/' if tag=='material_outline' else '/Game/CM_1014/')+tag+'_'+model.replace(' ','_')
report={'warnings':[],'failures':[],'checks':{},'assets':{},'material_routes':{}}
try:
    u.SystemLibrary.execute_console_command(None,'Interchange.FeatureFlags.Import.FBX 0')
    imported=import_fbx(manifest['files']['model']['path'],asset_root+'/Meshes','SK_Character','model',owner='cm_1014_'+tag+'_'+model)
    mesh=next((asset for asset in imported if isinstance(asset,u.SkeletalMesh)),None)
    assert mesh, imported
    cfg={'character_backend':'mmdbridge_fbx','character_master_material':manifest['profile']['parents'][0],
         'eye_master_material':'','character_material_profile':manifest['profile']['id'],
         'character_material_profiles_dir':manifest['profile_dir'],'bake_resolution':2048}
    manager=Materials(asset_root,'cm_1014_'+tag+'_'+model,report,cfg)
    for record in manifest['materials']:
        instance=manager.build_character(record)
        assert manager.lib.has_material_usage(instance,u.MaterialUsage.MATUSAGE_MORPH_TARGETS)
        note=record.get('character_material',{})
        if note.get('role','').startswith('cloth_') and note.get('sources',{}).get('lightmap',{}).get('kind')=='generated':
            gain=manager.lib.get_material_instance_vector_parameter_value(instance,'Ramp_Cloth_1_LitGain')
            assert abs(gain.r-1/.1145833358168602)<.001,gain
        if note.get('role')=='face':
            shadow=manager.lib.get_material_instance_vector_parameter_value(instance,'ShadowColor')
            expected=manifest['profile']['role_presets']['face']['vectors']['ShadowColor']
            assert all(abs(a-b)<1e-6 for a,b in zip((shadow.r,shadow.g,shadow.b,shadow.a),expected)),shadow
        if note.get('role')=='eye':
            shadow=manager.lib.get_material_instance_vector_parameter_value(instance,'ShadowColor')
            assert abs(shadow.r-.65)<1e-6 and not manager.lib.get_material_instance_static_switch_parameter_value(instance,'UseRamp')
        if model=='YYB GenZ Miku' and record['name'] in ('Body','Hair02'):
            assert instance.get_editor_property('base_property_overrides').get_editor_property('blend_mode')==u.BlendMode.BLEND_OPAQUE
    manager.assign(mesh,manifest['materials'])
    if eye_lighting:
        baseline_report={'warnings':[],'failures':[],'checks':{},'assets':{},'material_routes':{}}
        baseline=Materials(asset_root+'/EyeBaseline','eye_baseline_'+tag+'_'+model,baseline_report,dict(cfg,character_material_profile='generic'))
        report['eye_nolight_before_overrides']={}
        for record in manifest['materials']:
            if manager._eye_material(record):
                old=baseline.build_character(record)
                report['eye_nolight_before_overrides'][manager.assets[record['id']]]=old.get_path_name()
        assert report['eye_nolight_before_overrides']
        report['eye_slots']=[r['name'] for r in manifest['materials'] if manager._eye_material(r)]
    u.EditorAssetLibrary.save_loaded_asset(mesh)
    editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
    preview_path=asset_root+'/Preview'
    assert (editor.load_level(preview_path) if u.EditorAssetLibrary.does_asset_exist(preview_path) else editor.new_level(preview_path))
    actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    for previous_actor in actors.get_all_level_actors():
        if 'CM_1014_fixture' in [str(t) for t in previous_actor.tags]:
            actors.destroy_actor(previous_actor)
    actor=actors.spawn_actor_from_class(u.SkeletalMeshActor,u.Vector())
    actor.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)
    actor.skeletal_mesh_component.set_editor_property('render_static',False)
    actor.skeletal_mesh_component.set_component_tick_enabled(True)
    actor.skeletal_mesh_component.set_editor_property('visibility_based_anim_tick_option',u.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
    actor.skeletal_mesh_component.set_editor_property('enable_update_rate_optimizations',False)
    attach_runtime(actor,mesh,manifest['materials'],manager)
    if manifest['profile']['sdf']['format']=='none':
        assert 'face_runtime' not in report['checks']
        for record in manifest['materials']:
            instance=u.load_asset(manager.assets[record['id']])
            assert not manager.lib.get_material_instance_static_switch_parameter_value(instance,'UseSDF?')
            assert 'SDF_Atlas' not in [str(n) for n in manager.lib.get_texture_parameter_names(instance.parent)]
        report['checks']['sdf_disabled']=True
    light=actors.spawn_actor_from_class(u.DirectionalLight,u.Vector(0,0,300),u.Rotator(-25,0,0))
    light.light_component.set_editor_property('atmosphere_sun_light',True)
    light.light_component.set_editor_property('atmosphere_sun_light_index',0)
    light.light_component.set_editor_property('intensity',3.)
    sky=actors.spawn_actor_from_class(u.SkyLight,u.Vector())
    sky.light_component.set_editor_property('intensity',.3)
    sky.light_component.set_editor_property('real_time_capture',True)
    actors.spawn_actor_from_class(u.SkyAtmosphere,u.Vector())
    animation_assets=import_fbx(manifest['files']['animation']['path'],asset_root+'/Animations','A_HeadTurn','animation',
        skeleton=mesh.skeleton,owner='cm_1014_'+tag+'_'+model)
    animation=next((a for a in animation_assets if isinstance(a,u.AnimSequence)),None)
    assert animation
    blueprint=character_blueprint(manager,mesh,manifest['materials'],animation)
    if blueprint:
        actors.destroy_actor(actor)
        actor=actors.spawn_actor_from_class(blueprint.generated_class(),u.Vector())
        assert actor.skeletal_mesh_component.get_skeletal_mesh_asset()==mesh
        assert actor.outline_mesh.get_skeletal_mesh_asset()==mesh
        assert not actor.outline_mesh.get_editor_property('leader_pose_component')
        assert len(actor.outline_mesh.get_materials())==len(mesh.materials)
        for component in (actor.skeletal_mesh_component,actor.outline_mesh):
            assert component.get_editor_property('animation_data').get_editor_property('anim_to_play')==animation
        attach_runtime(actor,mesh,manifest['materials'],manager)
    options=u.AnimPoseEvaluationOptions(evaluate_curves=True,optional_skeletal_mesh=mesh,should_retarget=False)
    head=next((b['exported'] for b in manifest['bones'] if b['original'].lower() in ('head','頭','头')),None)
    report['animation_head_rotations']=[]
    report['animation_morph_probe']={p['exported']:[] for p in manifest.get('morph_probe',[])}
    for sample_time in (0.,1/30,2/30):
        pose=u.AnimPoseExtensions.get_anim_pose_at_time(animation,sample_time,options)
        rotation=u.AnimPoseExtensions.get_bone_pose(pose,u.Name(head),u.AnimPoseSpaces.WORLD).rotation
        report['animation_head_rotations'].append([rotation.x,rotation.y,rotation.z,rotation.w])
        for name,values in report['animation_morph_probe'].items():
            values.append(u.AnimPoseExtensions.get_curve_weight(pose,u.Name(name)))
    for name,values in report['animation_morph_probe'].items():
        assert max(values)-min(values)>.5,(name,values)
    sequence_path=asset_root+'/S_Character'
    sequence=u.load_asset(sequence_path)
    if not sequence:
        sequence=u.AssetToolsHelpers.get_asset_tools().create_asset('S_Character',asset_root,u.LevelSequence,u.LevelSequenceFactoryNew())
    for binding in list(sequence.get_bindings()): binding.remove()
    for track in list(sequence.get_tracks()): sequence.remove_track(track)
    sequence.set_display_rate(u.FrameRate(30,1)); sequence.set_playback_start(0); sequence.set_playback_end(9)
    single=manifest['profile']['sdf']['format']=='threshold_lr'
    # Hold each sun direction so the render-thread atmosphere uniforms can settle.
    frame_count=31 if single else 9
    if eye_lighting:
        assert not single and manifest['profile']['sdf']['format']=='none'
        frame_count=21
    motion_start=28 if single else 6
    capture_frames=[3,7,11,15,19,23,28,29,30] if single else list(range(9))
    if eye_lighting:capture_frames+= [12,16,20]
    sequence.set_playback_end(frame_count)
    binding=sequence.add_possessable(actor)
    track=binding.add_track(u.MovieSceneSkeletalAnimationTrack)
    for start in (motion_start,):
        section=track.add_section(); section.set_range(start,start+3)
        params=section.get_editor_property('params'); params.animation=animation; params.force_custom_mode=True; section.set_editor_property('params',params)
    if blueprint:
        outline_binding=sequence.add_possessable(actor.outline_mesh);outline_binding.set_parent(binding)
        outline_track=outline_binding.add_track(u.MovieSceneSkeletalAnimationTrack)
        section=outline_track.add_section();section.set_range(motion_start,motion_start+3)
        params=section.get_editor_property('params');params.animation=animation;params.force_custom_mode=True
        section.set_editor_property('params',params)
        report['checks']['independent_animation_tracks']=2
    head=next((b['exported'] for b in manifest['bones'] if b['original'].lower() in ('head','頭','头')),None)
    center=actor.skeletal_mesh_component.get_socket_location(head) if head else actor.get_actor_bounds(False)[0]
    report['reference_bone_positions']={}
    for bone in manifest['bones']:
        if bone['original'] in ('頭','头','head','左目','右目','左腕','右腕'):
            position=actor.skeletal_mesh_component.get_socket_location(bone['exported'])
            report['reference_bone_positions'][bone['original']]=[position.x,position.y,position.z]
    distance=85
    if os.environ.get('MMDBRIDGE_CHARACTER_TEST_FULL_BODY')=='1':
        center,extent=actor.get_actor_bounds(False)
        distance=max(extent.z*7,85)
    camera=actors.spawn_actor_from_class(u.CineCameraActor,center+u.Vector(distance,0,0))
    camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(camera.get_actor_location(),center),False)
    camera.get_cine_camera_component().set_editor_property('current_focal_length',35.)
    focus=camera.get_cine_camera_component().get_editor_property('focus_settings')
    focus.focus_method=u.CameraFocusMethod.DISABLE
    camera.get_cine_camera_component().set_editor_property('focus_settings',focus)
    post=camera.get_cine_camera_component().get_editor_property('post_process_settings')
    for key,value in {'override_auto_exposure_method':True,'auto_exposure_method':u.AutoExposureMethod.AEM_MANUAL,
        'override_auto_exposure_bias':True,'auto_exposure_bias':0.,'override_auto_exposure_apply_physical_camera_exposure':True,
        'auto_exposure_apply_physical_camera_exposure':False,'override_motion_blur_amount':True,'motion_blur_amount':0.}.items():
        post.set_editor_property(key,value)
    camera.get_cine_camera_component().set_editor_property('post_process_settings',post)
    camera_binding=sequence.add_possessable(camera)
    cut=sequence.add_track(u.MovieSceneCameraCutTrack).add_section(); cut.set_range(0,frame_count)
    camera_id=u.MovieSceneObjectBindingID()
    camera_id.set_editor_property('guid',camera_binding.get_id())
    cut.set_camera_binding_id(camera_id)
    light_binding=sequence.add_possessable(light)
    transform=light_binding.add_track(u.MovieScene3DTransformTrack).add_section(); transform.set_range(0,frame_count)
    channels=transform.get_all_channels()
    for i,value in enumerate((0.,0.,300.,0.,-20.,180.,1.,1.,1.)):
        channels[i].set_default(value)
    angles=[(-20.,180.),(-20.,-135.),(-20.,135.),(-75.,180.),(60.,180.),(-20.,0.),(-20.,180.),(-20.,180.),(-20.,180.)]
    if manifest['profile']['sdf']['format']=='threshold_lr':
        # DirectionalLight rotation points away from the direction toward the sun.
        angles[1:5]=[(0.,-105.),(0.,105.),(0.,-90.),(0.,90.)]
    light_frames=[0,4,8,12,16,20,24,28,29] if single else list(range(9))
    for frame,(pitch,yaw) in zip(light_frames,angles):
        channels[4].add_key(u.FrameNumber(frame),pitch,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
        channels[5].add_key(u.FrameNumber(frame),yaw,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
    if eye_lighting:
        channels[4].add_key(u.FrameNumber(9),-20.,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
        # Camera is on +X; directional-light forward points away from the sun.
        channels[5].add_key(u.FrameNumber(9),180.,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
        for obj,binding,values in ((light.light_component,light_binding,[(0,3.),(12,3.),(13,0.),(16,0.),(17,.05),(20,.05)]),
                                   (sky.light_component,sequence.add_possessable(sky),[(0,.3),(12,.3),(13,0.),(16,0.),(17,.01),(20,.01)])):
            bind_float(sequence,binding,obj,'Intensity','Intensity',values)
        close=actor.skeletal_mesh_component.get_socket_location(head)
        close_location=close+u.Vector(85,0,0)
        close_rotation=u.MathLibrary.find_look_at_rotation(close_location,close)
        camera_track=camera_binding.add_track(u.MovieScene3DTransformTrack).add_section();camera_track.set_range(0,frame_count)
        camera_channels=camera_track.get_all_channels()
        location=camera.get_actor_location();rotation=camera.get_actor_rotation()
        for channel,initial,portrait in zip(camera_channels,
            (location.x,location.y,location.z,rotation.roll,rotation.pitch,rotation.yaw,1.,1.,1.),
            (close_location.x,close_location.y,close_location.z,close_rotation.roll,close_rotation.pitch,close_rotation.yaw,1.,1.,1.)):
            channel.add_key(u.FrameNumber(0),initial,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
            channel.add_key(u.FrameNumber(9),portrait,interpolation=u.MovieSceneKeyInterpolation.CONSTANT)
        report['eye_lighting_frames']={'normal':12,'no_light':16,'weak':20}
    u.EditorAssetLibrary.save_loaded_asset(sequence)
    preset=u.load_asset(asset_root+'/MRQ_Character') or u.AssetToolsHelpers.get_asset_tools().create_asset(
        'MRQ_Character',asset_root,u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    preset.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase)
    preset.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution=u.IntPoint(1280,720); output.file_name_format='{frame_number}'
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.spatial_sample_count=1; aa.temporal_sample_count=1; aa.engine_warm_up_count=8; aa.render_warm_up_count=8
    console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    console.add_or_update_console_variable('ShowFlag.PathTracing',0.)
    console.add_or_update_console_variable('r.MotionBlurQuality',0.)
    u.EditorAssetLibrary.save_loaded_asset(preset)
    for fixture_actor in (actor,light,sky,camera):
        fixture_actor.tags=['CM_1014_fixture']
    # Own preview levels were created exclusively by this fixture.
    for fixture_actor in actors.get_all_level_actors():
        if isinstance(fixture_actor,u.SkyAtmosphere): fixture_actor.tags=['CM_1014_fixture']
    assert editor.save_current_level()
    report['render_assets']={'level':preview_path,'sequence':sequence_path,'preset':preset.get_path_name(),
                             'animation':animation.get_path_name()}
    report['character_animation_probe']={'head_bone':head,'morphs':[p['exported'] for p in manifest.get('morph_probe',[])]} if blueprint else {}
    report['render_frame_count']=frame_count
    report['capture_frames']=capture_frames
    report['mesh']=mesh.get_path_name()
    report['slots']=[{'name':str(slot.material_slot_name),'material':slot.material_interface.get_path_name() if slot.material_interface else None}
                     for slot in mesh.materials]
    report['sdf_textures']={}
    report['sdf_off_overrides']={}
    for record in manifest['materials']:
        spec=record.get('textures',{}).get('face_sdf' if single else 'sdf_atlas')
        if spec:
            texture=manager.texture(spec,data=True)
            info=dict(u.MMDBridgeCharacterLibrary.texture_platform_info(texture))
            assert info['is_r16f']==int(not single),info
            width=manifest['profile']['sdf'].get('resolution',1024) if single else manifest['profile']['sdf']['tile_size']*16
            height=width if single else manifest['profile']['sdf']['tile_size']*9
            assert (info['width'],info['height'])==(width,height),info
            info['format']=manifest['profile']['sdf']['format']
            if single:
                assert manager.lib.get_material_instance_texture_parameter_value(u.load_asset(manager.assets[record['id']]),'SDF_Tex')==texture
            report['sdf_textures'][record['id']]=info
            source=manager.assets[record['id']]
            target=source.split('.')[0]+'_SDFOff'
            baseline=u.load_asset(target)
            if baseline: manager.verify_asset(baseline)
            else:
                baseline=u.EditorAssetLibrary.duplicate_asset(source,target)
                manager.managed(baseline)
            current=u.load_asset(source)
            manager.lib.set_material_instance_parent(baseline,current.parent)
            for property_name in ('texture_parameter_values','scalar_parameter_values','vector_parameter_values','base_property_overrides'):
                baseline.set_editor_property(property_name,current.get_editor_property(property_name))
            manager.lib.set_material_instance_static_switch_parameter_value(baseline,'UseSDF?' if single else 'UseSDFAtlas',False)
            enable_character_usage(baseline)
            manager.lib.update_material_instance(baseline)
            u.EditorAssetLibrary.save_loaded_asset(baseline);manager.protect_asset(baseline)
            report['sdf_off_overrides'][source]=baseline.get_path_name()
    report['status']='ue_assets_created'
except Exception:
    report['status']='failed'; report['error']=traceback.format_exc()
    raise
finally:
    atomic_json(folder/'ue_receipt.json',report)
