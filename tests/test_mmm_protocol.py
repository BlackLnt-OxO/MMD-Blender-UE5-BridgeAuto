"""Unit tests for the MMM plugin protocol (方案 §21 / §22 / §23).

The point of these tests is the *validation* logic, not the file IO: a
``heartbeat.json`` that exists is worthless unless it belongs to the current
job and is fresh.  Every rejection path is covered, because the failure mode it
guards against — mistaking a leftover MMM for the current job's plugin — looks
exactly like success.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mmd_bridge.mmm_protocol import (
    MMM_PLUGIN_NOT_READY,
    MMM_PLUGIN_PROTOCOL_MISMATCH,
    MMM_PLUGIN_STALE,
    PLUGIN_VERSION_EXPECTED,
    PROTOCOL_VERSION,
    Heartbeat,
    JobRequest,
    PluginNotReady,
    clear_active_job,
    heartbeat_is_current,
    process_alive,
    read_active_job,
    read_heartbeat,
    read_snapshot,
    read_state,
    wait_for_plugin,
    write_active_job,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _heartbeat_payload(*, job_id="job1", nonce="n1", protocol=PROTOCOL_VERSION,
                       age_seconds=0.0, pid=1234, scene_state="Editing",
                       marker=0) -> dict:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "protocol": protocol,
        "job_id": job_id,
        "nonce": nonce,
        "pid": pid,
        "plugin_version": PLUGIN_VERSION_EXPECTED,
        "updated": stamp.isoformat(),
        "scene_state": scene_state,
        "marker_position": marker,
    }


def _write_heartbeat(job_dir: Path, **kwargs) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "heartbeat.json"
    target.write_text(json.dumps(_heartbeat_payload(**kwargs)), encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# JobRequest
# --------------------------------------------------------------------------

def test_job_request_generates_a_nonce():
    request = JobRequest(job_id="j", job_dir="/tmp/j")
    assert request.nonce, "必须自动生成 nonce"


def test_job_request_nonces_are_unique():
    a = JobRequest(job_id="j", job_dir="/tmp/j")
    b = JobRequest(job_id="j", job_dir="/tmp/j")
    assert a.nonce != b.nonce


def test_job_request_keeps_explicit_nonce():
    request = JobRequest(job_id="j", job_dir="/tmp/j", nonce="fixed")
    assert request.nonce == "fixed"


def test_active_job_round_trip(tmp_path):
    path = tmp_path / "active_job.json"
    request = JobRequest(job_id="job-1", job_dir=str(tmp_path / "mmm"),
                         effective_model="m.pmx", source_vmd="s.vmd",
                         start_frame=0, end_frame=6754)
    write_active_job(request, path=path)

    loaded = read_active_job(path=path)
    assert loaded is not None
    assert loaded.job_id == "job-1"
    assert loaded.nonce == request.nonce
    assert loaded.end_frame == 6754
    assert loaded.protocol == PROTOCOL_VERSION


def test_active_job_written_as_json_with_protocol(tmp_path):
    path = tmp_path / "active_job.json"
    write_active_job(JobRequest(job_id="j", job_dir="d"), path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["protocol"] == PROTOCOL_VERSION
    assert set(payload) >= {"job_id", "nonce", "job_dir", "start_frame", "end_frame"}


def test_clear_active_job_removes_file(tmp_path):
    path = tmp_path / "active_job.json"
    write_active_job(JobRequest(job_id="j", job_dir="d"), path=path)
    clear_active_job(path=path)
    assert not path.exists()


def test_clear_active_job_is_idempotent(tmp_path):
    clear_active_job(path=tmp_path / "never-existed.json")


def test_read_active_job_returns_none_when_absent(tmp_path):
    assert read_active_job(path=tmp_path / "nope.json") is None


def test_read_active_job_returns_none_on_garbage(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_active_job(path=path) is None


# --------------------------------------------------------------------------
# Heartbeat parsing
# --------------------------------------------------------------------------

def test_read_heartbeat_parses_fields(tmp_path):
    _write_heartbeat(tmp_path, job_id="j", nonce="n", scene_state="PhysicsBaking",
                     marker=512, pid=4321)
    hb = read_heartbeat(tmp_path)
    assert hb is not None
    assert hb.job_id == "j"
    assert hb.nonce == "n"
    assert hb.scene_state == "PhysicsBaking"
    assert hb.marker_position == 512
    assert hb.pid == 4321
    assert hb.protocol == PROTOCOL_VERSION


def test_read_heartbeat_computes_age(tmp_path):
    _write_heartbeat(tmp_path, age_seconds=2.0)
    hb = read_heartbeat(tmp_path)
    assert hb is not None
    assert 1.5 <= hb.age <= 3.0


def test_read_heartbeat_returns_none_when_absent(tmp_path):
    assert read_heartbeat(tmp_path) is None


def test_read_heartbeat_handles_unparsable_timestamp(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({"protocol": 1, "job_id": "j", "updated": "not-a-date"}),
        encoding="utf-8")
    hb = read_heartbeat(tmp_path)
    assert hb is not None
    assert hb.age == float("inf")


def test_read_state_and_snapshot(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(
        json.dumps({"scene_state": "Editing", "bake_observed": True}),
        encoding="utf-8")
    (tmp_path / "bake_snapshot.json").write_text(
        json.dumps({"baked_bones": ["a"], "unbaked_bones": []}),
        encoding="utf-8")

    assert read_state(tmp_path)["bake_observed"] is True
    assert read_snapshot(tmp_path, "bake")["baked_bones"] == ["a"]


def test_read_state_returns_empty_dict_when_absent(tmp_path):
    assert read_state(tmp_path) == {}
    assert read_snapshot(tmp_path, "bake") == {}


# --------------------------------------------------------------------------
# Validation — the part that actually protects the pipeline
# --------------------------------------------------------------------------

def test_current_heartbeat_is_accepted():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="n",
                   pid=os_pid(), age=0.1)
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n")
    assert ok is True
    assert code == ""


def os_pid() -> int:
    import os
    return os.getpid()


def test_missing_heartbeat_is_not_ready():
    ok, code = heartbeat_is_current(None, job_id="j", nonce="n")
    assert ok is False
    assert code == MMM_PLUGIN_NOT_READY


def test_wrong_nonce_is_stale():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="old",
                   pid=os_pid(), age=0.1)
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="new")
    assert ok is False
    assert code == MMM_PLUGIN_STALE


def test_wrong_job_id_is_stale():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="other", nonce="n",
                   pid=os_pid(), age=0.1)
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n")
    assert ok is False
    assert code == MMM_PLUGIN_STALE


def test_old_heartbeat_is_stale():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="n",
                   pid=os_pid(), age=99.0)
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n", max_age=5.0)
    assert ok is False
    assert code == MMM_PLUGIN_STALE


def test_unparsable_timestamp_is_stale():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="n",
                   pid=os_pid(), age=float("inf"))
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n")
    assert ok is False
    assert code == MMM_PLUGIN_STALE


def test_protocol_mismatch_is_reported_separately():
    hb = Heartbeat(protocol=PROTOCOL_VERSION + 1, job_id="j", nonce="n",
                   pid=os_pid(), age=0.1)
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n")
    assert ok is False
    assert code == MMM_PLUGIN_PROTOCOL_MISMATCH


def test_dead_pid_is_stale():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="n",
                   pid=999999999, age=0.1)
    if process_alive(999999999):
        pytest.skip("无法在本平台判定 pid 存活")
    ok, code = heartbeat_is_current(hb, job_id="j", nonce="n")
    assert ok is False
    assert code == MMM_PLUGIN_STALE


def test_pid_check_can_be_disabled():
    hb = Heartbeat(protocol=PROTOCOL_VERSION, job_id="j", nonce="n",
                   pid=999999999, age=0.1)
    ok, _ = heartbeat_is_current(hb, job_id="j", nonce="n", check_pid=False)
    assert ok is True


def test_process_alive_rejects_nonsense_pid():
    assert process_alive(0) is False
    assert process_alive(-1) is False


# --------------------------------------------------------------------------
# wait_for_plugin
# --------------------------------------------------------------------------

def test_wait_for_plugin_returns_when_heartbeat_appears(tmp_path):
    _write_heartbeat(tmp_path, job_id="j", nonce="n", pid=os_pid())
    hb = wait_for_plugin(tmp_path, job_id="j", nonce="n", timeout=1.0)
    assert hb.job_id == "j"


def test_wait_for_plugin_times_out_with_actionable_message(tmp_path):
    with pytest.raises(PluginNotReady) as excinfo:
        wait_for_plugin(tmp_path, job_id="j", nonce="n", timeout=0.3, poll=0.05)
    assert excinfo.value.code == MMM_PLUGIN_NOT_READY
    assert "插件可能未被 MMM 加载" in excinfo.value.message


def test_wait_for_plugin_rejects_leftover_mmm(tmp_path):
    """A previous job's heartbeat must not satisfy the current wait."""
    _write_heartbeat(tmp_path, job_id="old-job", nonce="old-nonce", pid=os_pid())
    with pytest.raises(PluginNotReady) as excinfo:
        wait_for_plugin(tmp_path, job_id="new-job", nonce="new-nonce",
                        timeout=0.3, poll=0.05)
    assert excinfo.value.code == MMM_PLUGIN_STALE
    assert "上一次任务残留" in excinfo.value.message


