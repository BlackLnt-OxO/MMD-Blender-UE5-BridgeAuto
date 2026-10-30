"""Bounded logs and lifetime management for a compiler process and its children."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque


@dataclass(frozen=True)
class BuildLimits:
    timeout_seconds: float = 1800
    max_log_bytes: int = 64 * 1024 * 1024
    repeated_errors: int = 20

    def __post_init__(self):
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('Build timeout must be finite and positive')
        if self.max_log_bytes <= 0 or self.repeated_errors <= 0:
            raise ValueError('Log and repeated-error limits must be positive')


class BuildError(RuntimeError):
    def __init__(self, reason, log_dir):
        self.reason = reason
        self.log_dir = Path(log_dir)
        super().__init__(f'{reason}. Build diagnostics: {self.log_dir}')


def print_build_message(message, *, file=None):
    """A legacy Windows console encoding must not interrupt a healthy compiler."""
    stream = sys.stdout if file is None else file
    try:
        print(message, file=stream, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        printable = str(message).encode(encoding, errors='backslashreplace').decode(encoding)
        print(printable, file=stream, flush=True)


class _ProcessTree:
    """A private Windows job prevents orphan compiler processes after cancellation."""

    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return

        class BasicLimit(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                        ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class IOCount(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [('basic', BasicLimit), ('io', IOCount),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, child):
        if os.name == 'nt' and not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def stop(self, child):
        self.close()
        if os.name != 'nt':
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


class _ErrorRepeats:
    def __init__(self, limit):
        self.limit = limit
        self.recent = deque()
        self.counts = Counter()

    def check(self, line):
        lowered = line.lower()
        if 'uba' in lowered and 'failed to create directory' in lowered:
            return 'UBA could not create its working directory; compilation stopped'
        if not re.search(r'\berror\b|\bfatal\b|access is denied|exception|failed to create', lowered):
            return None
        normalized = re.sub(r'\d+', '#', lowered)
        self.recent.append(normalized)
        self.counts[normalized] += 1
        if len(self.recent) > 256:
            previous = self.recent.popleft()
            self.counts[previous] -= 1
            if not self.counts[previous]:
                del self.counts[previous]
        if self.counts[normalized] >= self.limit:
            return f'Repeated compiler error reached limit ({self.limit})'
        return None


def _log_bytes(directory):
    return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())


def run_build(command, *, cwd, log_dir, limits=BuildLimits(), env=None):
    """Run without a shell; bound memory, logs, runtime and descendant lifetime."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    console = log_dir / 'console.log'
    messages = queue.Queue(maxsize=64)
    stopped = threading.Event()
    tree = _ProcessTree()
    child = None
    reader = None
    failure = None
    pending = b''
    repeats = _ErrorRepeats(limits.repeated_errors)
    started = time.monotonic()

    def read_output(pipe):
        try:
            while not stopped.is_set():
                chunk = pipe.read(4096)
                if not chunk:
                    break
                while not stopped.is_set():
                    try:
                        messages.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            while not stopped.is_set():
                try:
                    messages.put(None, timeout=0.1)
                    break
                except queue.Full:
                    pass

    try:
        kwargs = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        child = subprocess.Popen(command, **kwargs)
        try:
            tree.attach(child)
        except BaseException:
            child.kill()
            child.wait(timeout=10)
            raise
        reader = threading.Thread(target=read_output, args=(child.stdout,), daemon=True)
        reader.start()
        ended = False
        written = 0
        with console.open('xb', buffering=0) as log:
            while not ended or child.poll() is None:
                if time.monotonic() - started >= limits.timeout_seconds:
                    failure = f'Compiler timed out after {limits.timeout_seconds:g} seconds'
                    break
                if _log_bytes(log_dir) >= limits.max_log_bytes:
                    failure = f'Build logs reached limit ({limits.max_log_bytes} bytes)'
                    break
                try:
                    chunk = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    ended = True
                    continue
                remaining = limits.max_log_bytes - written
                log.write(chunk[:remaining])
                written += min(len(chunk), remaining)
                pending += chunk
                lines = pending.split(b'\n')
                pending = lines.pop()
                if len(pending) >= 16384:
                    lines.append(pending)
                    pending = b''
                for raw in lines:
                    line = raw.decode('utf-8', errors='replace').rstrip('\r')
                    print_build_message(line)
                    failure = repeats.check(line)
                    if failure:
                        break
                if failure:
                    break
                if len(chunk) > remaining:
                    failure = f'Build logs reached limit ({limits.max_log_bytes} bytes)'
                    break
            if pending:
                line = pending.decode('utf-8', errors='replace')
                print_build_message(line)
                failure = failure or repeats.check(line)
        if failure:
            raise BuildError(failure, log_dir)
        code = child.wait(timeout=10)
        if code:
            raise BuildError(f'Compiler exited with code {code}', log_dir)
        if _log_bytes(log_dir) >= limits.max_log_bytes:
            raise BuildError(f'Build logs reached limit ({limits.max_log_bytes} bytes)', log_dir)
        return {'returncode': code, 'elapsed_seconds': round(time.monotonic() - started, 3),
                'log_bytes': _log_bytes(log_dir)}
    finally:
        stopped.set()
        if child is not None:
            tree.stop(child)
            if child.stdout:
                child.stdout.close()
        else:
            tree.close()
        if reader:
            reader.join(timeout=2)
