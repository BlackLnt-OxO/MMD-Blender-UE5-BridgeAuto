"""Python side of the MMM plugin protocol — plan §21 / §22 / §23.

Two directions, one shared file and one per-job directory:

.. code-block:: text

    Python  ──write──▶  %LOCALAPPDATA%/MMDBridge/mmm_bridge/active_job.json
                                                              │
    MMM plugin ──read──────────────────────────────────────────┘
                                                              │
    MMM plugin ──write──▶  <job_dir>/heartbeat.json
                           <job_dir>/state.json
                           <job_dir>/*_snapshot.json
                                                              │
    Python  ──read─────────────────────────────────────────────┘

Why ``job_id`` **and** ``nonce``
--------------------------------
``active_job.json`` is a single fixed path shared by every job, and MMM processes
outlive the run that started them.  Without both identifiers a leftover MMM —
or simply the previous job's ``heartbeat.json`` still sitting in its directory —
would look exactly like "the current job's plugin is online".  Every read here
validates protocol version, job id, nonce, freshness, and (when possible) that
the reported pid is still alive.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import atomic_json

__all__ = [
    "MMM_PLUGIN_NOT_READY",
    "MMM_PLUGIN_PROTOCOL_MISMATCH",
    "MMM_PLUGIN_STALE",
    "PLUGIN_VERSION_EXPECTED",
    "PROTOCOL_VERSION",
    "Heartbeat",
    "JobRequest",
    "PluginNotReady",
    "active_job_path",
    "bridge_dir",
    "clear_active_job",
    "heartbeat_is_current",
    "process_alive",
    "read_active_job",
    "read_heartbeat",
    "read_snapshot",
    "read_state",
    "wait_for_plugin",
    "write_active_job",
]

#: Must match ``Protocol.Version`` in the C# plugin.
PROTOCOL_VERSION = 1

#: Recorded by the plugin in every heartbeat; used for diagnostics only.
PLUGIN_VERSION_EXPECTED = "1.0"

#: Default freshness window: a heartbeat older than this is not "online".
DEFAULT_MAX_AGE = 5.0

MMM_PLUGIN_NOT_READY = "MMM_PLUGIN_NOT_READY"
MMM_PLUGIN_STALE = "MMM_PLUGIN_STALE"
MMM_PLUGIN_PROTOCOL_MISMATCH = "MMM_PLUGIN_PROTOCOL_MISMATCH"


class PluginNotReady(RuntimeError):
    """The plugin did not come online, or its telemetry cannot be trusted."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def bridge_dir() -> Path:
    """``%LOCALAPPDATA%/MMDBridge/mmm_bridge`` — the shared handshake folder."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    return Path(local) / "MMDBridge" / "mmm_bridge"


def active_job_path() -> Path:
    return bridge_dir() / "active_job.json"


# --------------------------------------------------------------------------
# Request (Python -> plugin)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JobRequest:
    """What the plugin needs to know about the job it is serving."""

    job_id: str
    job_dir: str
    effective_model: str = ""
    source_vmd: str = ""
    #: ``repair_manifest.json`` 的完整路径。
    #:
    #: 不能假定它在 ``job_dir`` 下 —— 实测它在 ``<job_root>/pmx/`` 里，
    #: 而 ``job_dir`` 是 ``<job_root>/mmm/``。早期版本因此让插件读到空的目标清单，
    #: ``bake_snapshot`` 的 ``expected_count`` 恒为 0，**断言静默失效**。
    manifest_path: str = ""
    start_frame: int = 0
    end_frame: int = 0
    nonce: str = ""
    protocol: int = PROTOCOL_VERSION

    def __post_init__(self):
        if not self.nonce:
            # A fresh nonce per job is what makes a leftover MMM detectable.
            object.__setattr__(self, "nonce", uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "job_id": self.job_id,
            "nonce": self.nonce,
            "job_dir": str(self.job_dir),
            "effective_model": str(self.effective_model),
            "source_vmd": str(self.source_vmd),
            "manifest_path": str(self.manifest_path),
            "start_frame": int(self.start_frame),
            "end_frame": int(self.end_frame),
        }


def write_active_job(request: JobRequest, *, path: str | Path | None = None) -> Path:
    """Publish the current job.  Written atomically — the plugin polls this file."""
    target = Path(path) if path else active_job_path()
    atomic_json(target, request.to_dict())
    return target


def clear_active_job(*, path: str | Path | None = None) -> None:
    """Remove the handshake file so a stale plugin stops writing telemetry."""
    target = Path(path) if path else active_job_path()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def read_active_job(*, path: str | Path | None = None) -> JobRequest | None:
    target = Path(path) if path else active_job_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    try:
        return JobRequest(
            job_id=str(payload["job_id"]),
            job_dir=str(payload["job_dir"]),
            effective_model=str(payload.get("effective_model", "")),
            source_vmd=str(payload.get("source_vmd", "")),
            manifest_path=str(payload.get("manifest_path", "")),
            start_frame=int(payload.get("start_frame", 0)),
            end_frame=int(payload.get("end_frame", 0)),
            nonce=str(payload.get("nonce", "")),
            protocol=int(payload.get("protocol", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Telemetry (plugin -> Python)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Heartbeat:
    protocol: int = 0
    job_id: str = ""
    nonce: str = ""
    pid: int = 0
    plugin_version: str = ""
    scene_state: str = ""
    marker_position: int = 0
    updated: datetime | None = None
    #: Seconds between ``updated`` and the moment it was read.
    age: float = float("inf")
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "job_id": self.job_id,
            "nonce": self.nonce,
            "pid": self.pid,
            "plugin_version": self.plugin_version,
            "scene_state": self.scene_state,
            "marker_position": self.marker_position,
            "updated": self.updated.isoformat() if self.updated else "",
            "age": round(self.age, 3),
        }


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_heartbeat(job_dir: str | Path, *,
                   now: float | None = None) -> Heartbeat | None:
    """Read ``<job_dir>/heartbeat.json``, or ``None`` if unreadable."""
    payload = _read_json(Path(job_dir) / "heartbeat.json")
    if payload is None:
        return None

    updated = _parse_timestamp(str(payload.get("updated", "")))
    current = time.time() if now is None else now
    age = float("inf")
    if updated is not None:
        age = max(0.0, current - updated.timestamp())

    def _int(key: str) -> int:
        try:
            return int(payload.get(key, 0))
        except (TypeError, ValueError):
            return 0

    return Heartbeat(
        protocol=_int("protocol"),
        job_id=str(payload.get("job_id", "")),
        nonce=str(payload.get("nonce", "")),
        pid=_int("pid"),
        plugin_version=str(payload.get("plugin_version", "")),
        scene_state=str(payload.get("scene_state", "")),
        marker_position=_int("marker_position"),
        updated=updated,
        age=age,
        raw=payload,
    )


def read_state(job_dir: str | Path) -> dict[str, Any]:
    """Read ``<job_dir>/state.json``; empty dict when absent."""
    return _read_json(Path(job_dir) / "state.json") or {}


def read_snapshot(job_dir: str | Path, name: str) -> dict[str, Any]:
    """Read ``<job_dir>/<name>_snapshot.json``; empty dict when absent."""
    return _read_json(Path(job_dir) / f"{name}_snapshot.json") or {}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def process_alive(pid: int) -> bool:
    """Best-effort liveness check.  Returns True when it cannot tell.

    "Cannot tell" must not be treated as dead: refusing a healthy plugin is
    worse than accepting a stale one that the freshness window will catch.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259       # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def heartbeat_is_current(
    heartbeat: Heartbeat | None,
    *,
    job_id: str,
    nonce: str,
    max_age: float = DEFAULT_MAX_AGE,
    now: float | None = None,
    check_pid: bool = True,
) -> tuple[bool, str]:
    """Decide whether *heartbeat* proves *this* job's plugin is online.

    Returns ``(ok, code)``; ``code`` is an empty string when ok.
    """
    if heartbeat is None:
        return False, MMM_PLUGIN_NOT_READY
    if heartbeat.protocol != PROTOCOL_VERSION:
        return False, MMM_PLUGIN_PROTOCOL_MISMATCH
    if heartbeat.job_id != job_id or heartbeat.nonce != nonce:
        return False, MMM_PLUGIN_STALE

    current = time.time() if now is None else now
    age = heartbeat.age
    if age == float("inf") or age > max_age:
        return False, MMM_PLUGIN_STALE
    if check_pid and not process_alive(heartbeat.pid):
        return False, MMM_PLUGIN_STALE
    return True, ""


