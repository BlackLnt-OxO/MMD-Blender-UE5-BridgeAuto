from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from .core import Job, Settings, atomic_json, read_json, resource_root, stable_id, file_hash, task_transfer_folder
from . import process
from .mmd_avi import run_bake
from .blender_validate import roundtrip_ready
from .character_profiles import character_alpha_ready
from .ue_project import ensure_render_defaults, ensure_builtin_plugins

UE_CODE_MODULES=('character_material_preset.py','ue_character_material_preset.py','character_custom_materials.py','ue_custom_materials.py','ue_custom_material_edit.py','effective_inputs.py','pmx_binary.py','pmx_reader.py','pmx_model.py','export_quality.py','orchestration.py','ue_host.py','ue_reference.py','ue_materials.py','ue_appearance.py','ue_usd.py',
                 'fog_occlusion.py','ue_fog_occlusion.py','ue_fog_froxel.py','native_fog_candidate.py',
                  'ue_foliage.py','ue_render_policy.py','material_graph.py','material_contract.py','ocio_cache.py','ue_shader_data.py','material_policy.py','material_plan.py','material_workflow.py','material_results.py','material_repair.py','ue_material_repair.py','material_surface.py','ue_material_surface.py','process.py','core.py','character_profiles.py','face_sdf.py','ue_character_materials.py','character_pbr.py','ue_character_pbr.py','ue_character_pbr_edit.py',
                  'character_texture_review.py','character_geometry.py','character_geometry_roles.py','blender_contour.py')


def app_home():
    root = os.environ.get("MMDBRIDGE_HOME")
    return Path(root) if root else Path(os.environ.get("LOCALAPPDATA",Path.home())) / "MMDBridge"


def reserve_ue_asset_id(job, settings):
    """Reserve a readable source name in the selected project's Content folder."""
    project = Path(settings.project).resolve()
    content = project.parent / 'Content'
    destination = (content / settings.content_root.removeprefix('/Game/')).resolve()
    if not destination.is_relative_to(content.resolve()):
        raise ValueError('UE 内容目录超出项目 Content 范围')
    scene_only = bool(settings.scene and not settings.model)
    match = re.fullmatch(r'(.+)_(\d+)', job.data['id'])
    if not match:
        raise ValueError('任务名称缺少可读编号')
    stem, first = match.group(1), int(match.group(2))
    if scene_only:
        stem = re.sub(r'[^\w]+', '_', Path(settings.scene).stem).strip('_')[:100] or 'Scene'
        first = 1
    for number in range(first, 10000):
        asset_id = f'{stem}_{number:02d}'
        folder = destination / (asset_id if scene_only else 'A_' + asset_id)
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        atomic_json(folder / '.mmd_bridge_namespace.json',
                    {'asset_id': asset_id, 'job': str(job.folder)})
        return asset_id
    raise ValueError('该源文件的 UE 内容目录编号已用尽')


def connection_folder(home, project):
    project = Path(project).resolve()
    home = Path(home)
    config = project.parent / "Plugins/MMDBridge/Content/Python/bridge_config.json"
    if config.is_file():
        try:
            saved = read_json(config)
            mailbox = Path(saved["mailbox"]).resolve()
            if (Path(saved["project"]).resolve() == project and
                    mailbox.is_relative_to((home / "connections").resolve())):
                return mailbox
        except (OSError, ValueError, KeyError):
            pass
    legacy = home / "connections" / stable_id(str(project).casefold())
    if legacy.exists():
        return legacy
    stem = re.sub(r"[^A-Za-z0-9]+", "_", project.stem).strip("_")[:32] or "UnrealProject"
    for number in range(1, 10000):
        mailbox = home / "connections" / f"Connection_{stem}_{number:02d}"
        if not mailbox.exists():
            return mailbox
    raise RuntimeError(f"UE 连接目录编号已用尽: {stem}")


def ensure_blender_job_stopped(folder):
    """Do not clear a stop marker while an older exporter can still write files."""
    if os.name != 'nt':
        return
    result = process.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                          "Get-CimInstance Win32_Process -Filter \"Name = 'blender.exe'\" -ErrorAction Stop | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                         capture_output=True, text=True, errors='replace', timeout=15, creationflags=0x08000000)
    if result.returncode:
        raise ValueError('无法确认旧 Blender 导出进程是否已退出，请关闭旧导出进程后重试恢复。')
    records = json.loads(result.stdout or '[]')
    if isinstance(records, dict):
        records = [records]
    target = str(Path(folder).resolve()).casefold()
    for record in records:
        line = record.get('CommandLine')
        if line is None or target in line.casefold():
            raise ValueError(f"旧 Blender 导出进程仍在运行（PID {record['ProcessId']}）；请先停止旧任务后再恢复，避免两个进程同时写入。")


class MissingUpstreamPlugins(ValueError):
    pass


def install_upstream_plugins(settings):
    script = resource_root() / 'scripts/install_upstream_plugins.ps1'
    result = process.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                          '-File', str(script), '-Project', settings.project, '-Unreal', settings.unreal],
                         capture_output=True, text=True, errors='replace', timeout=1800,
                         creationflags=0x08000000)
    if result.returncode:
        raise ValueError('插件安装未完成：\n' + result.stdout + '\n' + result.stderr)
    return result.stdout


def check_upstream_installation(settings):
    """Early file-level check; native module loading is checked again in UE."""
    if (settings.flow in ("scene", "camera") or settings.character_backend != "mmd2unreal" or
            not any((settings.model, settings.motion, settings.lipsync, settings.camera)) or not settings.project):
        return
    roots = [Path(settings.project).parent / "Plugins"]
    if settings.unreal:
        roots.append(Path(settings.unreal).parents[2] / "Plugins")
    configured = {p["Name"]: p.get("Enabled", True) for p in read_json(settings.project).get("Plugins", [])}
    missing = []
    for name in ("MMD2Unreal", "KawaiiPhysics"):
        if configured.get(name) is False or not any(next(root.rglob(name + ".uplugin"), None) for root in roots if root.exists()):
            missing.append(name)
    if missing:
        raise MissingUpstreamPlugins("缺少或未启用 " + "、".join(missing) + "。可从 GitHub 下载插件并安装到当前 UE 工程。请先保存并关闭该工程的 UE 编辑器。")


def install_viewport_helper(project, root, unreal=None):
    """Install the editor-only reader only for the binary's exact engine build."""
    source = root/'plugins/unreal/MMDBridgeViewport'
    modules = source/'Binaries/Win64/UnrealEditor.modules'
    engine_modules = Path(unreal).with_name('UnrealEditor.modules') if unreal else None
    if (not modules.is_file() or engine_modules is None or not engine_modules.is_file() or
            read_json(modules).get('BuildId') != read_json(engine_modules).get('BuildId')):
        return False
    if not read_json(modules).get('BuildId'):
        return False
    target = Path(project).parent/'Plugins/MMDBridgeViewport'
    marker = target/'.mmd_bridge_managed'
    if target.exists() and not marker.exists():
        raise ValueError('视口接口插件目录不属于本工具，已停止安装')
    def copy_changed(src, dst):
        # The editor holds an installed DLL open. Reconnecting must not try to
        # overwrite the same binary; a changed binary still requires restart.
        if Path(dst).is_file() and file_hash(src) == file_hash(dst):
            return str(dst)
        return shutil.copy2(src, dst)
    shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('Intermediate'),
                    copy_function=copy_changed)
    marker.write_text('MMDBridge viewport 0.1.0\n',encoding='utf-8')
    return True


def install_ue_bridge(project, home=None, unreal=None, character_settings=None):
    project = Path(project).resolve()
    if project.suffix.lower() != ".uproject" or not project.is_file():
        raise ValueError("请选择有效的 .uproject")
    root = resource_root()
    folder = project.parent / "Plugins/MMDBridge"
    marker = folder/".mmd_bridge_managed"
    if folder.exists() and not marker.exists():
        raise ValueError("同名插件目录不属于本工具，已停止安装")
    folder.mkdir(parents=True,exist_ok=True)
    shutil.copytree(root/"plugins/unreal/MMDBridge",folder,dirs_exist_ok=True)
    module_folder = folder / "Content/Python/mmd_bridge"
    module_folder.mkdir(parents=True,exist_ok=True)
    for name in ("character_material_preset.py","ue_character_material_preset.py","character_custom_materials.py","ue_custom_materials.py","ue_custom_material_edit.py","__init__.py","effective_inputs.py","pmx_binary.py","pmx_reader.py","pmx_model.py","orchestration.py","core.py","ue_host.py","ue_materials.py","material_policy.py","ue_usd.py","material_slots.py","process.py",
                 "export_quality.py","ue_camera_cut_baker.py","ue_mmd2unreal.py","ue_appearance.py","scene_appearance.py","fog_occlusion.py","ue_fog_occlusion.py","ue_fog_froxel.py","native_fog_candidate.py","ue_reference.py","bake_cache.py","ue_foliage.py","ue_render_policy.py","ue_standalone_render.py","material_graph.py","material_contract.py","material_plan.py","material_workflow.py","material_results.py","material_repair.py","ue_material_repair.py","material_surface.py","ue_material_surface.py","ocio_cache.py","ue_shader_data.py","character_profiles.py","face_sdf.py","ue_character_materials.py","character_pbr.py","ue_character_pbr.py","ue_character_pbr_edit.py",
                 "character_texture_review.py","character_geometry.py","character_geometry_roles.py","blender_contour.py"):
        shutil.copy2(root/"mmd_bridge"/name,module_folder/name)
    shutil.copytree(root/'profiles', folder/'Content/Python/profiles', dirs_exist_ok=True)
    shutil.copytree(root/'resources/character_materials/Character_PBR',
                    folder/'Content/Python/resources/character_materials/Character_PBR', dirs_exist_ok=True)
    mailbox = connection_folder(home or app_home(), project)
    mailbox.mkdir(parents=True,exist_ok=True)
    atomic_json(folder/"Content/Python/bridge_config.json",{"project":str(project),"mailbox":str(mailbox.resolve())})
    info = read_json(project)
    plugins = info.setdefault("Plugins",[])
    names = ["MMDBridge","PythonScriptPlugin","EditorScriptingUtilities","SequencerScripting","USDImporter","OpenColorIO","MovieRenderPipeline"]
    if install_viewport_helper(project,root,unreal):
        names.append('MMDBridgeViewport')
    elif (project.parent/'Plugins/MMDBridgeViewport/.mmd_bridge_managed').is_file():
        # A previously installed DLL must not load after the engine changes.
        current=next((p for p in plugins if p['Name']=='MMDBridgeViewport'),None)
        if current:current['Enabled']=False
    for name in names:
        current = next((p for p in plugins if p["Name"]==name),None)
        if current:
            current["Enabled"] = True
        else:
            plugins.append({"Name":name,"Enabled":True})
    backup = project.with_suffix(".uproject.mmdb-backup")
    if not backup.exists():
        shutil.copy2(project,backup)
    atomic_json(project,info)
    ensure_render_defaults(project)
    marker.write_text("MMDBridge 0.1.0\n",encoding="utf-8")
    if character_settings and character_settings.get('character_backend') == 'mmdbridge_fbx':
        from .character_resources import install as install_character_resources
        install_character_resources(character_settings,root,project,unreal)
    return mailbox


