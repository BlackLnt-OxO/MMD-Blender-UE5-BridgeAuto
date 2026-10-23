"""Run the installed MMDBridge Mode 1 exporter in an isolated MMD process."""
from __future__ import annotations

import ctypes
import hashlib
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
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import Thread

from .core import Job, Settings, atomic_json, file_hash, read_json, stable_id
from .effective_inputs import write_effective_inputs
from .plan_v2_prep import prepare_character_v2, PreparedCharacterV2
from .pmx_reader import read_pmx
from .vmd_reader import decode_name_field
from .export_quality import check_quality


MMD_EXE = "MikuMikuDance_CHS.exe"
SCRIPT = "mmdbridge_vmd_mode1.py"
WM_COMMAND = 0x111
MODEL_LOAD_ID = 435
MOTION_LOAD_ID = 209
AVI_EXPORT_ID = 223
MMD_EXIT_ID = 200
CBT_DLL_SHA256 = '74f27cee28b0060d5b79b73b66fa665c3500eb19aa65e1baacc9f97c54e637a3'
CBT_MODULE_OFFSET = 0x59672
CBT_MODULE_LOAD = bytes.fromhex('4c8b05f7950600')
CBT_LOCAL_MODULE = bytes.fromhex('4533c090909090')


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
                  "active_morph_tracks": Counter(),
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
                        weight = struct.unpack_from("<f", data, pos + name_size + 4)[0]
                        if weight != 0:
                            result["active_morph_tracks"][name] += 1
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


def constant_physics_gaps(path: Path, names: list[str], end: int) -> dict:
    """Accept only exact constant intervals removed by Mode 1's postprocessor.

    Upstream src/d3d9/vmd.cpp PostProcessKeyframes removes equal middle
    poses with a zero threshold. A moving gap, duplicate, or missing endpoint
    remains a validation error; the original VMD is never rewritten.
    """
    tracks = {name: {} for name in names}
    slots = {}
    with Path(path).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        offset = 50 if b'0002' in data[:30] else 40
        count = struct.unpack_from('<I', data, offset)[0]
        for pos in range(offset + 4, offset + 4 + count * 111, 111):
            raw = data[pos:pos + 15]
            if raw not in slots:
                name = decode_name_field(raw).name
                slots[raw] = name if name in tracks else None
            name = slots[raw]
            if name is None:
                continue
            frame = struct.unpack_from('<I', data, pos + 15)[0]
            if frame in tracks[name]:
                raise ValueError(f'物理骨轨道存在重复帧: {name}:{frame}')
            tracks[name][frame] = data[pos + 19:pos + 47]
    gaps = {}
    for name, poses in tracks.items():
        frames = sorted(poses)
        if not frames or frames[0] != 0 or frames[-1] != end:
            raise ValueError(f'物理骨轨道缺少范围端点: {name}')
        intervals = []
        for left, right in zip(frames, frames[1:]):
            if right == left + 1:
                continue
            if poses[left] != poses[right]:
                raise ValueError(f'物理骨轨道未逐帧烘焙，缺帧区间姿态变化: {name}:{left}..{right}')
            intervals.append([left, right])
        gaps[name] = intervals
    return gaps


