"""Run the installed MMDBridge Mode 1 exporter in an isolated MMD process."""
from __future__ import annotations

import ctypes
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import time
import traceback
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .core import Job, atomic_json, file_hash, read_json
from .effective_inputs import write_effective_inputs
from .plan_v2_prep import prepare_character_v2, PreparedCharacterV2
from .pmx_reader import read_pmx
from .vmd_reader import decode_name_field


MMD_EXE = "MikuMikuDance_CHS.exe"
SCRIPT = "mmdbridge_vmd_mode1.py"
WM_COMMAND = 0x111
MODEL_LOAD_ID = 435
MOTION_LOAD_ID = 209
AVI_EXPORT_ID = 223
MMD_EXIT_ID = 200


def scan_vmd(path: str | Path) -> dict:
    """Inspect a large VMD without materializing millions of keyframe objects."""
    path = Path(path)
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        size = len(data)
        if size < 54 or not data[:30].startswith(b"Vocaloid Motion Data"):
            raise ValueError(f"无效 VMD 文件: {path}")
        offset = 50 if b"0002" in data[:30] else 40
        sections = (("bone", 111, 15), ("morph", 23, 15),
                    ("camera", 61, 0), ("light", 28, 0), ("shadow", 9, 0))
        result = {"path": str(path), "bytes": size, "counts": {},
                  "bone_tracks": Counter(), "morph_tracks": Counter(),
                  "bone_min": None, "bone_max": None, "morph_max": None}
        for kind, width, name_size in sections:
            if offset + 4 > size:
                raise ValueError(f"VMD {kind} 计数截断")
            count = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if count > (size - offset) // width:
                raise ValueError(f"VMD {kind} 区段截断")
            result["counts"][kind] = count
            if name_size:
                tracks = result[f"{kind}_tracks"]
                for pos in range(offset, offset + count * width, width):
                    name = decode_name_field(data[pos:pos + name_size]).name
                    frame = struct.unpack_from("<I", data, pos + name_size)[0]
                    tracks[name] += 1
                    if kind == "bone":
                        low, high = result["bone_min"], result["bone_max"]
                        result["bone_min"] = frame if low is None else min(low, frame)
                        result["bone_max"] = frame if high is None else max(high, frame)
                    elif kind == "morph":
                        high = result["morph_max"]
                        result["morph_max"] = frame if high is None else max(high, frame)
            offset += count * width
        if offset + 4 > size:
            raise ValueError("VMD IK 区段计数截断")
        ik_count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        for _ in range(ik_count):
            if offset + 9 > size:
                raise ValueError("VMD IK 帧截断")
            item_count = struct.unpack_from("<I", data, offset + 5)[0]
            offset += 9
            if item_count > (size - offset) // 21:
                raise ValueError("VMD IK 项截断")
            offset += item_count * 21
        if offset != size:
            raise ValueError("VMD 存在未识别尾部数据")
        result["counts"]["ik"] = ik_count
        return result


