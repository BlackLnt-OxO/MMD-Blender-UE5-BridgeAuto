"""Launch external applications without inheriting frozen Python DLL paths."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import signal
import threading

_launch_lock = threading.RLock()
_gpu_memory_static = None


def terminate_pid(pid, expected_executable=None):
    """Immediately stop a task process and its children (including FBX validators)."""
    if os.name == 'nt':
        if expected_executable:
            result = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                          f'(Get-CimInstance Win32_Process -Filter "ProcessId = {int(pid)}").ExecutablePath'],
                         capture_output=True, text=True, timeout=10, creationflags=0x08000000)
            actual = result.stdout.strip()
            if not actual:
                if pid_exists(pid):
                    raise RuntimeError(f'无法确认任务进程 {pid} 的程序路径')
                return
            if Path(actual).resolve() != Path(expected_executable).resolve():
                return  # The recorded PID has since been reused by another app.
        result = run(['taskkill.exe', '/PID', str(int(pid)), '/T', '/F'],
                     capture_output=True, creationflags=0x08000000, timeout=10)
        if result.returncode and pid_exists(pid):
            raise RuntimeError(f'无法终止任务进程 {pid}')
    else:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def physical_memory_info():
    """Return Windows physical memory in MiB for large USD load preflight."""
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {"total_mb": int(status.total_phys / (1024 * 1024)),
            "available_mb": int(status.avail_phys / (1024 * 1024))}


def _shared_gpu_memory_limit_mb():
    """Task Manager exposes half of physical RAM as the shared GPU limit."""
    info = physical_memory_info()
    return int(round(info["total_mb"] / 2)) if info else 0


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


def shared_gpu_memory_percent(info):
    """Task Manager's Shared GPU Memory percentage, or None if unavailable."""
    if not info or not info.get("shared_total_mb") or info.get("shared_used_mb") is None:
        return None
    return info["shared_used_mb"] * 100.0 / info["shared_total_mb"]


def gpu_budget_percent(info):
    """Watch each pool separately; shared RAM never enlarges the VRAM budget."""
    if not info:
        return None
    values=[]
    if info.get('total_mb') and info.get('used_mb') is not None:
        values.append(info['used_mb']*100/info['total_mb'])
    shared=shared_gpu_memory_percent(info)
    if shared is not None:
        values.append(shared)
    for used,total in (('dedicated_used_mb','dedicated_total_mb'),('local_used_mb','local_budget_mb'),
                       ):
        if info.get(total) and info.get(used) is not None:values.append(info[used]*100/info[total])
    return max(values) if values else None


def dxgi_process_memory_info():
    """Query this process' adapter budget, never pretend it is another PID's usage."""
    if os.name!='nt':return None
    import uuid
    class Description(ctypes.Structure):
        _fields_=[('description',ctypes.c_wchar*128),('vendor',ctypes.c_uint),('device',ctypes.c_uint),
            ('subsystem',ctypes.c_uint),('revision',ctypes.c_uint),('video',ctypes.c_size_t),
            ('system',ctypes.c_size_t),('shared',ctypes.c_size_t),('luid',ctypes.c_uint64),('flags',ctypes.c_uint)]
    class Memory(ctypes.Structure):
        _fields_=[('budget',ctypes.c_uint64),('used',ctypes.c_uint64),
                  ('available',ctypes.c_uint64),('reservation',ctypes.c_uint64)]
    def method(pointer,index,restype,*types):
        table=ctypes.cast(pointer,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype,ctypes.c_void_p,*types)(table[index])
    def guid(value):return (ctypes.c_byte*16).from_buffer_copy(uuid.UUID(value).bytes_le)
    factory=ctypes.c_void_p();adapters=[];samples=[]
    try:
        dxgi=ctypes.WinDLL('dxgi.dll')
        create=dxgi.CreateDXGIFactory1;create.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)];create.restype=ctypes.c_long
        iid=guid('770aae78-f26f-4dba-a829-253c83d1b387')
        if create(ctypes.byref(iid),ctypes.byref(factory))<0:return None
        for index in range(16):
            adapter=ctypes.c_void_p()
            if method(factory,12,ctypes.c_long,ctypes.c_uint,ctypes.POINTER(ctypes.c_void_p))(factory,index,ctypes.byref(adapter))<0:break
            adapters.append(adapter);desc=Description()
            if method(adapter,10,ctypes.c_long,ctypes.POINTER(Description))(adapter,ctypes.byref(desc))<0 or not desc.video:continue
            adapter3=ctypes.c_void_p();iid3=guid('645967a4-1392-4310-a798-8053ce3e93fd')
            if method(adapter,0,ctypes.c_long,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p))(adapter,ctypes.byref(iid3),ctypes.byref(adapter3))<0:continue
            adapters.append(adapter3);local=Memory();shared=Memory()
            query=method(adapter3,14,ctypes.c_long,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(Memory))
            if query(adapter3,0,0,ctypes.byref(local))<0:continue
            query(adapter3,0,1,ctypes.byref(shared))
            samples.append({'adapter':desc.description,'dedicated_total_mb':desc.video/1048576,
                'local_budget_mb':local.budget/1048576,'local_used_mb':local.used/1048576,
                'nonlocal_budget_mb':shared.budget/1048576,'nonlocal_used_mb':shared.used/1048576,
                'dxgi_scope':'current_process','dxgi_pid':os.getpid()})
        return max(samples,key=lambda s:s['dedicated_total_mb']) if samples else None
    except (OSError,AttributeError,ValueError):return None
    finally:
        for pointer in reversed(adapters):method(pointer,2,ctypes.c_uint)(pointer)
        if factory:method(factory,2,ctypes.c_uint)(factory)


def render_memory_info():
    """Global PDH plus process-local DXGI, called inside the rendering worker."""
    info=dict(gpu_memory_info() or {})
    local=dxgi_process_memory_info()
    if local:info.update(local)
    physical=physical_memory_info()
    if physical:
        info.update(system_total_mb=physical['total_mb'],
                    system_used_mb=physical['total_mb']-physical['available_mb'])
    return info or None


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