def validate_mode1(path: Path, prepared, end: int, *, full: bool, policy='strict') -> dict:
    result = scan_vmd(path)  # Broken/truncated files remain fatal.
    issues = []
    check_quality(result["bone_min"] == 0 and result["bone_max"] == end,
                  f"烘焙帧范围不完整: {result['bone_min']}..{result['bone_max']}，应为 0..{end}", issues, policy)
    expected = set(prepared.expected_bones)
    missing = expected - result["bone_tracks"].keys()
    check_quality(not missing, f"烘焙缺少 {len(missing)} 条物理骨轨道: {sorted(missing)[:8]}", issues, policy)
    sparse = sorted(name for name in expected - missing if result["bone_tracks"][name] != end + 1)
    constant_gaps = {}
    if sparse:
        try:
            constant_gaps = constant_physics_gaps(path, sparse, end)
        except ValueError as exc:
            check_quality(False, str(exc), issues, policy)
    source = scan_vmd(prepared.motion)
    if full:
        model = read_pmx(prepared.model)
        model_bones = {bone.name_jp for bone in model.bones}
        usable_source = set(source["bone_tracks"]) & model_bones
        retained = usable_source & result["bone_tracks"].keys()
        # Mode 1 intentionally omits IK/control tracks, while some source
        # tracks contain only keys outside a short probe's range.
        if usable_source and len(retained) < int(len(usable_source) * .9):
            check_quality(False, f"烘焙只保留 {len(retained)}/{len(usable_source)} 条原动作模型轨道", issues, policy)
    omitted_zero_morphs = []
    unmatched_morphs = []
    if full and prepared.lipsync:
        lip = scan_vmd(prepared.lipsync)
        # Mode 1 omits tracks that stay at zero throughout the source motion.
        requested = set(lip["active_morph_tracks"])
        model_morphs = getattr(model, 'morph_names', None)
        if model_morphs is not None:
            unmatched_morphs = sorted(requested - set(model_morphs))
            requested &= set(model_morphs)
        missing_morphs = requested - result["morph_tracks"].keys()
        if missing_morphs:
            check_quality(False, f"烘焙丢失口型轨道: {sorted(missing_morphs)[:8]}", issues, policy)
        omitted_zero_morphs = sorted(set(lip["morph_tracks"]) - set(lip['active_morph_tracks'])
                                     - result["morph_tracks"].keys())
    return {"path": str(path), "bytes": result["bytes"],
            "counts": result["counts"], "bone_range": [result["bone_min"], result["bone_max"]],
            "bone_track_count": len(result["bone_tracks"]),
            "physics_tracks_found": len(expected - missing),
            "validation_policy": policy, "quality_issues": issues,
            "quality_status": "needs_review" if issues else "passed",
            "constant_physics_intervals": constant_gaps,
            "morph_track_count": len(result["morph_tracks"]),
            "lipsync_zero_tracks_omitted": omitted_zero_morphs,
            "unmatched_lipsync_morphs": unmatched_morphs}


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
    return configure_runtime(source, target, end, output, progress)


def runtime_hook_dll(source):
    """Correct only the verified upstream build's current-thread CBT hMod argument."""
    data = (source / 'd3d9.dll').read_bytes()
    # Current-process thread hooks require hMod=NULL (SetWindowsHookExW documentation).
    # shortcut: other upstream builds stay untouched; add a verified fix if they need it.
    if hashlib.sha256(data).hexdigest() != CBT_DLL_SHA256:
        return data
    if data[CBT_MODULE_OFFSET:CBT_MODULE_OFFSET + len(CBT_MODULE_LOAD)] != CBT_MODULE_LOAD:
        raise ValueError('MMDBridge CBT 修复位置与已验证版本不符')
    return data[:CBT_MODULE_OFFSET] + CBT_LOCAL_MODULE + data[CBT_MODULE_OFFSET + len(CBT_MODULE_LOAD):]


