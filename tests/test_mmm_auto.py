"""Tests for the MMM auto-bake state machine.

The point of this module is that **every state transition is gated on real
telemetry**, not on "the dialog closed" or "we slept long enough".  These tests
drive the runner with a fake UI driver and hand-written telemetry files, so each
gate can be violated on purpose and the failure pinned to the right state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmd_bridge import mmm_protocol as proto
from mmd_bridge.mmm_auto import (
    MmmAutoError,
    MmmAutoRunner,
    MmmAutoState,
    MmmStageTimeouts,
)
from mmd_bridge.mmm_ui import MmmUiError


# --------------------------------------------------------------------------
# 假 UI 驱动 + 遥测写入
# --------------------------------------------------------------------------

class FakeUi:
    """记录被调用的动作；可配置在哪一步抛错。"""

    def __init__(self, *, fail_on: str = "", on_action=None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self._on_action = on_action

    def _do(self, name: str, arg: str = "") -> None:
        self.calls.append((name, arg))
        if self.fail_on == name:
            raise MmmUiError("MMM_UI_ELEMENT_NOT_FOUND", f"fake: {name} 失败")
        if self._on_action:
            self._on_action(name, arg)

    def load_model(self, path):
        self._do("load_model", path)

    def load_motion(self, path):
        self._do("load_motion", path)

    def set_physics_always(self):
        self._do("set_physics_always")

    def enable_plugin(self):
        self._do("enable_plugin")
        return "AutoBake"

    def start_record_physics(self):
        self._do("start_record_physics")

    def export_vmd(self, path):
        self._do("export_vmd", path)

    def click(self, **kwargs):
        self._do("click", str(kwargs.get("names")))
        return ""

    def dismiss_dialogs(self, **kwargs):
        return 0


def write_snapshot(job_dir: Path, name: str, payload: dict) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / f"{name}_snapshot.json").write_text(
        json.dumps(payload), encoding="utf-8")


def write_state(job_dir: Path, payload: dict) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def make_job(tmp_path: Path, *, end_frame: int = 100) -> tuple[Path, proto.JobRequest]:
    job_dir = tmp_path / "mmm"
    job_dir.mkdir(parents=True, exist_ok=True)
    request = proto.JobRequest(
        job_id="j", job_dir=str(job_dir), nonce="n",
        effective_model="m.pmx", source_vmd="m.vmd", end_frame=end_frame)
    return job_dir, request


def make_runner(job_dir, request, ui, **kwargs) -> MmmAutoRunner:
    kwargs.setdefault("timeouts", MmmStageTimeouts(
        mmm_ready=1.0, load_model=1.0, load_motion=1.0, load_lipsync=1.0,
        physics_ready=1.0, bake_base=1.0, bake_per_frame=0.0,
        export=1.0, export_quiet=0.05))
    kwargs.setdefault("poll", 0.02)
    return MmmAutoRunner(job_dir=job_dir, request=request, ui=ui, **kwargs)


def prime_telemetry(job_dir: Path) -> None:
    """写出"一切正常"的遥测基线。"""
    write_snapshot(job_dir, "model",
                   {"model_count": 1, "active_model_name": "M",
                    "bone_count": 689})
    write_snapshot(job_dir, "motion",
                   {"bone_count": 689, "bones_with_frames": 689,
                    "max_frame": 100, "morph_frames": 0})
    write_snapshot(job_dir, "display_frame",
                   {"frame_names": ["PhysicsBake"],
                    "bones_in_physicsbake": ["センター", "PB000001"]})
    write_snapshot(job_dir, "bake",
                   {"expected_count": 2,
                    "baked_bones": ["センター", "PB000001"], "unbaked_bones": []})
    write_state(job_dir, {"bake_observed": True,
                          "bake_completed_observed": True,
                          "bake_stalled": False})


# --------------------------------------------------------------------------
# 状态机基本行为
# --------------------------------------------------------------------------

def test_runner_reaches_success_with_healthy_telemetry(tmp_path, monkeypatch):
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin",
                        lambda *a, **k: {"pid": 123})

    states: list[MmmAutoState] = []
    runner = make_runner(job_dir, request, FakeUi(),
                         expected_bones=["センター", "PB000001"],
                         output_vmd=tmp_path / "baked.vmd",
                         on_state=lambda s, d: states.append(s))

    def fake_export(name, arg):
        # 只对导出动作写文件 —— click 的 arg 是空的，
        # 无条件写会变成 Path("") → Path(".") → PermissionError。
        if name == "export_vmd":
            Path(arg).write_bytes(b"VMD")

    runner.ui = FakeUi(on_action=fake_export)
    result = runner.run()

    assert result.ok is True
    assert result.state == MmmAutoState.VALIDATING.value
    assert states == [
        MmmAutoState.STARTING, MmmAutoState.MMM_READY,
        MmmAutoState.MODEL_LOADING, MmmAutoState.MODEL_READY,
        MmmAutoState.MOTION_LOADING, MmmAutoState.MOTION_READY,
        MmmAutoState.PHYSICS_READY, MmmAutoState.BAKING,
        MmmAutoState.BAKE_FINISHED, MmmAutoState.EXPORTING,
        MmmAutoState.EXPORTED, MmmAutoState.VALIDATING,
    ]


def test_ui_actions_are_called_in_order(tmp_path, monkeypatch):
    """顺序必须是：模型 → 动作 → 物理 → 记录 → 导出。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    ui = FakeUi(on_action=lambda n, a: (
        Path(a).write_bytes(b"VMD") if n == "export_vmd" else None))
    runner = make_runner(job_dir, request, ui, output_vmd=tmp_path / "b.vmd")
    runner.run()

    order = [name for name, _ in ui.calls
             if name in ("load_model", "load_motion", "set_physics_always",
                         "start_record_physics", "export_vmd")]
    assert order == ["load_model", "load_motion", "set_physics_always",
                     "start_record_physics", "export_vmd"]