def running_projects():
    if os.name != "nt":
        return []
    command = "Get-CimInstance Win32_Process -Filter \"Name = 'UnrealEditor.exe' OR Name = 'UnrealEditor-Cmd.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = process.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=12,creationflags=0x08000000)
    if result.returncode:
        raise OSError('无法读取 UE 工程进程，不能安全判断目标工程是否运行')
    try:
        records = json.loads(result.stdout or "[]")
        records = [records] if isinstance(records,dict) else records
        projects = []
        for record in records:
            line = record.get("CommandLine") or ""
            match = re.search(r'"([^\"]+\.uproject)"|([^\s\"]+\.uproject)',line,re.I)
            if match:
                path = next(g for g in match.groups() if g)
                if Path(path).is_file():
                    projects.append({"path":str(Path(path).resolve()),"pid":record["ProcessId"]})
        return projects
    except (ValueError,TypeError):
        return []


def bridge_status(project, home=None):
    """Return the local install state and live UE connection state."""
    if not project:
        return {"state": "unknown", "label": "未选择 UE 项目", "color": "#aab4bd"}
    project_path = Path(project).resolve()
    plugin = project_path.parent / "Plugins" / "MMDBridge"
    required = (
        plugin / ".mmd_bridge_managed",
        plugin / "Content" / "Python" / "bridge_config.json",
        plugin / "Content" / "Python" / "mmd_bridge" / "ue_host.py",
        plugin / "Content" / "Python" / "mmd_bridge" / "ue_camera_cut_baker.py",
    )
    if project_path.suffix.lower() != ".uproject" or not project_path.is_file() or not all(
        path.is_file() for path in required
    ):
        return {"state": "not_installed", "label": "未安装", "color": "#ff6b6b"}

    mailbox = connection_folder(home or app_home(), project_path)
    heartbeat = mailbox / "heartbeat.json"
    if heartbeat.exists():
        try:
            beat = read_json(heartbeat)
            same_project = (
                str(Path(beat.get("project", "")).resolve()).casefold()
                == str(project_path).casefold()
            )
            if same_project and time.time() - float(beat.get("time", 0)) < 8:
                if any(not (resource_root() / 'mmd_bridge' / name).is_file() or
                       file_hash(resource_root() / 'mmd_bridge' / name) != digest
                       for name, digest in ((n,beat.get('code_hashes',{}).get(n)) for n in UE_CODE_MODULES)):
                    return {'state':'needs_restart', 'label':'桥接已连接但运行旧代码；更新插件后重启 UE', 'color':'#ffd166'}
                return {"state": "connected", "label": "已安装并连接", "color": "#71e39b"}
        except (OSError, ValueError, TypeError):
            pass
    return {"state": "installed_offline", "label": "已安装但未重启或未启用", "color": "#ffd166"}


