"""Tests for the Stage 2.9 acceptance harness.

The harness itself has no MMM dependency for the parts under test here: the
``verify`` path re-runs the final gate against artefacts already on disk, which
is exactly what makes Stage 2.9 repeatable without reopening MMM.
"""
from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path

import pytest

from scripts import mmm_e2e
from tests.test_vmd_reader import _bone_frame, make_vmd


@pytest.fixture(autouse=True)
def isolated_localappdata(tmp_path, monkeypatch):
    """``prepare()`` 会在 LOCALAPPDATA 下建任务目录 —— 测试绝不能写进真实用户目录。"""
    local = tmp_path / "LocalAppData"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _job(tmp_path, *, added=("スカート_0_0",), probes=("センター",),
         baked_bones=None, source_bones=None, record_vmd=True) -> Path:
    """Build a minimal job directory the way the pipeline would leave it."""
    root = tmp_path / "job"
    (root / "pmx").mkdir(parents=True)
    (root / "mmm").mkdir(parents=True)

    # 源动作：层 B 需要它真实存在，且其轨道要能在 baked 里找到。
    source_vmd = root / "source.vmd"
    src_bones = source_bones if source_bones is not None else ["センター"]
    source_vmd.write_bytes(make_vmd(bone_frames=[
        _bone_frame(name, frame) for name in src_bones for frame in (0, 5, 10)]))

    manifest = {
        "source_model": "D:/models/m.pmx",
        "added_bone_names": list(added),
        "bake_probe_bone_names": list(probes),
        "missing_physics_bone_count": len(added),
    }
    if record_vmd:
        manifest["source_vmd"] = str(source_vmd)
    (root / "pmx" / "repair_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    bones = baked_bones if baked_bones is not None else list(added) + list(probes)
    bones = sorted(set(bones) | set(src_bones))     # 层 B：源轨道必须保留
    frames = []
    for name in bones:
        for frame in (0, 5, 10):
            frames.append(_bone_frame(name, frame))
    (root / "mmm" / "baked.vmd").write_bytes(make_vmd(bone_frames=frames))
    return root


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_report_passes_only_when_every_check_passes():
    report = mmm_e2e.Report()
    report.add("a", "第一项", True)
    report.add("b", "第二项", True)
    assert report.passed is True

    report.add("c", "第三项", False, "坏了")
    assert report.passed is False


def test_report_treats_skipped_as_non_blocking():
    report = mmm_e2e.Report()
    report.add("a", "第一项", True)
    report.add("b", "跳过项", False, "本次不适用", skipped=True)
    assert report.passed is True
    assert "[SKIP]" in report.render()


def test_empty_report_does_not_pass():
    assert mmm_e2e.Report().passed is False


def test_render_lists_every_check_with_its_detail():
    report = mmm_e2e.Report()
    report.add("a", "模型加载", True, "bones=689")
    report.add("b", "Bake 观测", False, "未观察到")
    text = report.render()
    assert "模型加载" in text and "bones=689" in text
    assert "Bake 观测" in text and "未观察到" in text
    assert "❌ FAIL" in text


# --------------------------------------------------------------------------
# verify —— the final gate, repeatable without MMM
# --------------------------------------------------------------------------

def test_verify_passes_on_a_complete_job(tmp_path, capsys):
    root = _job(tmp_path)
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 0
    assert (root / "mmm" / "validation.json").is_file()
    payload = json.loads((root / "mmm" / "validation.json").read_text("utf-8"))
    assert payload["ok"] is True
    assert payload["status"] == "pass"


def test_verify_fails_when_a_bone_is_missing(tmp_path):
    """The gate must actually fail — not just always pass."""
    root = _job(tmp_path, added=("スカート_0_0", "スカート_0_1"),
                baked_bones=["スカート_0_0", "センター"])
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 1
    payload = json.loads((root / "mmm" / "validation.json").read_text("utf-8"))
    assert payload["ok"] is False
    codes = {issue["code"] for issue in payload["issues"]}
    assert "VMD_EXPORT_MISSING_PHYSICS_BONES" in codes


def test_verify_refuses_an_empty_expectation(tmp_path):
    """Empty expectation would make the subset check vacuously true."""
    root = _job(tmp_path, added=(), probes=())
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 2


def test_verify_rejects_incomplete_job_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mmm_e2e.main(["verify", "--job", str(empty)]) == 2


def test_verify_rejects_job_without_baked_vmd(tmp_path):
    root = _job(tmp_path)
    (root / "mmm" / "baked.vmd").unlink()
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 2


def test_verify_uses_the_union_of_repair_and_probe_bones(tmp_path):
    """探针骨也必须被验证 —— 这是 missing==0 分支唯一的依据。"""
    root = _job(tmp_path, added=("スカート_0_0",), probes=("センター",))
    (root / "mmm" / "baked.vmd").write_bytes(make_vmd(bone_frames=[
        _bone_frame("スカート_0_0", 0), _bone_frame("スカート_0_0", 5)]))
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 1


# --------------------------------------------------------------------------
# wait helpers
# --------------------------------------------------------------------------

def test_wait_for_file_stable_returns_true_for_a_stable_file(tmp_path):
    target = tmp_path / "baked.vmd"
    target.write_bytes(b"x" * 100)
    assert mmm_e2e.wait_for_file_stable(target, timeout=3.0, quiet=0.3,
                                        poll=0.1) is True


def test_wait_for_file_stable_times_out_when_absent(tmp_path):
    assert mmm_e2e.wait_for_file_stable(tmp_path / "nope.vmd", timeout=0.5,
                                        quiet=0.2, poll=0.1) is False


def test_wait_for_file_stable_ignores_empty_file(tmp_path):
    target = tmp_path / "baked.vmd"
    target.write_bytes(b"")
    assert mmm_e2e.wait_for_file_stable(target, timeout=0.5, quiet=0.2,
                                        poll=0.1) is False


def test_wait_for_bake_returns_when_completion_is_observed(tmp_path):
    job = tmp_path / "mmm"
    job.mkdir()

    def writer():
        time.sleep(0.2)
        (job / "state.json").write_text(json.dumps({
            "scene_state": "Editing",
            "marker_position": 512,
            "bake_observed": True,
            "bake_completed_observed": True,
            "bake_stalled": False,
        }), encoding="utf-8")

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        state = mmm_e2e.wait_for_bake(job, timeout=5.0, poll=0.05)
    finally:
        thread.join()
    assert state["bake_completed_observed"] is True


def test_wait_for_bake_times_out_without_state(tmp_path):
    job = tmp_path / "mmm"
    job.mkdir()
    assert mmm_e2e.wait_for_bake(job, timeout=0.4, poll=0.1) == {}


# --------------------------------------------------------------------------
# prepare() failure paths
# --------------------------------------------------------------------------

def test_prepare_fails_cleanly_when_pmx_is_missing(tmp_path):
    ctx = mmm_e2e.prepare(tmp_path / "nope.pmx", tmp_path / "nope.vmd",
                          tmp_path / "mmm", isolate=False)
    report = ctx["report"]
    assert report.passed is False
    assert report.checks[0].key == "prep"


def test_prepare_reports_bypass_for_a_model_without_physics(tmp_path):
    from tests.test_pmx_physics_displayframe import make_pmx
    pmx = tmp_path / "static.pmx"
    pmx.write_bytes(make_pmx(bones=[("センター", True)], rigid_bodies=[],
                             display_frames=[("Root", [0])]))
    vmd = tmp_path / "m.vmd"
    vmd.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", 0)]))

    ctx = mmm_e2e.prepare(pmx, vmd, tmp_path / "mmm", isolate=False)

    report = ctx["report"]
    assert ctx.get("bypassed") is True
    assert report.passed is True
    assert any(c.key == "bypass" and c.ok for c in report.checks)


def test_prepare_routes_unrepresentable_names_to_alias(tmp_path):
    """名字写不进 VMD 不再拒绝模型 —— 改为自动生成别名工作模型。

    以前这里断言 report.passed is False；现在应当通过，
    并报告"改用别名"这一项（方案《VMD 骨骼名限制自动 Alias》§18 / §19）。
    """
    from tests.test_pmx_physics_displayframe import make_pmx
    bad_name = "左发_0_1"          # 简体字「发」不在 CP932 里
    pmx = tmp_path / "long.pmx"
    pmx.write_bytes(make_pmx(
        bones=[("センター", True), (bad_name, True)],
        rigid_bodies=[(bad_name, 1, 1)],
        display_frames=[("Root", [0, 1])]))
    vmd = tmp_path / "m.vmd"
    vmd.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", 0)]))

    ctx = mmm_e2e.prepare(pmx, vmd, tmp_path / "mmm", isolate=False)

    report = ctx["report"]
    # 前置阶段不该再有"名称不可表示"的失败项。
    prep_checks = [c for c in report.checks if c.key in ("prep", "representable")]
    assert all(c.ok for c in prep_checks), "名称问题不该再让前置阶段失败"
    alias_checks = [c for c in report.checks if c.key == "alias"]
    assert alias_checks, "应当报告别名路由"
    assert "改用别名" in alias_checks[0].detail
    # MMM 必须加载别名模型，否则找不到骨骼。
    assert ctx["prep"].uses_vmd_alias is True


# --------------------------------------------------------------------------
# 人工确认 Gate（方案 §26）
# --------------------------------------------------------------------------

def test_stdin_is_interactive_never_raises():
    assert mmm_e2e.stdin_is_interactive() in (True, False)


def test_confirm_step_skips_cleanly_when_not_interactive(monkeypatch, capsys):
    """非交互环境必须明确告知跳过，绝不静默当成已确认。"""
    monkeypatch.setattr(mmm_e2e, "stdin_is_interactive", lambda: False)
    assert mmm_e2e.confirm_step("模型与动作", ["模型: a.pmx"]) is True
    out = capsys.readouterr().out
    assert "跳过人工确认" in out


def test_confirm_step_accepts_on_enter(monkeypatch):
    monkeypatch.setattr(mmm_e2e, "stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "")
    assert mmm_e2e.confirm_step("标题", ["x"]) is True


def test_confirm_step_rejects_on_stop(monkeypatch):
    monkeypatch.setattr(mmm_e2e, "stdin_is_interactive", lambda: True)
    for answer in ("s", "stop", "n", "no"):
        monkeypatch.setattr("builtins.input", lambda a=answer: a)
        assert mmm_e2e.confirm_step("标题", ["x"]) is False


def test_confirm_step_handles_eof(monkeypatch):
    """Ctrl-D / 管道输入结束不能抛异常，只能算未确认。"""
    monkeypatch.setattr(mmm_e2e, "stdin_is_interactive", lambda: True)

    def boom():
        raise EOFError()

    monkeypatch.setattr("builtins.input", boom)
    assert mmm_e2e.confirm_step("标题", ["x"]) is False


def test_confirm_step_shows_the_context(monkeypatch, capsys):
    monkeypatch.setattr(mmm_e2e, "stdin_is_interactive", lambda: False)
    mmm_e2e.confirm_step("模型与动作", ["模型: D:/ProcessedPMX.pmx",
                                        "骨骼 689 根"])
    out = capsys.readouterr().out
    assert "D:/ProcessedPMX.pmx" in out
    assert "骨骼 689 根" in out


def test_observe_skips_bake_when_not_confirmed(tmp_path):
    """操作者判定不正常时必须停止，而不是继续等 Bake。"""
    report = mmm_e2e.Report()
    report.add("model", "模型加载", True, "bones=1")
    report.add("motion", "动作加载", True, "1 根有帧")

    class _Prep:
        effective_model = "D:/m.pmx"
        repair_expected_bones: list = []
        expected_bones: list = []

    class _Req:
        job_dir = str(tmp_path)
        source_vmd = "D:/s.vmd"

    class _Source:
        bone_frame_range = (0, 30)

    ctx = {"report": report, "prep": _Prep(), "request": _Req(),
           "source": _Source(), "job_root": tmp_path}

    mmm_e2e.observe_and_verify(ctx, timeout=1.0, confirm=True)
    # 非交互环境下 confirm_step 返回 True，因此会走到"等 Bake"并超时；
    # 关键是结果里出现了人工确认这一项。
    assert any(c.key == "manual_confirm" for c in report.checks)


def test_manual_confirm_check_fails_the_report_when_rejected():
    report = mmm_e2e.Report()
    report.add("model", "模型加载", True)
    report.add("manual_confirm", "人工确认模型与动作正常", False, "操作者判定异常")
    assert report.passed is False


# --------------------------------------------------------------------------
# 烘焙动作文件定位（MMM 默认导出名会带后缀）
# --------------------------------------------------------------------------

def test_find_baked_prefers_exact_name(tmp_path):
    (tmp_path / "baked.vmd").write_bytes(b"x" * 100)
    (tmp_path / "Prhythmatic_baked.vmd").write_bytes(b"y" * 200)
    assert mmm_e2e.find_baked_vmd(tmp_path).name == "baked.vmd"


def test_find_baked_accepts_suffixed_name(tmp_path):
    """MMM 的"导出动作"默认沿用原名加后缀，操作者通常直接确认。"""
    (tmp_path / "Prhythmatic攝晍梡3.0baked.vmd").write_bytes(b"z" * 300)
    found = mmm_e2e.find_baked_vmd(tmp_path)
    assert found is not None
    assert found.name == "Prhythmatic攝晍梡3.0baked.vmd"


def test_find_baked_is_case_insensitive(tmp_path):
    (tmp_path / "Model_BAKED.vmd").write_bytes(b"z" * 10)
    assert mmm_e2e.find_baked_vmd(tmp_path) is not None


def test_find_baked_ignores_unrelated_vmd(tmp_path):
    """不能拿一个不相干的 VMD 去"验证通过"。"""
    (tmp_path / "source.vmd").write_bytes(b"z" * 10)
    (tmp_path / "camera.vmd").write_bytes(b"z" * 10)
    assert mmm_e2e.find_baked_vmd(tmp_path) is None


def test_find_baked_ignores_empty_files(tmp_path):
    (tmp_path / "baked.vmd").write_bytes(b"")
    assert mmm_e2e.find_baked_vmd(tmp_path) is None


def test_find_baked_ignores_directories_named_baked(tmp_path):
    (tmp_path / "baked.vmd").mkdir()
    assert mmm_e2e.find_baked_vmd(tmp_path) is None


def test_find_baked_returns_none_on_empty_dir(tmp_path):
    assert mmm_e2e.find_baked_vmd(tmp_path) is None


def test_find_baked_picks_the_newest_candidate(tmp_path):
    import os
    old = tmp_path / "a_baked.vmd"
    old.write_bytes(b"old" * 10)
    new = tmp_path / "b_baked.vmd"
    new.write_bytes(b"new" * 10)
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    assert mmm_e2e.find_baked_vmd(tmp_path) == new


def test_wait_for_baked_returns_once_stable(tmp_path):
    import threading

    def writer():
        time.sleep(0.2)
        (tmp_path / "Modelbaked.vmd").write_bytes(b"v" * 512)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        found = mmm_e2e.wait_for_baked_vmd(tmp_path, timeout=5.0,
                                           quiet=0.3, poll=0.05)
    finally:
        thread.join()
    assert found is not None and found.name == "Modelbaked.vmd"


def test_wait_for_baked_times_out(tmp_path):
    assert mmm_e2e.wait_for_baked_vmd(tmp_path, timeout=0.4,
                                      quiet=0.2, poll=0.1) is None


def test_verify_accepts_a_suffixed_baked_file(tmp_path):
    """verify 子命令也必须认得带后缀的文件名。"""
    root = _job(tmp_path)
    exact = root / "mmm" / "baked.vmd"
    suffixed = root / "mmm" / "MyModelbaked.vmd"
    exact.rename(suffixed)
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 0


def test_verify_reports_missing_baked_clearly(tmp_path, capsys):
    root = _job(tmp_path)
    (root / "mmm" / "baked.vmd").unlink()
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 2
    assert "baked" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 走别名时，verify 的期望清单必须转换成别名
# --------------------------------------------------------------------------

def test_verify_maps_expected_names_to_aliases(tmp_path, capsys):
    """烘焙后的 VMD 里存的是别名 —— 拿原名比对会全部匹配不上。

    这个坑的表现很像"烘焙失败"：层 C 会报告期望骨骼一根都不存在。
    """
    import json as _json
    root = _job(tmp_path, added=("左发_0_1",), probes=(),
                baked_bones=["PB000007"], source_bones=[])
    # 别名模型/动作 + 映射文件
    (root / "pmx" / "vmd_alias_map.json").write_text(_json.dumps({
        "version": 1, "model": "m.pmx",
        "bones": [{"bone_index": 7, "original_name": "左发_0_1",
                   "alias": "PB000007"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "pmx" / "source_alias.vmd").write_bytes(
        make_vmd(bone_frames=[]))

    code = mmm_e2e.main(["verify", "--job", str(root)])

    out = capsys.readouterr().out
    assert "该任务使用 VMD 别名" in out
    assert code == 0, "别名转换后期望清单应当能对上"


def test_verify_without_alias_map_keeps_original_names(tmp_path):
    """没有映射文件时行为不变 —— 不能影响已跑通的原流程。"""
    root = _job(tmp_path)
    assert mmm_e2e.main(["verify", "--job", str(root)]) == 0
