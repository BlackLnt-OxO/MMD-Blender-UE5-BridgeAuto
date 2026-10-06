"""Owned minimal railway stage, two camera cuts and a faithful MRQ test preset."""
import sys,traceback,shutil
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.ue_shader_data import restore,asset_dependencies
OUT=ROOT/'work/material_functions_implementation';asset_root='/Game/MMDBridgeValidation/FunctionsV2/TreeRender'
report={'complete':False,'project':u.Paths.get_project_file_path()}
try:
    assert Path(report['project']).stem=='TESTON1_1005_02'
    destination=Path(u.Paths.project_content_dir())/'MMDBridgeValidation/FunctionsV2/TreeRender/Source'
    destination.mkdir(parents=True,exist_ok=True)
    for name in ('scene.usda','colors.usdc'):shutil.copy2(OUT/'blender_tree'/name,destination/name)
    random_value=.7215813398361206
    atomic_json(destination/'package.json',{'shader_data':[{'prim_path':'/World/ColoredMesh','object_random':random_value}]})
    world=u.EditorLoadingAndSavingUtils.new_blank_map(False);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    stage=actors.spawn_actor_from_class(u.UsdStageActor,u.Vector());stage.set_actor_label('Cypress_Function_Validation')
    stage.set_render_context(u.Name('unreal'));stage.set_root_layer(str(destination/'scene.usda'))
    report['shader_data']=restore(stage)
    mesh=stage.get_generated_component('/World/ColoredMesh/Mesh');assert isinstance(mesh,u.StaticMeshComponent)
    assert mesh.get_num_materials()==3
    report['materials']=[mesh.get_material(i).get_path_name() for i in range(3)]
    center,extent,radius=u.SystemLibrary.get_component_bounds(mesh);target=u.Vector(center.x,center.y,center.z)
    size=max(extent.x,extent.y,extent.z,100)
    cameras=[]
    for offset in ((2.8,-3.5,.7),(-2.8,-3.5,.7)):
        position=target+u.Vector(*(v*size for v in offset))
        camera=actors.spawn_actor_from_class(u.CineCameraActor,position,u.MathLibrary.find_look_at_rotation(position,target))
        camera.get_cine_camera_component().set_editor_property('current_focal_length',35.)
        cameras.append(camera)
    sun=actors.spawn_actor_from_class(u.DirectionalLight,u.Vector(),u.Rotator(-35,-25,0))
    sun.light_component.set_editor_property('intensity',6.)
    sun.light_component.set_editor_property('cast_raytraced_shadow',u.CastRayTracedShadow.ENABLED)
    sky=actors.spawn_actor_from_class(u.SkyLight,u.Vector());sky.light_component.set_editor_property('intensity',1.)
    sky.light_component.set_editor_property('real_time_capture',True)
    # Actor bindings must be authored against the saved level's path, rather
    # than the transient editor world which disappears in an independent run.
    level=asset_root+'/Maps/Cypress';assert u.EditorLoadingAndSavingUtils.save_map(world,level)
    sequence=u.load_asset(asset_root+'/Sequences/LS_Cypress')
    if sequence:
        for binding in sequence.get_bindings():binding.remove()
        for track in sequence.get_tracks():sequence.remove_track(track)
    else:sequence=u.AssetToolsHelpers.get_asset_tools().create_asset('LS_Cypress',asset_root+'/Sequences',u.LevelSequence,u.LevelSequenceFactoryNew())
    assert sequence
    sequence.set_display_rate(u.FrameRate(24,1));sequence.set_playback_start(0);sequence.set_playback_end(300)
    track=sequence.add_track(u.MovieSceneCameraCutTrack)
    for camera,start,end in zip(cameras,(0,150),(150,300)):
        binding=sequence.add_possessable(camera);section=track.add_section();section.set_range(start,end)
        camera_id=u.MovieSceneObjectBindingID();camera_id.set_editor_property('guid',binding.get_id());section.set_camera_binding_id(camera_id)
    assert u.EditorAssetLibrary.save_loaded_asset(sequence)
    preset=u.load_asset(asset_root+'/Sequences/MRQ_Cypress') or u.AssetToolsHelpers.get_asset_tools().create_asset('MRQ_Cypress',asset_root+'/Sequences',u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    assert preset
    from mmd_bridge.ue_appearance import configure_mrq_defaults
    configure_mrq_defaults(preset,'lumen')
    preset.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase)
    preset.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting);output.output_resolution=u.IntPoint(2560,1080)
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.engine_warm_up_count=16;aa.render_warm_up_count=16;aa.temporal_sample_count=1;aa.spatial_sample_count=1
    console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for key,value in {'r.DynamicGlobalIlluminationMethod':1,'r.ReflectionMethod':1,'r.Lumen.HardwareRayTracing':1,
                      'r.RayTracing.Shadows':1,'r.RayTracing.ForceAllRayTracingEffects':-1}.items():
        console.add_or_update_console_variable(key,value)
    assert u.EditorAssetLibrary.save_loaded_asset(preset)
    level=asset_root+'/Maps/Cypress';assert u.EditorLoadingAndSavingUtils.save_map(world,level)
    job=OUT/'tree_render_job';job.mkdir(exist_ok=True)
    atomic_json(job/'job.json',{'settings':{'project':str(Path(report['project']).resolve()),
        'unreal':str(Path(u.Paths.convert_relative_path_to_full(u.Paths.engine_dir()))/'Binaries/Win64/UnrealEditor.exe')}})
    atomic_json(job/'ue_report.json',{'assets':{'level':level,'sequence':sequence.get_path_name(),
        'materials':report['materials'],'source_packages':asset_dependencies(report['materials']),
                                             'render_preset':preset.get_path_name()}})
    report.update(complete=True,level=level,sequence=sequence.get_path_name(),preset=preset.get_path_name(),
                  resolution=[2560,1080],frames=300,cuts=[0,150],bounds=[str(center),str(extent)])
except Exception:report['error']=traceback.format_exc();u.log_error(report['error'])
atomic_json(OUT/'tree_render_preparation.json',report)
print('TREE_RENDER_PREPARED',report['complete'],flush=True)
