"""Saved-preset background rendering: isolated output, receipts and validation."""
import re
import shutil
import time
import uuid
from pathlib import Path
from .core import atomic_json, file_hash, read_json, resource_root
from .ue_render_policy import standalone_command


def asset_path(value):
    value = str(value).strip()
    # Accept Content Browser Copy Reference as well as plain soft object paths.
    if "'" in value:
        match = re.fullmatch(r"[\w/\.]+?'(/(?:Game|Engine)/[^']+)'", value)
        if not match:
            raise ValueError('请输入 UE 资产路径，如 /Game/Task/Sequences/MRQ_SourceLook')
        value = match[1]
    if (not value.startswith(('/Game/', '/Engine/')) or any(c in value for c in '\\"\'\r\n\t :')
            or any(part in ('', '.', '..') for part in value.split('/')[1:])):
        raise ValueError('请输入有效的 UE 资产路径，如 /Game/Task/Sequences/MRQ_SourceLook')
    return value


def resolve_preset(report, preset, pipeline):
    if not str(preset).strip():
        assets = report.get('assets', {})
        preset = assets.get('render_presets', {}).get(pipeline) or assets.get('render_preset')
        if not preset:
            raise ValueError('任务尚未生成渲染预设，请先完成场景导入或手动选择已有 MRQ 预设')
    return asset_path(preset)


def install_runtime(project):
    """Update only our managed plugin; do not rewrite project rendering defaults."""
    project = Path(project).resolve()
    plugin = project.parent / 'Plugins/MMDBridge'
    if not (plugin / '.mmd_bridge_managed').is_file():
        raise ValueError('请先为该工程安装工具的 UE 桥接插件；后台渲染无需打开编辑器')
    configured = {p['Name']: p.get('Enabled', True) for p in read_json(project).get('Plugins', [])}
    for name in ('MMDBridge', 'PythonScriptPlugin', 'MovieRenderPipeline'):
        if configured.get(name) is False:
            raise ValueError('工程禁用了后台渲染所需插件：' + name)
    root = resource_root()
    target = plugin / 'Content/Python'
    for name in ('__init__.py', 'core.py', 'process.py', 'ue_standalone_render.py', 'ue_foliage.py','ue_render_policy.py','ue_shader_data.py',
                 'ue_appearance.py','fog_occlusion.py','ue_fog_occlusion.py','ue_fog_froxel.py','ue_materials.py','material_contract.py','ocio_cache.py','material_graph.py','material_policy.py','scene_appearance.py','material_slots.py'):
        shutil.copy2(root / 'mmd_bridge' / name, target / 'mmd_bridge' / name)
    shutil.copy2(root / 'plugins/unreal/MMDBridge/Content/Python/init_unreal.py', target / 'init_unreal.py')


def source_fingerprint(job,request):
    """Changing saved materials, geometry, sequence or render policy invalidates resume."""
    from .material_graph import signature
    project=Path(request['project']).parent;content=project/'Content';files=[]
    for key in ('level','sequence','preset'):
        package=request[key].split('.')[0]
        if package.startswith('/Game/'):
            files.extend([content/(package[6:]+'.uasset'),content/(package[6:]+'.umap')])
    assets=read_json(job.folder/'ue_report.json').get('assets',{})
    materials=assets.get('materials',{})
    paths=list(materials.values()) if isinstance(materials,dict) else list(materials)
    paths+=assets.get('source_packages',[])
    for path in paths:
        package=str(path).split('.')[0]
        if package.startswith('/Game/'):
            files.extend([content/(package[6:]+'.uasset'),content/(package[6:]+'.umap')])
    level=request['level'].split('.')[0]
    if '/Maps/' in level:
        root=content/level[6:].split('/Maps/')[0]
        files.extend(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in ('.uasset','.umap','.usdc','.usda','.json'))
    files.extend((project/'Config').glob('*.ini'))
    runtime=project/'Plugins/MMDBridge/Content/Python'
    files.extend(runtime/'mmd_bridge'/name for name in
                 ('ue_standalone_render.py','ue_render_policy.py','ue_foliage.py','ue_shader_data.py',
                  'ue_appearance.py','scene_appearance.py','fog_occlusion.py','ue_fog_occlusion.py','ue_fog_froxel.py'))
    files.append(runtime/'init_unreal.py')
    files.extend([job.folder/'manifest.json',job.folder/'ue_report.json'])
    return signature([[str(p.resolve()),file_hash(p)] for p in sorted(set(files)) if p.is_file()])


