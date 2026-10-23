"""UE -game executor: render a saved preset without creating an editor world."""
import os
import re
import time
import traceback
from pathlib import Path
import unreal as u
from .core import atomic_json, read_json, stable_id

_REQUEST = read_json(os.environ['MMDBRIDGE_STANDALONE_REQUEST'])
_RUN = Path(_REQUEST['run'])
_STATE = {'success': None, 'progress': 0.0, 'errors': [], 'preset_modified': False}
_NEXT_UPDATE = 0.0
_NEXT_GPU_CHECK = 0.0
_MATERIAL_VARIANTS = None
_VARIANT_COMPONENTS = set()


def apply_material_variants():
    """Optional A/B overrides affect live components in this isolated process only."""
    global _MATERIAL_VARIANTS
    paths = _REQUEST.get('material_overrides', {})
    if not paths:
        return
    if _MATERIAL_VARIANTS is None:
        _MATERIAL_VARIANTS = {}
        for source, target in paths.items():
            asset = u.load_asset(target)
            if not isinstance(asset, u.MaterialInterface):
                raise RuntimeError('测试材质不存在: ' + target)
            _MATERIAL_VARIANTS[source] = asset
    changed = 0
    for component in u.ObjectIterator(u.MeshComponent):
        if not component.get_owner() or component.get_path_name() in _VARIANT_COMPONENTS:
            continue
        matches = False
        for slot, material in enumerate(component.get_materials()):
            replacement = _MATERIAL_VARIANTS.get(material.get_path_name()) if material else None
            if replacement:
                component.set_material(slot, replacement)
                matches = True
        if matches:
            changed += 1
            _VARIANT_COMPONENTS.add(component.get_path_name())
    if changed:
        record(material_variant_components=len(_VARIANT_COMPONENTS))


def record(**values):
    _STATE.update(values, updated=time.time())
    atomic_json(_RUN / 'runtime.json', _STATE)


def probe_face_directions(now):
    if not _REQUEST.get('face_direction_probe'):
        return
    samples=_STATE.setdefault('face_direction_samples',[])
    if len(samples)>=512:
        return
    for component in u.ObjectIterator(u.MMDBridgeFaceDirectionComponent):
        if not component.get_owner():
            continue
        values=dict(u.MMDBridgeCharacterLibrary.face_direction_state(component))
        values={k:[round(v.x,5),round(v.y,5),round(v.z,5)] for k,v in values.items()}
        if samples and samples[-1]['component']==component.get_path_name() and samples[-1]['values']==values:
            continue
        animation=component.get_editor_property('target_mesh').get_anim_instance()
        samples.append({'time':now,'component':component.get_path_name(),
                        'world_time':u.GameplayStatics.get_time_seconds(component),
                        'animation_class':animation.get_class().get_name() if animation else None,
                        'values':values})


def probe_character_pbr(now):
    settings = _REQUEST.get('character_pbr_probe', {})
    if not settings:
        return
    samples = _STATE.setdefault('character_pbr_samples', [])
    if len(samples) >= 512:
        return
    for component in u.ObjectIterator(u.SkeletalMeshComponent):
        mesh = component.get_skeletal_mesh_asset()
        if not component.get_owner() or not mesh or mesh.get_path_name() != settings['mesh']:
            continue
        if ':PersistentLevel.' not in component.get_path_name():
            continue
        animation = component.get_anim_instance()
        rotation = component.get_socket_rotation(settings['head_bone'])
        eyes={}
        for name,bone in settings.get('eye_bones',{}).items():
            eye=component.get_socket_rotation(bone)
            eyes[name]=[eye.pitch,eye.yaw,eye.roll]
        sample = {'component': component.get_path_name(),
                  'rotation': [rotation.pitch, rotation.yaw, rotation.roll],
                  'eyes':eyes,
                  'morphs': {name: animation.get_curve_value(name) if animation else 0.
                             for name in settings.get('morphs', [])},
                  'lights': {key: [float(c.get_editor_property('intensity')) for c in u.ObjectIterator(cls)
                                   if c.get_owner() and ':PersistentLevel.' in c.get_path_name()]
                             for key, cls in (('directional', u.DirectionalLightComponent), ('sky', u.SkyLightComponent))}}
        if settings.get('bone_probes'):
            bone_values={}
            for name in settings['bone_probes']:
                transform=component.get_socket_transform(name,u.RelativeTransformSpace.RTS_WORLD)
                position=transform.translation;rotation=component.get_socket_rotation(name)
                scale=getattr(transform,'scale3d',None)
                bone_values[name]={'position':[position.x,position.y,position.z],
                    'rotation':[rotation.pitch,rotation.yaw,rotation.roll],
                    'scale':[scale.x,scale.y,scale.z] if scale is not None else None}
            sample['bone_probes']=bone_values
        if settings.get('hidden_slots'):
            values=[]
            for slot in settings['hidden_slots']:
                material=component.get_material(slot)
                if material is None:continue
                value={'slot':slot,'material':material.get_path_name(),'class':material.get_class().get_name()}
                try:
                    value['opacity']=(material.get_scalar_parameter_value('Opacity') if isinstance(material,u.MaterialInstanceDynamic)
                                      else u.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(material,'Opacity'))
                except Exception as exc:value['opacity_read_error']=str(exc)
                values.append(value)
            sample['hidden_material_parameters']=values
        if not samples or any(samples[-1].get(key) != value for key, value in sample.items()):
            samples.append(dict(sample, time=now))