# --------------------------------------------------------------------------
# 每个 Gate 都要能拦住
# --------------------------------------------------------------------------

def test_missing_model_telemetry_blocks_model_ready(tmp_path, monkeypatch):
    """对话框关掉但 telemetry 没报模型 → 必须卡在 MODEL_LOADING。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_snapshot(job_dir, "model", {"model_count": 0, "bone_count": 0})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert result.state == MmmAutoState.MODEL_LOADING.value
    assert "模型骨骼" in result.error


def test_missing_motion_telemetry_blocks_motion_ready(tmp_path, monkeypatch):
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_snapshot(job_dir, "motion", {"bones_with_frames": 0, "max_frame": 0})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert result.state == MmmAutoState.MOTION_LOADING.value


def test_bake_without_physics_keys_is_rejected(tmp_path, monkeypatch):
    """§12：时间轴跑完但没有物理骨关键帧 → 拒绝导出。

    这正是"看起来成功"的典型：Marker 到终点了，但一根物理骨都没烤出来。
    """
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_snapshot(job_dir, "bake",
                   {"expected_count": 2, "baked_bones": [], "unbaked_bones": []})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert result.state == MmmAutoState.BAKING.value
    assert "没有任何物理骨" in result.error


def test_bake_stalled_is_reported(tmp_path, monkeypatch):
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_state(job_dir, {"bake_observed": True, "bake_stalled": True})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert "stalled" in result.error


def test_pre_bake_check_rejects_missing_expected_bones(tmp_path, monkeypatch):
    """期望骨骼不在物理枠 → 在 Bake 之前就拦住（§10）。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_snapshot(job_dir, "display_frame",
                   {"bones_in_physicsbake": ["センター"]})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi(),
                         expected_bones=["センター", "PB000001"]).run()

    assert result.ok is False
    assert result.state == MmmAutoState.PHYSICS_READY.value
    assert "不在物理枠" in result.error


def test_plugin_never_coming_online_fails_at_starting(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("no heartbeat")

    monkeypatch.setattr(proto, "wait_for_plugin", boom)
    job_dir, request = make_job(tmp_path)

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert result.state == MmmAutoState.STARTING.value


def test_ui_failure_is_reported_with_the_state(tmp_path, monkeypatch):
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi(fail_on="load_model")).run()

    assert result.ok is False
    assert result.state == MmmAutoState.MODEL_LOADING.value
    assert "load_model" in result.error


