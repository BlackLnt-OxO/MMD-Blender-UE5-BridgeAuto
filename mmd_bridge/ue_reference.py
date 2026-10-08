"""Native single-frame MRQ rendering for the desktop bridge."""
import time
import traceback
import unreal as u
from .ue_foliage import restore_stage_components
from .core import Job, atomic_json, read_json, file_hash
from .material_graph import signature
from . import process

ACTIVE = None


def save_reference_preset(manager, base_preset, config, cfg, pipeline):
    identity=signature(['reference-preset-v2',pipeline,cfg,
        base_preset.get_path_name(),file_hash(manager.asset_file(base_preset))])[:12]
    preset=manager.create(('MRQ_Lumen_' if pipeline=='lumen' else 'MRQ_PathTracing_')+identity,
        u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    preset.copy_from(config)
    if not u.EditorAssetLibrary.save_loaded_asset(preset):raise RuntimeError('参考渲染配置保存失败')
    manager.protect_asset(preset)
    return preset


def reference_camera(world, scene):
    """Apply source optics only to the temporary render world."""
    from .ue_appearance import configure_camera
    source = next((item for item in scene.get('objects',[]) if item.get('camera',{}).get('active')),None)
    if not source:
        raise ValueError('导出包缺少源活动相机参数，不能核对参考帧')
    cameras = u.GameplayStatics.get_all_actors_of_class(world,u.CineCameraActor)
    matches = [actor for actor in cameras if actor.get_actor_label() == source['name']]
    if not matches and len(cameras) == 1:
        matches = cameras
    if len(matches) != 1:
        raise ValueError('无法唯一匹配源活动相机，停止参考帧渲染')
    component = matches[0].get_component_by_class(u.CineCameraComponent)
    configure_camera(component,source['camera'],scene)
    focus = component.get_editor_property('focus_settings')
    return {'actor':matches[0].get_path_name(), 'focal_length':component.get_editor_property('current_focal_length'),
            'aperture':component.get_editor_property('current_aperture'), 'focus_method':str(focus.focus_method),
            'focus_distance_cm':focus.manual_focus_distance}


def hide_reference_objects(world, scene, names):
    """Hide source objects in the temporary PIE render world, never the saved map."""
    hidden = []
    stages = u.GameplayStatics.get_all_actors_of_class(world, u.UsdStageActor)
    for name in names:
        entries = [item for item in scene.get('hierarchy', []) if item['name'] == name]
        if len(entries) != 1:
            raise ValueError('无法唯一匹配预览隐藏物体: ' + name)
        paths = [item['prim_path'] for item in entries[0].get('parts', [])]
        if not paths:
            paths = [entries[0]['prim_path']]
        for path in paths:
            components = [stage.get_generated_component(path) or stage.get_generated_component(entries[0]['prim_path'])
                          for stage in stages]
            components = [component for component in components if component is not None]
            if len(components) != 1:
                raise ValueError('无法唯一匹配预览 USD 组件: ' + path)
            component = components[0]
            component.set_visibility(False, True)
            component.set_hidden_in_game(True, True)
            hidden.append({'name': name, 'prim_path': path, 'component': component.get_path_name()})
    return hidden


def reference_scene_geometry(world, scene, hidden_names=()):
    """Reject a character-only image when the source scene has USD instances."""
    all_instances = scene.get('instances', [])
    instances = [item for item in all_instances if item.get('name') not in hidden_names]
    if not instances:
        return {'expected_instances': 0, 'samples': []}
    stages = u.GameplayStatics.get_all_actors_of_class(world, u.UsdStageActor)
    if len(stages) != 1:
        raise ValueError('参考帧世界缺少唯一 USD 场景，拒绝输出角色独照')
    foliage_distance = restore_stage_components(stages[0])
    records = instances[::max(1, len(instances) // 25)] + [instances[-1]]
    samples = []
    for item in records:
        component = stages[0].get_generated_component(item['prim_path'])
        if component is None:
            raise ValueError('参考帧世界缺少 USD 实例组件: ' + item['prim_path'])
        meshes = [child for child in [component] + list(component.get_children_components(True))
                  if isinstance(child, u.StaticMeshComponent) and child.get_editor_property('static_mesh')]
        samples.append({'prim_path': item['prim_path'], 'component': component.get_path_name(),
                        'mesh_components': len(meshes)})
    if not any(item['mesh_components'] for item in samples):
        raise ValueError('参考帧世界 USD 实例没有可渲染网格，拒绝输出角色独照')
    return {'expected_instances': len(all_instances), 'renderable_instances':len(instances),
            'intentionally_hidden_instances': len(all_instances)-len(instances), 'samples': samples,
            'foliage_distance': foliage_distance}


def start(folder, finished, pipeline=None, isolated_queue=False):
    global ACTIVE
    if ACTIVE is not None:
        raise ValueError('参考帧正在渲染，请等待完成或停止任务')
    job = Job(folder)
    cfg = job.data['settings']
    pipeline = pipeline or cfg.get('scene_render_pipeline', 'path_tracing')
    if pipeline not in ('lumen', 'path_tracing'):
        raise ValueError('未知场景渲染管线')
    report = read_json(job.folder / 'ue_report.json')
    scene = read_json(job.folder / 'manifest.json')['scene']
    if not scene.get('reference_camera'):
        raise ValueError('场景缺少源活动相机，请重新导出场景')
    if not hasattr(u, 'MoviePipelinePIEExecutor'):
        raise ValueError('需部署并启用 MovieRenderPipeline 插件，重启 UE 后再渲染')
    if u.EditorLoadingAndSavingUtils.get_dirty_map_packages():
        raise ValueError('请先保存 UE 当前关卡，再渲染场景参考帧')
    queue_sub = u.get_editor_subsystem(u.MoviePipelineQueueSubsystem)
    if queue_sub.is_rendering():
        raise ValueError('UE 已在渲染，请等待完成')
    queue = u.MoviePipelineQueue() if isolated_queue else queue_sub.get_queue()
    if queue.get_jobs():
        raise ValueError('UE 渲染队列已有任务；请先保存或清空队列，避免覆盖现有任务')
    from .ue_appearance import viewport_volume_settings
    memory_profile = cfg.get('ue_render_memory_profile', 'balanced')
    # Apply the selected tracing path before translating the large map, rather
    # than first allocating its hardware RT scene and switching at MRQ start.
    viewport_volume_settings(False)
    viewport_volume_settings(True, pipeline, memory_profile, cfg.get('foliage_distance_scale', 2.),
                             cfg.get('fog_occlusion_quality','legacy'),scene.get('volumes',[]))
    # Reopening an already loaded large USD world repeats translation and can
    # temporarily retain another set of geometry/texture resources.
    current = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    current_path = current.get_path_name().split('.')[0] if current else None
    if current_path != report['assets']['level']:
        if not u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level']):
            raise RuntimeError('不能打开已转换场景关卡')
    from .ue_appearance import restore_viewport_look, viewport_volume_settings, lumen_post_process, LUMEN_RENDER_CVARS, VOLUME_RENDER_CVARS
    from .ue_host import restore_scene_actor_labels
    restore_scene_actor_labels()
    actor_sub = u.get_editor_subsystem(u.EditorActorSubsystem)
    restore_viewport_look(actor_sub)
    for actor in actor_sub.get_all_level_actors():
        if pipeline == 'lumen' and isinstance(actor, u.PostProcessVolume) and actor.get_actor_label() == 'Source render look':
            settings = actor.get_editor_property('settings')
            lumen_post_process(settings)
            actor.set_editor_property('settings', settings)
    viewport_volume_settings(False)
    viewport_volume_settings(True,pipeline,memory_profile,cfg.get('foliage_distance_scale', 2.),
                             cfg.get('fog_occlusion_quality','legacy'),scene.get('volumes',[]))
    # The owned preview level keeps the same Lumen methods after reopening.
    if not u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level():
        raise RuntimeError('不能保存参考帧关卡，拒绝释放编辑器世界')
    out = job.folder / 'visual'
    out.mkdir(exist_ok=True)
    render_job = queue.allocate_new_job(u.MoviePipelineExecutorJob)
    render_job.map = u.SoftObjectPath(report['assets']['level'])
    render_job.sequence = u.SoftObjectPath(report['assets']['sequence'])
    base_preset = u.load_asset(report['assets']['render_preset'])
    render_job.set_configuration(base_preset)
    config = render_job.get_configuration()
    output = config.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution = u.IntPoint(*scene['reference_camera']['resolution'])
    output.output_directory = u.DirectoryPath(str(out))
    output.file_name_format = 'ue'
    output.override_existing_output = True
    output.use_custom_playback_range = True
    output.custom_start_frame = cfg.get('scene_frame', 1)
    output.custom_end_frame = output.custom_start_frame + 1
    for setting in config.get_all_settings():
        if isinstance(setting, u.MoviePipelineDeferredPassBase):
            config.remove_setting(setting)
    deferred = config.find_or_add_setting_by_class(u.MoviePipelineDeferredPass_PathTracer if pipeline == 'path_tracing' else u.MoviePipelineDeferredPassBase)
    cvars = config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    if pipeline == 'lumen':
        for name,value in LUMEN_RENDER_CVARS.items():
            cvars.add_or_update_console_variable(name,float(value))
    for name in (('r.PathTracing.HeterogeneousVolumes',) if pipeline == 'path_tracing' else ('r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod')):
        cvars.add_or_update_console_variable(name, 1.)
    # A global ShowFlag override can force path tracing even on a deferred pass.
    cvars.add_or_update_console_variable('ShowFlag.PathTracing', 1. if pipeline == 'path_tracing' else 0.)
    config.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
    aa = config.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.spatial_sample_count, aa.temporal_sample_count = (32 if pipeline == 'path_tracing' else 1), 1
    if pipeline == 'lumen':
        aa.override_anti_aliasing = True
        aa.anti_aliasing_method = u.AntiAliasingMethod.AAM_TSR
    aa.engine_warm_up_count, aa.render_warm_up_count = 60, 8
    from .ue_appearance import configure_mrq_defaults
    memory_profile = cfg.get('ue_render_memory_profile', 'balanced')
    from .ue_render_policy import memory_cvars
    memory_values = memory_cvars(memory_profile) if pipeline == 'lumen' else {}
    game_overrides = configure_mrq_defaults(config, pipeline, memory_profile, cfg.get('foliage_distance_scale', 2.),
                                          cfg.get('fog_occlusion_quality','legacy'),scene.get('volumes',[]))
    from .ue_materials import Materials
    owner = u.EditorAssetLibrary.get_metadata_tag(base_preset, 'MMDBridge.Owner')
    if not owner:
        queue.delete_job(render_job)
        raise ValueError('渲染基础配置缺少资产归属标记，拒绝覆盖')
    root = report.get('root') or report['assets']['level'].split('/Maps/')[0]
    manager = Materials(root, owner, report, cfg)
    preset=save_reference_preset(manager,base_preset,config,cfg,pipeline)
    render_job.set_configuration(preset)
    report['assets'].setdefault('render_presets', {})[pipeline] = preset.get_path_name()
    atomic_json(job.folder / 'ue_report.json', report)
    # MRQ opens the saved target map in its own PIE world. Keeping the same
    # large USD world in the editor simultaneously duplicates scene resources.
    # Only release the owned map after its edits and render preset are saved.
    actor = None
    actor_sub = None
    blank_world = u.EditorLoadingAndSavingUtils.new_blank_map(False)
    if blank_world is None:
        queue.delete_job(render_job)
        raise RuntimeError('无法释放编辑器场景副本，参考帧渲染未启动')
    u.SystemLibrary.collect_garbage()
    state = {'executor': None, 'queue': queue, 'isolated_queue': isolated_queue,
             'paused': False, 'check_at': 0, 'tick': None,
             'editor_world_released': True,
             'cancelled': False, 'transitions': [], 'renderer': {'pass': deferred.get_class().get_name(), 'mode': pipeline,
                    'anti_aliasing': {'spatial_samples': aa.spatial_sample_count,
                                      'temporal_samples': aa.temporal_sample_count,
                                      'override': aa.override_anti_aliasing,
                                      'method': str(aa.anti_aliasing_method)},
                    'game_overrides': None if game_overrides is None else {
                        'cinematic_quality': game_overrides.cinematic_quality_settings,
                        'texture_streaming': str(game_overrides.texture_streaming),
                        'force_lod_zero': game_overrides.use_lod_zero,
                        'disable_hlods': game_overrides.disable_hlods,
                        'high_quality_shadows': game_overrides.use_high_quality_shadows,
                        'override_view_distance': game_overrides.override_view_distance_scale},
                    'memory_profile': memory_profile,
                    'memory_budget_console_variables': memory_values,
                    'preset': preset.get_path_name(), 'path_tracing_force_off_observed': False,
                    'actual_passes': [s.get_class().get_name() for s in render_job.get_configuration().get_all_settings()
                                      if isinstance(s, u.MoviePipelineDeferredPassBase)],
                    'high_quality_translucency_reflections': pipeline == 'lumen'}}
    ACTIVE = state

    def finalize(success):
        global ACTIVE
        if state['tick'] is not None:
            u.unregister_slate_post_tick_callback(state['tick'])
        queue.delete_job(render_job)
        for old_executor in state.pop('retired_executors', []):
            old_executor.on_executor_finished_delegate.remove_callable(finish)
            old_executor.on_individual_job_started_delegate.remove_callable(camera_started)
        state['queue'] = None
        ACTIVE = None
        complete = bool(success and not state['cancelled'] and state.get('source_camera') and (out / 'ue.png').is_file())
        if pipeline == 'lumen' and not state['renderer']['path_tracing_force_off_observed']:
            complete = False
        atomic_json(out / 'ue_render.json', {'complete': complete, 'cancelled': state['cancelled'],
                    'image_sha256': file_hash(out / 'ue.png') if complete else None,
                    'gpu': process.gpu_memory_info(), 'gpu_transitions': state['transitions'], 'renderer': state['renderer'],
                    'appearance': state.get('appearance', {}),
                    'source_camera':state.get('source_camera'), 'camera_error':state.get('camera_error'),
                    'hidden_objects': state.get('hidden_objects', []),
                    'fog_supplement':state.get('fog_supplement'),
                    'scene_geometry': state.get('scene_geometry'),
                    'level': report['assets']['level'], 'sequence': report['assets']['sequence'],
                    'editor_world_released': state['editor_world_released'],
                    'resolution': scene['reference_camera']['resolution'], 'frame': cfg.get('scene_frame', 1)})
        job.status('stopped' if state['cancelled'] else 'needs_review' if complete else 'failed',
                   '参考帧渲染已停止' if state['cancelled'] else 'UE 参考帧已生成；画面需复核' if complete else 'UE 参考帧渲染失败，请查看 UE 日志')
        try:
            if state.get('finish_timeout'):
                job.status('failed', '参考帧结束后 PIE 未正常退出；保留渲染回执，请在 UE 检查')
            elif not u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level']):
                job.status('failed', '参考帧已结束，但无法重新打开已保存转换关卡')
        finally:
            finished()

    def finish(executor, success):
        if executor is not None and executor != state['executor']:
            return  # A late delegate from a cancelled attempt cannot end a new one.
        if executor is not None:
            state.setdefault('retired_executors', []).append(executor)
        state['executor'] = None
        if state['paused'] and not state['cancelled']:
            return
        # Cancel/error delegates may arrive while PIE is still tearing down.
        # Keep the bridge busy and the tick alive until restoring a level is safe.
        state['finishing'] = True
        state['finish_success'] = success
        state['finish_started'] = time.monotonic()

    def camera_started(render_job):
        try:
            world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world()
            if cfg.get('fog_occlusion_quality') in ('vsm_finer','vsm_nocache'):
                from .ue_fog_occlusion import apply_world
                stages=u.GameplayStatics.get_all_actors_of_class(world,u.UsdStageActor)
                state['fog_supplement']=apply_world(world,stages,scene.get('volumes',[]),cfg['fog_occlusion_quality'],pipeline)
            state['scene_geometry'] = reference_scene_geometry(world, scene, cfg.get('reference_hidden_objects', []))
            state['source_camera'] = reference_camera(world,scene)
            state['hidden_objects'] = hide_reference_objects(world, scene, cfg.get('reference_hidden_objects', []))
            if cfg.get('scene_import_profile') == 'render_optimized':
                for actor in u.GameplayStatics.get_all_actors_of_class(world, u.Actor):
                    for component in actor.get_components_by_class(u.StaticMeshComponent):
                        component.set_collision_enabled(u.CollisionEnabled.NO_COLLISION)
        except Exception:
            state['camera_error'] = traceback.format_exc()
            state['executor'].cancel_all_jobs()

    def launch():
        executor = u.MoviePipelinePIEExecutor()
        executor.on_executor_finished_delegate.add_callable_unique(finish)
        executor.on_individual_job_started_delegate.add_callable_unique(camera_started)
        state['executor'] = executor
        job.status('ue_render', '正在用 ' + ('路径追踪' if pipeline == 'path_tracing' else 'Lumen') + ' 渲染 UE 源活动相机参考帧')
        if isolated_queue:
            executor.execute(queue)
        else:
            queue_sub.render_queue_with_executor_instance(executor)

    def tick(delta):
        if state.get('finishing'):
            if not u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world():
                finalize(state['finish_success'])
            elif time.monotonic() - state['finish_started'] > 120:
                state['finish_timeout'] = True
                finalize(state['finish_success'])
            return
        if u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world():
            values = {name: u.SystemLibrary.get_console_variable_int_value(name) for name in (
                'ShowFlag.PathTracing', 'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod', 'r.AntiAliasingMethod')}
            if pipeline != 'lumen' or values['ShowFlag.PathTracing'] == 0:
                state['renderer'].update(values)
                state['renderer']['path_tracing_force_off_observed'] = values['ShowFlag.PathTracing'] == 0
        if time.monotonic() < state['check_at']:
            return
        state['check_at'] = time.monotonic() + 2
        try:
            if u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world():
                state['appearance'] = {'console_variables': {
                    name:u.SystemLibrary.get_console_variable_float_value(name)
                    for name in {**VOLUME_RENDER_CVARS, **(LUMEN_RENDER_CVARS if pipeline == 'lumen' else {}),
                                 **memory_values}}}
            if (job.folder / 'stop').exists():
                state['cancelled'], state['paused'] = True, False
                if state['executor']:
                    state['executor'].cancel_all_jobs()
                else:
                    finish(None, False)
                return
            info = process.gpu_memory_info()
            used = process.gpu_budget_percent(info) or 0
            if state['paused']:
                if (state['executor'] is None and used < cfg.get('ue_vram_resume_percent', 80)
                        and not u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world()):
                    state['paused'] = False
                    state['transitions'].append({'state': 'resumed', 'gpu': info})
                    launch()
            elif cfg.get('render_vram_guard_enabled',False) and used >= cfg.get('ue_vram_pause_percent', 90):
                state['paused'] = True
                state['transitions'].append({'state': 'paused', 'gpu': info})
                job.status('ue_wait_vram', 'GPU 专用加共享内存达到 90%，暂停渲染；降至 80% 自动恢复', operation='reference')
                if state['executor']:
                    state['executor'].cancel_all_jobs()
            elif state['executor'] is None:
                launch()
        except Exception:
            state['cancelled'], state['paused'] = True, False
            if state['executor']:
                state['executor'].cancel_all_jobs()
            else:
                finish(None, False)
            job.status('failed', traceback.format_exc())

    atomic_json(out / 'ue_render.json', {'complete': False, 'state': 'starting'})
    job.status('ue_render', '准备 UE 参考帧；按源相机与画幅渲染')
    state['tick'] = u.register_slate_post_tick_callback(tick)