def make_plan(job, preset, output_base='', frame=None, pipeline='lumen'):
    if pipeline not in ('lumen', 'path_tracing'):
        raise ValueError('未知渲染管线')
    if frame is not None and (isinstance(frame, bool) or not isinstance(frame, int)):
        raise ValueError('测试帧必须为整数')
    cfg = job.data['settings']
    report = read_json(job.folder / 'ue_report.json')
    assets = report.get('assets', {})
    if not assets.get('level') or not assets.get('sequence'):
        raise ValueError('任务缺少已保存的 UE 关卡或序列，请先完成导入')
    preset = resolve_preset(report, preset, pipeline)
    level, sequence = map(asset_path, (assets['level'], assets['sequence']))
    token = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    run = job.folder / 'renders' / ('Render_' + token)
    output = (Path(output_base).expanduser().resolve() / run.name) if output_base else run / 'output'
    request = {'id': token, 'project': str(Path(cfg['project']).resolve()),
        'level': level, 'sequence': sequence, 'preset': preset, 'pipeline': pipeline,
        'output': str(output), 'frame': frame, 'run': str(run),'frame_ledger':True,'capture_gpu_csv':True,
        'gpu_abort_percent': int(cfg.get('ue_vram_pause_percent', 90)),
        'render_vram_guard_enabled': bool(cfg.get('render_vram_guard_enabled',False)),
        'masked_materials': [item['path'] for item in report.get('checks', {}).get('foliage_distance', {}).get('mask_sampling', [])]}
    if assets.get('render_preset') and preset == asset_path(assets['render_preset']):
        request['expected_resolution']=[int(cfg['width']),int(cfg['height'])]
    # Generated presets follow the selected renderer. Custom presets retain their
    # own enabled passes; their actual pass names still drive frame validation.
    owned_presets = [assets.get('render_preset'), *assets.get('memory_presets',{}).values()]
    if preset in [asset_path(path) for path in owned_presets if path]:
        request['renderer_override'] = pipeline
    native_lights = report.get('checks',{}).get('scene_lights_native',{}).get('lights',[])
    request['world_light_bindings'] = [dict(actor_name=item['actor'].rsplit('.',1)[-1],
        source=item['snapshot']['source']) for item in native_lights if item.get('snapshot')]
    sun = report.get('checks',{}).get('world_lighting',{}).get('sun')
    if sun and sun.get('component') and sun.get('source'):
        request['world_light_bindings'].append(dict(actor_name=sun['component'].rsplit('.',2)[-2],
                                                    source=sun['source']))
    request['scene_light_scale'] = report.get('checks',{}).get('scene',{}).get('light_scale',cfg.get('scene_light_scale',1.))
    request['world_volume_paths'] = [item['prim_path'] for item in
        report.get('checks',{}).get('usd_stage',{}).get('volumes',[]) if item.get('prim_path')]
    profile = next((key for key, path in assets.get('memory_presets', {}).items()
                    if path == preset), 'balanced')
    request['memory_profile']=profile
    request['fog_occlusion_quality']=cfg.get('fog_occlusion_quality','legacy')
    manifest=job.folder/'manifest.json'
    request['fog_volume_sources']=(read_json(manifest).get('scene',{}).get('volumes',[])
                                    if manifest.is_file() and request['fog_occlusion_quality']!='legacy' else [])
    request['gpu_resume_percent']=int(cfg.get('ue_vram_resume_percent',80))
    request['source_fingerprint']=source_fingerprint(job,request)
    command = standalone_command(cfg, report, preset, profile, request=request,
                                 allow_custom_preset=True)
    command.append('-abslog=' + str(run / 'unreal.log'))
    # Every run gets new directories, so an old image cannot prove success.
    run.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(run / 'request.json', request)
    atomic_json(run / 'plan.json', {'command': command, 'request': request,
                                   'started': False, 'preset_modified': False})
    return run, request, command