def wait_for_plugin(
    job_dir: str | Path,
    *,
    job_id: str,
    nonce: str,
    timeout: float = 60.0,
    max_age: float = DEFAULT_MAX_AGE,
    poll: float = 0.25,
    check_pid: bool = True,
) -> Heartbeat:
    """Block until this job's plugin reports in.

    Raises:
        PluginNotReady: with ``MMM_PLUGIN_NOT_READY`` on timeout, or
            ``MMM_PLUGIN_STALE`` / ``MMM_PLUGIN_PROTOCOL_MISMATCH`` when a
            heartbeat was seen but could not be trusted.
    """
    deadline = time.time() + max(0.0, timeout)
    last_code = MMM_PLUGIN_NOT_READY
    last_heartbeat: Heartbeat | None = None

    while True:
        heartbeat = read_heartbeat(job_dir)
        if heartbeat is not None:
            last_heartbeat = heartbeat
        ok, code = heartbeat_is_current(
            heartbeat, job_id=job_id, nonce=nonce,
            max_age=max_age, check_pid=check_pid)
        if ok:
            return heartbeat
        last_code = code or MMM_PLUGIN_NOT_READY

        if time.time() >= deadline:
            detail = _not_ready_detail(job_dir, job_id, nonce, last_heartbeat,
                                       last_code, timeout)
            raise PluginNotReady(last_code, detail)
        time.sleep(max(0.01, poll))