def configure_runtime(source, target, end, output, progress):
    if source.resolve() == target.resolve():
        raise ValueError('Runtime 配置不能修改上游原安装目录')
    dll = target / 'd3d9.dll'
    fixed = runtime_hook_dll(source)
    if dll.read_bytes() != fixed:
        dll.write_bytes(fixed)
    ini = target / "mmdbridge_settings" / f"{MMD_EXE}.ini"
    config = (source / 'mmdbridge_settings' / ini.name).read_text(encoding='utf-16')
    for key, value in (("ScriptName", SCRIPT), ("ScriptCallSetting", "1"),
                       ("StartFrame", "0"), ("EndFrame", str(end)), ("ExportFPS", "30")):
        config, count = re.subn(rf"(?m)^{key}=.*$", f"{key}={value}", config)
        if count != 1:
            raise ValueError(f"MMDBridge 配置缺少唯一的 {key}")
    ini.write_text(config, encoding="utf-16")

    script = target / "mmdbridge_scripts" / SCRIPT
    code = (source / 'mmdbridge_scripts' / SCRIPT).read_text(encoding='utf-8')
    old = 'from mmdbridge_vmd import *'
    if code.count(old) != 1 or code.count('dst_dir = "out"') != 1:
        raise ValueError("Mode 1 脚本版本与自动化适配器不匹配")
    code = code.replace(old, old + "\nmessagebox = lambda *args, **kwargs: None", 1)
    code = code.replace('dst_dir = "out"', f"dst_dir = {str(output)!r}", 1)
    code += (
        "\nif framenumber == start_frame:\n"
        "    import json as _json\n"
        f"    with open({str(progress.with_name('render_started.json'))!r}, 'w', encoding='utf-8') as _stream:\n"
        "        _json.dump({'frame': framenumber, 'end': end_frame, 'pid': os.getpid()}, _stream)\n"
        "\nif framenumber == start_frame or framenumber == end_frame or framenumber % 100 == 0:\n"
        f"    _path = {str(progress)!r}\n"
        "    import json as _json\n"
        "    with open(_path + '.tmp', 'w', encoding='utf-8') as _stream:\n"
        "        _json.dump({'frame': framenumber, 'end': end_frame, "
        "'completed': framenumber == end_frame}, _stream)\n"
        "    for _retry in range(20):\n"
        "        try:\n"
        "            os.replace(_path + '.tmp', _path)\n"
        "            break\n"
        "        except PermissionError:\n"
        "            if _retry == 19:\n"
        "                raise\n"
        "            time.sleep(.05)\n"
    )
    script.write_text(code, encoding="utf-8")
    return target / MMD_EXE