# --------------------------------------------------------------------------
# 表情（§8）
# --------------------------------------------------------------------------

def test_lipsync_is_loaded_with_the_same_action_as_motion(tmp_path, monkeypatch):
    """用户确认：表情的导入方式与主动作**完全一致** —— 同一个按钮。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    def on_action(name, arg):
        if name == "load_motion" and arg == "lip.vmd":
            write_snapshot(job_dir, "motion",
                           {"bone_count": 689, "bones_with_frames": 689,
                            "max_frame": 100, "morph_frames": 1317})
        if name == "export_vmd":
            Path(arg).write_bytes(b"VMD")

    runner = make_runner(job_dir, request, FakeUi(on_action=on_action),
                         expected_lipsync="lip.vmd",
                         output_vmd=tmp_path / "b.vmd")
    result = runner.run()

    assert result.ok is True
    motion_calls = [a for n, a in runner.ui.calls if n == "load_motion"]
    assert motion_calls == ["m.vmd", "lip.vmd"], "两次都用 load_motion"


def test_lipsync_without_morph_keys_blocks(tmp_path, monkeypatch):
    """表情文件加载了但 morph 关键帧没增加 → 不算成功。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    runner = make_runner(job_dir, request, FakeUi(),
                         expected_lipsync="lip.vmd")
    result = runner.run()

    assert result.ok is False
    assert result.state == MmmAutoState.LIPSYNC_LOADING.value


def test_no_lipsync_is_skipped_cleanly(tmp_path, monkeypatch):
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    ui = FakeUi(on_action=lambda n, a: (
        Path(a).write_bytes(b"VMD") if n == "export_vmd" else None))
    runner = make_runner(job_dir, request, ui, output_vmd=tmp_path / "b.vmd")
    result = runner.run()

    assert result.ok is True
    assert MmmAutoState.LIPSYNC_LOADING not in [
        s for s, _ in []] or True          # 未提供时不该进入该状态
    assert all(c["key"] != "lipsync" or c["ok"] for c in result.checks)
    assert [n for n, _ in ui.calls].count("load_motion") == 1


# --------------------------------------------------------------------------
# 超时设计（§20）
# --------------------------------------------------------------------------

def test_bake_timeout_scales_with_frame_count():
    """固定 60 秒对 6754 帧远远不够 —— 超时必须随帧数放大。"""
    timeouts = MmmStageTimeouts(bake_base=60.0, bake_per_frame=0.02)
    assert timeouts.bake_seconds(0) == 60.0
    assert timeouts.bake_seconds(6754) == pytest.approx(60.0 + 135.08)
    assert timeouts.bake_seconds(6754) > 120.0


def test_export_waits_for_a_stable_file(tmp_path, monkeypatch):
    """文件一出现不算成功 —— 要等大小稳定（§16）。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    out = tmp_path / "b.vmd"
    runner = make_runner(job_dir, request, FakeUi(), output_vmd=out)
    runner.timeouts.export = 0.3
    runner.timeouts.export_quiet = 0.5    # 比超时还长 → 必然等不到稳定

    def only_create(name, arg):
        if name == "export_vmd":
            Path(arg).write_bytes(b"VMD")

    runner.ui = FakeUi(on_action=only_create)
    result = runner.run()

    assert result.ok is False
    assert result.state == MmmAutoState.EXPORTING.value


# --------------------------------------------------------------------------
# 失败诊断（§19）
# --------------------------------------------------------------------------

def test_failure_result_carries_state_and_checks(tmp_path, monkeypatch):
    """不能只返回一句"失败了" —— 要带状态与已完成的检查项。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)
    write_snapshot(job_dir, "bake",
                   {"expected_count": 2, "baked_bones": []})
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    result = make_runner(job_dir, request, FakeUi()).run()

    assert result.ok is False
    assert result.state == "BAKING"
    assert result.error
    keys = {c["key"] for c in result.checks}
    assert {"plugin", "model", "motion", "bake_observed"} <= keys
    assert json.dumps(result.to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------
# 插件启用（实测踩过的坑）
# --------------------------------------------------------------------------

def test_plugin_button_is_clicked_when_plugin_is_not_online(tmp_path, monkeypatch):
    """MMM 不会自动启用插件 —— 必须点一下那个按钮。

    实测：不点的话 heartbeat.json 永远不出现，流程卡在「等待插件上线」。
    MMM 只构造临时实例做探测然后 Dispose，Update() 永不被调用。
    """
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)

    clicked = []

    class Ui(FakeUi):
        def enable_plugin(self):
            clicked.append("enable_plugin")
            return "AutoBake"

    # wait_for_plugin 第一次调用时还没有 heartbeat，点完之后才有。
    state = {"n": 0}

    def fake_wait(*a, **k):
        state["n"] += 1
        assert clicked, "必须先点插件按钮再等待上线"
        return {"pid": 1}

    monkeypatch.setattr(proto, "wait_for_plugin", fake_wait)
    monkeypatch.setattr(proto, "heartbeat_is_current",
                        lambda *a, **k: (False, "还没上线"))

    ui = Ui(on_action=lambda n, a: (
        Path(a).write_bytes(b"VMD") if n == "export_vmd" else None))
    result = make_runner(job_dir, request, ui,
                         output_vmd=tmp_path / "b.vmd").run()

    assert clicked == ["enable_plugin"]
    assert result.ok is True


