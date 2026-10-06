"""MMM 安装检测、Runtime 隔离与插件安装 — 方案 §8 / §9 / §13 / §14 / §40。

MMM 是免安装软件，**不写注册表**，因此没有可靠的"官方发现方式"。
本模块的策略分两层：

1. **手动指定为主** —— 方案 §8 明确「不要花大量成本做复杂自动发现」。
   用户在 GUI 里选一次目录，之后持久化。
2. **轻量启发式检测为辅** —— 只在若干常见根目录做**有深度上限**的扫描，
   用于首次配置时给用户几个候选，而不是替代手动指定。

版本判定读 `MikuMikuMoving64.exe` 的 **PE Version Resource**，
绝不根据目录名（`MikuMikuMoving64_v1292`）判断 —— 用户可任意重命名。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "MMM_NOT_FOUND",
    "MMM_RUNTIME_BUSY",
    "MMM_SDK_MISSING",
    "MMM_VERSION_SUPPORTED",
    "MMM_VERSION_UNSUPPORTED",
    "UI_LANGUAGE",
    "PLUGIN_DLL_NAME",
    "LOCK_FILE_NAME",
    "MmmInstall",
    "MmmRuntimeError",
    "RuntimeLock",
    "acquire_lock",
    "baseline_dir",
    "clear_stale_lock",
    "discover_mmm",
    "force_ui_language",
    "install_plugin",
    "lock_path",
    "lock_status",
    "plugin_status",
    "prepare_runtime",
    "read_lock",
    "read_pe_version",
    "release_lock",
    "restore_baseline",
    "runtime_dir",
    "runtime_root",
    "runtime_status",
    "uninstall_plugin",
    "validate_mmm_dir",
]

MMM_VERSION_SUPPORTED = "1.2.9.2"

PLUGIN_DLL_NAME = "MMDBridgeMMMPlugin.dll"

MMM_NOT_FOUND = "MMM_NOT_FOUND"
MMM_VERSION_UNSUPPORTED = "MMM_VERSION_UNSUPPORTED"
MMM_SDK_MISSING = "MMM_SDK_MISSING"

#: MMM 会写回这些文件；隔离 Runtime 需要它们作为 baseline（方案 §9）。
BASELINE_FILES = ("Settings.xml", "DockLayout.xml", "CommandBarLayout.xml")

#: 检测时最多向下几层。MMM 通常直接在某个盘的浅层目录里。
_DISCOVER_MAX_DEPTH = 3
_DISCOVER_TIME_BUDGET = 20.0


class MmmRuntimeError(RuntimeError):
    """MMM 配置或 Runtime 操作失败。携带稳定 ``code``。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# PE 版本资源（方案 §13）
# --------------------------------------------------------------------------

def read_pe_version(path: str | Path) -> dict[str, str]:
    """读取 Windows PE 版本资源。

    返回 ``{"FileVersion": ..., "ProductName": ..., "CompanyName": ...}``；
    读不到时返回空 dict（绝不抛异常）。
    """
    if os.name != "nt":
        return {}
    target = Path(path)
    if not target.is_file():
        return {}

    try:
        version = ctypes.WinDLL("version")
    except OSError:
        return {}

    try:
        size = version.GetFileVersionInfoSizeW(ctypes.c_wchar_p(str(target)), None)
        if not size:
            return {}
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(ctypes.c_wchar_p(str(target)), 0,
                                           size, buffer):
            return {}

        result: dict[str, str] = {}
        for lang, codepage in _translations(version, buffer) or [(0x0409, 0x04B0)]:
            prefix = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\"
            for key in ("FileVersion", "ProductVersion", "ProductName",
                        "CompanyName", "FileDescription"):
                value = _query_string(version, buffer, prefix + key)
                if value:
                    result.setdefault(key, value)
        return result
    except (OSError, ValueError):
        return {}