@contextmanager
def reusable_runtime(job, source, end, output, progress):
    """Reuse one verified installation copy, exclusively until its MMD process exits."""
    missing = missing_runtime_files(source)
    if missing:
        raise ValueError(f'MMDBridge 安装目录缺少 {missing[0]}')
    names = (MMD_EXE, 'd3d9.dll', 'python312.dll', f'mmdbridge_scripts/{SCRIPT}', f'mmdbridge_settings/{MMD_EXE}.ini')
    hashes = {name: file_hash(source / name) for name in names}
    runtime_hashes = {name: hashes[name] for name in names[:3]}
    runtime_hashes['d3d9.dll'] = hashlib.sha256(runtime_hook_dll(source)).hexdigest()
    home = Path(os.environ.get('MMDBRIDGE_HOME') or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MMDBridge')
    source_ini = (source / names[-1]).read_text(encoding='utf-16')
    identity_ini = re.sub(r'(?m)^(ScriptName|ScriptCallSetting|StartFrame|EndFrame|ExportFPS)=.*$', '', source_ini)
    cache = home / 'mmd_runtime' / stable_id(str(source).casefold(), *list(hashes.values())[:-1], identity_ini)
    if not cache.resolve().is_relative_to((home / 'mmd_runtime').resolve()):
        raise ValueError('Runtime 缓存路径超出缓存目录')
    cache.mkdir(parents=True, exist_ok=True)
    runtime = cache / 'runtime'
    marker = cache / 'source.json'
    with (cache / 'runtime.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        while True:
            if (job.folder / 'stop').exists():
                raise RuntimeError('用户停止了 MMDBridge 烘焙')
            try:
                acquire()
                break
            except OSError:
                time.sleep(.2)
        try:
            if runtime.resolve().parent != cache.resolve():
                raise ValueError('Runtime 缓存路径超出缓存目录')
            if runtime.exists():
                if not marker.is_file() or read_json(marker).get('source') != str(source):
                    raise ValueError('Runtime 缓存缺少所属来源记录，拒绝覆盖')
                if any(not (runtime / name).is_file() or file_hash(runtime / name) != runtime_hashes[name]
                       for name in names[:3]):
                    shutil.rmtree(runtime)
            atomic_json(marker, {'source': str(source), 'hashes': hashes, 'runtime_hashes': runtime_hashes})
            if runtime.exists():
                configure_runtime(source, runtime, end, output, progress)
            else:
                _runtime(source, runtime, end, output, progress)
            yield runtime / MMD_EXE
        finally:
            (cache / 'active.json').unlink(missing_ok=True)
            release()


class MotionLoadRace(RuntimeError):
    """A known load confirmation crossed into the AVI transaction; discard this attempt."""


def is_motion_confirmation(dialog):
    return ('動作数据' in dialog.window_text() and
            any('同骨骼名' in child.window_text() for child in dialog.children()))


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
            if title in ('导出AVI', 'AVI导出设定') and any(is_motion_confirmation(w) for w in dialogs):
                raise MotionLoadRace('MMD 动作确认迟于加载完成回执，拒绝继续本次 AVI 导出')
            match = next((w for w in dialogs if title in w.window_text()), None)
            if match:
                return match
            seen = [w.window_text() for w in dialogs]
            time.sleep(.2)
        raise TimeoutError(f"MMD 未出现预期对话框 {title}；当前窗口 {seen}")

    def _button(self, dialog, text: str):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            buttons = [w for w in dialog.children() if w.class_name() == 'Button'
                       and text in w.window_text()]
            if len(buttons) == 1:
                break
            if self.process.poll() is not None:
                raise RuntimeError(f'MMD 等待按钮时退出: {self.process.returncode}')
            time.sleep(.1)
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
        deadline = time.monotonic() + 30
        filename_ids = (1001, 1148, 1152) if action == '保存' else (1148, 1152)
        while time.monotonic() < deadline:
            edits = [w for w in dialog.children() if w.class_name() == 'Edit' and w.control_id() in filename_ids]
            if len(edits) == 1:
                break
            if self.process.poll() is not None:
                raise RuntimeError(f'MMD 等待文件输入框时退出: {self.process.returncode}')
            time.sleep(.1)
        if not edits:
            raise RuntimeError(f"{title} 没有文件名输入框")
        edits[0].set_edit_text(str(path))
        self._button(dialog, action)

    def _command(self, command: int, *, wait: bool = False):
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
        if wait:
            import win32gui
            # Wait for this handler, not a WM_NULL that can run inside its modal loop.
            win32gui.SendMessageTimeout(self.main.handle, WM_COMMAND, command, 0, 33, 90000)
        elif not ctypes.windll.user32.PostMessageW(self.main.handle, WM_COMMAND, command, 0):
            raise RuntimeError(f'MMD 菜单命令发送失败: {command}')

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
        completion = Future()

        def send():
            try:
                completion.set_result(self._command(MOTION_LOAD_ID, wait=True))
            except Exception as exc:
                completion.set_exception(exc)

        # The native handler owns both file/confirmation dialogs and the actual load.
        Thread(target=send, name='MMD-motion-command', daemon=True).start()
        self._file("载入动作数据", path, "打开")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'MMD 加载动作时退出: {self.process.returncode}')
            dialogs = self._visible_dialogs()
            if dialogs:
                if len(dialogs) != 1 or not is_motion_confirmation(dialogs[0]):
                    raise RuntimeError('MMD 动作确认内容与已验证版本不符')
                self._button(dialogs[0], '确定')
                continue
            if completion.done():
                completion.result()
                if self.main.is_enabled() and not self._visible_dialogs():
                    return
            time.sleep(.05)
        raise TimeoutError('MMD 动作加载未完成')

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
    if attempt.get('runtime_reusable'):
        attempt['runtime_cleanup'] = {'path': attempt['runtime'], 'status': 'retained_for_reuse'}
    elif attempt.get('runtime'):
        runtime = folder / 'runtime'
        runtime_cleanup = {'path': str(runtime), 'status': 'already_absent'}
        try:
            if (runtime.resolve().parent != folder.resolve() or
                    Path(attempt['runtime']).resolve() != runtime.resolve() / MMD_EXE):
                raise ValueError('Runtime 清理路径不是本次任务的临时副本')
            if runtime.exists():
                shutil.rmtree(runtime)
                runtime_cleanup.update(status='deleted', deleted_at=time.time())
        except (OSError, ValueError) as exc:
            runtime_cleanup.update(status='failed', error=str(exc))
        attempt['runtime_cleanup'] = runtime_cleanup
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
            report = validate_mode1(baked, prepared, end, full=label == 'full',
                                    policy=job.data['settings'].get('export_validation_policy', 'strict'))
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
    with reusable_runtime(job, source, end, output, progress) as executable:
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
                   "avi": str(avi), "runtime_reusable": True}
        atomic_json(folder / "attempt.json", attempt)
        process = ui = None
        try:
            if (job.folder / 'stop').exists():
                raise RuntimeError('用户停止了 MMDBridge 烘焙')
            process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
            attempt['mmd_pid'] = process.pid
            atomic_json(executable.parent.parent / 'active.json', {'job': str(job.folder), 'pid': process.pid})
            atomic_json(folder / 'attempt.json', attempt)
            ui = MmdUi(process.pid, process)
            job.status('mmd_prepare', 'MMD 正在载入角色模型')
            ui.load_model(prepared.model)
            job.status('mmd_prepare', 'MMD 正在应用角色动作，等待原生窗口就绪')
            ui.load_motion(prepared.motion)
            if prepared.lipsync:
                job.status('mmd_prepare', 'MMD 正在应用角色表情，等待原生窗口就绪')
                ui.load_motion(prepared.lipsync)
            job.status('mmd_prepare', 'MMD 动作已就绪，准备 AVI 导出')
            ui.start_avi(avi, end)
            job.status('mmd_prepare', 'AVI 已提交，等待 MMDBridge 第 0 帧回执')
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
                    details = [{'title': w.window_text(),
                                'text': '\n'.join(child.window_text() for child in w.children()
                                                  if child.window_text())} for w in dialogs]
                    atomic_json(folder / 'error_dialogs.json', details)
                    if any(is_motion_confirmation(w) for w in dialogs):
                        raise MotionLoadRace(f'MMD 导出阶段收到迟到的动作确认: {details}')
                    raise RuntimeError(f"MMD 导出期间出现对话框: {details}")
                if 'render_started_at' not in attempt:
                    try:
                        ack = read_json(folder / 'render_started.json')
                    except (OSError, ValueError):
                        ack = {}
                    if ack:
                        if ack != {'frame': 0, 'end': end, 'pid': process.pid}:
                            raise ValueError(f'MMDBridge 首帧回执与本次导出不符: {ack}')
                        attempt['render_started_at'] = time.time()
                        atomic_json(folder / 'attempt.json', attempt)
                    elif time.monotonic() - started > 90:
                        raise TimeoutError('MMDBridge 未收到本次导出的第 0 帧回执')
                    else:
                        time.sleep(.05)
                        continue
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
            report = validate_mode1(files[0], prepared, end, full=label == "full",
                                    policy=job.data["settings"].get("export_validation_policy", "strict"))
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
            if ui is not None:
                try:
                    dialogs = ui._visible_dialogs()
                    if dialogs:
                        atomic_json(folder / 'error_dialogs.json', [
                            {'title': w.window_text(), 'text': '\n'.join(c.window_text() for c in w.children())}
                            for w in dialogs])
                except Exception as capture_error:
                    attempt['dialog_capture_error'] = str(capture_error)
            attempt.update({"validated": False, "failed_at": time.time(), "error": str(exc)})
            atomic_json(folder / "attempt.json", attempt)
            raise
        finally:
            if process is not None and process.poll() is None:
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