def test_wait_for_plugin_reports_protocol_mismatch(tmp_path):
    _write_heartbeat(tmp_path, job_id="j", nonce="n", pid=os_pid(),
                     protocol=PROTOCOL_VERSION + 7)
    with pytest.raises(PluginNotReady) as excinfo:
        wait_for_plugin(tmp_path, job_id="j", nonce="n", timeout=0.3, poll=0.05)
    assert excinfo.value.code == MMM_PLUGIN_PROTOCOL_MISMATCH


def test_wait_for_plugin_picks_up_a_later_write(tmp_path):
    """The real sequence: MMM is still starting when the wait begins."""
    import threading

    def writer():
        time.sleep(0.15)
        _write_heartbeat(tmp_path, job_id="j", nonce="n", pid=os_pid())

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        hb = wait_for_plugin(tmp_path, job_id="j", nonce="n",
                             timeout=3.0, poll=0.05)
    finally:
        thread.join()
    assert hb.job_id == "j"


# --------------------------------------------------------------------------
# Heartbeat serialisation
# --------------------------------------------------------------------------

def test_heartbeat_to_dict_is_json_safe():
    hb = Heartbeat(protocol=1, job_id="j", nonce="n", pid=1,
                   updated=datetime.now(timezone.utc), age=0.5)
    payload = json.loads(json.dumps(hb.to_dict(), ensure_ascii=False))
    assert payload["job_id"] == "j"
    assert payload["age"] == 0.5