def _translations(version, buffer) -> list[tuple[int, int]]:
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    ok = version.VerQueryValueW(buffer,
                                ctypes.c_wchar_p("\\VarFileInfo\\Translation"),
                                ctypes.byref(pointer), ctypes.byref(length))
    if not ok or not pointer:
        return []
    count = length.value // 4
    array = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
    pairs = []
    for index in range(count):
        pairs.append((array[index * 2], array[index * 2 + 1]))
    return pairs


def _query_string(version, buffer, subblock: str) -> str:
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    ok = version.VerQueryValueW(buffer, ctypes.c_wchar_p(subblock),
                                ctypes.byref(pointer), ctypes.byref(length))
    if not ok or not pointer or not length.value:
        return ""
    return ctypes.wstring_at(pointer, length.value).strip("\x00").strip()


# --------------------------------------------------------------------------
# 安装校验
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MmmInstall:
    """一个可用的 MMM 安装。"""

    directory: Path
    executable: Path
    file_version: str = ""
    product_name: str = ""
    company_name: str = ""
    source: str = "手动"

    @property
    def plugin_dir(self) -> Path:
        return self.directory / "Plugins"

    @property
    def sdk_dll(self) -> Path:
        return self.directory / "System" / "x64" / "MikuMikuPlugin.dll"

    @property
    def supported(self) -> bool:
        return self.file_version == MMM_VERSION_SUPPORTED

    def summary(self) -> str:
        mark = "受支持" if self.supported else "未验证版本"
        return (f"{self.executable.name} · {self.file_version or '版本未知'} "
                f"({mark}) · {self.directory}")


def _find_executable(directory: Path) -> Path | None:
    for name in ("MikuMikuMoving64.exe", "MikuMikuMoving.exe"):
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def validate_mmm_dir(path: str | Path, *, source: str = "手动",
                     require_supported: bool = False) -> MmmInstall:
    """校验一个目录是否是 MMM 安装目录。

    Raises:
        MmmRuntimeError: ``MMM_NOT_FOUND``（exe 不存在）、
            ``MMM_VERSION_UNSUPPORTED``（版本不符且 ``require_supported``）、
            ``MMM_SDK_MISSING``（缺 ``System/x64/MikuMikuPlugin.dll``）。
    """
    directory = Path(path)
    if not directory.is_dir():
        raise MmmRuntimeError(MMM_NOT_FOUND, f"目录不存在：{directory}")

    executable = _find_executable(directory)
    if executable is None:
        raise MmmRuntimeError(
            MMM_NOT_FOUND,
            f"该目录下找不到 MikuMikuMoving64.exe：{directory}")

    info = read_pe_version(executable)
    install = MmmInstall(
        directory=directory,
        executable=executable,
        file_version=info.get("FileVersion", ""),
        product_name=info.get("ProductName", ""),
        company_name=info.get("CompanyName", ""),
        source=source)

    # 防呆：防止用户指到别的程序上。
    if install.product_name and "MikuMikuMoving" not in install.product_name:
        raise MmmRuntimeError(
            MMM_NOT_FOUND,
            f"{executable.name} 的 ProductName 是 {install.product_name!r}，"
            "不是 MikuMikuMoving。请选择正确的 MMM 目录。")

    if not install.sdk_dll.is_file():
        raise MmmRuntimeError(
            MMM_SDK_MISSING,
            f"缺少 SDK：{install.sdk_dll}\n"
            "该目录可能不是完整的 MMM 安装。")

    if require_supported and install.file_version != MMM_VERSION_SUPPORTED:
        raise MmmRuntimeError(
            MMM_VERSION_UNSUPPORTED,
            f"当前只支持 MMM {MMM_VERSION_SUPPORTED}，检测到 "
            f"{install.file_version or '未知版本'}（{executable}）")

    return install


# --------------------------------------------------------------------------
# 轻量检测（方案 §8：不追求复杂自动发现）
# --------------------------------------------------------------------------

def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    # 便携软件常见于非系统盘根目录。
    for letter in "DEFGH":
        root = Path(f"{letter}:/")
        if root.is_dir():
            roots.append(root)
    home = Path.home()
    for name in ("Desktop", "Downloads", "Documents"):
        candidate = home / name
        if candidate.is_dir():
            roots.append(candidate)
    seen: set[str] = set()
    unique = []
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