def probe_character_animation(now):
    settings=_REQUEST.get('character_animation_probe',{})
    if not settings:
        return
    samples=_STATE.setdefault('character_animation_samples',[])
    if len(samples)>=512:
        return
    for actor in u.ObjectIterator(u.MMDBridgeCharacterActor):
        main,outline=actor.skeletal_mesh_component,actor.outline_mesh
        instances=[c.get_anim_instance() for c in (main,outline)]
        if not all(instances):
            continue
        values=[]
        for component,instance in zip((main,outline),instances):
            position=component.get_socket_location(settings['head_bone'])
            rotation=component.get_socket_rotation(settings['head_bone'])
            values.append({'position':[position.x,position.y,position.z],
                'rotation':[rotation.pitch,rotation.yaw,rotation.roll],
                'morphs':{name:instance.get_curve_value(name) for name in settings.get('morphs',[])}})
        sample={'actor':actor.get_path_name(),'independent_instances':instances[0]!=instances[1],
                'main':values[0],'outline':values[1]}
        if settings.get('light_intensities'):
            sample['light_intensities']={key:[float(c.get_editor_property('intensity')) for c in u.ObjectIterator(cls)
                if c.get_owner() and ':PersistentLevel.' in c.get_owner().get_path_name()]
                for key,cls in (('directional',u.DirectionalLightComponent),('sky',u.SkyLightComponent))}
        if samples and all(samples[-1].get(k)==v for k,v in sample.items()):
            continue
        samples.append(dict(sample,time=now))


