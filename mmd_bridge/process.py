"""Launch external applications without inheriting frozen Python DLL paths."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

_launch_lock = threading.RLock()
_gpu_memory_static = None


def popen(args, **kwargs):
    bundle = getattr(sys, '_MEIPASS', None)
    if not bundle:
        return subprocess.Popen(args, **kwargs)
    env = dict(kwargs.pop('env', os.environ))
    root = os.path.normcase(os.path.abspath(bundle))
    env['PATH'] = os.pathsep.join(part for part in env.get('PATH', '').split(os.pathsep)
        if not os.path.normcase(os.path.abspath(part)).startswith(root + os.sep)
        and os.path.normcase(os.path.abspath(part)) != root)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    with _launch_lock:
        if os.name == 'nt':
            kernel = ctypes.windll.kernel32
            previous = ctypes.create_unicode_buffer(32768)
            kernel.GetDllDirectoryW(len(previous), previous)
            kernel.SetDllDirectoryW(None)
        try:
            return subprocess.Popen(args, env=env, **kwargs)
        finally:
            if os.name == 'nt':
                kernel.SetDllDirectoryW(previous.value or None)


def run(args, *, timeout=None, check=False, capture_output=False, **kwargs):
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with popen(args, **kwargs) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            raise subprocess.TimeoutExpired(args, timeout, stdout, stderr)
        result = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def nvidia_memory_info():
    """Return NVIDIA GPU 0 memory in MiB, or None when unavailable."""
    executable = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
    if not executable and os.name == "nt":
        candidate = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        return None
    try:
        result = run(
            [executable, "--query-gpu=index,name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            timeout=5, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode:
            return None
        first = next((line for line in result.stdout.splitlines() if line.strip()), "")
        parts = [part.strip() for part in first.split(",")]
        if len(parts) < 5:
            return None
        return {
            "index": int(parts[0]), "name": parts[1], "total_mb": int(parts[2]),
            "used_mb": int(parts[3]), "free_mb": int(parts[4]),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _windows_gpu_memory_samples():
    """Read Task Manager's per-adapter dedicated/shared usage via PDH."""
    if os.name != "nt":
        return None
    pdh = ctypes.windll.pdh
    query = ctypes.c_void_p()
    counters = []

    class ValueUnion(ctypes.Union):
        _fields_ = [("long_value", ctypes.c_long), ("double_value", ctypes.c_double),
                    ("large_value", ctypes.c_longlong), ("wide_value", ctypes.c_wchar_p)]

    class CounterValue(ctypes.Structure):
        _fields_ = [("status", ctypes.c_ulong), ("value", ValueUnion)]

    class CounterItem(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar_p), ("value", CounterValue)]

    pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
        return None
    try:
        for counter_name in ("Dedicated Usage", "Shared Usage"):
            counter = ctypes.c_void_p()
            path = rf"\GPU Adapter Memory(*)\{counter_name}"
            if pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter)) != 0:
                return None
            counters.append((counter_name, counter))
        if pdh.PdhCollectQueryData(query) != 0:
            return None
        result = {}
        for counter_name, counter in counters:
            size = ctypes.c_ulong(0)
            count = ctypes.c_ulong(0)
            status = pdh.PdhGetFormattedCounterArrayW(
                counter, 0x400, ctypes.byref(size), ctypes.byref(count), None)
            if status & 0xFFFFFFFF not in (0, 0x800007D2):  # ERROR_SUCCESS / PDH_MORE_DATA
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if pdh.PdhGetFormattedCounterArrayW(
                    counter, 0x400, ctypes.byref(size), ctypes.byref(count), buffer) != 0:
                return None
            items = ctypes.cast(buffer, ctypes.POINTER(CounterItem))
            key = "dedicated_bytes" if counter_name.startswith("Dedicated") else "shared_bytes"
            for index in range(count.value):
                item = items[index]
                if item.name and item.value.status == 0:
                    result.setdefault(item.name, {})[key] = max(0, int(item.value.value.large_value))
        return result or None
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        pdh.PdhCloseQuery(query)


def _shared_gpu_memory_limit_mb():
    """Task Manager exposes half of physical RAM as the shared GPU limit."""
    if os.name != "nt":
        return 0

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(round(status.total_phys / 2 / (1024 * 1024)))


def gpu_memory_info():
    """Return Task Manager-style GPU memory (dedicated + shared) in MiB.

    NVIDIA's dedicated usage identifies the matching Windows adapter once; all
    later samples use the cheap PDH counters directly. Non-Windows or unavailable
    counters fall back to dedicated NVIDIA memory for safety.
    """
    global _gpu_memory_static
    if os.name != "nt":
        return nvidia_memory_info()
    samples = _windows_gpu_memory_samples()
    if not samples:
        return nvidia_memory_info()
    if not _gpu_memory_static or _gpu_memory_static["instance"] not in samples:
        nvidia = nvidia_memory_info()
        if not nvidia:
            return None
        expected = nvidia["used_mb"]
        instance = min(samples, key=lambda name: abs(
            samples[name].get("dedicated_bytes", 0) / (1024 * 1024) - expected))
        _gpu_memory_static = {
            "instance": instance, "index": nvidia["index"], "name": nvidia["name"],
            "dedicated_total_mb": nvidia["total_mb"],
            "shared_total_mb": _shared_gpu_memory_limit_mb(),
        }
    static = _gpu_memory_static
    sample = samples[static["instance"]]
    dedicated_used = sample.get("dedicated_bytes", 0) / (1024 * 1024)
    shared_used = sample.get("shared_bytes", 0) / (1024 * 1024)
    total = static["dedicated_total_mb"] + static["shared_total_mb"]
    used = int(round(dedicated_used + shared_used))
    return {
        "index": static["index"], "name": static["name"], "scope": "task_manager_total",
        "total_mb": total, "used_mb": used, "free_mb": max(0, total-used),
        "dedicated_total_mb": static["dedicated_total_mb"],
        "dedicated_used_mb": int(round(dedicated_used)),
        "shared_total_mb": static["shared_total_mb"],
        "shared_used_mb": int(round(shared_used)),
    }


def pid_exists(pid):
    """Cheap process-liveness check used while UE's Python tick is busy importing."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process_handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(process_handle)