def prepare_job(job):
    cfg = job.data["settings"]
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
    return prepared


def run_multi_bake(job):
    cfg = job.data['settings']
    data = job.data
    entries = data.get('character_jobs')
    if not entries:
        entries = []
        sources = [{key: cfg.get(key, '') for key in ('model', 'motion', 'lipsync')}] + cfg['additional_characters']
        for index, character in enumerate(sources, 1):
            folder = job.folder / 'characters'
            folder.mkdir(parents=True, exist_ok=True)
            with_camera = index == 1 and bool(cfg.get('camera'))
            settings = replace(Settings(**cfg), additional_characters=[], flow='auto' if with_camera else 'character',
                               scene=cfg['scene'] if with_camera else '', camera=cfg['camera'] if with_camera else '',
                               working_blend='', project='', unreal='',
                               character_output_dir=str(folder), **character)
            child = Job.create(folder, settings, name=f'C_{index:04d}')
            child_data = child.data
            child_data['settings']['character_output_dir'] = str(child.folder)
            atomic_json(child.path, child_data)
            entries.append(str(child.folder))
        data['character_jobs'] = entries
        atomic_json(job.path, data)
    if len(entries) != len(cfg['additional_characters']) + 1:
        raise ValueError('多角色任务数量与配置不一致')
    end = max([data['vmds'].get('camera', {}).get('max_frame', 0)] + [
        max(value['max_frame'] for value in Job(folder).data['vmds'].values()) for folder in entries])
    required = int(640 * 360 * 4 * (end + 1) * 1.2) + 2_000_000_000
    if shutil.disk_usage(job.folder).free < required:
        raise ValueError(f'AVI 临时空间不足，至少需要 {required / 1e9:.1f} GB')
    children = []
    for folder in entries:
        if (job.folder / 'stop').exists():
            raise RuntimeError('多角色烘焙已停止')
        child = Job(folder)
        if not child.folder.is_relative_to(job.folder / 'characters'):
            raise ValueError('角色任务目录超出本任务')
        child.verify_inputs()
        (child.folder / 'stop').unlink(missing_ok=True)
        child_data = child.data
        child_data['shared_bake_end'] = end
        atomic_json(child.path, child_data)
        children.append(child)
    for index, child in enumerate(children, 1):
        if (job.folder / 'stop').exists():
            raise RuntimeError('多角色烘焙已停止')
        job.status('mmd_prepare', f'MMDBridge 依次烘焙角色 {index}/{len(children)}: {Path(child.data["settings"]["model"]).stem}')
        run_bake(child)
        status = read_json(child.folder / 'status.json')
        if status.get('state') != 'mmd_bake_done':
            raise RuntimeError(f'角色 {index}: {status.get("message", "烘焙未完成")}')
    job.status('mmd_bake_done', f'{len(entries)} 个角色独立烘焙与校验完成，等待 Blender')