def prepare_job(queue):
    """Copy settings into a transient job; never save or edit the source asset."""
    actual = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
    if actual != Path(_REQUEST['project']).resolve():
        raise RuntimeError('后台渲染工程与任务不匹配')
    source = u.load_asset(_REQUEST['preset'])
    if not isinstance(source, u.MoviePipelinePrimaryConfig):
        raise ValueError('所选资产不是 MRQ 配置预设：' + _REQUEST['preset'])
    if not isinstance(u.load_asset(_REQUEST['sequence']), u.LevelSequence):
        raise ValueError('渲染序列不存在：' + _REQUEST['sequence'])
    job = queue.allocate_new_job(u.MoviePipelineExecutorJob)
    job.map, job.sequence = u.SoftObjectPath(_REQUEST['level']), u.SoftObjectPath(_REQUEST['sequence'])
    job.set_configuration(source)
    config = job.get_configuration()
    console = config.find_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    values = {entry.name: entry.value for entry in console.get_console_variables() if entry.is_enabled} if console else {}
    added = []
    if _REQUEST.get('renderer_override'):
        from .ue_render_policy import ensure_mrq_image_output
        renderer = _REQUEST['renderer_override']
        ensure_mrq_image_output(config,renderer,u)
        console=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
        console.add_or_update_console_variable('ShowFlag.PathTracing',float(renderer=='path_tracing'))
        # UE 5.8 path tracing uses the shared heterogeneous-volume switch.
        if renderer=='path_tracing':
            console.add_or_update_console_variable('r.HeterogeneousVolumes',1.)
    if not any(isinstance(s, u.MoviePipelineRenderPass) and s.is_enabled() for s in config.get_all_settings()):
        pipeline = _REQUEST['pipeline']
        if 'ShowFlag.PathTracing' in values:
            pipeline = 'path_tracing' if values['ShowFlag.PathTracing'] else 'lumen'
        config.find_or_add_setting_by_class(u.MoviePipelineDeferredPass_PathTracer
            if pipeline == 'path_tracing' else u.MoviePipelineDeferredPassBase)
        added.append('render_pass')
    if not any(isinstance(s, u.MoviePipelineOutputBase) and s.is_enabled() for s in config.get_all_settings()):
        config.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
        added.append('png_output')
    output = config.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    if _REQUEST.get('expected_resolution'):
        output.output_resolution=u.IntPoint(*_REQUEST['expected_resolution'])
    output.output_directory = u.DirectoryPath(_REQUEST['output'])
    output.override_existing_output = False
    if _REQUEST['frame'] is not None:
        output.use_custom_playback_range = True
        output.custom_start_frame = _REQUEST['frame']
        output.custom_end_frame = _REQUEST['frame'] + 1
    if _REQUEST.get('frame_ledger'):
        from .ue_render_policy import mrq_beauty_passes
        passes = mrq_beauty_passes(config,u)
        if _REQUEST.get('ranges') and _REQUEST.get('frame_contract',{}).get('passes',['FinalImage']) != passes:
            raise ValueError('恢复渲染通道与原帧契约不一致，请开始新的渲染')
        config.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
        sequence=u.load_asset(_REQUEST['sequence'])
        start=output.custom_start_frame if output.use_custom_playback_range else sequence.get_playback_start()
        end=output.custom_end_frame if output.use_custom_playback_range else sequence.get_playback_end()
        sequence_rate=sequence.get_display_rate()
        output_rate=output.output_frame_rate
        # Native FFrameRate wrappers may compare object identity. Compare the
        # rational values, including equivalent fractions such as 60/2 and 30/1.
        same_rate=(output_rate.denominator>0 and sequence_rate.denominator>0 and
                   output_rate.numerator*sequence_rate.denominator == sequence_rate.numerator*output_rate.denominator)
        if output.use_custom_frame_rate and not same_rate:
            raise ValueError('帧恢复暂不支持与序列不同的输出帧率，请使用序列帧率')
        output.file_name_format='MMDBridge_{render_pass}_{frame_number}'
        output.frame_number_offset=0
        record(frame_contract={'start':start,'end':end,'step':output.output_frame_step,
               'resolution':[output.output_resolution.x,output.output_resolution.y],'passes':passes})
        record(actual_renderer={'passes':passes,'pipeline':'path_tracing' if 'PathTracer' in passes else 'lumen'})
    if _REQUEST.get('ranges'):
        start,end=_REQUEST['ranges'][0]
        output.use_custom_playback_range=True;output.custom_start_frame=start;output.custom_end_frame=end
    if _REQUEST.get('fog_occlusion_quality','legacy')!='legacy':
        from .ue_fog_occlusion import configure as configure_fog_occlusion
        fog_status=configure_fog_occlusion(config,_REQUEST['fog_occlusion_quality'],
            _STATE.get('actual_renderer',{}).get('pipeline',_REQUEST['pipeline']),
            _REQUEST.get('fog_volume_sources',[]))
    else:
        fog_status=dict(requested='legacy',effective='legacy',status='legacy',parameters_changed=False)
    record(fog_occlusion=fog_status)
    config.initialize_transient_settings()
    profile=_REQUEST.get('memory_profile')
    budgets={};game_state={}
    if profile is not None:
        from .ue_render_policy import memory_cvars
        budgets=memory_cvars(profile)
        game=config.find_or_add_setting_by_class(u.MoviePipelineGameOverrideSetting)
        game.texture_streaming=u.MoviePipelineTextureStreamingMethod.NONE
        game.cinematic_quality_settings=True
        for name in ('use_lod_zero','disable_hlods','use_high_quality_shadows','override_view_distance_scale','flush_grass_streaming'):
            setattr(game,name,False)
        console=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
        for key,value in budgets.items():console.add_or_update_console_variable(key,value)
        game_state={'cinematic':game.cinematic_quality_settings,'texture_streaming':str(game.texture_streaming),
                    'lod_zero':game.use_lod_zero,'game_mode':str(game.get_editor_property('soft_game_mode_override'))}
    console=config.find_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    values={entry.name:entry.value for entry in console.get_console_variables() if entry.is_enabled} if console else {}
    record(state='prepared', source_preset=source.get_path_name(), added_missing_settings=added,
           output=_REQUEST['output'], console_variables=values,
           resolution=[output.output_resolution.x, output.output_resolution.y],memory_profile=profile,
           effective_budget=budgets,game_overrides=game_state)
    return job