#: 目录名里出现这些词才继续向下搜。没有这个限制，depth=3 会变成整盘遍历。
_MMM_NAME_HINTS = ("miku", "mmm", "moving", "dance", "mmd", "初音")


def _has_mmm_hint(directory: Path) -> bool:
    name = directory.name.casefold()
    return any(hint in name for hint in _MMM_NAME_HINTS)


def discover_mmm(roots: list[str | Path] | None = None, *,
                 extra_roots: list[str | Path] | None = None,
                 max_depth: int = _DISCOVER_MAX_DEPTH,
                 time_budget: float = _DISCOVER_TIME_BUDGET,
                 max_children: int = 400) -> list[MmmInstall]:
    """在常见目录里搜索 MMM 安装。

    搜索是**刻意受限**的，因为方案 §8 明确说不要把成本花在复杂自动发现上：

    * 传入 ``roots`` 时**只搜这些目录**（测试与调用方可控）；
      否则用 ``_candidate_roots()`` 的常见位置。
    * 第 0/1 层全部检查；再往下**只进入名字带 MMM 线索的目录**，
      否则 depth=3 就等价于遍历整块磁盘。
    * 每个目录最多看 ``max_children`` 个子项，并受 ``time_budget`` 约束。

    找不到就返回空列表 —— 手动指定才是主要方式。
    """
    search_roots = [Path(p) for p in (roots if roots is not None else _candidate_roots())]
    if extra_roots:
        search_roots = search_roots + [Path(p) for p in extra_roots]

    deadline = time.time() + time_budget
    found: list[MmmInstall] = []
    seen: set[str] = set()

    def children(directory: Path) -> list[Path]:
        try:
            items = []
            for entry in directory.iterdir():
                if len(items) >= max_children:
                    break
                if entry.is_dir():
                    items.append(entry)
            return items
        except (OSError, PermissionError):
            return []

    def consider(directory: Path) -> None:
        executable = _find_executable(directory)
        if executable is None:
            return
        key = str(directory).casefold()
        if key in seen:
            return
        seen.add(key)
        try:
            found.append(validate_mmm_dir(directory, source="自动检测"))
        except MmmRuntimeError:
            pass

    def walk(directory: Path, depth: int) -> None:
        if time.time() > deadline or depth > max_depth:
            return
        consider(directory)
        if depth == max_depth:
            return
        for child in children(directory):
            if time.time() > deadline:
                return
            # 超过第 1 层后，只进入名字有 MMM 线索的目录。
            if depth >= 1 and not _has_mmm_hint(child):
                continue
            walk(child, depth + 1)

    for root in search_roots:
        if time.time() > deadline:
            break
        walk(root, 0)

    return found



# --------------------------------------------------------------------------
# Runtime 隔离（方案 §9 / §14）
# --------------------------------------------------------------------------

def runtime_root(version: str = MMM_VERSION_SUPPORTED) -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "MMDBridge" / "mmm_runtime" / version


def runtime_dir(version: str = MMM_VERSION_SUPPORTED) -> Path:
    """隔离 Runtime 的 MMM 副本目录。"""
    return runtime_root(version) / "runtime"


def baseline_dir(version: str = MMM_VERSION_SUPPORTED) -> Path:
    """baseline 配置目录（方案 §9：每次 Job 前恢复）。"""
    return runtime_root(version) / "baseline"


def runtime_status(version: str = MMM_VERSION_SUPPORTED) -> dict:
    directory = runtime_dir(version)
    executable = _find_executable(directory)
    baseline_files = []
    for name in BASELINE_FILES:
        try:
            if (baseline_dir(version) / name).is_file():
                baseline_files.append(name)
        except OSError:
            pass
    return {
        "version": version,
        "runtime_dir": str(directory),
        "baseline_dir": str(baseline_dir(version)),
        "prepared": executable is not None,
        "executable": str(executable) if executable else "",
        "baseline_files": baseline_files,
    }


