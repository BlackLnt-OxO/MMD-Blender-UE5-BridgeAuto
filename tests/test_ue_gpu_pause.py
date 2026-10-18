import importlib
import sys
import types

import pytest


def test_gpu_budget_pauses_before_next_texture(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    host = importlib.import_module("mmd_bridge.ue_host")
    monkeypatch.setattr(host, "collect_import_garbage", lambda: None)
    monkeypatch.setattr(host.process, "render_memory_info", lambda: {"shared_total_mb": 100,
                                                                     "shared_used_mb": 86})
    job = types.SimpleNamespace(folder=tmp_path, status=lambda *args, **kwargs: states.append((args, kwargs)))
    states = []
    with pytest.raises(host.GpuPause):
        host.check_gpu_budget(job, {"ue_vram_pause_percent": 90,
                                    "ue_vram_resume_percent": 80}, {"checks": {}})
    assert states[0][0][0] == "ue_wait_vram"
    assert "共享显存占用 86.0%" in states[0][0][1]
    assert (tmp_path / "ue_report.json").is_file()


@pytest.mark.parametrize('shared', [{'shared_total_mb': 8000, 'shared_used_mb': 1000}, {}])
def test_import_does_not_pause_for_full_dedicated_memory(tmp_path, monkeypatch, shared):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    host = importlib.import_module("mmd_bridge.ue_host")
    info = dict(dedicated_used_mb=8000, dedicated_total_mb=8000,
                local_used_mb=8000, local_budget_mb=6500, **shared)
    monkeypatch.setattr(host.process, "render_memory_info", lambda: info)
    states = []
    job = types.SimpleNamespace(folder=tmp_path, status=lambda *args, **kwargs: states.append(args))
    report = {"checks": {}}
    host.check_gpu_budget(job, {"ue_vram_pause_percent": 90}, report)
    assert states == []
    assert report['metrics']['memory_peak']['dedicated_used_mb'] == 8000


def test_scene_light_uses_calibrated_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    host = importlib.import_module("mmd_bridge.ue_host")
    light = {"energy": 120, "color": [1, 1, 1]}
    assert host.scene_light_intensity(light, {}) == pytest.approx(12294)
    assert host.scene_light_intensity(light, {"scene_light_scale": 0}) == 0
