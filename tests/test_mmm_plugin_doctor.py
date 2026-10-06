"""Tests for the plugin-load diagnosis.

The whole point of the doctor is to turn "插件没上线" into one of four
distinguishable states.  These tests pin that mapping down, because getting it
wrong would send someone down the wrong debugging path.
"""
from __future__ import annotations

import pytest

from mmd_bridge import mmm_protocol as proto
from scripts import mmm_plugin_doctor as doctor


@pytest.fixture(autouse=True)
def isolated_localappdata(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def write_log(lines: list[str]) -> None:
    folder = proto.bridge_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin_startup.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def test_no_log_means_not_loaded(tmp_path):
    code, text = doctor.diagnose(tmp_path)
    assert code == "NOT_LOADED"
    # 提示必须点明"新鲜度"这一层 —— 只看文件存在会把上次会话的残留误判成在跑。
    assert "新鲜" in text
    assert "自诊断日志" in text


def test_static_ctor_only_means_loaded_but_not_enabled(tmp_path):
    write_log(["2026-09-20 20:00:00.000  [pid 1]  静态构造：类型被首次访问（DLL 已加载）"])
    code, text = doctor.diagnose(tmp_path)
    assert code == "LOADED_NOT_ENABLED"
    assert "没有实例化" in text
    assert "人工" in text, "必须指向方案 §50 的人工启用分支"


def test_instance_ctor_without_update(tmp_path):
    write_log([
        "静态构造：类型被首次访问（DLL 已加载）",
        "实例构造：MMM 创建了插件实例",
    ])
    code, _ = doctor.diagnose(tmp_path)
    assert code == "INSTANTIATED_NO_UPDATE"


def test_update_seen_means_plugin_is_running(tmp_path):
    write_log([
        "静态构造：类型被首次访问（DLL 已加载）",
        "实例构造：MMM 创建了插件实例",
        "Update() 首次调用 frame=0 elapsed=0.016",
    ])
    code, text = doctor.diagnose(tmp_path)
    assert code == "UPDATE_RAN"
    assert "写文件失败" in text


def test_update_wins_over_earlier_markers(tmp_path):
    """日志是累积的，判定必须看**最深**的那一层。"""
    write_log([
        "静态构造：类型被首次访问（DLL 已加载）",
        "实例构造：MMM 创建了插件实例",
        "Initialize() 被调用",
        "Enabled() 被调用 —— 插件已被 MMM 启用",
        "Scene 已注入（插件可读场景状态）",
        "Update() 首次调用 frame=0 elapsed=0.016",
    ])
    assert doctor.diagnose(None)[0] == "UPDATE_RAN"


def test_unrecognised_log_is_unknown(tmp_path):
    write_log(["随便一行不认识的内容"])
    assert doctor.diagnose(tmp_path)[0] == "UNKNOWN"


def test_read_startup_log_handles_absent_file(tmp_path):
    path, lines = doctor.read_startup_log()
    assert lines == []
    assert path.name == "plugin_startup.log"


def test_describe_reports_absent_file(tmp_path):
    assert doctor.describe(tmp_path / "nope.dll") == "不存在"


def test_describe_reports_size_and_time(tmp_path):
    target = tmp_path / "a.dll"
    target.write_bytes(b"x" * 1234)
    text = doctor.describe(target)
    assert "1234 字节" in text


def test_main_reports_missing_runtime(tmp_path, monkeypatch, capsys):
    from mmd_bridge import mmm_runtime as rt
    monkeypatch.setattr(rt, "runtime_status",
                        lambda version=rt.MMM_VERSION_SUPPORTED: {
                            "runtime_dir": str(tmp_path / "nope"),
                            "prepared": False})
    assert doctor.main([]) == 2
    assert "尚未准备" in capsys.readouterr().out


# --------------------------------------------------------------------------
# telemetry 必须优先于日志
# --------------------------------------------------------------------------

def make_telemetry(tmp_path) -> "object":
    """造一个只有 telemetry、没有自诊断日志的任务目录。"""
    job = tmp_path / "job" / "mmm"
    job.mkdir(parents=True)
    (job / "heartbeat.json").write_text('{"protocol":1}', encoding="utf-8")
    (job / "state.json").write_text('{"scene_state":"Editing"}', encoding="utf-8")
    (job / "model_snapshot.json").write_text('{"bone_count":689}',
                                             encoding="utf-8")
    return job


def test_running_wins_over_an_empty_log(tmp_path):
    """实测踩过的坑：日志为空（旧 DLL 没埋点）但 telemetry 正常在刷，
    旧版工具却报 NOT_LOADED。telemetry 才是插件的实际产出。"""
    job = make_telemetry(tmp_path)
    code, text = doctor.diagnose(tmp_path, job_dir=job)
    assert code == "RUNNING"
    assert "正在上报" in text


def test_running_wins_even_with_a_stale_log(tmp_path):
    write_log(["静态构造：类型被首次访问（DLL 已加载）"])
    job = make_telemetry(tmp_path)
    code, _ = doctor.diagnose(tmp_path, job_dir=job)
    assert code == "RUNNING"


def test_telemetry_state_detects_partial_files(tmp_path):
    job = tmp_path / "mmm"
    job.mkdir()
    (job / "heartbeat.json").write_text("{}", encoding="utf-8")
    ok, detail = doctor.telemetry_state(job)
    assert ok is True
    assert "最近一次写入" in detail


def test_telemetry_state_without_job_dir():
    ok, detail = doctor.telemetry_state(None)
    assert ok is False
    assert "没有活动任务目录" in detail


def test_telemetry_state_with_empty_job_dir(tmp_path):
    ok, detail = doctor.telemetry_state(tmp_path)
    assert ok is False
    assert "没有任何 telemetry" in detail


def test_not_loaded_message_mentions_the_stale_build_case(tmp_path):
    """旧 DLL（埋点之前的构建）不写日志，这个可能性必须写在提示里。"""
    code, text = doctor.diagnose(tmp_path, job_dir=None)
    assert code == "NOT_LOADED"
    assert "埋点" in text


# --------------------------------------------------------------------------
# 只看当前会话 —— 日志是累积的，旧会话的 Update 不能证明现在在跑
# --------------------------------------------------------------------------

def _log_line(pid: int, text: str, *, when: str = "2026-09-20 21:16:40.806"):
    return f"{when}  [pid {pid}]  {text}"


def test_latest_session_keeps_only_the_last_pid():
    lines = [
        _log_line(111, "实例构造：MMM 创建了插件实例"),
        _log_line(111, "Update() 首次调用 frame=0 elapsed=0.03"),
        _log_line(222, "实例构造：MMM 创建了插件实例"),
    ]
    kept = doctor.latest_session(lines)
    assert len(kept) == 1
    assert "[pid 222]" in kept[0]


def test_latest_session_with_empty_input():
    assert doctor.latest_session([]) == []


def test_latest_session_without_pid_markers_returns_everything():
    lines = ["没有 pid 标记的一行", "另一行"]
    assert doctor.latest_session(lines) == lines


def test_old_session_update_does_not_fool_the_doctor(tmp_path):
    """实测踩过的坑：上一次会话跑过 Update，工具却报"现在在跑"。"""
    write_log([
        _log_line(111, "实例构造：MMM 创建了插件实例"),
        _log_line(111, "Enabled() 被调用 —— 插件已被 MMM 启用"),
        _log_line(111, "Update() 首次调用 frame=0 elapsed=0.03"),
        # ↓ 新会话，只探测到 Initialize
        _log_line(222, "静态构造：类型被首次访问（DLL 已加载）"),
        _log_line(222, "实例构造：MMM 创建了插件实例"),
        _log_line(222, "Initialize() 被调用"),
    ])
    code, text = doctor.diagnose(tmp_path, job_dir=None)
    assert code == "INSTANTIATED_NO_UPDATE", "旧会话的 Update 不该被算进来"
    assert "点一下插件的按钮" in text


def test_enabled_but_no_update_yet(tmp_path):
    write_log([
        _log_line(222, "实例构造：MMM 创建了插件实例"),
        _log_line(222, "Enabled() 被调用 —— 插件已被 MMM 启用"),
    ])
    code, _ = doctor.diagnose(tmp_path, job_dir=None)
    assert code == "ENABLED_NO_UPDATE"


def test_update_in_current_session_reports_update_ran(tmp_path):
    write_log([
        _log_line(222, "实例构造：MMM 创建了插件实例"),
        _log_line(222, "Update() 首次调用 frame=0 elapsed=0.03"),
    ])
    code, text = doctor.diagnose(tmp_path, job_dir=None)
    assert code == "UPDATE_RAN"
    assert "写文件失败" in text


def test_probe_only_session_points_at_the_button(tmp_path):
    """MMM 只探测不启用时，提示必须直接给出"点按钮"这个动作。"""
    write_log([
        _log_line(222, "实例构造：MMM 创建了插件实例"),
        _log_line(222, "ApplicationForm 被写入 MikuMikuMoving.TopForm"),
        _log_line(222, "Initialize() 被调用"),
        _log_line(222, "Dispose() 被调用"),
    ])
    code, text = doctor.diagnose(tmp_path, job_dir=None)
    assert code == "INSTANTIATED_NO_UPDATE"
    assert "探测" in text
    assert "AutoBake" in text