def prepare_runtime(mmm_dir: str | Path, *, version: str = MMM_VERSION_SUPPORTED,
                    force: bool = False, install: MmmInstall | None = None,
                    on_progress=None) -> Path:
    """把 MMM 整体复制到隔离目录，并生成 baseline 配置。

    已存在且含 exe 时**直接复用**（``force=True`` 才重新复制）。
    源目录**只读**，绝不修改。

    Raises:
        MmmRuntimeError: 源目录无效，或复制后校验失败。
    """
    source = install or validate_mmm_dir(mmm_dir)
    target = runtime_dir(version)
    executable = _find_executable(target)

    if executable is not None and not force:
        return target

    if on_progress:
        on_progress(f"复制 MMM → {target}")

    if force and target.is_dir():
        # 真正的"重新复制"：先清空，否则旧文件会残留下来，
        # 而 force 的语义就是"恢复到刚复制完的状态"。
        shutil.rmtree(target, ignore_errors=True)

    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source.directory, target, dirs_exist_ok=True)

    if _find_executable(target) is None:
        raise MmmRuntimeError(
            MMM_NOT_FOUND,
            f"复制完成但 {target} 下仍找不到 MMM 可执行文件")

    # baseline：把 MMM 当前的三个配置文件存一份，供每次 Job 前复位。
    baseline = baseline_dir(version)
    baseline.mkdir(parents=True, exist_ok=True)
    for name in BASELINE_FILES:
        candidate = target / name
        if candidate.is_file():
            shutil.copy2(candidate, baseline / name)

    # **刻意不锁定界面语言。**
    #
    # 曾经加过"强制 zh-CN"，理由是 UIA 按按钮显示文本定位控件。
    # 但那是错的：用户实际安装的 MMM 发行版
    # （F:\MikumikuDance Exe\test\MikuMikuMoving64_v1292）**只带 ja 语言包**，
    # 强制 zh-CN 会让界面回退到不确定的内置资源。
    #
    # 正确做法是**让 UIA 驱动适配目标发行版的语言**，
    # 候选表已覆盖中/日/英（见 mmd_bridge/mmm_ui.py）。
    # 需要时可用 force_ui_language() 手动改，但不自动执行。
    return target


#: ``force_ui_language()`` 的默认目标语言。
#:
#: **注意：不要自动套用它。**
#:
#: 曾经在 prepare_runtime 里自动强制成 zh-CN，理由是"UIA 按按钮显示文本
#: 定位控件，锁死语言就能只支持一种"。但用户实际安装的 MMM 发行版
#: （``F://MikumikuDance Exe\test\MikuMikuMoving64_v1292``）**只带 ja 语言包**，
#: 强制 zh-CN 会让界面回退到不确定的内置资源 —— 反而更不可控。
#:
#: 正确做法：让 UIA 驱动适配目标发行版的语言
#: （``mmd_bridge/mmm_ui.py`` 的候选表已覆盖中/日/英）。
#: 本函数保留仅供手工诊断用。
UI_LANGUAGE = "zh-CN"


def force_ui_language(target_dir: Path, language: str = UI_LANGUAGE) -> bool:
    """把某个 MMM 目录的 `Settings.xml` 界面语言改成 ``language``。

    返回是否真的改动了。文件不存在或没有 ``<Language>`` 节点时返回 False。

    用正则而不是 XML 解析 —— 要**保住原文件的其余部分逐字节不变**，
    解析再序列化会重排属性、改变换行与缩进，那些差异会让 MMM 认为配置变了。
    """
    settings = Path(target_dir) / "Settings.xml"
    if not settings.is_file():
        return False
    try:
        text = settings.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return False

    pattern = re.compile(r"<Language>[^<]*</Language>")
    if pattern.search(text):
        replaced = pattern.sub(f"<Language>{language}</Language>", text, count=1)
    else:
        # 没有该节点时插到 <Settings> 之后。
        replaced = re.sub(r"(<Settings[^>]*>)",
                          rf"\1\n    <Language>{language}</Language>",
                          text, count=1)
    if replaced == text:
        return False
    settings.write_text(replaced, encoding="utf-8")
    return True


