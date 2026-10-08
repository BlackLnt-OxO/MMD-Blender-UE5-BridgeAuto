"""MRQ memory budgets; texture pool limits are not whole-GPU memory limits."""

MEMORY_PROFILES = ('balanced', 'low', 'quality', 'compatible')


def mrq_beauty_passes(config, native):
    """Frame contracts follow the enabled native pass, not a UI pipeline label."""
    passes = []
    other = []
    for setting in config.get_all_settings():
        if not isinstance(setting,native.MoviePipelineRenderPass) or not setting.is_enabled():
            continue
        if isinstance(setting,native.MoviePipelineDeferredPass_PathTracer):
            name = 'PathTracer'
        elif type(setting) == native.MoviePipelineDeferredPassBase:
            name = 'FinalImage'
        else:
            # Auxiliary/custom passes keep rendering. The beauty-frame contract
            # does not pretend to validate their additional files.
            other.append(setting.get_class().get_name())
            continue
        if name not in passes:
            passes.append(name)
    if not passes:
        raise ValueError('MRQ 缺少可验证的图像渲染通道: '+', '.join(other))
    return passes


def ensure_mrq_image_output(config, pipeline, native):
    """Saved owned presets must contain a renderer as well as a file writer."""
    if pipeline not in ('lumen', 'path_tracing'):
        raise ValueError('未知渲染管线: ' + str(pipeline))
    path_tracer = native.MoviePipelineDeferredPass_PathTracer
    for setting in list(config.get_all_settings()):
        if isinstance(setting, native.MoviePipelineDeferredPassBase):
            if isinstance(setting, path_tracer) != (pipeline == 'path_tracing'):
                config.remove_setting(setting)
    renderer = config.find_or_add_setting_by_class(path_tracer if pipeline == 'path_tracing'
                                                   else native.MoviePipelineDeferredPassBase)
    renderer.set_is_enabled(True)
    config.find_or_add_setting_by_class(native.MoviePipelineImageSequenceOutput_PNG).set_is_enabled(True)
    return renderer


BALANCED_CVARS = {
    'r.TextureStreaming': 1,
    'r.Streaming.PoolSize': 2048,
    'r.Streaming.LimitPoolSizeToVRAM': 1,
    'r.Streaming.MipBias': 0,
    'r.TSR.History.ScreenPercentage': 100,
    'r.LumenScene.SurfaceCache.AtlasSize': 2048,
    'r.LumenScene.SurfaceCache.CardMaxResolution': 256,
    'r.RenderTargetPoolMin': 400,
    'r.Shadow.Virtual.MaxPhysicalPages': 4096,
    # Cine removes the dynamic-geometry budget. Bound BLAS work separately
    # from texture caches; these limits do not thin the visible forest.
    'r.RayTracing.UseReferenceBasedResidency': 1,
    'r.RayTracing.ResidentGeometryMemoryPoolSizeInMB': 1024,
    'r.RayTracing.DynamicGeometry.MaxUpdatePrimitivesPerFrame': 100000,
    'r.RayTracing.DynamicGeometry.PriorityBasedUpdate': 1,
}
LOW_CVARS = {
    **BALANCED_CVARS,
    'r.Streaming.PoolSize': 1536,
    'r.LumenScene.SurfaceCache.AtlasSize': 1024,
    'r.LumenScene.SurfaceCache.CardMaxResolution': 128,
    'r.Lumen.ScreenProbeGather.DownsampleFactor': 16,
    'r.Lumen.Reflections.DownsampleFactor': 2,
    'r.RenderTargetPoolMin': 300,
    'r.DOF.Gather.ResolutionDivisor': 2,
    'r.RayTracing.ResidentGeometryMemoryPoolSizeInMB': 768,
    'r.RayTracing.DynamicGeometry.MaxUpdatePrimitivesPerFrame': 30000,
}
COMPATIBLE_CVARS = {
    **LOW_CVARS,
    'r.Lumen.HardwareRayTracing': 0,
    'r.Lumen.HardwareRayTracing.LightingMode': 0,
    'r.RayTracing.Shadows': 0,
    'r.RayTracing.ForceAllRayTracingEffects': 0,
    # Avoid constructing RT geometry after Cine enables it again. Raster
    # geometry, skeletal animation and mesh distance fields remain enabled.
    'r.RayTracing.Geometry.StaticMeshes': 0,
    'r.RayTracing.Geometry.InstancedStaticMeshes': 0,
    'r.RayTracing.Geometry.SkeletalMeshes': 0,
    'r.RayTracing.Geometry.Landscape': 0,
    'r.Shadow.Virtual.Enable': 1,
}
OWNED_MEMORY_CVARS = set(BALANCED_CVARS) | set(LOW_CVARS) | set(COMPATIBLE_CVARS)