def validate_mode1(path: Path, prepared, end: int, *, full: bool) -> dict:
    result = scan_vmd(path)
    if result["bone_min"] != 0 or result["bone_max"] != end:
        raise ValueError(f"烘焙帧范围不完整: {result['bone_min']}..{result['bone_max']}，应为 0..{end}")
    expected = set(prepared.expected_bones)
    missing = expected - result["bone_tracks"].keys()
    if missing:
        raise ValueError(f"烘焙缺少 {len(missing)} 条物理骨轨道: {sorted(missing)[:8]}")
    sparse = [name for name in expected if result["bone_tracks"][name] < end + 1]
    if sparse:
        raise ValueError(f"物理骨轨道未逐帧烘焙: {sparse[:8]}")
    source = scan_vmd(prepared.motion)
    if full:
        model_bones = {bone.name_jp for bone in read_pmx(prepared.model).bones}
        usable_source = set(source["bone_tracks"]) & model_bones
        retained = usable_source & result["bone_tracks"].keys()
        # Mode 1 intentionally omits IK/control tracks, while some source
        # tracks contain only keys outside a short probe's range.
        if usable_source and len(retained) < int(len(usable_source) * .9):
            raise ValueError(f"烘焙只保留 {len(retained)}/{len(usable_source)} 条原动作模型轨道")
    if full and prepared.lipsync:
        lip = scan_vmd(prepared.lipsync)
        missing_morphs = set(lip["morph_tracks"]) - result["morph_tracks"].keys()
        if missing_morphs:
            raise ValueError(f"烘焙丢失口型轨道: {sorted(missing_morphs)[:8]}")
    return {"path": str(path), "bytes": result["bytes"],
            "counts": result["counts"], "bone_range": [result["bone_min"], result["bone_max"]],
            "bone_track_count": len(result["bone_tracks"]),
            "physics_tracks_found": len(expected),
            "morph_track_count": len(result["morph_tracks"])}


def missing_runtime_files(source: Path) -> list[str]:
    return [name for name in (MMD_EXE, 'd3d9.dll', 'python312.dll', f'mmdbridge_scripts/{SCRIPT}',
                             f'mmdbridge_settings/{MMD_EXE}.ini') if not (source / name).is_file()]


def _runtime(source: Path, target: Path, end: int, output: Path, progress: Path) -> Path:
    missing = missing_runtime_files(source)
    if missing:
        raise ValueError(f"MMDBridge 安装目录缺少 {missing[0]}")
    if target.exists():
        raise ValueError(f"任务 Runtime 已存在，拒绝复用: {target}")

    def ignore(folder, names):
        return {"out"} if Path(folder).resolve() == source.resolve() and "out" in names else set()

    shutil.copytree(source, target, ignore=ignore)
    (target / "out").mkdir()
    ini = target / "mmdbridge_settings" / f"{MMD_EXE}.ini"
    config = ini.read_text(encoding="utf-16")
    for key, value in (("ScriptName", SCRIPT), ("ScriptCallSetting", "1"),
                       ("StartFrame", "0"), ("EndFrame", str(end)), ("ExportFPS", "30")):
        config, count = re.subn(rf"(?m)^{key}=.*$", f"{key}={value}", config)
        if count != 1:
            raise ValueError(f"MMDBridge 配置缺少唯一的 {key}")
    ini.write_text(config, encoding="utf-16")

    script = target / "mmdbridge_scripts" / SCRIPT
    code = script.read_text(encoding="utf-8")
    old = 'from mmdbridge_vmd import *'
    if code.count(old) != 1 or code.count('dst_dir = "out"') != 1:
        raise ValueError("Mode 1 脚本版本与自动化适配器不匹配")
    code = code.replace(old, old + "\nmessagebox = lambda *args, **kwargs: None", 1)
    code = code.replace('dst_dir = "out"', f"dst_dir = {str(output)!r}", 1)
    code += (
        "\nif framenumber == start_frame or framenumber == end_frame or framenumber % 100 == 0:\n"
        f"    _path = {str(progress)!r}\n"
        "    import json as _json\n"
        "    with open(_path + '.tmp', 'w', encoding='utf-8') as _stream:\n"
        "        _json.dump({'frame': framenumber, 'end': end_frame, "
        "'completed': framenumber == end_frame}, _stream)\n"
        "    os.replace(_path + '.tmp', _path)\n"
    )
    script.write_text(code, encoding="utf-8")
    return target / MMD_EXE