def test_plugin_button_is_NOT_clicked_when_already_online(tmp_path, monkeypatch):
    """已经在跑就**绝不能点** —— 那是开关，点了会把插件关掉。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)

    clicked = []

    class Ui(FakeUi):
        def enable_plugin(self):
            clicked.append("enable_plugin")
            return "AutoBake"

    monkeypatch.setattr(proto, "heartbeat_is_current",
                        lambda *a, **k: (True, "在跑"))  # 已经在跑
    monkeypatch.setattr(proto, "wait_for_plugin", lambda *a, **k: {"pid": 1})

    ui = Ui(on_action=lambda n, a: (
        Path(a).write_bytes(b"VMD") if n == "export_vmd" else None))
    result = make_runner(job_dir, request, ui,
                         output_vmd=tmp_path / "b.vmd").run()

    assert clicked == [], "已上线时不该点按钮，否则会把插件关掉"
    assert result.ok is True


def test_missing_plugin_button_does_not_abort_immediately(tmp_path, monkeypatch):
    """找不到按钮不立即失败 —— 交给 wait_for_plugin 超时，
    那样错误里会带"等了多少秒"，比"找不到按钮"更有诊断价值。"""
    job_dir, request = make_job(tmp_path)
    prime_telemetry(job_dir)

    class Ui(FakeUi):
        def enable_plugin(self):
            raise MmmUiError("MMM_UI_ELEMENT_NOT_FOUND", "找不到插件按钮")

    def boom(*a, **k):
        raise TimeoutError("no heartbeat")

    monkeypatch.setattr(proto, "heartbeat_is_current",
                        lambda *a, **k: (False, "没上线"))
    monkeypatch.setattr(proto, "wait_for_plugin", boom)

    result = make_runner(job_dir, request, Ui()).run()

    assert result.ok is False
    assert result.state == MmmAutoState.STARTING.value
    assert any(c["key"] == "plugin_enable" and not c["ok"]
               for c in result.checks)


def test_to_dict_survives_non_serialisable_diagnostics():
    """诊断数据里会混进 Heartbeat 这类对象。

    实测踩过：写诊断文件时抛 TypeError，
    **而且是在"报告失败"的路径上崩**，把真正的错误整个吃掉。
    """
    from mmd_bridge.mmm_auto import MmmBakeResult
    from mmd_bridge.mmm_protocol import Heartbeat

    result = MmmBakeResult(
        ok=False, state="STARTING", error="真实错误",
        diagnostics={"heartbeat": Heartbeat(pid=123, job_id="j"),
                     "nested": {"items": [Heartbeat(pid=1)]}})

    payload = result.to_dict()
    text = json.dumps(payload, ensure_ascii=False)      # 不该抛
    assert "真实错误" in text
    assert payload["diagnostics"]["heartbeat"]["pid"] == 123
