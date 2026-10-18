import os
import subprocess
import sys
import pytest
from mmd_bridge import process
from mmd_bridge.core import resource_root


def test_frozen_resources_and_child_environment(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    monkeypatch.setenv('PATH', str(bundle) + os.pathsep + str(bundle/'PySide6') + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('PYTHONPATH', str(bundle))
    assert resource_root() == bundle/'bridge_resources'
    result = process.run([sys.executable, '-c', 'import os; print(os.environ.get("PYTHONPATH", "clean")); print(os.environ["PATH"])'],
                         capture_output=True, text=True, check=True)
    assert result.stdout.startswith('clean\n')
    assert str(bundle) not in result.stdout


def test_nvidia_memory_info_parses_query(monkeypatch):
    monkeypatch.setattr(process.shutil, 'which', lambda name: 'nvidia-smi.exe')
    monkeypatch.setattr(process, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, '0, NVIDIA GeForce RTX, 8188, 4939, 3249\n', ''))
    assert process.nvidia_memory_info() == {
        'index': 0, 'name': 'NVIDIA GeForce RTX', 'total_mb': 8188,
        'used_mb': 4939, 'free_mb': 3249,
    }


def test_gpu_memory_info_matches_task_manager_total(monkeypatch):
    mib = 1024 * 1024
    monkeypatch.setattr(process, '_gpu_memory_static', None)
    monkeypatch.setattr(process, '_windows_gpu_memory_samples', lambda: {
        'nvidia_luid': {'dedicated_bytes': 5500*mib, 'shared_bytes': 400*mib},
        'integrated_luid': {'dedicated_bytes': 0, 'shared_bytes': 10*mib},
    })
    monkeypatch.setattr(process, '_shared_gpu_memory_limit_mb', lambda: 8052)
    monkeypatch.setattr(process, 'nvidia_memory_info', lambda: {
        'index': 0, 'name': 'NVIDIA GeForce RTX', 'total_mb': 8188,
        'used_mb': 5490, 'free_mb': 2698,
    })

    info = process.gpu_memory_info()

    assert info['scope'] == 'task_manager_total'
    assert info['total_mb'] == 16240
    assert info['used_mb'] == 5900
    assert info['dedicated_used_mb'] == 5500
    assert info['shared_used_mb'] == 400
    assert round(process.shared_gpu_memory_percent(info), 2) == round(400 / 8052 * 100, 2)
    assert process.shared_gpu_memory_percent({'total_mb': 8192, 'used_mb': 5900}) is None


def test_pid_exists_for_current_process():
    assert process.pid_exists(os.getpid())
    assert not process.pid_exists(-1)


@pytest.mark.parametrize('shared_used,expected', [(0, 0), (400, 5), (7200, 90)])
def test_gpu_protection_ignores_other_memory_pools(shared_used, expected):
    info = dict(total_mb=16000, used_mb=16000,
                dedicated_used_mb=8000, dedicated_total_mb=8000,
                local_used_mb=8000, local_budget_mb=6500,
                nonlocal_used_mb=8000, nonlocal_budget_mb=8000,
                system_used_mb=16000, system_total_mb=16000,
                shared_used_mb=shared_used, shared_total_mb=8000)
    assert process.gpu_budget_percent(info) == expected


@pytest.mark.parametrize('shared', [{}, {'shared_total_mb': 0, 'shared_used_mb': 100},
                                  {'shared_total_mb': 8000},
                                  {'shared_total_mb': 8000, 'shared_used_mb': None}])
def test_gpu_protection_has_no_dedicated_fallback(shared):
    assert process.gpu_budget_percent(dict(total_mb=8000, used_mb=8000,
                                          local_used_mb=8000, local_budget_mb=6500,
                                          **shared)) is None