def restore_baseline(version: str = MMM_VERSION_SUPPORTED) -> list[str]:
    """把 baseline 配置写回 Runtime（方案 §9：每次 Job 前调用）。

    返回被恢复的文件名列表。baseline 里没有的文件不动。
    """
    target = runtime_dir(version)
    baseline = baseline_dir(version)
    if not target.is_dir() or not baseline.is_dir():
        return []
    restored = []
    for name in BASELINE_FILES:
        source = baseline / name
        if source.is_file():
            shutil.copy2(source, target / name)
            restored.append(name)
    return restored


# --------------------------------------------------------------------------
# 单实例锁（方案 §15）
# --------------------------------------------------------------------------
#
# active_job.json / heartbeat.json 都是**固定路径的共享资源**，
# 因此同一个 Runtime 同时只能服务一个 Job —— 否则两个 Job 会互相覆盖对方的
# 握手文件与 telemetry。锁的作用就是把这种冲突变成一次明确的拒绝。

MMM_RUNTIME_BUSY = "MMM_RUNTIME_BUSY"

LOCK_FILE_NAME = ".lock"


def lock_path(version: str = MMM_VERSION_SUPPORTED) -> Path:
    return runtime_dir(version) / LOCK_FILE_NAME


@dataclass(frozen=True)
class RuntimeLock:
    """当前 Runtime 的占用者。"""

    pid: int = 0
    job_id: str = ""
    nonce: str = ""
    created: str = ""
    alive: bool = False
    age: float = float("inf")
    raw: dict = None

    def summary(self) -> str:
        state = "运行中" if self.alive else "已失效"
        return (f"{state} · pid={self.pid} · job={self.job_id} · "
                f"已持续 {self.age:.0f}s")


def read_lock(version: str = MMM_VERSION_SUPPORTED) -> RuntimeLock | None:
    """读取锁文件。文件不存在或内容损坏都返回 ``None``（视为无锁）。"""
    path = lock_path(version)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    from .mmm_protocol import process_alive       # 延迟导入，避免循环

    pid = 0
    try:
        pid = int(payload.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0

    created = str(payload.get("created", ""))
    age = float("inf")
    if created:
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = max(0.0, time.time() - parsed.timestamp())
        except ValueError:
            pass

    return RuntimeLock(
        pid=pid,
        job_id=str(payload.get("job_id", "")),
        nonce=str(payload.get("nonce", "")),
        created=created,
        alive=process_alive(pid) if pid > 0 else False,
        age=age,
        raw=payload)


def lock_status(version: str = MMM_VERSION_SUPPORTED) -> dict:
    lock = read_lock(version)
    return {
        "path": str(lock_path(version)),
        "locked": lock is not None,
        "busy": bool(lock and lock.alive),
        "stale": bool(lock and not lock.alive),
        "pid": lock.pid if lock else 0,
        "job_id": lock.job_id if lock else "",
        "nonce": lock.nonce if lock else "",
        "age": lock.age if lock else float("inf"),
        "summary": lock.summary() if lock else "无锁",
    }


def acquire_lock(job_id: str, nonce: str, *,
                 version: str = MMM_VERSION_SUPPORTED,
                 pid: int | None = None,
                 force: bool = False) -> RuntimeLock:
    """占用 Runtime。

    * 无锁 → 建立并返回；
    * 锁属于**同一个 job_id + nonce** → 视为重入，刷新后返回（幂等）；
    * 锁属于别的活进程 → 抛 :class:`MmmRuntimeError` ``MMM_RUNTIME_BUSY``；
    * 锁已失效（pid 不存在）→ 安全清理后重新建立。

    Args:
        force: 忽略活锁强制占用。仅供排障使用，正常流程不要传。
    """
    existing = read_lock(version)
    if existing is not None and existing.alive and not force:
        same_owner = (existing.job_id == job_id
                      and existing.nonce == nonce and nonce)
        if not same_owner:
            raise MmmRuntimeError(
                MMM_RUNTIME_BUSY,
                f"MMM Runtime 正被另一个任务占用：{existing.summary()}\n"
                f"（{lock_path(version)}）\n"
                "同一 Runtime 同时只能服务一个任务；"
                "等它结束，或用 --force 强制接管。")

    target = lock_path(version)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(pid) if pid is not None else os.getpid(),
        "job_id": job_id,
        "nonce": nonce,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, target)
    return read_lock(version)