class MmdUi:
    """Control only the MMD process launched for this job."""

    def __init__(self, pid: int, process: subprocess.Popen):
        from pywinauto import Application
        self.process = process
        self.app = Application(backend="win32").connect(process=pid, timeout=45)
        self.main = self.app.window(class_name="Polygon Movie Maker")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"MMD 启动失败，退出码 {process.returncode}")
            if self.main.exists() and self.main.is_visible():
                return
            dialogs = self._visible_dialogs()
            if dialogs:
                message = " ".join(child.window_text() for dialog in dialogs
                                   for child in dialog.children() if child.class_name() == "Static")
                raise RuntimeError(f"MMD 启动时出现对话框: {dialogs[0].window_text()} {message}")
            time.sleep(.3)
        raise TimeoutError("MMD 主窗口未就绪")

    def _visible_dialogs(self):
        from pywinauto.controls.hwndwrapper import InvalidWindowHandle
        try:
            return [w for w in self.app.windows() if w.is_visible() and w.class_name() == "#32770"]
        except InvalidWindowHandle:
            # File dialogs replace HWNDs while pywinauto enumerates them.
            return []

    def dialog(self, title: str, timeout: float = 30):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("MMD 在等待对话框时退出")
            dialogs = self._visible_dialogs()
            match = next((w for w in dialogs if title in w.window_text()), None)
            if match:
                return match
            seen = [w.window_text() for w in dialogs]
            time.sleep(.2)
        raise TimeoutError(f"MMD 未出现预期对话框 {title}；当前窗口 {seen}")

    @staticmethod
    def _button(dialog, text: str):
        buttons = [w for w in dialog.children() if w.class_name() == "Button"
                   and text in w.window_text()]
        if len(buttons) != 1:
            raise RuntimeError(f"对话框 {dialog.window_text()} 未找到唯一按钮 {text}")
        button = buttons[0]
        if not ctypes.windll.user32.PostMessageW(dialog.handle, WM_COMMAND,
                                                  button.control_id(), button.handle):
            raise RuntimeError(f"对话框 {dialog.window_text()} 按钮 {text} 命令发送失败")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not ctypes.windll.user32.IsWindow(dialog.handle) or not ctypes.windll.user32.IsWindowVisible(dialog.handle):
                return
            time.sleep(.2)
        raise TimeoutError(f"对话框 {dialog.window_text()} 按钮 {text} 未关闭窗口")

    def _file(self, title: str, path: Path, action: str):
        dialog = self.dialog(title)
        edits = [w for w in dialog.children() if w.class_name() == "Edit"]
        if not edits:
            raise RuntimeError(f"{title} 没有文件名输入框")
        edits[0].set_edit_text(str(path))
        self._button(dialog, action)

    def _command(self, command: int):
        ids = set()

        def walk(items):
            for item in items:
                ids.add(item.get("item_id"))
                nested = item.get("menu_items")
                if isinstance(nested, dict):
                    walk(nested.get("menu_items", []))

        walk(self.main.menu_items())
        if command not in ids:
            raise RuntimeError(f"MMD 菜单缺少命令 {command}，版本可能不兼容")
        ctypes.windll.user32.PostMessageW(self.main.handle, WM_COMMAND, command, 0)

    def load_model(self, path: Path):
        buttons = [w for w in self.main.children() if w.class_name() == "Button"
                   and ctypes.windll.user32.GetDlgCtrlID(w.handle) == MODEL_LOAD_ID]
        if len(buttons) != 1:
            raise RuntimeError("MMD 模型加载控件与已验证版本不符")
        if not ctypes.windll.user32.PostMessageW(self.main.handle, WM_COMMAND,
                                                  MODEL_LOAD_ID, buttons[0].handle):
            raise RuntimeError("MMD 模型加载命令发送失败")
        self._file("打开文件", path, "打开")
        info = self.dialog("模型信息", timeout=90)
        self._button(info, "确定")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not self._visible_dialogs():
                return
            time.sleep(.2)
        raise TimeoutError("MMD 模型加载后未退出信息对话框")

    def load_motion(self, path: Path):
        self._command(MOTION_LOAD_ID)
        self._file("载入动作数据", path, "打开")
        confirm = self.dialog("動作数据", timeout=90)
        if not any("同骨骼名" in w.window_text() for w in confirm.children()):
            raise RuntimeError("MMD 动作确认内容与已验证版本不符")
        self._button(confirm, "确定")

    def start_avi(self, path: Path, end: int):
        self._command(AVI_EXPORT_ID)
        self._file("导出AVI", path, "保存")
        dialog = self.dialog("AVI导出设定")
        edits = {w.control_id(): w for w in dialog.children() if w.class_name() == "Edit"}
        if set(edits) != {609, 610, 611, 612, 613}:
            raise RuntimeError("AVI 设置窗口结构与已验证版本不符")
        if (edits[612].window_text().strip(), edits[613].window_text().strip()) != ("640", "360"):
            raise RuntimeError("AVI 导出尺寸不是预期的 640×360")
        for control_id, value in ((611, "30"), (609, "0"), (610, str(end))):
            edits[control_id].set_edit_text(value)
        codecs = [w for w in dialog.children() if w.class_name() == "ComboBox"]
        if len(codecs) != 1 or codecs[0].window_text() != "未圧縮":
            raise RuntimeError("AVI 编码器不是已验证的未压缩格式")
        self._button(dialog, "确定")


