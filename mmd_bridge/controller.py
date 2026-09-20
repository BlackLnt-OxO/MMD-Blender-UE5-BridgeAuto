from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .core import Job, atomic_json, read_json, resource_root, stable_id
from . import process


def app_home():
    root = os.environ.get("MMDBRIDGE_HOME")
    return Path(root) if root else Path(os.environ.get("LOCALAPPDATA",Path.home())) / "MMDBridge"


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
            raise ValueError(f"旧 Blender 导出进程仍在运行（PID {record['ProcessId']}）；停止按钮只发送停止请求。请等待它退出，或确认终止该后台导出进程后再恢复，避免两个进程同时写入。")


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
    if settings.character_backend != "mmd2unreal" or not any((settings.model, settings.motion, settings.lipsync, settings.camera)) or not settings.project:
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


def install_ue_bridge(project, home=None):
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
    for name in ("__init__.py","core.py","ue_host.py","ue_materials.py","ue_usd.py","material_slots.py",
                 "process.py","ue_camera_cut_baker.py","ue_mmd2unreal.py"):
        shutil.copy2(root/"mmd_bridge"/name,module_folder/name)
    mailbox = (home or app_home()) / "connections" / stable_id(str(project).casefold())
    mailbox.mkdir(parents=True,exist_ok=True)
    atomic_json(folder/"Content/Python/bridge_config.json",{"project":str(project),"mailbox":str(mailbox.resolve())})
    info = read_json(project)
    plugins = info.setdefault("Plugins",[])
    for name in ("MMDBridge","PythonScriptPlugin","EditorScriptingUtilities","SequencerScripting","USDImporter"):
        current = next((p for p in plugins if p["Name"]==name),None)
        if current:
            current["Enabled"] = True
        else:
            plugins.append({"Name":name,"Enabled":True})
    backup = project.with_suffix(".uproject.mmdb-backup")
    if not backup.exists():
        shutil.copy2(project,backup)
    atomic_json(project,info)
    marker.write_text("MMDBridge 0.1.0\n",encoding="utf-8")
    return mailbox