class Controller:
    def __init__(self,home=None):
        self.home = home or app_home()
        self.home.mkdir(parents=True,exist_ok=True)
        self.job = None
        self.processes = []
        self.scene_started = False
        self.ue_started = False
        self.ue_requested = False
        self.ue_vram_next_check = 0.0
        self.ue_process_next_check = 0.0
        self.ue_restart_not_before = 0.0
        self.ue_restart_attempts = 0
        self.log_handles = []
        self.mailbox = None
        self.stopped = False
        # 独立的CameraCutAuto会话，不复用主自动化 Job，避免把它接入模型/动作导出流程。
        self.camera_cut_session = None
        self.camera_cut_project = None
        self.orchestrator = None
        self.defer_ue = False
        self.shared_bake_end = 0
        self.camera_cut_pending_mode = None
        self.camera_cut_requested = None
        self.bake_thread = None
        self.geometry_thread = None
        self.character_started = False
        self.reference_request_id = None
        self.standalone_render = None
        self.material_repair = None
        self.plugins_restart_required = False
        self.active_character_job = None

    def load_scene_results(self, folder):
        self.orchestrator = None
        if self.processes or self.reference_busy():
            raise ValueError('请等待当前任务结束或停止，再选择场景任务')
        job = Job(folder)
        scene = read_json(job.folder / 'manifest.json').get('scene', {})
        frozen = ((job.folder/'scene_checkpoint.json').is_file() and
                  job.data['settings'].get('scene_format')=='usd_stage' and bool(job.data['settings'].get('scene')))
        if scene.get('route') != 'usd_stage' and not frozen:
            raise ValueError('请选择已导出或具有冻结断点的 USD 场景任务')
        self.job = job
        self.material_repair = job.data.get('material_repair')
        self.scene_started = self.ue_started = self.ue_requested = True
        # Loading results must never silently resume a paused repair.
        self.stopped = bool(self.material_repair) or scene.get('route')!='usd_stage'
        return job

    def reference_busy(self):
        if getattr(self, 'custom_edit_busy', False):
            return True
        if self.orchestrator and self.orchestrator.child and (
                not self.stopped or self.orchestrator.busy()):
            return True
        if self.material_repair and not self.stopped:
            return True
        if self.standalone_render and self.standalone_render.get('active'):
            return True
        if not self.job:
            return False
        if self.reference_request_id and self.mailbox:
            receipt = self.mailbox / 'render_response.json'
            response = read_json(receipt) if receipt.exists() else {}
            if response.get('id') != self.reference_request_id or response.get('state') not in ('needs_review', 'failed', 'stopped'):
                return True
        path = self.job.folder / 'status.json'
        status = read_json(path) if path.exists() else {}
        return status.get('state') in ('reference_render', 'ue_render') or (
            status.get('state') == 'ue_wait_vram' and status.get('operation') == 'reference')

    def render_background(self, preset, output_base='', frame=None, pipeline='lumen', calibration=False, resume=None):
        """Render the selected saved preset in one -game process, without import."""
        from .standalone_render import install_runtime, make_plan, resume_plan, resolve_preset
        if not self.job or self.processes or self.reference_busy():
            raise ValueError('请先选择已导入任务，并等待当前操作完成')
        cfg = self.job.data['settings']
        running = self._same_project_running(cfg)
        if running is not False:
            raise ValueError('请先保存并关闭该 UE 工程，再开始后台渲染；工具不会自动关闭编辑器。'
                             if running else '无法确认 UE 进程状态，请关闭该工程后重试')
        from .ue_render_policy import standalone_command
        report = read_json(self.job.folder / 'ue_report.json')
        # Validate executable/project before updating the managed runtime.
        preset = resolve_preset(report, preset, pipeline)
        standalone_command(cfg, report, preset, allow_custom_preset=True)
        install_runtime(cfg['project'])
        if resume is not None and cfg.get('render_vram_guard_enabled',False):
            memory=dict(process.gpu_memory_info() or {})
            physical=process.physical_memory_info()
            if physical:memory.update(system_total_mb=physical['total_mb'],system_used_mb=physical['total_mb']-physical['available_mb'])
            percent=process.gpu_budget_percent(memory)
            if percent is None or percent>=int(cfg.get('ue_vram_resume_percent',80)):
                raise ValueError('需要读取共享显存占用，且低于恢复阈值（默认 80%）')
            run,request,command=resume_plan(self.job,resume)
        elif resume is not None:
            run,request,command=resume_plan(self.job,resume)
        else:
            run, request, command = make_plan(self.job, preset, output_base, frame, pipeline)
        if calibration:
            request['calibration']={'scope':'scene','basis':'native_gpu_csv','isolated_process':True}
            atomic_json(run/'request.json',request)
        env = dict(os.environ, MMDBRIDGE_STANDALONE_REQUEST=str(run / 'request.json'))
        log = (run / 'launcher.log').open('w', encoding='utf-8')
        try:
            child = process.popen(command, env=env, cwd=str(Path(cfg['unreal']).parent),
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=0x08000000 if os.name == 'nt' else 0)
        except Exception:
            log.close()
            raise
        self.standalone_render = {'active': True, 'run': run, 'process': child, 'log': log,
                                 'request': request, 'started': time.time(), 'gpu_check_at': 0.}
        self.processes.append(('standalone_render', child))
        self.stopped = False
        atomic_json(run / 'plan.json', {'command': command, 'request': request,
            'started': True, 'pid': child.pid, 'preset_modified': False})
        return run

    def _poll_background(self):
        from .standalone_render import finish_result
        session = self.standalone_render
        run, child = session['run'], session['process']
        code = child.poll()
        runtime = read_json(run / 'runtime.json') if (run / 'runtime.json').is_file() else {}
        if code is None and time.monotonic() >= session.get('gpu_check_at', 0):
            # Also watch from outside UE: USD/texture loading can block its tick
            # while allocating enough GPU memory to fail before MRQ can cancel.
            session['gpu_check_at'] = time.monotonic() + 2.
            memory = dict(process.gpu_memory_info() or {})
            physical=process.physical_memory_info()
            if physical:memory.update(system_total_mb=physical['total_mb'],system_used_mb=physical['total_mb']-physical['available_mb'])
            # DXGI values read in the launcher belong to the launcher. Only the
            # worker's receipt can provide this rendering process's local budget.
            if runtime.get('gpu',{}).get('dxgi_pid')==child.pid:
                memory.update({k:v for k,v in runtime['gpu'].items() if k.startswith(('local_','nonlocal_','dxgi_'))})
            percent = process.gpu_budget_percent(memory)
            if session['request'].get('render_vram_guard_enabled',False) and percent is not None and percent >= session['request'].get('gpu_abort_percent', 90):
                process.terminate_pid(child.pid, expected_executable=Path(self.job.data['settings']['unreal']).with_name('UnrealEditor-Cmd.exe'))
                child.wait(timeout=10)
                code = child.poll()
                runtime.update(success=False, state='failed', gpu_budget_exceeded=True,
                               gpu=memory, gpu_budget_percent=percent, stopped_by='external_watchdog')
                atomic_json(run / 'runtime.json', runtime)
        if code is None:
            progress = min(1., max(0., float(runtime.get('progress', 0.))))
            return {'state': 'background_render', 'progress': progress,
                    'message': ('后台 UE 正在渲染：' + format(progress * 100, '.1f') + '%'
                                if runtime.get('state') == 'rendering' else '后台 UE 正在启动或加载关卡…'),
                    'output': session['request']['output']}
        session['active'] = False
        session['log'].close()
        self.processes = [(mode, process) for mode, process in self.processes if process is not child]
        result = finish_result(run, code)
        session['result'] = result
        message = ('后台渲染完成，生成 ' + str(result['output_files']) + ' 个文件；画面需复核'
                   if result['complete'] else
                   '后台渲染未完成，请查看本次日志和结果回执')
        if result.get('runtime', {}).get('gpu_budget_exceeded'):
            message = '共享显存达到阈值，后台渲染已退出；完成帧保留，低于恢复阈值后可继续'
        return {'state': 'needs_review' if result['complete'] else 'failed', 'message': message,
                'output': result['output'], 'report': str(run / 'result.json')}

    def stop_background(self):
        session = self.standalone_render
        if not session or not session.get('active'):
            return
        child = session['process']
        if child.poll() is None:
            process.terminate_pid(child.pid, expected_executable=Path(self.job.data['settings']['unreal']).with_name('UnrealEditor-Cmd.exe'))
            child.wait(timeout=10)
        session['active'] = False
        session['log'].close()
        self.processes = [(mode, process) for mode, process in self.processes if process is not child]
        atomic_json(session['run'] / 'result.json', dict(session['request'], complete=False,
            state='stopped', message='用户停止后台渲染；已写出的文件保留', preset_modified=False))

    def render_reference(self, kind, pipeline=None, memory_profile=None):
        if kind not in ('blender', 'ue'):
            raise ValueError('未知参考帧类型')
        if pipeline is not None and pipeline not in ('lumen', 'path_tracing'):
            raise ValueError('未知场景渲染管线')
        if memory_profile is not None and memory_profile not in ('balanced', 'low', 'quality', 'compatible'):
            raise ValueError('未知渲染显存策略')
        if not self.job or self.reference_busy() or self.processes:
            raise ValueError('请先选择已转换的场景任务，并等待当前操作完成')
        scene = read_json(self.job.folder / 'manifest.json').get('scene', {})
        if scene.get('route') != 'usd_stage' or not scene.get('reference_camera'):
            raise ValueError('任务缺少 USD 场景或活动参考相机，请重新导出')
        data = self.job.data
        cfg = data['settings']
        if memory_profile is not None:
            cfg['ue_render_memory_profile'] = memory_profile
            atomic_json(self.job.folder / 'job.json', data)
        if kind == 'blender':
            if not Path(cfg.get('blender', '')).is_file() or not Path(cfg.get('scene', '')).is_file():
                raise ValueError('源 Blender 软件或场景文件不存在')
            (self.job.folder / 'stop').unlink(missing_ok=True)
            self.stopped = False
            self.job.status('reference_render', '启动 Blender 源参考帧渲染')
            self.launch_blender('reference')
            return
        report = read_json(self.job.folder / 'ue_report.json')
        if not all(report.get('assets', {}).get(key) for key in ('level', 'sequence', 'render_preset')):
            raise ValueError('UE 关卡、序列或源色彩渲染配置缺失，请先完成场景导入')
        if not Path(cfg.get('project', '')).is_file() or not Path(cfg.get('unreal', '')).is_file():
            raise ValueError('任务的 UE 软件或工程不存在')
        ensure_render_defaults(cfg['project'])
        running = self._same_project_running(cfg)
        if running is None:
            raise ValueError('无法确认 UE 工程状态，请关闭该工程后重试')
        if running:
            deployed = Path(cfg['project']).parent / 'Plugins/MMDBridge/Content/Python/mmd_bridge'
            if any(not (deployed / name).is_file() or file_hash(deployed / name) != file_hash(resource_root() / 'mmd_bridge' / name)
                   for name in UE_CODE_MODULES):
                raise ValueError('UE 正在运行旧桥接，请先保存工程、安装 UE 桥接插件并重启编辑器')
            heartbeat = connection_folder(self.home, cfg['project']) / 'heartbeat.json'
            beat = read_json(heartbeat) if heartbeat.is_file() else {}
            if any(beat.get('code_hashes', {}).get(name) != file_hash(resource_root() / 'mmd_bridge' / name)
                   for name in UE_CODE_MODULES):
                raise ValueError('UE 尚未加载当前参考帧桥接，请等待连接就绪；更新插件后需重启 UE')
        else:
            install_ue_bridge(cfg['project'], self.home, cfg.get('unreal'))
        self.mailbox = connection_folder(self.home, cfg['project'])
        self.mailbox.mkdir(parents=True, exist_ok=True)
        (self.job.folder / 'stop').unlink(missing_ok=True)
        self.stopped = False
        self.ue_started = self.ue_requested = True
        self.job.status('ue_render', '等待 UE 桥接，准备渲染源活动相机参考帧')
        self.reference_request_id = uuid.uuid4().hex
        atomic_json(self.mailbox / 'render_request.json', {'id': self.reference_request_id, 'job': str(self.job.folder),
                    'pipeline': pipeline or cfg.get('scene_render_pipeline', 'path_tracing')})
        if not running:
            process.popen([cfg['unreal'], cfg['project']], cwd=str(Path(cfg['unreal']).parent),
                          creationflags=0x08000000 if os.name == 'nt' else 0)

    def inspect_selected_materials(self, preferred_project=None):
        live = [read_json(p) for p in (self.home/'connections').glob('*/heartbeat.json')]
        live = [b for b in live if time.time()-b.get('time', 0) < 8 and process.pid_exists(b.get('pid', -1))]
        project = live[0]['project'] if len(live) == 1 else preferred_project or None
        return self.material_surface_command('inspect', project=project)

    def material_surface_command(self, action, project=None, job_folder=None, **payload):
        if self.processes or self.reference_busy(): raise ValueError('请先停止当前转换或渲染操作')
        if project is None and job_folder is not None: project = Job(job_folder).data['settings']['project']
        if project is None and action != 'inspect' and self.job: project = self.job.data['settings']['project']
        if project is None:
            live = [read_json(p) for p in (self.home/'connections').glob('*/heartbeat.json')]
            live = [b for b in live if time.time()-b.get('time', 0) < 8 and process.pid_exists(b.get('pid', -1))]
            if len(live) != 1: raise ValueError('请打开一个已连接的 UE 工程；多个工程运行时请在工具中选择目标工程')
            project = live[0]['project']
        box = connection_folder(self.home, project)
        beat_path = box/'heartbeat.json'
        beat = read_json(beat_path) if beat_path.is_file() else {}
        if time.time()-beat.get('time', 0) > 8 or not process.pid_exists(beat.get('pid', -1)):
            raise ValueError('请先打开对应 UE 工程，并等待桥接连接')
        if str(Path(beat.get('project', '')).resolve()).casefold() != str(Path(project).resolve()).casefold():
            raise ValueError('UE 桥接连接不属于当前任务工程')
        if any(beat.get('code_hashes', {}).get(name) != file_hash(resource_root()/'mmd_bridge'/name)
               for name in UE_CODE_MODULES):
            raise ValueError('UE 仍运行旧桥接代码，请保存并重启 UE 后使用材质切换')
        request_path = box/'material_surface_request.json'
        receipt_path = box/'material_surface_response.json'
        previous = read_json(request_path) if request_path.is_file() else {}
        receipt = read_json(receipt_path) if receipt_path.is_file() else {}
        if (previous and (receipt.get('id') != previous['id'] or receipt.get('state') == 'running') and
                previous.get('ue_pid', beat['pid']) == beat['pid']):
            raise ValueError('上一次材质切换仍在处理，请等待回执')
        identity = uuid.uuid4().hex
        source = str(job_folder) if job_folder is not None else str(self.job.folder) if self.job else ''
        atomic_json(request_path, dict(payload, id=identity, job=source, action=action, ue_pid=beat['pid']))
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            result = read_json(receipt_path) if receipt_path.is_file() else {}
            if result.get('id') == identity and result.get('state') in ('complete','failed'):
                if result['state'] == 'failed': raise ValueError(result.get('error', '材质切换失败'))
                return result['result']
            time.sleep(.2)
        raise ValueError('UE 材质操作尚未返回；请查看桥接回执，勿重复提交')

    def apply_custom_materials(self, settings, sources):
        if not sources:
            raise ValueError('未选择自选材质槽')
        model = sources[0]['model']
        digest = sources[0]['model_sha256']
        if any(row['model_sha256'] != digest for row in sources):
            raise ValueError('一次只能应用同一 PMX 的材质')
        payload = {'model': model, 'model_sha256': digest,
                   'pmx_indices': [row['pmx_index'] for row in sources], 'settings': settings}
        self.material_surface_command('custom_targets', project=settings['project'], **payload)
        folder = self.home/'custom_material_edits'/uuid.uuid4().hex
        folder.mkdir(parents=True)
        request = folder/'request.json'
        atomic_json(request, {'settings': settings, 'sources': sources})
        executable = settings.get('character_blender') or settings.get('blender', '')
        if not Path(executable).is_file():
            raise ValueError('请先选择用于贴图副本处理的 Blender')
        self.custom_edit_busy = True
        self.custom_edit_cancelled = False
        try:
            with (folder/'blender.log').open('w', encoding='utf8') as log:
                self.custom_texture_process = process.popen([executable, '--background', '--factory-startup',
                    '--disable-autoexec', '--python-exit-code', '1', '--python',
                    str(resource_root()/'scripts/prepare_custom_materials.py'), '--', str(request)],
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=0x08000000 if os.name == 'nt' else 0)
                code = self.custom_texture_process.wait(timeout=300)
            if self.custom_edit_cancelled:
                raise ValueError('用户已停止自选材质准备，UE 绑定未改变')
            if code or not (folder/'prepared.json').is_file():
                raise ValueError('自选材质贴图准备失败，原绑定保留；日志: ' + str(folder/'blender.log'))
        finally:
            child = getattr(self, 'custom_texture_process', None)
            if child is not None and child.poll() is None:
                process.terminate_pid(child.pid, expected_executable=executable)
            self.custom_texture_process = None
            self.custom_edit_busy = False
        prepared = read_json(folder/'prepared.json')
        return self.material_surface_command('custom_apply', project=settings['project'], records=prepared['records'], **payload)

    def switch_selected_materials(self, targets, mode):
        matched = {row['job'] for row in targets if row.get('job')}
        if len(matched) > 1: raise ValueError('请一次勾选同一转换场景中的材质槽')
        job = Job(next(iter(matched))) if matched else self.job
        if job is None: raise ValueError('没有可靠的转换记录，不能重建表面逻辑')
        result = self.material_surface_command('build', job_folder=job.folder, targets=targets, mode=mode)
        folder = Path(result['folder'])
        cfg = job.data['settings']
        atomic_json(self.home/'material_surface_last.json', {'job': str(job.folder), 'folder': str(folder), 'project': cfg['project']})
        if all(row.get('binding') == 'native' for row in targets):
            return self.material_surface_command('commit', job_folder=job.folder, folder=str(folder))
        log_path = folder/'blender_layer.log'
        with log_path.open('w', encoding='utf-8') as log:
            completed = process.run([cfg['blender'], '--background', '--factory-startup', '--disable-autoexec',
                '--python-exit-code', '1', '--python', str(resource_root()/'scripts/blender_surface_layer.py'),
                '--', str(folder)], stdout=log, stderr=subprocess.STDOUT, timeout=120,
                creationflags=0x08000000 if os.name == 'nt' else 0)
        if completed.returncode or not (folder/'layer.json').is_file():
            raise ValueError('独立材质绑定层生成失败，原绑定保留；日志：'+str(log_path))
        return self.material_surface_command('commit', job_folder=job.folder, folder=str(folder))

    def undo_selected_materials(self):
        last_path = self.home/'material_surface_last.json'
        job = Job(read_json(last_path)['job']) if last_path.is_file() else self.job
        folders = sorted((job.folder/'MaterialSurfaces').glob('S_*'), reverse=True) if job else []
        latest = next((p for p in folders if
                       (p/'journal.json').is_file() and read_json(p/'journal.json').get('state') == 'applying' or
                       (p/'receipt.json').is_file() and read_json(p/'receipt.json').get('committed') and
                       not read_json(p/'receipt.json').get('undone')), None)
        if latest is None: raise ValueError('没有可撤销的材质表面切换')
        return self.material_surface_command('undo', job_folder=job.folder, folder=str(latest))

    def launch_blender(self,mode, job=None):
        job = job or self.job
        cfg = job.data["settings"]
        executable = (cfg.get('character_blender') or cfg['blender']) if mode == 'character' else cfg['blender']
        args = [executable,"--factory-startup"]
        if mode in ("scene", "scene_import", "camera", "reference", "material_repair") or cfg["unattended"] or cfg.get("character_backend") == "mmdbridge_fbx":
            args += ["--background"]
        if mode == "character" and cfg["working_blend"] and cfg.get("character_backend") != "mmdbridge_fbx":
            args += ["--disable-autoexec",cfg["working_blend"]]
        args += ["--python-exit-code","1","--python",str(resource_root()/"scripts/blender_entry.py"),"--",str(job.folder),mode]
        log = (job.folder/f"blender_{mode}.log").open("w",encoding="utf-8")
        self.log_handles.append(log)
        child = process.popen(args,stdout=log,stderr=subprocess.STDOUT,cwd=resource_root(),
            creationflags=0x08000000 if os.name=="nt" else 0)
        self.processes.append((mode,child))

    def start(self,settings):
        from .orchestration import CompositeRun, create_parent, enabled
        if enabled(settings):
            if self.reference_busy() or any(child.poll() is None for _, child in self.processes):
                raise ValueError('请等待当前任务结束或停止，再开始组合任务')
            self.job = create_parent(settings)
            if settings.project and Path(settings.project).is_file():
                data = self.job.data
                data['asset_id'] = reserve_ue_asset_id(self.job, settings)
                atomic_json(self.job.path, data)
            self.stopped = False
            self.scene_started = self.ue_started = self.ue_requested = False
            self.mailbox = None
            self.processes = []
            self.orchestrator = CompositeRun(self)
            return self.job
        self.orchestrator = None
        return self._start_single(settings)
    def _start_single(self,settings):
        if self.reference_busy():
            raise ValueError('参考帧仍在渲染或停止中，请等待 UE 回执再开始新任务')
        if settings.content_root == '/Game/On_9_24_开发_PlanV2/On_9_25/On_9_26':
            # Loading an old GUI configuration must not carry the obsolete
            # dated default into a new import. Resume keeps existing namespaces.
            settings = replace(settings, content_root=Settings().content_root)
        check_upstream_installation(settings)
        settings.validate()
        transfer = task_transfer_folder(settings)
        settings = replace(settings, character_output_dir=str(transfer))
        root = transfer / "MMDBridge_Jobs"
        self.job = Job.create(root,settings)
        data = self.job.data
        data["geometry_input_identity_version"] = getattr(self, "geometry_input_identity_version", 1)
        if hasattr(self, 'custom_material_binding_version'):
            data['custom_material_binding_version'] = self.custom_material_binding_version
        if data.get('custom_material_binding_version') == 1 and settings.model:
            from .character_custom_materials import inspect_model, plan
            for source in inspect_model(settings.model)['materials']:
                plan(data['settings'], source)
        # Reserve names across transfer roots when the target project is known.
        # Export-only scene tasks persist the source name and local task number.
        if self.shared_bake_end:
            data['shared_bake_end'] = int(self.shared_bake_end)
        data['asset_id'] = (reserve_ue_asset_id(self.job, settings) if settings.project else
                            data['id'] + '_' + stable_id(str(self.job.folder).casefold())[:8])
        if settings.scene and not settings.model:
            source_name = re.sub(r'[^\w]+', '_', Path(settings.scene).stem).strip('_')[:100] or 'Scene'
            data['asset_folder'] = (data['asset_id'] if settings.project else
                                    source_name + '_' + data['id'].rsplit('_', 1)[1])
        atomic_json(self.job.path, data)
        self.stopped = False
        self.scene_started = not bool(settings.scene)
        self.ue_started = self.ue_requested = False
        self.ue_vram_next_check = self.ue_process_next_check = 0.0
        self.ue_restart_not_before = 0.0
        self.ue_restart_attempts = 0
        self.mailbox = None
        self.processes = []
        self.character_started = False
        self.active_character_job = None
        self.bake_thread = None
        if settings.flow == "camera":
            self.launch_blender("camera")
            return self.job
        if settings.flow == "scene":
            self.launch_blender("scene")
            self.scene_started = True
            return self.job
        if settings.character_backend == "mmd2unreal":
            data = self.job.data
            end = settings.end or max((v["max_frame"] * settings.fps // 30 + getattr(settings, key + "_offset", 0)
                                       for key, v in data["vmds"].items()), default=settings.fps * 10)
            end = max(settings.start, end)
            atomic_json(self.job.folder / "manifest.json", {
                "schema": 2, "job_id": data["id"], "asset_id": data["asset_id"],
                "character_backend": "mmd2unreal", "fps": settings.fps,
                "frame_range": [settings.start, end], "files": {}, "materials": [], "warnings": []})
            if settings.scene:
                self.launch_blender("scene")
                self.scene_started = True
            else:
                self.job.status("scene_done", "等待 UE：将弹出 MMD2Unreal 官方导入窗口")
            return self.job
        if settings.character_backend == "mmdbridge_fbx":
            from .character_geometry_review import requires_preflight
            if requires_preflight(self.job.data['settings']):
                self._begin_geometry_review()
                return self.job
            self.bake_thread = threading.Thread(target=run_bake, args=(self.job,), daemon=True)
            self.bake_thread.start()
            return self.job
        self.launch_blender("character" if settings.model else "scene")
        if not settings.model:
            self.scene_started = True
        return self.job

    def _geometry_review_current(self):
        from .character_geometry_review import configuration_signature
        path = self.job.folder/'geometry_review_confirmed.json'
        return path.is_file() and read_json(path).get('signature') == configuration_signature(self.job.data['settings'])

    def _begin_geometry_review(self):
        from .character_geometry_review import build_review
        if self.geometry_thread and self.geometry_thread.is_alive():
            raise ValueError('上一任务的几何预检尚未退出，请稍后恢复')
        job = self.job
        job.status('geometry_preflight', '正在预检局部一级细分部件；尚未启动外部工具')
        def analyze():
            try:
                report = build_review(job.data['settings'])
                if self.stopped or self.job is not job or (job.folder/'stop').exists(): return
                atomic_json(job.folder/'geometry_review.json', report)
                for warning in report['warnings']:
                    if (job.folder/'stop').exists(): return
                    job.status('geometry_warning', '警告：'+warning, severity='warning')
                if self.stopped or (job.folder/'stop').exists(): return
                if report['pending']:
                    job.status('awaiting_geometry_selection', '嘴唇、脖子或面部未单独识别，请确认局部细分范围后继续', severity='warning')
                else:
                    atomic_json(job.folder/'geometry_review_confirmed.json', {'signature':report['signature'],'automatic':True})
                    job.status('geometry_review_ready', '局部细分预检完成，准备继续')
            except Exception as exc:
                if not self.stopped and self.job is job:
                    job.status('failed', '局部细分预检失败：'+str(exc))
        self.geometry_thread = threading.Thread(target=analyze, daemon=True)
        self.geometry_thread.start()

    def confirm_geometry_selection(self, selections):
        if self.orchestrator and self.orchestrator.child:
            return self.orchestrator.child.confirm_geometry_selection(selections)
        from .character_geometry_review import apply_selection, configuration_signature
        if not self.job or read_json(self.job.folder/'status.json').get('state') != 'awaiting_geometry_selection':
            raise ValueError('当前任务不在几何补选检查点')
        report = read_json(self.job.folder/'geometry_review.json')
        data = self.job.data
        if report['signature'] != configuration_signature(data['settings']):
            self.job.status('failed', '几何预检输入已改变，请新建任务；原预检报告保留')
            raise ValueError('预检输入已改变，请新建任务重新分析')
        data['settings'] = apply_selection(data['settings'], report, selections)
        atomic_json(self.job.path, data)
        atomic_json(self.job.folder/'geometry_review_confirmed.json', {
            'signature':configuration_signature(data['settings']), 'automatic':False, 'selections':selections})
        self.job.status('geometry_review_ready', '部件选择已保存，自动继续导入')
        return data['settings']['character_geometry_overrides']

    def cancel_geometry_selection(self):
        if self.orchestrator and self.orchestrator.child:
            return self.orchestrator.child.cancel_geometry_selection()
        self.stopped = True
        (self.job.folder/'stop').touch()
        self.job.status('cancelled', '用户取消了局部细分补选；未启动外部工具')

    def reconvert_scene_materials(self, settings):
        """Archive the last result, retaining the source and frozen geometry."""
        if self.job is None or self.processes or self.reference_busy():
            raise ValueError('请先选择已停止的场景任务')
        folder = self.job.folder
        cfg = dict(self.job.data['settings'])
        if not cfg.get('scene') or cfg.get('scene_format') != 'usd_stage':
            raise ValueError('材质重做仅适用于 USD 场景任务')
        ensure_blender_job_stopped(folder)
        archive = folder / ('PreviousMaterials_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
        archive.mkdir()
        for name in ('job.json', 'manifest.json', 'ue_report.json', 'ue_checkpoint.json', 'status.json', 'scene_checkpoint.json'):
            if (folder/name).is_file(): shutil.copy2(folder/name, archive/name)
        if (folder/'visual').is_dir(): shutil.copytree(folder/'visual', archive/'visual')
        for key in ('material_strategy', 'material_workflow', 'material_bake_fallback', 'material_complex_strategy', 'material_conversion_timeout', 'bake_resolution'):
            cfg[key] = getattr(settings, key)
        data = dict(self.job.data); data['settings'] = cfg
        if cfg.get('material_bake_fallback') and cfg.get('material_complex_strategy')=='pbr_bake' and cfg.get('material_strategy')!='nodes':
            data['material_bake_retry'] = dict(data.get('material_bake_retry',{}), **self._material_retry_sources())
        atomic_json(self.job.path, data)
        manifest_path = folder/'manifest.json'
        if manifest_path.is_file():
            manifest = read_json(manifest_path); manifest.pop('scene', None)
            atomic_json(manifest_path, manifest)
        (folder/'ue_checkpoint.json').unlink(missing_ok=True)
        self.resume(folder)
        self.job.status('character_done', '已归档旧材质和图片：复用冻结几何，按当前策略重做材质')
        return archive

    def _material_retry_sources(self):
        from .material_policy import basic_pbr_retry_sources
        report_path = self.job.folder/'ue_report.json'
        if not report_path.is_file():return {}
        report = read_json(report_path)
        hashes=report.get('runtime_code_hashes',{})
        if any(hashes.get(name)!=file_hash(resource_root()/'mmd_bridge'/name) for name in UE_CODE_MODULES):
            # Renderer-version-specific compiler failures are stale; observed
            # transmission failures remain valid only for the identical source.
            source_hash=self.job.data.get('inputs',{}).get('scene')
            findings={identity:item for identity,item in report.get('checks',{}).get('material_visual_findings',{}).items()
                if source_hash and item.get('source_sha256')==source_hash}
            if not findings:return {}
            if file_hash(Path(self.job.data['settings']['scene']))!=source_hash:return {}
            report=dict(report,failures=[],material_routes={},checks={'material_visual_findings':findings})
        records = report.get('material_source_records',[])
        manifest_path=self.job.folder/'manifest.json'
        if records and manifest_path.is_file():
            manifest_data=read_json(manifest_path)
            source_records={r['id']:r for r in manifest_data.get('materials',[])+manifest_data.get('scene',{}).get('materials',[])}
            records=[dict(source_records.get(r['id'],{}),**r) for r in records]
        if not records:
            manifest = self.job.folder/'manifest.json'
            candidates = [manifest] + [p/'manifest.json' for p in sorted(self.job.folder.glob('PreviousMaterials_*'),reverse=True)]
            for path in candidates:
                if not path.is_file():continue
                if path!=manifest:
                    old_report=path.parent/'ue_report.json'
                    if not old_report.is_file() or file_hash(old_report)!=file_hash(report_path):continue
                data=read_json(path)
                records=data.get('materials',[])+data.get('scene',{}).get('materials',[])
                if records:break
        return basic_pbr_retry_sources(records,report,self.job.data['settings']['scene'])

    def _poll_material_bake_retry(self, state, cfg):
        if cfg.get('material_workflow') == 'legacy_main_v1': return False
        if (state not in ('complete','needs_review') or self.processes or self.reference_busy() or
                not cfg.get('scene') or cfg.get('scene_format')!='usd_stage' or
                not cfg.get('material_bake_fallback') or cfg.get('material_complex_strategy')!='pbr_bake' or
                cfg.get('material_strategy')=='nodes' or not self.mailbox):return False
        request=self.mailbox/'request.json';response=self.mailbox/'response.json'
        if not request.is_file() or not response.is_file():return False
        sent,receipt=read_json(request),read_json(response)
        if (sent.get('job')!=str(self.job.folder) or sent.get('id')!=receipt.get('id') or
                receipt.get('state') not in ('complete','needs_review')):return False
        pending=set(self._material_retry_sources())-set(self.job.data.get('material_bake_retry',{}))
        if not pending:return False
        from .core import Settings
        self.reconvert_scene_materials(Settings(**cfg))
        return True

    def repair_white_materials(self, selected=None):
        if not self.job or self.processes or self.reference_busy():
            raise ValueError('请先选择已停止的场景任务')
        from .material_repair import prepare
        folder = prepare(self.job, selected)
        self._start_material_repair(folder.name, 'repair')
        return folder

    def undo_material_repair(self, batch):
        if not self.job or self.processes or self.reference_busy(): raise ValueError('请先停止当前操作')
        folder = self.job.folder/'MaterialRepairs'/batch
        receipt = read_json(folder/'receipt.json')
        if not receipt.get('committed') or receipt.get('undone'): raise ValueError('没有可撤销的已提交补修')
        self._start_material_repair(batch, 'undo')

    def _start_material_repair(self, batch, action):
        self.material_repair = {'batch':batch,'action':action}
        data = self.job.data; data['material_repair'] = self.material_repair
        atomic_json(self.job.path,data)
        self.stopped = False; self.ue_started = self.ue_requested = False
        (self.job.folder/'stop').unlink(missing_ok=True)
        folder = self.job.folder/'MaterialRepairs'/batch
        exported=read_json(folder/'export.json') if (folder/'export.json').is_file() else {}
        if action == 'repair' and not exported.get('complete'):
            self.job.status('material_repair_export','仅导出白模材质候选（实验性）')
            self.launch_blender('material_repair')
        else:
            self.job.status('material_repair_ready','等待连接 UE，只更新原场景白模绑定')

    def _poll_material_repair(self):
        path = self.job.folder/'status.json'; status = read_json(path)
        if self.stopped: return status
        active=[]
        for mode,child in self.processes:
            code=child.poll()
            if code is None: active.append((mode,child))
            elif code != 0: self.job.status('failed','白模补修导出失败，原场景保持原绑定；请查看日志')
        self.processes=active; status=read_json(path)
        if status['state'] in ('complete','needs_review','failed','stopped') and not active:
            self.material_repair=None
            if status['state'] in ('complete','needs_review'):
                data=self.job.data;data.pop('material_repair',None);atomic_json(self.job.path,data)
            return status
        if not active:
            if self.ue_started and not self._heartbeat_fresh(self.job.data['settings']) and self._bridge_process_running(self.job.data['settings']) is False:
                self.job.status('failed', '补修编辑器已退出；还原点保留，请恢复本批补修')
                self.material_repair = None
                return read_json(path)
            if not self.ue_started: self.launch_ue()
            if self._heartbeat_fresh(self.job.data['settings']) and not self.ue_requested:
                self._send_ue_request()
        return read_json(path)

    def resume(self,folder):
        if self.orchestrator and self.orchestrator.child and (
                not self.stopped or self.orchestrator.busy()):
            raise ValueError('请先停止当前组合任务，并等待模块进程退出后再恢复')
        resumed_parent = Job(folder)
        if resumed_parent.data.get('orchestration_version'):
            from .orchestration import CompositeRun
            self.job = resumed_parent
            self.stopped = False
            self.scene_started = self.ue_started = self.ue_requested = False
            self.mailbox = None
            self.processes = []
            self.orchestrator = CompositeRun(self, resume=True)
            return
        self.orchestrator = None
        if self.bake_thread and self.bake_thread.is_alive():
            raise ValueError('上一任务的 MMD 停止处理尚未退出，请稍后恢复')
        resumed = Job(folder)
        resumed.verify_inputs()
        settings = resumed.data['settings']
        mode1 = settings.get('character_backend') == 'mmdbridge_fbx'
        manifest_path = resumed.folder/'manifest.json'
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        if (resumed.folder/'character_checkpoint.json').is_file():
            from .character_texture_review import check_checkpoint
            check_checkpoint(settings, manifest)
        geometry_checkpoint = resumed.folder/'character_checkpoint.json'
        if geometry_checkpoint.is_file():
            from .character_geometry import check_geometry_checkpoint
            saved_geometry = read_json(geometry_checkpoint)
            if settings.get('character_geometry_mode') is not None or saved_geometry.get('geometry_policy') is not None:
                check_geometry_checkpoint(saved_geometry, settings, manifest.get('character_geometry'), resumed.folder)
        if settings.get('scene') or (settings.get('model') and settings.get('character_backend') != 'mmd2unreal') or settings.get('flow') == 'camera':
            ensure_blender_job_stopped(folder)
        self.job = resumed
        if resumed.data.get('material_repair'):
            repair=resumed.data['material_repair']
            self._start_material_repair(repair['batch'],repair['action'])
            return
        (self.job.folder/"stop").unlink(missing_ok=True)
        self.stopped = False
        self.ue_started = self.ue_requested = False
        self.ue_vram_next_check = self.ue_process_next_check = 0.0
        self.ue_restart_not_before = 0.0
        self.ue_restart_attempts = 0
        self.processes = []
        self.bake_thread = None
        self.character_started = False
        cfg = self.job.data["settings"]
        self.active_character_job = None
        from .character_geometry_review import requires_preflight
        if requires_preflight(cfg) and not self._geometry_review_current():
            self.scene_started = not bool(cfg.get('scene'))
            self._begin_geometry_review()
            return
        if cfg.get('additional_characters'):
            children = [Job(path) for path in self.job.data.get('character_jobs', [])]
            for child in children:
                if not child.folder.is_relative_to(self.job.folder / 'characters'):
                    raise ValueError('角色任务目录超出本任务')
                ensure_blender_job_stopped(child.folder)
                child.verify_inputs()
                checkpoint = child.folder/'character_checkpoint.json'
                if checkpoint.is_file():
                    from .character_geometry import check_geometry_checkpoint
                    expected = dict(child.data['settings'])
                    if cfg.get('character_geometry_mode') is not None:
                        for key in ('character_geometry_mode','character_geometry_overrides','character_material_overrides','character_pbr_options'):
                            expected[key] = cfg.get(key, {})
                    child_manifest=read_json(child.folder/'manifest.json') if (child.folder/'manifest.json').is_file() else {}
                    saved_geometry=read_json(checkpoint)
                    if expected.get('character_geometry_mode') is not None or saved_geometry.get('geometry_policy') is not None:
                        check_geometry_checkpoint(saved_geometry, expected, child_manifest.get('character_geometry'), child.folder)
            if len(children) == len(cfg['additional_characters']) + 1 and all(
                    (child.folder / 'effective.json').is_file() for child in children):
                if not self.exports_ready():
                    self.job.status('mmd_bake_done', '恢复多角色：复用各角色烘焙，继续 Blender 导出')
                else:
                    self.scene_started = not cfg['scene'] or 'scene' in manifest
                    self.job.status('scene_done' if self.scene_started else 'character_done', '恢复多角色装配任务')
            else:
                self.bake_thread = threading.Thread(target=run_bake, args=(self.job,), daemon=True)
                self.bake_thread.start()
            return
        needed = {"model","animation"} | ({"camera"} if cfg["camera"] and not mode1 else set())
        if cfg.get('flow') == 'camera' and not manifest.get('camera_samples'):
            self.scene_started = True
            self.job.status('camera_export', '恢复独立镜头采样；等待 Blender 读取检查点')
            self.launch_blender('camera')
            return
        files = manifest.get('files', {})
        character_ready = all(kind in files and Path(files[kind].get('path', '')).is_file() and
                              file_hash(files[kind]['path']) == files[kind].get('sha256') for kind in needed)
        if cfg['model'] and cfg.get('character_backend') != 'mmd2unreal' and (
                not character_ready or (mode1 and (not roundtrip_ready(resumed.folder, manifest) or not character_alpha_ready(manifest)))):
            self.scene_started = not bool(cfg['scene'])
            if mode1 and not (resumed.folder/'effective.json').is_file():
                self.job.status('mmd_prepare', '恢复物理处理；等待读取已验证的检查点')
                self.bake_thread = threading.Thread(target=run_bake, args=(self.job,), daemon=True)
                self.bake_thread.start()
            else:
                self.job.status('preparing', '恢复角色导出；等待 Blender 读取检查点')
                self.launch_blender('character')
                self.character_started = True
            return
        self.character_started = True
        self.scene_started = not cfg["scene"] or "scene" in manifest
        checkpoint = self.job.folder/"ue_checkpoint.json"
        message = ("恢复任务：UE 连接后将从上次导入检查点继续" if checkpoint.exists()
                   else "恢复任务：UE 连接后继续导入")
        if not self.scene_started:
            message = ("恢复任务：从冻结几何、已分析网格和 USD 分块还原点继续；仅重做当前未完成步骤"
                       if (self.job.folder / 'scene_checkpoint.json').is_file() else
                       "恢复任务：从已提交的冻结或尺寸统计批次继续；仅重做最后未提交的一批"
                       if any((self.job.folder / 'scene_checkpoint').glob('Freeze_*.json.gz')) else
                       "恢复任务：此旧任务没有网格还原点，需重新分析场景；已有匹配贴图会复用")
        self.job.status("scene_done" if self.scene_started else "character_done",message)

    def connect(self,project):
        if self.orchestrator and not self.orchestrator.assembling:
            return self.orchestrator.connect(project)
        if not self.job:
            raise ValueError("尚无任务")
        if not project or Path(project).suffix.lower() != ".uproject" or not Path(project).is_file():
            raise ValueError("请先选择有效的 .uproject")
        changed_plugins = ensure_builtin_plugins(project)
        ensure_render_defaults(project)
        data = self.job.data
        data["settings"]["project"] = str(Path(project).resolve())
        atomic_json(self.job.path,data)
        if changed_plugins and self._same_project_running(data['settings']):
            self.plugins_restart_required = True
        self.mailbox = connection_folder(self.home, project)
        self.ue_requested = False
        if self.exports_ready():
            checkpoint = self.job.folder/"ue_checkpoint.json"
            message = ("等待 UE 桥接插件就绪；连接后将自动从上次检查点续传" if checkpoint.exists()
                       else "等待 UE 桥接插件就绪；首次安装后需重启编辑器")
            self.job.status("awaiting_ue",message)

    def start_camera_cut(self, project):
        """创建独立CameraCutAuto会话，并排队固定流程的第一步 probe。"""
        project = Path(project).resolve()
        if project.suffix.lower() != ".uproject" or not project.is_file():
            raise ValueError("请选择有效的 .uproject")
        base = "CameraCut_" + re.sub(r"[^A-Za-z0-9]+", "_", project.stem).strip("_")[:32]
        for number in range(1, 10000):
            identifier = f"{base}_{number:02d}"
            session = self.home / "camera_cut" / identifier
            try:
                session.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(f"CameraCut 会话编号已用尽: {base}")
        atomic_json(session / "camera_cut.json", {
            "id": identifier, "project": str(project), "created": time.time(),
            "resource_root": str(resource_root()),
        })
        self.camera_cut_session = session
        self.camera_cut_project = project
        self.camera_cut_pending_mode = "probe"
        self.camera_cut_requested = None
        Job(session).status("camera_cut_waiting_ue", "CameraCutAuto：等待 UE 桥接插件就绪；请确认目标关卡序列已在 Sequencer 中打开")
        return session

    def camera_cut_command(self, mode, cut_frames=None):
        if self.camera_cut_session is None or self.camera_cut_project is None:
            raise ValueError("尚未创建CameraCutAuto会话")
        if mode not in ("probe", "detect", "bake", "verify"):
            raise ValueError(f"未知CameraCutAuto模式：{mode}")
        mailbox = connection_folder(self.home, self.camera_cut_project)
        mailbox.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        payload = {
            "id": request_id, "session": str(self.camera_cut_session), "mode": mode,
            "cut_frames": sorted({int(frame) for frame in (cut_frames or [])}),
            "project": str(self.camera_cut_project),
        }
        atomic_json(mailbox / "camera_cut_request.json", payload)
        self.camera_cut_requested = request_id
        self.camera_cut_pending_mode = None
        Job(self.camera_cut_session).status("camera_cut_queued", f"CameraCutAuto：已提交 {mode} 模式，等待 UE 执行",
                                            mode=mode)
        return request_id

    def poll_camera_cut(self):
        """读取独立镜头会话状态；UE 心跳出现后自动提交尚未发送的模式。"""
        if self.camera_cut_session is None:
            return {}
        session = self.camera_cut_session
        status_path = session / "status.json"
        status = read_json(status_path) if status_path.exists() else {}
        if self.camera_cut_pending_mode and self.camera_cut_requested is None:
            heartbeat = connection_folder(self.home, self.camera_cut_project) / "heartbeat.json"
            if heartbeat.exists():
                try:
                    beat = read_json(heartbeat)
                    project_ok = (str(Path(beat.get("project", "")).resolve()).casefold()
                                  == str(self.camera_cut_project).casefold())
                    if project_ok and time.time() - float(beat.get("time", 0)) < 8:
                        self.camera_cut_command(self.camera_cut_pending_mode)
                        status = read_json(status_path) if status_path.exists() else status
                except (OSError, ValueError, TypeError):
                    pass
        return status

    def stop_camera_cut(self):
        if self.camera_cut_session is not None:
            Job(self.camera_cut_session).status("stopped", "CameraCutAuto已停止")
        self.camera_cut_pending_mode = None
        self.camera_cut_requested = None

    def poll_character_exports(self):
        """Convert each character, then hand one manifest to scene/UE."""
        try:
            active = self.active_character_job
            if active:
                status = read_json(active.folder / 'status.json')
                running = [(mode, child) for mode, child in self.processes if child.poll() is None]
                if running:
                    previous = read_json(self.job.folder / 'status.json')
                    if previous.get('child_updated') != status.get('updated'):
                        self.job.status('characters_export', status.get('message', '导出角色'),
                                        character_job=str(active.folder), child_updated=status.get('updated'))
                    return read_json(self.job.folder / 'status.json')
                failed = any(child.returncode != 0 for _, child in self.processes)
                self.processes = []
                if failed or status.get('state') != 'character_done':
                    raise RuntimeError(f'角色导出失败: {active.folder}\n{status.get("message", "缺少完成回执")}')
                self.active_character_job = None
            children = [Job(path) for path in self.job.data['character_jobs']]
            manifests = []
            for index, child in enumerate(children, 1):
                if not child.folder.is_relative_to(self.job.folder / 'characters'):
                    raise ValueError('角色任务目录超出本任务')
                path = child.folder / 'manifest.json'
                manifest = read_json(path) if path.is_file() else {}
                complete = roundtrip_ready(child.folder, manifest) and character_alpha_ready(manifest) and all(
                    manifest.get('files', {}).get(kind, {}).get('sha256') and
                    Path(manifest['files'][kind].get('path', '')).is_file() and
                    file_hash(manifest['files'][kind]['path']) == manifest['files'][kind]['sha256']
                    for kind in ('model', 'animation'))
                if not complete:
                    self.job.status('characters_export', f'Blender 导出角色 {index}/{len(children)}',
                                    character_job=str(child.folder))
                    self.active_character_job = child
                    self.launch_blender('character', job=child)
                    return read_json(self.job.folder / 'status.json')
                manifests.append({**manifest, 'label': Path(child.data['settings']['model']).stem})
            if len(manifests) != len(self.job.data['settings']['additional_characters']) + 1:
                raise ValueError('角色清单不完整')
            first = manifests[0]
            if any(item['fps'] != first['fps'] for item in manifests):
                raise ValueError('多角色帧率不一致')
            merged = {**first, 'job_id': self.job.data['id'], 'asset_id': self.job.data['asset_id'],
                      'characters': manifests,
                      'materials': [record for item in manifests for record in item['materials']],
                      'warnings': [f'{item["label"]}: {warning}' for item in manifests for warning in item['warnings']],
                      'frame_range': [min(item['frame_range'][0] for item in manifests),
                                      max(item['frame_range'][1] for item in manifests)]}
            previous = self.job.folder / 'manifest.json'
            if previous.is_file() and 'scene' in read_json(previous):
                merged['scene'] = read_json(previous)['scene']
            atomic_json(self.job.folder / 'manifest.json', merged)
            for item in manifests:
                for warning in item.get('roundtrip_warnings', []):
                    self.job.status('warning', f'{item["label"]}: {warning}', severity='warning')
            self.character_started = True
            self.scene_started = not bool(self.job.data['settings']['scene']) or 'scene' in merged
            self.job.status('character_done', f'{len(children)} 个角色导出完成，等待统一场景与序列装配')
        except Exception as exc:
            self.job.status('failed', str(exc))
        return read_json(self.job.folder / 'status.json')

    def exports_ready(self):
        if self.job and self.job.data.get('orchestration_version'):
            path = self.job.folder / 'manifest.json'
            return path.is_file() and bool(read_json(path).get('composition'))
        if not self.job or not (self.job.folder/'manifest.json').exists():
            return False
        manifest = read_json(self.job.folder/'manifest.json')
        cfg = self.job.data['settings']
        if cfg.get('additional_characters'):
            characters = manifest.get('characters', [])
            if len(characters) != len(cfg['additional_characters']) + 1:
                return False
            folders = self.job.data.get('character_jobs', [])
            if len(folders) != len(characters):
                return False
            for folder, character in zip(folders, characters):
                if not roundtrip_ready(folder, character) or not character_alpha_ready(character):
                    return False
                for kind in ('model', 'animation'):
                    entry = character.get('files', {}).get(kind, {})
                    if not Path(entry.get('path', '')).is_file() or file_hash(entry['path']) != entry.get('sha256'):
                        return False
        if cfg.get('flow') == 'camera':
            return bool(manifest.get('camera_samples'))
        if cfg.get('character_backend') == 'mmdbridge_fbx' and cfg['model'] and not cfg.get('additional_characters'):
            if not roundtrip_ready(self.job.folder, manifest) or not character_alpha_ready(manifest):
                return False
        camera_fbx = cfg['camera'] and cfg.get('character_backend') != 'mmdbridge_fbx'
        needed = ({'model','animation'}|({'camera'} if camera_fbx else set())) if cfg['model'] else set()
        if cfg.get('character_backend', 'fbx_legacy') == 'mmd2unreal':
            needed = set()
        if cfg.get('character_backend') == 'mmdbridge_fbx' and cfg['camera']:
            scene = manifest.get('scene', {})
            if not scene.get('camera') or scene.get('route') != 'usd_stage':
                return False
        return needed.issubset(manifest.get('files',{})) and (not cfg['scene'] or 'scene' in manifest)

    def launch_ue(self):
        if self.defer_ue:
            self.job.status('exports_done', '模块本地处理完成；等待独立 UE 导入')
            return
        cfg = self.job.data["settings"]
        if self.plugins_restart_required and self._same_project_running(cfg):
            self.ue_started = self.ue_requested = True
            self.job.status('ue_wait_restart', '已自动启用所需官方插件；请保存并重启 UE，再恢复任务')
            return
        self.plugins_restart_required = False
        from .usd_scene_optimizer import needs_import_compaction
        if not self.material_repair and needs_import_compaction(self.job):
            self.job.status('scene_import_optimizing', '导入前优化 USD 散件；复用现有网格与贴图')
            self.launch_blender('scene_import')
            return
        if not cfg["unreal"]:
            self.job.status("awaiting_project","请选择 UE 引擎及项目后连接")
            return
        args = [cfg["unreal"]]
        if cfg["project"]:
            args.append(cfg["project"])
            # Import starts in a small map. Restoring the previous huge Stage
            # before the tool opens its scratch map doubles startup pressure.
            args.append('/Engine/Maps/Entry')
            self.connect(cfg["project"])
            if self.plugins_restart_required:
                self.ue_started = self.ue_requested = True
                self.job.status('ue_wait_restart', '已自动启用所需官方插件；请保存并重启 UE，再恢复任务')
                return
            heartbeat=self.mailbox/'heartbeat.json'
            plugin_code = Path(cfg['project']).parent / 'Plugins/MMDBridge/Content/Python/mmd_bridge'
            stale = any(not (plugin_code / name).is_file() or
                        file_hash(plugin_code / name) != file_hash(resource_root() / 'mmd_bridge' / name)
                        for name in UE_CODE_MODULES+('scene_appearance.py',))
            if heartbeat.exists() and not stale:
                beat=read_json(heartbeat)
                if time.time()-beat['time']<5 and (beat.get('pid') is None or process.pid_exists(beat['pid'])) and str(Path(beat['project']).resolve()).casefold()==str(Path(cfg['project']).resolve()).casefold():
                    self.ue_started=True
                    return
            # A running editor may not have produced a bridge heartbeat yet (startup,
            # disabled plugin, or restart still required). Do not launch the same
            # .uproject a second time merely because the heartbeat is absent.
            target = str(Path(cfg["project"]).resolve()).casefold()
            try:
                already_open = any(
                    str(Path(record["path"]).resolve()).casefold() == target
                    for record in running_projects()
                )
            except Exception:
                already_open = False
            plugin_code = Path(cfg["project"]).parent / "Plugins/MMDBridge/Content/Python/mmd_bridge"
            if already_open and stale:
                self.ue_started = True
                self.ue_requested = True
                self.job.status("ue_wait_restart", "UE 正在运行旧版桥接代码；请保存并关闭该工程，随后恢复任务以加载新的 Actor 层级逻辑")
                return
            if not already_open:
                install_ue_bridge(cfg["project"], self.home, cfg.get('unreal'), character_settings=cfg)
            if already_open:
                self.ue_started = True
                self.job.status(
                    "awaiting_ue",
                    "已检测到该 UE 项目正在运行；不会重复启动。正在等待桥接插件连接，若长期为黄色请启用插件并重启 UE。",
                )
                return
        if cfg.get("unattended"):
            args.append("-unattended")
        from .ue_render_policy import shader_worker_arguments
        args+=shader_worker_arguments(cfg)
        process.popen(args,cwd=str(Path(cfg["unreal"]).parent))
        self.ue_started = True
        self.job.status("awaiting_ue" if cfg["project"] else "awaiting_project",
                        "请在 UE 中选择或创建项目，桌面工具将检测已打开的项目")

    def _heartbeat_fresh(self, cfg, max_age=8):
        if not self.mailbox:
            return False
        heartbeat = self.mailbox/"heartbeat.json"
        if not heartbeat.exists():
            return False
        try:
            beat = read_json(heartbeat)
            return (time.time()-float(beat.get("time",0)) < max_age and
                    (beat.get('pid') is None or process.pid_exists(beat['pid'])) and
                    str(Path(beat.get("project","")).resolve()).casefold() ==
                    str(Path(cfg["project"]).resolve()).casefold())
        except (OSError, ValueError, TypeError):
            return False

    def _same_project_running(self, cfg):
        target = str(Path(cfg["project"]).resolve()).casefold()
        try:
            return any(str(Path(record["path"]).resolve()).casefold() == target
                       for record in running_projects())
        except Exception:
            return None

    def _bridge_process_running(self, cfg):
        if self.mailbox:
            heartbeat = self.mailbox/"heartbeat.json"
            if heartbeat.exists():
                try:
                    beat = read_json(heartbeat)
                    same_project = (str(Path(beat.get("project","")).resolve()).casefold() ==
                                    str(Path(cfg["project"]).resolve()).casefold())
                    if same_project and beat.get("pid") is not None:
                        if process.pid_exists(beat["pid"]):
                            return True
                        # A stale heartbeat can belong to the editor that just
                        # crashed.  Do not conclude that the newly restarted
                        # editor is also dead before checking its command line.
                except (OSError, ValueError, TypeError):
                    pass
        return self._same_project_running(cfg)

    def _send_ue_request(self):
        request = self.mailbox / "request.json"
        response = self.mailbox / "response.json"
        if request.exists():
            pending = read_json(request)
            receipt = read_json(response) if response.exists() else {}
            terminal = {"complete", "needs_review", "failed", "stopped", "cancelled", "ue_wait_vram",
                        "upstream_blocked", "ue_paused_gpu_crash", "ue_paused_resource_limit", "ue_wait_save"}
            if (pending.get("job") == str(self.job.folder) and
                    (pending.get("id") != receipt.get("id") or receipt.get("state") not in terminal)):
                self.ue_requested = True
                return
        command={"id":uuid.uuid4().hex,"job":str(self.job.folder)}
        if self.material_repair:
            command.update(action='material_repair',batch=self.material_repair['batch'],repair_action=self.material_repair['action'])
        atomic_json(request,command)
        self.ue_requested = True

    def _poll_vram_resume(self, status, cfg):
        now = time.time()
        retry_after = max(float(status.get("retry_after",0)), self.ue_vram_next_check)
        if now < retry_after:
            return
        self.ue_vram_next_check = now + 10
        info = process.gpu_memory_info()
        info=dict(info or {})
        physical=process.physical_memory_info()
        if physical:info.update(system_total_mb=physical['total_mb'],system_used_mb=physical['total_mb']-physical['available_mb'])
        resume_percent = int(cfg.get("ue_vram_resume_percent",80))
        used_percent = process.gpu_budget_percent(info)
        if used_percent is None:
            return
        if used_percent >= resume_percent:
            return
        if self._heartbeat_fresh(cfg):
            self._send_ue_request()
            self.job.status(
                "awaiting_ue",
                f"共享显存占用已降至 {used_percent:.1f}%（恢复阈值 {resume_percent}%），正在自动续传 UE 导入",
            )
        elif self._bridge_process_running(cfg) is False:
            self.ue_started = self.ue_requested = False
            self.launch_ue()

    def _poll_ue_crash_recovery(self, status, cfg):
        active = {"awaiting_ue","ue_bridge_wait","ue_prepare","ue_import","ue_materials",
                  "ue_scene","ue_sequence","ue_collision","ue_item_skipped"}
        if status.get("state") not in active or time.time()-float(status.get("updated",0)) < 15:
            return
        now = time.time()
        if now < self.ue_process_next_check:
            return
        self.ue_process_next_check = now + 10
        running = self._bridge_process_running(cfg)
        if running is True:
            if status.get("state") in {"awaiting_ue", "ue_bridge_wait"} and not self._heartbeat_fresh(cfg):
                message = (
                    "UE 项目已打开，但桥接插件尚未产生心跳，导入暂未开始。"
                    "请确认桥接状态为绿色；若为黄色，请重新安装插件并重启 UE。连接后任务会自动继续。"
                )
                if status.get("message") != message:
                    self.job.status("ue_bridge_wait", message)
            return
        if running is None:
            return
        if self.ue_restart_attempts >= 3:
            self.job.status("failed","UE 导入期间连续退出 3 次，已停止自动重启；请检查 UE 崩溃日志后恢复任务")
            return
        self.ue_restart_attempts += 1
        self.ue_started = self.ue_requested = False
        self.ue_restart_not_before = now + 10
        self.job.status("ue_restarting",f"检测到 UE 导入进程退出；将在 10 秒后自动重启并从检查点恢复（{self.ue_restart_attempts}/3）")

    def stop(self):
        child = getattr(self, 'custom_texture_process', None)
        if child is not None and child.poll() is None:
            self.custom_edit_cancelled = True
            process.terminate_pid(child.pid)
            return
        if self.orchestrator and not self.orchestrator.assembling:
            return self.orchestrator.stop()
        if self.standalone_render and self.standalone_render.get('active'):
            self.stop_background()
            return
        self.stopped = True
        if self.job:
            status_path = self.job.folder / 'status.json'
            status = read_json(status_path) if status_path.exists() else {}
            (self.job.folder/"stop").touch()
            self.job.status('stopping', '正在立即中断当前任务进程；已完成还原点保留')
            for mode, child in self.processes:
                if child.poll() is None:
                    process.terminate_pid(child.pid)
                    child.wait(timeout=10)
            self.processes = []
            folders = [self.job.folder]
            for folder in self.job.data.get('character_jobs', []):
                child_folder = Path(folder).resolve()
                if not child_folder.is_relative_to(self.job.folder / 'characters'):
                    raise ValueError('角色任务目录超出本任务')
                (child_folder / 'stop').touch()
                folders.append(child_folder)
            for folder in folders:
                for attempt_path in folder.glob('mmd_*/attempt.json'):
                    attempt = read_json(attempt_path)
                    if attempt.get('runtime_reusable'):
                        lease_path = Path(attempt['runtime']).parent.parent / 'active.json'
                        lease = read_json(lease_path) if lease_path.is_file() else {}
                        if lease.get('job') != str(folder) or lease.get('pid') != attempt.get('mmd_pid'):
                            continue
                    if not attempt.get('validated') and attempt.get('mmd_pid') and process.pid_exists(attempt['mmd_pid']):
                        process.terminate_pid(attempt['mmd_pid'], expected_executable=attempt['runtime'])
            # Native UE import calls cannot yield to Python cancellation. Only
            # terminate the project executing this job, never an idle editor.
            if self.mailbox and self.ue_requested and status.get('state') in {
                    'ue_prepare', 'ue_import', 'ue_materials', 'ue_scene', 'ue_sequence', 'ue_collision', 'ue_item_skipped', 'ue_warning', 'ue_material_repair'}:
                request_path = self.mailbox / 'request.json'
                request = read_json(request_path) if request_path.exists() else {}
                if request.get('job') == str(self.job.folder):
                    target = str(Path(self.job.data['settings']['project']).resolve()).casefold()
                    for record in running_projects():
                        if str(Path(record['path']).resolve()).casefold() == target:
                            process.terminate_pid(record['pid'])
            if self.mailbox:
                request_path = self.mailbox / 'request.json'
                if request_path.exists():
                    request = read_json(request_path)
                    if request.get('job') == str(self.job.folder):
                        atomic_json(self.mailbox / 'response.json', {'id': request['id'], 'state': 'stopped'})
            self.job.status("stopped", "当前任务已立即中断；已完成并落盘的还原点保留，恢复时重做当前未完成步骤。")

    def poll(self):
        if self.orchestrator:
            return self.orchestrator.poll()
        return self._poll_single()

    def _poll_single(self):
        if self.material_repair: return self._poll_material_repair()
        if self.standalone_render and self.standalone_render.get('active'):
            return self._poll_background()
        if not self.job:
            return {}
        path = self.job.folder/"status.json"
        status = read_json(path) if path.exists() else {}
        if self.stopped:
            self.processes = [(mode, child) for mode, child in self.processes if child.poll() is None]
            return read_json(path) if path.exists() else {"state":"stopped","message":"任务已停止"}
        cfg = self.job.data["settings"]
        state = status.get("state")
        if state in ('geometry_preflight', 'geometry_warning', 'awaiting_geometry_selection'):
            return status
        if state == 'geometry_review_ready':
            if not self._geometry_review_current():
                self._begin_geometry_review()
            elif not self.bake_thread or not self.bake_thread.is_alive():
                self.job.status('geometry_review_accepted', '几何部件已确认，开始角色流程')
                self.bake_thread = threading.Thread(target=run_bake, args=(self.job,), daemon=True)
                self.bake_thread.start()
            return read_json(path)
        if cfg.get('additional_characters') and state in ('mmd_bake_done', 'characters_export'):
            return self.poll_character_exports()
        if state == "mmd_bake_done" and not self.character_started:
            try:
                self.launch_blender("character")
                self.character_started = True
            except Exception as exc:
                self.job.status("failed", f"Blender 启动失败: {exc}")
                return read_json(path)
        if (state == 'ue_render' or (state == 'ue_wait_vram' and status.get('operation') == 'reference')) and time.time() - float(status.get('updated', 0)) > 30:
            if self._bridge_process_running(cfg) is False:
                self.reference_request_id = None
                self.job.status('failed', 'UE 参考帧渲染进程已退出，请查看 UE 日志后重试')
                return read_json(path)
        if state == "ue_wait_vram":
            if status.get('operation') != 'reference':
                self._poll_vram_resume(status,cfg)
        elif state == "ue_restarting" and not self.ue_started and time.time() >= self.ue_restart_not_before:
            self.launch_ue()
        export_busy=any(mode in ('character','scene','scene_import','camera') for mode,_ in self.processes)
        if state == "character_done" and not self.scene_started and not export_busy:
            self.scene_started = True
            self.launch_blender("scene")
        elif state in ("character_done","scene_done") and not self.ue_started and not export_busy and self.exports_ready():
            self.launch_ue()
        if self.mailbox and not self.ue_requested and not export_busy and self.exports_ready() and state not in ('scene_import_optimizing','failed','stopped','cancelled','upstream_blocked','complete','needs_review','ue_wait_restart','ue_paused_gpu_crash','ue_paused_resource_limit'):
            heartbeat = self.mailbox/"heartbeat.json"
            if heartbeat.exists():
                beat = read_json(heartbeat)
                if time.time()-beat["time"] < 5 and str(Path(beat["project"]).resolve()).casefold()==str(Path(cfg["project"]).resolve()).casefold():
                    self._send_ue_request()
        self._poll_ue_crash_recovery(status,cfg)
        active_processes = []
        scene_handoff_done=False
        for mode,process in self.processes:
            code = process.poll()
            if code is None:
                active_processes.append((mode,process))
                continue
            # Native receipt writes can finish between the first status read
            # and poll(). Use the latest state before declaring a missing receipt.
            state = read_json(path).get('state') if path.is_file() else state
            scene_receipt=False
            manifest_path=self.job.folder/'manifest.json'
            if mode in ('scene','scene_import') and manifest_path.is_file():
                exported=read_json(manifest_path).get('scene',{})
                usd=exported.get('usd')
                root=usd.get('root_layer') if isinstance(usd,dict) else usd
                finalized=isinstance(exported.get('metrics',{}).get('elapsed_seconds'),(int,float))
                scene_receipt=bool(root and Path(root).is_file() and
                    (finalized or exported.get('stats',{}).get('instances',0)>0))
                if mode=='scene_import' and scene_receipt:
                    from .usd_scene_optimizer import needs_import_compaction
                    scene_receipt=not needs_import_compaction(self.job)
            if code is not None and code != 0 and state not in ("complete","failed"):
                self.job.status("failed",f"{mode} 进程退出码 {code}；请查看任务日志")
            elif code == 0 and state in ({"preparing","baking_animation","exporting"} if mode == "character" else {"scene_export","scene_baking","camera_export","scene_import_optimizing"}):
                if not scene_receipt:self.job.status("failed",f"{mode} 进程已退出但没有完成回执；请查看日志")
                else:scene_handoff_done=True
            elif mode == 'reference' and code == 0 and state == 'reference_render':
                self.job.status('failed', 'Blender 参考帧进程已退出但没有完成回执，请查看日志')
        self.processes = active_processes
        if scene_handoff_done and not active_processes and read_json(path).get('state') in ('scene_export','scene_baking','scene_import_optimizing'):
            self.job.status('scene_done','已核验最终导出清单；子进程退出，继续导入')
            state='scene_done'
        if self._poll_material_bake_retry(state,cfg):return read_json(path)
        return read_json(path) if path.is_file() else status