def cleanup_export_avi(folder: Path, attempt: dict) -> dict:
    """Remove only this successful attempt's generated capture, after MMD exits."""
    if not attempt.get('validated'):
        return {'status': 'preserved', 'reason': 'export_not_validated'}
    expected = folder.resolve() / 'capture.avi'
    result = attempt.get('avi_result', {})
    target = Path(result.get('path') or attempt.get('avi') or expected)
    cleanup = {'path': str(expected), 'status': 'pending'}
    try:
        if target.resolve() != expected:
            raise ValueError('AVI 清理路径不是本次任务的 capture.avi')
        if target.is_file():
            cleanup['bytes'] = target.stat().st_size
            target.unlink()
            cleanup.update(status='deleted', deleted_at=time.time())
        else:
            cleanup['status'] = 'already_absent'
        attempt.setdefault('avi_result', result).update(deleted=True,
            deleted_at=cleanup.get('deleted_at', time.time()))
    except (OSError, ValueError) as exc:
        cleanup.update(status='failed', error=str(exc))
    attempt['avi_cleanup'] = cleanup
    atomic_json(folder / 'attempt.json', attempt)
    return cleanup


def _attempt(job: Job, source: Path, prepared, end: int, label: str) -> tuple[Path, dict]:
    if (job.folder / "stop").exists():
        raise RuntimeError("用户停止了 MMDBridge 烘焙")
    folder = job.folder / f"mmd_{label}"
    receipt = folder / 'attempt.json'
    if receipt.is_file():
        saved = read_json(receipt)
        baked = Path(saved.get('vmd', {}).get('path', ''))
        if (saved.get('validated') and saved.get('end_frame') == end and baked.is_file()
                and baked.resolve().is_relative_to(folder.resolve())
                and file_hash(baked) == saved.get('vmd', {}).get('sha256')):
            report = validate_mode1(baked, prepared, end, full=label == 'full')
            report['avi_cleanup'] = cleanup_export_avi(folder, saved)
            job.status('mmd_render', f'复用已完成并校验的 MMDBridge {label} 烘焙')
            return baked, report
    if folder.exists():
        number = 1
        archived = folder.with_name(f'{folder.name}_interrupted_{number:02d}')
        while archived.exists():
            number += 1
            archived = folder.with_name(f'{folder.name}_interrupted_{number:02d}')
        if not archived.resolve().is_relative_to(job.folder.resolve()):
            raise ValueError('MMD 中断记录路径超出任务目录')
        folder.rename(archived)
    folder.mkdir()
    output = folder / "vmd"
    output.mkdir()
    progress = folder / "progress.json"
    executable = _runtime(source, folder / "runtime", end, output, progress)
    if (job.folder / "stop").exists():
        raise RuntimeError("用户停止了 MMDBridge 烘焙")
    required = int(640 * 360 * 4 * (end + 1) * 1.2) + 2_000_000_000
    if shutil.disk_usage(folder).free < required:
        raise ValueError(f"磁盘剩余空间不足，AVI 阶段至少需要 {required / 1e9:.1f} GB")
    avi = folder / "capture.avi"
    attempt = {"mode": "MMDBridge Mode 1", "label": label, "started_at": time.time(),
               "end_frame": end, "source_model": str(prepared.source_model),
               "source_motion": str(prepared.source_motion),
               "model": str(prepared.model), "motion": str(prepared.motion),
               "lipsync": str(prepared.lipsync or ""), "runtime": str(executable),
               "avi": str(avi)}
    atomic_json(folder / "attempt.json", attempt)
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
    attempt["mmd_pid"] = process.pid
    atomic_json(folder / "attempt.json", attempt)
    try:
        if (job.folder / 'stop').exists():
            raise RuntimeError('用户停止了 MMDBridge 烘焙')
        ui = MmdUi(process.pid, process)
        ui.load_model(prepared.model)
        ui.load_motion(prepared.motion)
        if prepared.lipsync:
            ui.load_motion(prepared.lipsync)
        job.status("mmd_render", f"MMDBridge {label} 导出 0/{end}")
        ui.start_avi(avi, end)
        started = time.monotonic()
        last_progress = started
        last_frame = -1
        while True:
            if (job.folder / "stop").exists():
                raise RuntimeError("用户停止了 MMDBridge 烘焙")
            if process.poll() is not None:
                raise RuntimeError(f"MMD 导出期间退出，代码 {process.returncode}")
            dialogs = ui._visible_dialogs()
            if dialogs:
                raise RuntimeError(f"MMD 导出期间出现未知对话框: {[w.window_text() for w in dialogs]}")
            if progress.exists():
                try:
                    state = read_json(progress)
                except (OSError, ValueError):
                    state = {}
                frame = int(state.get("frame", -1))
                if frame > last_frame:
                    last_frame, last_progress = frame, time.monotonic()
                    job.status("mmd_render", f"MMDBridge {label} 导出 {frame}/{end}")
                if state.get("completed") and frame == end:
                    break
            if time.monotonic() - last_progress > 600 or time.monotonic() - started > 4 * 3600:
                raise TimeoutError(f"MMDBridge {label} 导出超时，最后帧 {last_frame}/{end}")
            time.sleep(.5)
        files = list(output.glob("*.vmd"))
        if len(files) != 1 or not avi.is_file() or avi.stat().st_size < 1024:
            raise ValueError(f"MMDBridge {label} 未生成唯一 VMD 和有效 AVI")
        report = validate_mode1(files[0], prepared, end, full=label == "full")
        atomic_json(folder / "validation.json", report)
        stat = files[0].stat()
        if stat.st_ctime < attempt["started_at"] - 2:
            raise ValueError("烘焙 VMD 创建时间早于本次 MMD 进程")
        avi_stat = avi.stat()
        attempt.update({"validated": True, "completed_at": time.time(),
                        "avi_result": {"path": str(avi.resolve()), "bytes": avi_stat.st_size,
                                       "created_at": avi_stat.st_ctime,
                                       "modified_at": avi_stat.st_mtime},
                        "vmd": {"path": str(files[0].resolve()), "bytes": stat.st_size,
                                "created_at": stat.st_ctime, "modified_at": stat.st_mtime,
                                "sha256": file_hash(files[0])}, "validation": report})
        atomic_json(folder / "attempt.json", attempt)
        return files[0], report
    except Exception as exc:
        attempt.update({"validated": False, "failed_at": time.time(), "error": str(exc)})
        atomic_json(folder / "attempt.json", attempt)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        if attempt.get('validated'):
            report['avi_cleanup'] = cleanup_export_avi(folder, attempt)
            if report['avi_cleanup']['status'] == 'failed':
                job.status('mmd_render', f'MMDBridge {label} 已完成；临时 AVI 清理失败：'
                           + report['avi_cleanup']['error'])


