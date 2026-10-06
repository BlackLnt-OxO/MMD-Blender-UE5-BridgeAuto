"""Unit tests for Stage 2.4 character preprocessing.

Covers the three branches the new main flow must distinguish, plus the two
mistakes that are easy to make and expensive to debug:

* treating a model with **no physics bones** as a failure instead of a bypass;
* treating a model whose physics bones are **all undisplayed** as a bypass
  instead of the primary repair target.
"""
from __future__ import annotations

import json

import pytest

from mmd_bridge.character_prep import (
    MAX_BAKE_PROBES,
    NO_PHYSICS_BONES,
    PHYSICS_BONES_NOT_VMD_REPRESENTABLE,
    CharacterPrepError,
    prepare_character_model,
    select_bake_probes,
)
from mmd_bridge.pmx_analyzer import analyze_model
from mmd_bridge.pmx_reader import read_pmx
from tests.test_pmx_physics_displayframe import make_pmx


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _write(tmp_path, name, **kwargs):
    target = tmp_path / name
    target.write_bytes(make_pmx(**kwargs))
    return target


def _model_with_physics(tmp_path, *, displayed: bool):
    """Two bones, one physics-driven; ``displayed`` controls the display frame."""
    frames = [("Root", [0, 1])] if displayed else [("Root", [0])]
    return _write(tmp_path, "m.pmx",
                  bones=[("センター", True), ("スカート01", True)],
                  rigid_bodies=[("スカート01", 1, 1)],
                  display_frames=frames)


# --------------------------------------------------------------------------
# Branch F — zero physics bones: bypass, not failure
# --------------------------------------------------------------------------

def test_zero_physics_bones_bypasses_mmm(tmp_path):
    source = _write(tmp_path, "static.pmx",
                    bones=[("センター", True), ("頭", True)],
                    rigid_bodies=[],
                    display_frames=[("Root", [0, 1])])
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.mmm_skipped is True
    assert result.mmm_skipped_reason == NO_PHYSICS_BONES
    assert result.physics_bone_count == 0


def test_zero_physics_bypass_does_not_raise_probe_unavailable(tmp_path):
    """A model with nothing to bake is healthy, not unverifiable."""
    source = _write(tmp_path, "static.pmx", rigid_bodies=[])
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
    assert result.mmm_skipped is True


def test_zero_physics_bypass_keeps_source_model_and_motion(tmp_path):
    source = _write(tmp_path, "static.pmx", rigid_bodies=[])
    motion = tmp_path / "source.vmd"
    result, _ = prepare_character_model(
        source, source_vmd=motion, output_dir=tmp_path / "job")

    assert result.effective_model == str(source)
    assert result.effective_motion == str(motion)
    assert result.expected_bones == []


def test_zero_physics_bypass_creates_no_processed_pmx(tmp_path):
    source = _write(tmp_path, "static.pmx", rigid_bodies=[])
    job = tmp_path / "job"
    prepare_character_model(source, output_dir=job)
    assert not (job / "ProcessedPMX.pmx").exists()


def test_zero_physics_bypass_records_reason_in_manifest(tmp_path):
    source = _write(tmp_path, "static.pmx", rigid_bodies=[])
    job = tmp_path / "job"
    result, _ = prepare_character_model(source, output_dir=job)

    manifest = json.loads((job / "repair_manifest.json").read_text("utf-8"))
    assert manifest["mmm_skipped"] is True
    assert manifest["mmm_skipped_reason"] == NO_PHYSICS_BONES
    assert manifest["physics_bone_count"] == 0
    assert result.repair_manifest["modified"] is False


# --------------------------------------------------------------------------
# The A/B distinction — must not be conflated
# --------------------------------------------------------------------------

def test_physics_bones_all_undisplayed_is_repaired_not_bypassed(tmp_path):
    """`displayed == 0` is the repair target, never a bypass."""
    source = _model_with_physics(tmp_path, displayed=False)
    job = tmp_path / "job"
    result, _ = prepare_character_model(source, output_dir=job)

    assert result.mmm_skipped is False
    assert result.physics_bone_count == 1
    assert result.displayed_physics_bone_count == 0
    assert result.missing_physics_bone_count == 1
    assert result.repaired is True
    assert result.repair_expected_bones == ["スカート01"]


# --------------------------------------------------------------------------
# Branch with repair
# --------------------------------------------------------------------------