def finish_result(run, exit_code):
    run = Path(run)
    request = read_json(run / 'request.json')
    runtime = read_json(run / 'runtime.json') if (run / 'runtime.json').is_file() else {}
    log = (run / 'unreal.log').read_text(encoding='utf-8', errors='replace') if (run / 'unreal.log').is_file() else ''
    camera_failed = ('No camera actor found on Eval Tick' in log or 'Failed to evaluate camera' in log)
    crash = any(term in log for term in ('DXGI_ERROR_DEVICE_REMOVED', 'DXGI_ERROR_DEVICE_HUNG',
                                         'GPU Crashed', 'Fatal error:'))
    output = Path(request['output'])
    images = [p for p in output.rglob('*') if p.is_file() and p.suffix.lower() in
              ('.png', '.exr', '.jpg', '.jpeg', '.bmp', '.tga', '.mov', '.mp4', '.avi', '.mxf')]
    passed = exit_code == 0 and runtime.get('success') is True and bool(images) and not camera_failed and not crash
    frames=None
    if request.get('frame_ledger'):
        from .render_frames import ledger
        contract=runtime.get('frame_contract')
        frames=ledger(output,contract) if contract else {'complete_frames':[],'missing_frames':[],'error':'missing_frame_contract'}
        atomic_json(run/'frames.json',frames)
        passed=passed and bool(frames['complete_frames']) and not frames.get('missing_frames') and not frames.get('error')
        if request.get('expected_resolution') and (not contract or contract.get('resolution')!=request['expected_resolution']):
            passed=False
    result = dict(request, complete=passed, exit_code=exit_code, runtime=runtime,
        camera_binding_failed=camera_failed, gpu_crash=crash, output_files=len(images),
        preview=str(images[0]) if images else '', preset_modified=False,
        visual_status='needs_review')
    result['material_runtime_failures']=list(dict.fromkeys(line.strip() for line in log.splitlines()
        if 'LogMaterial:' in line and ('Default Material will be used' in line or 'Failed to compile' in line)))
    if result['material_runtime_failures']:
        result['complete']=False
    world = runtime.get('runtime_world_validation') or runtime.get('runtime_world_validation_on_load')
    result['world_runtime_failures'] = world.get('errors',[]) if world else []
    if world is not None and not world.get('resource_match'):
        result['complete']=False
    result['resolution_match']=(not request.get('expected_resolution') or
        runtime.get('resolution')==request['expected_resolution'])
    if not result['resolution_match']:result['complete']=False
    if frames is not None:result['frames']=frames
    csv=run/'gpu.csv'
    if runtime.get('gpu_csv_path'):
        original=Path(runtime['gpu_csv_path'])
        if not original.is_absolute():original=Path(request['project']).parent/original
        if original.is_file() and original.name=='MMDBridge_'+request['id']+'.csv':shutil.copy2(original,csv)
    if csv.is_file():
        from .material_calibration import gpu_csv
        try:
            if frames is None or len(frames['complete_frames'])<16:
                raise ValueError('有效输出帧少于 16，预热和空闲 GPU 帧不能证明稳定场景耗时')
            result['gpu_measurement']=gpu_csv(csv,scene_only=runtime.get('gpu_capture_scope')=='producing_frames')
            result['gpu_measurement']['scope']=runtime.get('gpu_capture_scope','engine_frames_including_warmup')
        except ValueError as exc:result['gpu_measurement']={'status':'unmeasured','reason':str(exc)}
    if request['frame'] is not None and images:
        result['preview_sha256'] = file_hash(images[0])
    atomic_json(run / 'result.json', result)
    return result


def resume_plan(job,previous):
    """Reuse only verified frames; launch missing contiguous ranges in a fresh process."""
    from .render_frames import ledger,missing_ranges
    previous=Path(previous);request=read_json(previous/'request.json');runtime=read_json(previous/'runtime.json')
    if Path(request['project']).resolve()!=Path(job.data['settings']['project']).resolve():
        raise ValueError('恢复工程与当前任务不匹配')
    contract=runtime.get('frame_contract')
    if not request.get('frame_ledger') or not contract:raise ValueError('旧渲染缺少帧契约，不能安全恢复')
    if request.get('source_fingerprint')!=source_fingerprint(job,request):
        raise ValueError('源场景、材质、序列或渲染设置已更改，请开始新的渲染')
    frames=ledger(request['output'],contract);ranges=missing_ranges(frames,contract['step'])
    if not ranges:raise ValueError('所有要求的帧均已验证，无需恢复')
    token=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    run=previous.parent/('Render_'+token);run.mkdir()
    # Preserve truncated frames as evidence outside the output scan, so MRQ's
    # no-overwrite policy does not keep a partial file in place forever.
    quarantined=[];output=Path(request['output']).resolve()
    for index,value in enumerate(frames['invalid_files']):
        path=Path(value).resolve()
        if not path.is_relative_to(output):raise ValueError('无效帧路径超出当前渲染输出目录')
        target=run/'incomplete_frames'/(str(index)+'_'+path.name)
        target.parent.mkdir(parents=True,exist_ok=True);path.rename(target)
        quarantined.append({'source':str(path),'preserved':str(target)})
    atomic_json(run/'quarantined_frames.json',quarantined)
    request=dict(request,id=token,run=str(run),resume_of=str(previous),ranges=ranges,frame_contract=contract,
                 render_vram_guard_enabled=bool(job.data['settings'].get('render_vram_guard_enabled',False)))
    atomic_json(run/'request.json',request);atomic_json(run/'reused_frames.json',frames)
    report=read_json(job.folder/'ue_report.json')
    command=standalone_command(job.data['settings'],report,request['preset'],request.get('memory_profile','balanced'),
                              request=request,allow_custom_preset=True)
    command.append('-abslog='+str(run/'unreal.log'))
    atomic_json(run/'plan.json',dict(request=request,command=command,started=False,preset_modified=False))
    return run,request,command