def running_projects():
    if os.name != "nt":
        return []
    command = "Get-CimInstance Win32_Process -Filter \"Name = 'UnrealEditor.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = process.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=12,creationflags=0x08000000)
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

    mailbox = (home or app_home()) / "connections" / stable_id(str(project_path).casefold())
    heartbeat = mailbox / "heartbeat.json"
    if heartbeat.exists():
        try:
            beat = read_json(heartbeat)
            same_project = (
                str(Path(beat.get("project", "")).resolve()).casefold()
                == str(project_path).casefold()
            )
            if same_project and time.time() - float(beat.get("time", 0)) < 8:
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
        self.camera_cut_pending_mode = None
        self.camera_cut_requested = None

    def launch_blender(self,mode):
        cfg = self.job.data["settings"]
        args = [cfg["blender"],"--factory-startup"]
        if mode == "scene" or cfg["unattended"]:
            args += ["--background"]
        if mode == "character" and cfg["working_blend"]:
            args += ["--disable-autoexec",cfg["working_blend"]]
        args += ["--python-exit-code","1","--python",str(resource_root()/"scripts/blender_entry.py"),"--",str(self.job.folder),mode]
        log = (self.job.folder/f"blender_{mode}.log").open("w",encoding="utf-8")
        self.log_handles.append(log)
        child = process.popen(args,stdout=log,stderr=subprocess.STDOUT,cwd=resource_root(),
            creationflags=0x08000000 if os.name=="nt" else 0)
        self.processes.append((mode,child))

    def start(self,settings):
        check_upstream_installation(settings)
        self.job = Job.create(self.home/"jobs",settings)
        self.stopped = False
        self.scene_started = not bool(settings.scene)
        self.ue_started = self.ue_requested = False
        self.ue_vram_next_check = self.ue_process_next_check = 0.0
        self.ue_restart_not_before = 0.0
        self.ue_restart_attempts = 0
        self.mailbox = None
        self.processes = []
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
        self.launch_blender("character" if settings.model else "scene")
        if not settings.model:
            self.scene_started = True
        return self.job

    def resume(self,folder):
        resumed = Job(folder)
        resumed.verify_inputs()
        settings = resumed.data['settings']
        if settings.get('scene') or settings.get('character_backend', 'fbx_legacy') == 'fbx_legacy':
            ensure_blender_job_stopped(folder)
        self.job = resumed
        (self.job.folder/"stop").unlink(missing_ok=True)
        self.stopped = False
        self.ue_started = self.ue_requested = False
        self.ue_vram_next_check = self.ue_process_next_check = 0.0
        self.ue_restart_not_before = 0.0
        self.ue_restart_attempts = 0
        self.processes = []
        manifest_path = self.job.folder/"manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        cfg = self.job.data["settings"]
        needed = {"model","animation"} | ({"camera"} if cfg["camera"] else set())
        if cfg.get("character_backend", "fbx_legacy") == "fbx_legacy" and cfg["model"] and not needed.issubset(manifest.get("files",{})):
            raise ValueError("角色导出尚不完整，请回到原 Blender 窗口继续导出；窗口已关闭时需新建任务")
        self.scene_started = not cfg["scene"] or "scene" in manifest
        checkpoint = self.job.folder/"ue_checkpoint.json"
        message = ("恢复任务：UE 连接后将从上次导入检查点继续" if checkpoint.exists()
                   else "恢复任务：UE 连接后继续导入")
        if not self.scene_started:
            message = "恢复任务：将重新分析场景并写入 USD；匹配的已烘焙贴图会复用，不从中断网格编号续写"
        self.job.status("scene_done" if self.scene_started else "character_done",message)

    def connect(self,project):
        if not self.job:
            raise ValueError("尚无任务")
        if not project or Path(project).suffix.lower() != ".uproject" or not Path(project).is_file():
            raise ValueError("请先选择有效的 .uproject")
        data = self.job.data
        data["settings"]["project"] = str(Path(project).resolve())
        atomic_json(self.job.path,data)
        self.mailbox = self.home/"connections"/stable_id(str(Path(project).resolve()).casefold())
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
        identifier = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        session = self.home / "camera_cut" / identifier
        session.mkdir(parents=True, exist_ok=True)
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
        mailbox = self.home / "connections" / stable_id(str(self.camera_cut_project).casefold())
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
            heartbeat = self.home / "connections" / stable_id(str(self.camera_cut_project).casefold()) / "heartbeat.json"
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

    def exports_ready(self):
        if not self.job or not (self.job.folder/'manifest.json').exists():
            return False
        manifest = read_json(self.job.folder/'manifest.json')
        cfg = self.job.data['settings']
        needed = ({'model','animation'}|({'camera'} if cfg['camera'] else set())) if cfg['model'] else set()
        if cfg.get('character_backend', 'fbx_legacy') == 'mmd2unreal':
            needed = set()
        return needed.issubset(manifest.get('files',{})) and (not cfg['scene'] or 'scene' in manifest)

    def launch_ue(self):
        cfg = self.job.data["settings"]
        if not cfg["unreal"]:
            self.job.status("awaiting_project","请选择 UE 引擎及项目后连接")
            return
        args = [cfg["unreal"]]
        if cfg["project"]:
            args.append(cfg["project"])
            self.connect(cfg["project"])
            heartbeat=self.mailbox/'heartbeat.json'
            if heartbeat.exists():
                beat=read_json(heartbeat)
                if time.time()-beat['time']<5 and str(Path(beat['project']).resolve()).casefold()==str(Path(cfg['project']).resolve()).casefold():
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
            if already_open:
                self.ue_started = True
                self.job.status(
                    "awaiting_ue",
                    "已检测到该 UE 项目正在运行；不会重复启动。正在等待桥接插件连接，若长期为黄色请启用插件并重启 UE。",
                )
                return
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
        atomic_json(self.mailbox/"request.json",{"id":uuid.uuid4().hex,"job":str(self.job.folder)})
        self.ue_requested = True

    def _poll_vram_resume(self, status, cfg):
        now = time.time()
        retry_after = max(float(status.get("retry_after",0)), self.ue_vram_next_check)
        if now < retry_after:
            return
        self.ue_vram_next_check = now + 10
        info = process.gpu_memory_info()
        resume_percent = int(cfg.get("ue_vram_resume_percent",80))
        if not info or not info.get("total_mb"):
            return
        used_percent = info["used_mb"] * 100.0 / info["total_mb"]
        if used_percent > resume_percent:
            return
        if self._heartbeat_fresh(cfg):
            self._send_ue_request()
            self.job.status(
                "awaiting_ue",
                f"总 GPU 内存已降至 {used_percent:.1f}%（恢复阈值 {resume_percent}%），正在自动续传 UE 导入",
            )
        elif self._bridge_process_running(cfg) is False:
            self.ue_started = self.ue_requested = False
            self.launch_ue()

    def _poll_ue_crash_recovery(self, status, cfg):
        active = {"awaiting_ue","ue_bridge_wait","ue_prepare","ue_import","ue_materials",
                  "ue_scene","ue_sequence","ue_item_skipped"}
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
            self.job.status("failed","UE 导入期间连续退出 3 次，已停止自动重启；请降低贴图分辨率或提高显存安全阈值后恢复任务")
            return
        self.ue_restart_attempts += 1
        self.ue_started = self.ue_requested = False
        self.ue_restart_not_before = now + 10
        self.job.status("ue_restarting",f"检测到 UE 导入进程退出；将在 10 秒后自动重启并从检查点恢复（{self.ue_restart_attempts}/3）")

    def stop(self):
        if self.job:
            (self.job.folder/"stop").touch()
            self.job.status("stopped","已请求停止；当前不可中断的导入或烘焙步骤结束后停止。Blender/UE 窗口保留。")
        self.stopped = True

    def poll(self):
        if not self.job:
            return {}
        path = self.job.folder/"status.json"
        status = read_json(path) if path.exists() else {}
        if self.stopped:
            return {"state":"stopped","message":"任务已停止"}
        cfg = self.job.data["settings"]
        state = status.get("state")
        if state == "ue_wait_vram":
            self._poll_vram_resume(status,cfg)
        elif state == "ue_restarting" and not self.ue_started and time.time() >= self.ue_restart_not_before:
            self.launch_ue()
        if state == "character_done" and not self.scene_started:
            self.scene_started = True
            self.launch_blender("scene")
        elif state in ("character_done","scene_done") and not self.ue_started and self.exports_ready():
            self.launch_ue()
        if self.mailbox and not self.ue_requested and self.exports_ready() and state not in ('failed','stopped','cancelled','upstream_blocked','complete','needs_review'):
            heartbeat = self.mailbox/"heartbeat.json"
            if heartbeat.exists():
                beat = read_json(heartbeat)
                if time.time()-beat["time"] < 5 and str(Path(beat["project"]).resolve()).casefold()==str(Path(cfg["project"]).resolve()).casefold():
                    self._send_ue_request()
        self._poll_ue_crash_recovery(status,cfg)
        active_processes = []
        for mode,process in self.processes:
            code = process.poll()
            if code is None:
                active_processes.append((mode,process))
                continue
            if code is not None and code != 0 and state not in ("complete","failed"):
                self.job.status("failed",f"{mode} 进程退出码 {code}；请查看任务日志")
            elif code == 0 and state in ({"preparing","baking_animation","exporting"} if mode == "character" else {"scene_export"}):
                self.job.status("failed",f"{mode} 进程已退出但没有完成回执；请查看日志")
        self.processes = active_processes
        return status