def foliage_distance_cvars(scale=2.):
    """Keep raster mip/LOD detail farther away and bound RT instance culling.

    Texture boost remains subject to the selected streaming pool. Ray tracing
    still uses culling and residency; this does not include the whole forest.
    """
    import math
    if not math.isfinite(scale) or not 1 <= scale <= 4:
        raise ValueError('叶片显示距离倍率必须在 1 到 4 之间')
    return {
        'r.ViewDistanceScale': scale,
        'r.StaticMeshLODDistanceScale': 1. / scale,
        'foliage.LODDistanceScale': scale,
        'foliage.CullDistanceScale': scale,
        'foliage.MinimumScreenSize': 0.000005 / (scale * scale),
        'r.Streaming.Boost': scale,
        'r.RayTracing.Geometry.InstancedStaticMeshes.Culling': 1,
        'r.RayTracing.Geometry.InstancedStaticMeshes.CullAngle': 2. / scale,
        'r.RayTracing.Geometry.InstancedStaticMeshes.CullClusterRadius': 10000. * scale,
        'r.RayTracing.Geometry.InstancedStaticMeshes.LowScaleCullRadius': 1000. * scale,
        'r.RayTracing.Culling.Angle': 1. / scale,
    }


def lighting_cvars(profile):
    """Explicit restoration when switching back from software compatibility."""
    if profile not in MEMORY_PROFILES:
        raise ValueError('未知渲染显存策略: ' + str(profile))
    return ({'r.RayTracing.Shadows': 0, 'r.Lumen.HardwareRayTracing': 0,
             'r.Lumen.HardwareRayTracing.LightingMode': 0}
            if profile == 'compatible' else
            {'r.RayTracing.Shadows': 1, 'r.Lumen.HardwareRayTracing': 1,
             'r.Lumen.HardwareRayTracing.LightingMode': 1})


def memory_cvars(profile='balanced'):
    if profile not in MEMORY_PROFILES:
        raise ValueError('未知渲染显存策略: ' + str(profile))
    return dict({'balanced': BALANCED_CVARS, 'low': LOW_CVARS,
                 'compatible': COMPATIBLE_CVARS, 'quality': {}}[profile])


def shader_worker_arguments(cfg):
    """Bound worker processes for this launch without editing project ini files."""
    import os
    workers=max(1,min(4,int(cfg.get('ue_shader_workers',2))))
    reserved=max(0,(os.cpu_count() or 1)-workers)
    return ['-ini:Engine:[DevOptions.Shaders]:'+key+'='+str(value) for key,value in
            (('NumUnusedShaderCompilingThreads',reserved),('NumUnusedShaderCompilingThreadsDuringGame',reserved),
             ('ShaderCompilerCoreCountThreshold',9999))]


def standalone_command(cfg, report, preset, profile='balanced', *, request=None,
                       allow_custom_preset=False):
    from pathlib import Path
    executable = Path(cfg['unreal']).with_name('UnrealEditor-Cmd.exe')
    if not executable.is_file() or not Path(cfg['project']).is_file():
        raise ValueError('UE 渲染程序或工程不存在')
    level, sequence = report['assets']['level'], report['assets']['sequence']
    root = level.split('/Maps/')[0]
    if not allow_custom_preset and not preset.startswith(root + '/'):
        raise ValueError('渲染预设不属于当前任务')
    memory_cvars(profile)  # Validate before constructing a launch command.
    command = [str(executable), cfg['project'], level, '-game', '-RenderOffscreen',
            '-LevelSequence=' + sequence, '-MoviePipelineConfig=' + preset,
            '-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelineInProcessExecutor',
            '-unattended', '-nosound', '-log']
    if request is not None:
        # The runtime executor copies the selected preset and supplies missing
        # output/pass settings in memory. No editor world or asset save occurs.
        command = [str(executable), cfg['project'], '/Engine/Maps/Entry', '-game',
            '-RenderOffscreen', '-unattended', '-nosound', '-nosplash', '-log',
            '-windowed', '-ResX=320', '-ResY=180',
            '-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor',
            '-ExecutorPythonClass=/Engine/PythonTypes.MMDBridgeRuntimeExecutor']
    if profile == 'compatible':
        # Process-only ini overrides disable RT before the map loads. Persistent
        # project settings retain the requested hardware tracing defaults.
        command += ['-ini:Engine:[/Script/Engine.RendererSettings]:r.RayTracing=False,r.GenerateMeshDistanceFields=True',
                    '-ini:Engine:[SystemSettings]:r.Lumen.HardwareRayTracing=0,r.RayTracing.Shadows=0']
    if request is not None:
        values=memory_cvars(profile)
        if values:command.append('-ExecCmds='+','.join(f'{key} {value}' for key,value in values.items()))
    return command+shader_worker_arguments(cfg)