def release_lock(*, version: str = MMM_VERSION_SUPPORTED,
                 job_id: str | None = None) -> bool:
    """释放锁。

    传了 ``job_id`` 时**只有占用者匹配才释放** —— 否则一个刚启动的任务
    会把别人的锁删掉。返回是否真的删除了文件。
    """
    target = lock_path(version)
    if not target.is_file():
        return False
    if job_id is not None:
        existing = read_lock(version)
        if existing is not None and existing.job_id and existing.job_id != job_id:
            return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def clear_stale_lock(version: str = MMM_VERSION_SUPPORTED) -> bool:
    """清掉失效的锁。返回是否清理了。活锁不动。"""
    lock = read_lock(version)
    if lock is None or lock.alive:
        return False
    return release_lock(version=version)



# --------------------------------------------------------------------------
# 插件安装 / 卸载（方案 §40）
# --------------------------------------------------------------------------

def plugin_status(target: str | Path, *,
                  expected_dll: str | Path | None = None) -> dict:
    """检查插件是否已装进给定 MMM（或 Runtime）目录。"""
    directory = Path(target)
    installed = directory / "Plugins" / PLUGIN_DLL_NAME
    status = {
        "mmm_dir": str(directory),
        "plugin_path": str(installed),
        "installed": installed.is_file(),
        "size": 0,
        "mtime": "",
        "matches_build": None,
    }
    if status["installed"]:
        info = installed.stat()
        status["size"] = info.st_size
        status["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(info.st_mtime))
        if expected_dll and Path(expected_dll).is_file():
            status["matches_build"] = (
                Path(expected_dll).stat().st_size == info.st_size)
    return status


def install_plugin(target: str | Path, dll: str | Path, *,
                   backup: bool = True) -> Path:
    """把插件 DLL 复制进 ``<target>/Plugins/``。

    同名旧文件会被备份（``.dll.bak-<时间戳>``）而不是直接覆盖。

    Raises:
        MmmRuntimeError: 目标目录或源 DLL 无效。
    """
    directory = Path(target)
    plugins = directory / "Plugins"
    if not plugins.is_dir():
        raise MmmRuntimeError(
            MMM_NOT_FOUND,
            f"目标目录下没有 Plugins 文件夹：{plugins}\n"
            "请确认这是 MMM 安装目录（或已复制的 Runtime）。")

    source = Path(dll)
    if not source.is_file():
        raise MmmRuntimeError(
            MMM_NOT_FOUND,
            f"找不到插件 DLL：{source}\n"
            "请先构建 plugins/mmm/MMDBridgeMMMPlugin（见其 README.md）。")

    destination = plugins / PLUGIN_DLL_NAME
    if backup and destination.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(destination, destination.with_name(
            destination.name + f".bak-{stamp}"))

    shutil.copy2(source, destination)
    return destination


def uninstall_plugin(target: str | Path) -> bool:
    """从 ``<target>/Plugins/`` 移除插件。

    返回是否真的删掉了文件。**同时清理遗留的备份**，
    避免反复安装累积一堆 ``.bak-`` 文件。

    Raises:
        MmmRuntimeError: 目标目录下没有 Plugins 文件夹。
    """
    directory = Path(target)
    plugins = directory / "Plugins"
    if not plugins.is_dir():
        raise MmmRuntimeError(
            MMM_NOT_FOUND, f"目标目录下没有 Plugins 文件夹：{plugins}")

    removed = False
    destination = plugins / PLUGIN_DLL_NAME
    if destination.is_file():
        destination.unlink()
        removed = True

    for leftover in plugins.glob(PLUGIN_DLL_NAME + ".bak-*"):
        try:
            leftover.unlink()
            removed = True
        except OSError:
            pass
    return removed