@u.uclass()
class MMDBridgeRuntimeExecutor(u.MoviePipelinePythonHostExecutor):
    inner = u.uproperty(u.MoviePipelineInProcessExecutor)
    runtime_subsystem = u.uproperty(u.MoviePipelineQueueEngineSubsystem)

    def _post_init(self):
        self.inner = None
        self.runtime_subsystem = None

    @u.ufunction(override=True)
    def execute_delayed(self, in_pipeline_queue):
        try:
            subsystem = u.get_engine_subsystem(u.MoviePipelineQueueEngineSubsystem)
            if subsystem is None:
                # A late-loaded MRQ module can miss Engine subsystem collection
                # initialization in an uncooked -game run. Its public runtime
                # queue API also works on an owned Engine-outer subsystem.
                engines=[e for e in u.ObjectIterator(u.Object) if u.Object.get_name(e).rsplit('_',1)[0].endswith('GameEngine')
                         and u.Object.get_path_name(e).startswith('/Engine/Transient.')]
                record(engine_candidates=[u.Object.get_path_name(e) for e in engines])
                if len(engines)!=1:raise RuntimeError('无法唯一确定运行中的 GameEngine')
                subsystem=u.new_object(u.MoviePipelineQueueEngineSubsystem,outer=engines[0])
                record(runtime_subsystem='owned_engine_outer')
            self.runtime_subsystem=subsystem
            self.pipeline_queue = subsystem.get_queue()
            self.pipeline_queue.delete_all_jobs()
            prepare_job(self.pipeline_queue)
            if _REQUEST.get('ranges'):
                first=_REQUEST['ranges'][0]
                for start,end in _REQUEST['ranges'][1:]:
                    _REQUEST['ranges'][0]=[start,end]
                    prepare_job(self.pipeline_queue)
                _REQUEST['ranges'][0]=first
                record(frame_contract=_REQUEST['frame_contract'])
            self.inner = u.new_object(u.MoviePipelineInProcessExecutor, outer=self)
            self.inner.on_executor_finished_delegate.add_function_unique(self, 'finished')
            self.inner.on_executor_errored_delegate.add_function_unique(self, 'errored')
            if _REQUEST.get('validate_only'):
                record(state='validated', success=True, validation_only=True)
                self.on_executor_finished_impl()
                return
            record(state='loading', engine=u.SystemLibrary.get_engine_version())
            # The runtime subsystem sets bRenderViewport=false on the native
            # executor, avoiding a second player-viewport render/history buffer.
            subsystem.set_configuration(None, False)
            record(player_viewport_rendering=False)
            subsystem.render_queue_with_executor_instance(self.inner)
        except Exception:
            record(state='failed', success=False, errors=[traceback.format_exc()])
            u.log_error(_STATE['errors'][-1])
            self.on_executor_finished_impl()

    @u.ufunction(override=True)
    def on_map_load(self, world):
        try:
            if hasattr(u, 'UsdStageActor'):
                from .ue_foliage import restore_runtime_components
                from .ue_appearance import restore_volume_materials, runtime_world_snapshot
                count = 0
                volume_restored = []
                stages=u.GameplayStatics.get_all_actors_of_class(world, u.UsdStageActor)
                for actor in stages:
                    volume_restored.extend(restore_volume_materials(actor))
                    count += restore_runtime_components(actor, _REQUEST.get('masked_materials', []))
                record(runtime_foliage_components=count, runtime_volume_materials_restored=volume_restored)
                from .fog_occlusion import decision, NATIVE_PROFILES
                from .ue_fog_froxel import restore_native
                actual_pipeline=_STATE.get('actual_renderer',{}).get('pipeline',_REQUEST['pipeline'])
                native=decision(_REQUEST.get('fog_occlusion_quality','legacy'),actual_pipeline,
                    engine=u.SystemLibrary.get_engine_version(),volumes=_REQUEST.get('fog_volume_sources',[]))
                record(native_fog=restore_native(world,stages,
                    actual_pipeline,native.get('effective') in NATIVE_PROFILES,_REQUEST.get('fog_volume_sources',[])))
                if _REQUEST.get('fog_occlusion_quality') in ('vsm_finer','vsm_nocache'):
                    from .ue_fog_occlusion import apply_world
                    record(fog_supplement=apply_world(world,stages,_REQUEST.get('fog_volume_sources',[]),
                        _REQUEST['fog_occlusion_quality'],_STATE.get('actual_renderer',{}).get('pipeline',_REQUEST['pipeline']),
                        _REQUEST.get('world_light_bindings',[])))
                snapshot=runtime_world_snapshot(world,stages,
                    _STATE.get('actual_renderer',{}).get('pipeline',_REQUEST['pipeline']),
                    _REQUEST.get('world_light_bindings',[]),_REQUEST.get('scene_light_scale',1.),
                    _REQUEST.get('world_volume_paths',[]))
                record(runtime_world_validation_on_load=snapshot)
                if not snapshot['resource_match']:
                    raise RuntimeError('世界场景运行时资源校验失败: '+', '.join(snapshot['errors']))
        except Exception:
            # Distance restoration is part of the result, not silently ignored.
            _STATE['errors'].append(traceback.format_exc())
            record(distance_policy_failed=True, state='failed', success=False)
            if self.inner:
                self.inner.cancel_all_jobs()
            self.on_executor_finished_impl()

    @u.ufunction(override=True)
    def on_begin_frame(self):
        global _NEXT_UPDATE, _NEXT_GPU_CHECK
        super(MMDBridgeRuntimeExecutor, self).on_begin_frame()
        if _STATE.get('native_fog',{}).get('renderer')=='volumetric_fog':
            from .ue_fog_froxel import refresh_native_streaming
            refresh_native_streaming()
        # EndCapture is asynchronous and needs subsequent engine frames. MRQ's
        # command-line completion otherwise quits before the CSV writer flushes.
        if _STATE.get('state') == 'flushing_gpu_csv':
            path=Path(_STATE['gpu_csv_path'])
            complete=False
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0,path.stat().st_size-65536))
                    complete=b'[HasHeaderRowAtEnd]' in stream.read()
            except OSError:
                pass
            if complete or time.monotonic()-_STATE['csv_stop_monotonic']>30:
                record(state='finished' if _STATE['success'] else 'failed',gpu_csv_flushed=complete)
                self.on_executor_finished_impl()
            return
        if self.inner is None or _STATE.get('success') is not None:
            return
        probe_face_directions(time.monotonic())
        try:
            probe_character_pbr(time.monotonic())
            probe_character_animation(time.monotonic())
        except Exception:
            _STATE['errors'].append(traceback.format_exc())
            record(state='failed',success=False)
            self.inner.cancel_all_jobs();self.on_executor_finished_impl()
            return
        if _REQUEST.get('material_overrides') and not _STATE.get('material_variants_ready'):
            try:
                apply_material_variants()
                for active in u.ObjectIterator(u.MoviePipeline):
                    if (active.get_current_job() in self.pipeline_queue.get_jobs() and
                            active.get_pipeline_state()==u.MovieRenderPipelineState.PRODUCING_FRAMES):
                        if not _VARIANT_COMPONENTS:
                            raise RuntimeError('测试材质没有匹配到任何场景组件')
                        record(material_variants_ready=True)
                        break
            except Exception:
                _STATE['errors'].append(traceback.format_exc())
                record(state='failed',success=False)
                self.inner.cancel_all_jobs()
                self.on_executor_finished_impl()
                return
        now = time.monotonic()
        if now >= _NEXT_UPDATE or (_REQUEST.get('capture_gpu_csv') and not _STATE.get('gpu_csv_requested')):
            _NEXT_UPDATE = now + 1.0
            # ActiveMoviePipeline is a protected native property, unavailable
            # to -game Python. Public UObject iteration finds our own pipeline.
            try:
                for pipeline in u.ObjectIterator(u.MoviePipeline):
                    if pipeline.get_current_job() in self.pipeline_queue.get_jobs():
                        if not _STATE.get('world_validation_ready') and pipeline.get_pipeline_state()==u.MovieRenderPipelineState.PRODUCING_FRAMES:
                            from .ue_appearance import runtime_world_snapshot
                            world=pipeline  # Native UObject supplies its world as a context.
                            stages=u.GameplayStatics.get_all_actors_of_class(world,u.UsdStageActor) if hasattr(u,'UsdStageActor') else []
                            snapshot=runtime_world_snapshot(world,stages,
                                _STATE.get('actual_renderer',{}).get('pipeline',_REQUEST['pipeline']),
                                _REQUEST.get('world_light_bindings',[]),_REQUEST.get('scene_light_scale',1.),
                                _REQUEST.get('world_volume_paths',[]))
                            record(runtime_world_validation=snapshot,world_validation_ready=True)
                            if not snapshot['resource_match']:
                                raise RuntimeError('世界场景渲染资源校验失败: '+', '.join(snapshot['errors']))
                        record(state='rendering', progress=u.MoviePipelineLibrary.get_completion_percentage(pipeline))
                        if (_REQUEST.get('capture_gpu_csv') and not _STATE.get('gpu_csv_requested') and
                                pipeline.get_pipeline_state()==u.MovieRenderPipelineState.PRODUCING_FRAMES):
                            filename='MMDBridge_'+re.sub(r'[^A-Za-z0-9_-]','_',str(_REQUEST['id']))+'_'+stable_id(str(_REQUEST['id']))[:8]+'.csv'
                            u.SystemLibrary.execute_console_command(None,'r.GPUCsvStatsEnabled 1')
                            u.SystemLibrary.execute_console_command(None,'CSVProfile STARTFILE='+filename)
                            u.SystemLibrary.execute_console_command(None,'CSVProfile START')
                            record(gpu_csv_requested=True,gpu_capture_scope='producing_frames',
                                   gpu_csv_path=str(Path(u.Paths.project_saved_dir())/'Profiling/CSV'/filename))
                        break
            except Exception:
                _STATE['errors'].append(traceback.format_exc())
                record(state='failed', success=False)
                self.inner.cancel_all_jobs()
                self.on_executor_finished_impl()
                return
        if now >= _NEXT_GPU_CHECK:
            _NEXT_GPU_CHECK = now + max(.25,float(_REQUEST.get('gpu_sample_interval',2.)))
            from .process import render_memory_info, gpu_budget_percent
            memory = render_memory_info()
            percent = gpu_budget_percent(memory)
            actual={key:u.SystemLibrary.get_console_variable_float_value(key) for key in _STATE.get('effective_budget',{})}
            quality={key:u.SystemLibrary.get_console_variable_float_value(key) for key in
                     ('r.RayTracing','r.DynamicGlobalIlluminationMethod','r.ReflectionMethod','r.Lumen.HardwareRayTracing','r.RayTracing.Shadows')}
            peak=dict(_STATE.get('memory_peak',{}))
            for key,value in (memory or {}).items():
                if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
            fog={key:u.SystemLibrary.get_console_variable_float_value(key) for key in
                 ('r.VolumetricFog','r.VolumetricFog.GridPixelSize','r.VolumetricFog.GridSizeZ','r.VolumetricFog.InjectRaytracedLights')}
            if _STATE.get('gpu_csv_requested') and _STATE.get('state')=='rendering':
                samples=_STATE.setdefault('render_memory_samples',[])
                samples.append(dict(time=now,**(memory or {})))
            record(gpu=memory, gpu_budget_percent=percent,memory_peak=peak,actual_budget=actual,
                   actual_render_settings=quality,actual_fog_settings=fog)
            if _REQUEST.get('render_vram_guard_enabled',False) and percent is not None and percent >= _REQUEST.get('gpu_abort_percent', 90):
                record(state='stopping', gpu_budget_exceeded=True)
                self.inner.cancel_all_jobs()

    @u.ufunction(ret=None, params=[u.MoviePipelineExecutorBase, bool])
    def finished(self, executor, success):
        if _REQUEST.get('fog_occlusion_quality') in ('vsm_finer','vsm_nocache'):
            from .ue_fog_occlusion import restore_components
            restore_components()
        success = bool(success) and not _STATE['errors'] and not _STATE.get('gpu_budget_exceeded')
        record(state='finished' if success else 'failed', success=success,
               progress=1.0 if success else _STATE.get('progress', 0))
        if _REQUEST.get('capture_gpu_csv') and _STATE.get('gpu_csv_path'):
            _STATE['csv_stop_monotonic']=time.monotonic()
            record(state='flushing_gpu_csv')
            u.SystemLibrary.execute_console_command(None,'CSVProfile STOP')
            return
        self.on_executor_finished_impl()

    @u.ufunction(ret=None, params=[u.MoviePipelineExecutorBase, u.MoviePipeline, bool, u.Text])
    def errored(self, executor, pipeline, fatal, message):
        _STATE['errors'].append(str(message))
        record(state='failed' if fatal else 'rendering')