# --------------------------------------------------------------------------
# manifest_path（插件读目标骨骼清单用）
# --------------------------------------------------------------------------

def test_active_job_carries_manifest_path(tmp_path):
    """manifest 不在 job_dir 下 —— 必须显式告诉插件它在哪。"""
    path = tmp_path / "active_job.json"
    request = JobRequest(job_id="j", job_dir=str(tmp_path / "mmm"),
                         manifest_path=str(tmp_path / "pmx" / "repair_manifest.json"))
    write_active_job(request, path=path)

    loaded = read_active_job(path=path)
    assert loaded is not None
    assert loaded.manifest_path.endswith("repair_manifest.json")
    assert "pmx" in loaded.manifest_path


def test_manifest_path_defaults_to_empty(tmp_path):
    path = tmp_path / "active_job.json"
    write_active_job(JobRequest(job_id="j", job_dir="d"), path=path)
    assert read_active_job(path=path).manifest_path == ""


def test_manifest_path_present_in_serialised_payload(tmp_path):
    path = tmp_path / "active_job.json"
    write_active_job(JobRequest(job_id="j", job_dir="d",
                                manifest_path="X:/a/b.json"), path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_path"] == "X:/a/b.json"


def test_old_jobs_without_manifest_path_still_load(tmp_path):
    """向后兼容：老任务的 active_job.json 没有该字段也要能读。"""
    path = tmp_path / "active_job.json"
    path.write_text(json.dumps({
        "protocol": PROTOCOL_VERSION, "job_id": "j", "nonce": "n",
        "job_dir": "d",
    }), encoding="utf-8")
    loaded = read_active_job(path=path)
    assert loaded is not None
    assert loaded.job_id == "j"
    assert loaded.manifest_path == ""