def run_bake(job: Job) -> None:
    """Background-thread entry point; success hands effective inputs to Blender."""
    try:
        cfg = job.data["settings"]
        if cfg.get("additional_characters"):
            return run_multi_bake(job)
        job.status("mmd_prepare", "检查骨名、显示枠和动作")
        prepared = prepare_job(job)
        if prepared.frame_range[0] != 0:
            raise ValueError("当前 Mode 1 自动烘焙要求动作从第 0 帧开始")
        if prepared.requires_mmd_bake:
            source = Path(cfg["mmdbridge_path"]).resolve()
            lip_end = scan_vmd(prepared.lipsync)["morph_max"] if prepared.lipsync else None
            end = max(prepared.frame_range[1], lip_end or 0, job.data.get("shared_bake_end", 0))
            if end < 1:
                raise ValueError("动作至少需要 2 帧才能验证 MMDBridge")
            for retry in range(2):
                try:
                    baked, report = _attempt(job, source, prepared, end, "full")
                    break
                except MotionLoadRace:
                    if retry:
                        raise
                    job.status('warning', '检测到迟到的动作确认；保留失败现场，从干净进程重试本角色一次',
                               severity='warning')
            atomic_json(job.folder / "validation.json", report)
            for issue in report.get('quality_issues', []):
                job.status('warning', '烘焙结果已继续，需复核：' + issue, severity='warning')
            if report.get('unmatched_lipsync_morphs'):
                job.status('warning', '口型/表情中有 PMX 不存在的轨道，已记录而未映射到其它表情：' +
                           ', '.join(report['unmatched_lipsync_morphs']), severity='warning')
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