def test_missing_bones_triggers_repair_and_writes_processed_pmx(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    job = tmp_path / "job"
    result, _ = prepare_character_model(source, output_dir=job)

    assert (job / "ProcessedPMX.pmx").exists()
    assert result.effective_model == str(job / "ProcessedPMX.pmx")


def test_repair_is_verified_to_zero(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.missing_after_repair == 0
    assert result.missing_physics_bone_count == 1     # source figure preserved


def test_source_model_is_never_modified(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    before = source.read_bytes()
    prepare_character_model(source, output_dir=tmp_path / "job")
    assert source.read_bytes() == before


def test_repair_manifest_carries_added_bones(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.repair_manifest["modified"] is True
    assert result.repair_manifest["added_bone_names"] == ["スカート01"]
    assert result.repair_manifest["missing_physics_bone_count"] == 1


# --------------------------------------------------------------------------
# Branch without repair — the validator-expectation trap
# --------------------------------------------------------------------------

def test_no_repair_keeps_source_as_effective_model(tmp_path):
    source = _model_with_physics(tmp_path, displayed=True)
    job = tmp_path / "job"
    result, _ = prepare_character_model(source, output_dir=job)

    assert result.repaired is False
    assert result.effective_model == str(source)
    assert not (job / "ProcessedPMX.pmx").exists()


def test_no_repair_still_yields_expectations(tmp_path):
    """`missing == 0` must not leave the expectation list empty.

    An empty list makes the validator's ``expected ⊆ actual`` pass
    unconditionally, and passing only the manifest would skip layers C/D
    entirely — so probes are the only thing standing between this branch and
    an unverifiable bake.
    """
    source = _model_with_physics(tmp_path, displayed=True)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.repair_expected_bones == []
    assert result.bake_probe_bones == ["スカート01"]
    assert result.expected_bones == ["スカート01"]


def test_no_repair_manifest_has_probe_names(tmp_path):
    source = _model_with_physics(tmp_path, displayed=True)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    manifest = result.repair_manifest
    assert manifest["modified"] is False
    assert manifest["added_bone_names"] == []
    assert manifest["bake_probe_bone_names"] == ["スカート01"]


def test_expected_bones_never_empty_for_non_bypassed_model(tmp_path):
    for displayed in (True, False):
        source = _model_with_physics(tmp_path, displayed=displayed)
        result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
        assert result.mmm_skipped is False
        assert result.expected_bones, "非旁路模型的期望清单不能为空"


def test_expected_bones_is_union_of_repair_and_probe(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
    assert set(result.expected_bones) == (
        set(result.repair_expected_bones) | set(result.bake_probe_bones))


# --------------------------------------------------------------------------
# Representability gate
# --------------------------------------------------------------------------

def test_unencodable_name_gets_an_alias_instead_of_refusal(tmp_path):
    """简体字「发」不在 CP932 里 —— 以前直接拒绝模型，现在自动生成别名。

    方案《VMD 骨骼名限制自动 Alias》：VMD 的编码限制只是**中间传输格式**
    的限制，不该升级成整个流程的兼容性限制。
    """
    bad_name = "左发_0_1"
    source = _write(tmp_path, "long.pmx",
                    bones=[("センター", True), (bad_name, True)],
                    rigid_bodies=[(bad_name, 1, 1)],
                    display_frames=[("Root", [0, 1])])

    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.uses_vmd_alias is True
    assert result.alias_index_map == {1: "PB000001"}
    assert result.alias_name_map == {"PB000001": bad_name}
    assert result.alias_model.endswith('mmm_working_alias.pmx')
    assert result.alias_map_path.endswith('vmd_alias_map.json')


def test_gate_refuses_name_that_collides_after_truncation(tmp_path):
    a = "スカート_10_100"       # 15 bytes
    b = "スカート_10_1000"      # 16 bytes, truncates onto `a`
    source = _write(tmp_path, "collide.pmx",
                    bones=[("センター", True), (a, True), (b, True)],
                    rigid_bodies=[(a, 1, 1), (b, 2, 1)],
                    display_frames=[("Root", [0, 1, 2])])

    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    # 两根骨都必须拿到**不同**的别名，否则 VMD 里分不清。
    assert result.uses_vmd_alias is True
    aliases = [result.alias_index_map[i] for i in (1, 2)]
    assert len(set(aliases)) == 2
    assert result.alias_summary["reasons"].get("truncation_collision") == 2


def test_gate_allows_exactly_15_byte_name(tmp_path):
    name = "スカート_10_100"     # exactly 15 bytes — legal
    source = _write(tmp_path, "ok.pmx",
                    bones=[("センター", True), (name, True)],
                    rigid_bodies=[(name, 1, 1)],
                    display_frames=[("Root", [0, 1])])
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
    assert result.bake_probe_bones == [name]


def test_alias_applies_even_when_no_repair_was_needed(tmp_path):
    """**修复无关**的模型也可能不可表示 —— 这正是最常见的真实情况。

    芭芭拉.pmx 就是如此：不需要修复，但 34 根物理骨用了简体字。
    所以名称路由必须独立于修复路径运行。
    """
    bad_name = "左发_0_1"
    source = _write(tmp_path, "long.pmx",
                    bones=[("センター", True), (bad_name, True)],
                    rigid_bodies=[(bad_name, 1, 1)],
                    display_frames=[("Root", [0, 1])])   # nothing missing

    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.repaired is False, "这个模型不需要修复"
    assert result.uses_vmd_alias is True, "但仍需要别名"


def test_probes_use_the_alias_names(tmp_path):
    """探针名必须是**最终名字**（别名）—— 断言要对着烘焙后的 VMD 做。"""
    bad_name = "左发_0_1"
    source = _write(tmp_path, "long.pmx",
                    bones=[("センター", True), (bad_name, True)],
                    rigid_bodies=[(bad_name, 1, 1)],
                    display_frames=[("Root", [0, 1])])

    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")

    assert result.bake_probe_bones == ["PB000001"]
    assert bad_name not in result.bake_probe_bones


# --------------------------------------------------------------------------
# Probe selection
# --------------------------------------------------------------------------

def test_select_bake_probes_only_from_usable_candidates():
    from tests.test_pmx_physics_displayframe import model_from
    model = model_from(
        bones=[("センター", True), ("ok_bone", True)],
        rigid_bodies=[("ok_bone", 1, 1)],
        display_frames=[("Root", [0, 1])])
    probes, unusable = select_bake_probes(analyze_model(model))
    assert probes == ["ok_bone"]
    assert unusable == []


def test_select_bake_probes_filters_before_sampling():
    """A long name must not consume a probe slot."""
    from tests.test_pmx_physics_displayframe import model_from
    model = model_from(
        bones=[("センター", True),
               ("Bone_C2_04_01_ChiBang_LM_01", True),   # unusable, lower index
               ("ok_bone", True)],
        rigid_bodies=[("Bone_C2_04_01_ChiBang_LM_01", 1, 1), ("ok_bone", 2, 1)],
        display_frames=[("Root", [0, 1, 2])])
    probes, unusable = select_bake_probes(analyze_model(model), limit=1)
    assert probes == ["ok_bone"], "取前 N 个会选中不可用名称"
    assert unusable == ["Bone_C2_04_01_ChiBang_LM_01"]


def test_select_bake_probes_respects_limit():
    from tests.test_pmx_physics_displayframe import model_from
    bones = [("センター", True)] + [(f"b{i:02d}", True) for i in range(10)]
    bodies = [(f"b{i:02d}", i + 1, 1) for i in range(10)]
    model = model_from(bones=bones, rigid_bodies=bodies,
                       display_frames=[("Root", list(range(11)))])
    probes, _ = select_bake_probes(analyze_model(model), limit=3)
    assert len(probes) == 3


def test_max_bake_probes_is_applied_by_default(tmp_path):
    from tests.test_pmx_physics_displayframe import make_pmx as build
    bones = [("センター", True)] + [(f"b{i:03d}", True) for i in range(100)]
    bodies = [(f"b{i:03d}", i + 1, 1) for i in range(100)]
    source = tmp_path / "many.pmx"
    source.write_bytes(build(bones=bones, rigid_bodies=bodies,
                             display_frames=[("Root", list(range(101)))]))
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
    assert len(result.bake_probe_bones) == MAX_BAKE_PROBES


def test_probes_are_all_vmd_representable(tmp_path):
    source = _model_with_physics(tmp_path, displayed=True)
    result, _ = prepare_character_model(source, output_dir=tmp_path / "job")
    from mmd_bridge.vmd_reader import bone_name_issue
    assert all(bone_name_issue(n) is None for n in result.bake_probe_bones)


# --------------------------------------------------------------------------
# Dry run / reporting
# --------------------------------------------------------------------------

def test_dry_run_writes_no_files(tmp_path):
    source = _model_with_physics(tmp_path, displayed=False)
    job = tmp_path / "job"
    before = source.read_bytes()
    prepare_character_model(source, write_manifest=False)
    assert source.read_bytes() == before
    assert not job.exists()


def test_summary_reports_source_missing_count(tmp_path):
    """The summary must show why the repair ran, not the post-repair zero."""
    source = _model_with_physics(tmp_path, displayed=False)
    _, text = prepare_character_model(source, output_dir=tmp_path / "job")
    assert "源缺失 1" in text
    assert "修复后 0" in text


def test_summary_explains_bypass(tmp_path):
    source = _write(tmp_path, "static.pmx", rigid_bodies=[])
    _, text = prepare_character_model(source, output_dir=tmp_path / "job")
    assert NO_PHYSICS_BONES in text
    assert "跳过" in text