def run_bake(job: Job) -> None:
    """Background-thread entry point; success hands effective inputs to Blender."""
    try:
        cfg = job.data["settings"]
        job.status("mmd_prepare", "检查骨名、显示枠和动作")
        checkpoint = job.folder / 'mmd_prepared.json'
        if checkpoint.is_file():
            saved = read_json(checkpoint)
            if any(not Path(path).is_file() or file_hash(path) != digest for path, digest in saved['hashes'].items()):
                raise ValueError('MMD 输入还原点已改变，请新建任务')
            values = saved['prepared']
            for name in ('source_model', 'source_motion', 'model', 'motion', 'lipsync', 'name_map'):
                values[name] = Path(values[name]) if values[name] else None
            values['frame_range'] = tuple(values['frame_range'])
            values['expected_bones'] = tuple(values['expected_bones'])
            prepared = PreparedCharacterV2(**values)
        else:
            # An interrupted copy is not a completed input checkpoint.
            input_dir = job.folder / 'input'
            if input_dir.exists():
                number = 1
                archived = job.folder / f'input_interrupted_{number:02d}'
                while archived.exists():
                    number += 1
                    archived = job.folder / f'input_interrupted_{number:02d}'
                if not archived.resolve().is_relative_to(job.folder.resolve()):
                    raise ValueError('MMD 输入路径超出任务目录')
                input_dir.rename(archived)
            prepared = prepare_character_v2(cfg["model"], cfg["motion"],
                                            cfg.get("lipsync") or None, job.folder)
            values = {name: str(value) if isinstance(value, Path) else value for name, value in asdict(prepared).items()}
            paths = [prepared.model, prepared.motion, prepared.name_map] + ([prepared.lipsync] if prepared.lipsync else [])
            atomic_json(checkpoint, {'prepared': values, 'hashes': {str(path): file_hash(path) for path in paths}})
        if prepared.frame_range[0] != 0:
            raise ValueError("当前 Mode 1 自动烘焙要求动作从第 0 帧开始")
        if prepared.requires_mmd_bake:
            source = Path(cfg["mmdbridge_path"]).resolve()
            lip_end = scan_vmd(prepared.lipsync)["morph_max"] if prepared.lipsync else None
            end = max(prepared.frame_range[1], lip_end or 0)
            short_end = min(60, end)
            if short_end < 1:
                raise ValueError("动作至少需要 2 帧才能验证 MMDBridge")
            _attempt(job, source, prepared, short_end, "short")
            baked, report = _attempt(job, source, prepared, end, "full")
            atomic_json(job.folder / "validation.json", report)
            write_effective_inputs(job.folder, source_model=str(prepared.source_model),
                                   source_vmd=str(prepared.source_motion),
                                   effective_model=str(prepared.model),
                                   effective_motion=str(baked),
                                   repaired=bool(prepared.repaired_bone_count or prepared.renamed_bone_count),
                                   expected_bone_count=len(prepared.expected_bones),
                                   effective_lipsync="")
        else:
            write_effective_inputs(job.folder, source_model=str(prepared.source_model),
                                   source_vmd=str(prepared.source_motion),
                                   effective_model=str(prepared.model),
                                   effective_motion=str(prepared.motion),
                                   repaired=bool(prepared.repaired_bone_count or prepared.renamed_bone_count),
                                   mmm_skipped=True, mmm_skipped_reason="zero_physics_bones",
                                   expected_bone_count=0,
                                   effective_lipsync=str(prepared.lipsync or ""))
        job.status("mmd_bake_done", "MMDBridge 烘焙与校验完成，等待 Blender")
    except Exception:
        if (job.folder / "stop").exists():
            job.status("stopped", "MMDBridge 任务已停止")
        else:
            job.status("failed", traceback.format_exc())