def _not_ready_detail(job_dir: str | Path, job_id: str, nonce: str,
                      heartbeat: Heartbeat | None, code: str,
                      timeout: float) -> str:
    lines = [f"等待 MMM 插件上线超时（{timeout:.0f}s）",
             f"  任务目录 : {job_dir}",
             f"  期望 job_id : {job_id}",
             f"  期望 nonce  : {nonce}"]
    if heartbeat is None:
        lines.append("  未发现 heartbeat.json —— 插件可能未被 MMM 加载。")
        lines.append("  请确认 DLL 已放进 <MMM>/Plugins/，且插件在 MMM 中被启用。")
    else:
        lines.append(f"  实际 heartbeat: job_id={heartbeat.job_id!r} "
                     f"nonce={heartbeat.nonce!r} pid={heartbeat.pid} "
                     f"age={heartbeat.age:.2f}s protocol={heartbeat.protocol}")
        if code == MMM_PLUGIN_PROTOCOL_MISMATCH:
            lines.append(f"  协议版本不匹配：插件={heartbeat.protocol} "
                         f"期望={PROTOCOL_VERSION}")
        elif heartbeat.job_id != job_id or heartbeat.nonce != nonce:
            lines.append("  job_id/nonce 不匹配 —— 可能是上一次任务残留的 MMM 进程。")
        elif heartbeat.age > DEFAULT_MAX_AGE:
            lines.append("  heartbeat 已过期 —— 插件可能已停止 Update。")
        else:
            lines.append("  pid 已不存在 —— MMM 进程可能已退出。")
    return "\n".join(lines)
