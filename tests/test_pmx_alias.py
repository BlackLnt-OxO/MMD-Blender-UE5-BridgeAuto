"""Tests for Alias PMX generation (方案 §7 / §8 / §9).

The strongest guarantee here is structural: because the builder splices bytes
rather than re-serialising a parsed model, everything except the name fields is
**the same bytes**, not merely "equal after re-encoding".  The tests pin both
the splice and the verification that guards it.
"""
from __future__ import annotations

import struct

import pytest

from mmd_bridge.pmx_alias import (
    AliasModelError,
    build_alias_model,
    verify_alias_structure,
)
from mmd_bridge.pmx_reader import read_pmx
from tests.test_pmx_physics_displayframe import make_pmx


def write_model(tmp_path, name="m.pmx", bones=None):
    path = tmp_path / name
    path.write_bytes(make_pmx(
        bones=bones or [("センター", True), ("左足", True), ("右足", True)],
        rigid_bodies=[("左足", 1, 1)],
        display_frames=[("Root", [0, 1, 2])]))
    return path


# --------------------------------------------------------------------------
# 基本替换
# --------------------------------------------------------------------------

def test_alias_replaces_only_the_targeted_bones(tmp_path):
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})

    before = read_pmx(source)
    after = read_pmx(out)
    assert after.bones[0].name_jp == before.bones[0].name_jp, "未指定的骨不该变"
    assert after.bones[1].name_jp == "PB000001"
    assert after.bones[2].name_jp == before.bones[2].name_jp


def test_alias_changes_both_local_and_english_name(tmp_path):
    """方案 §8：英文名也要改，避免下游意外选中英文名。"""
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    after = read_pmx(out)
    assert after.bones[1].name_jp == "PB000001"
    assert after.bones[1].name_en == "PB000001"


def test_alias_preserves_bone_identity(tmp_path):
    """方案 §7：bone index / flags 必须不变 —— 改前后仍是同一根骨。"""
    source = write_model(tmp_path)
    before = read_pmx(source)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    after = read_pmx(out)

    assert [b.index for b in after.bones] == [b.index for b in before.bones]
    assert [b.flags for b in after.bones] == [b.flags for b in before.bones]


def test_alias_preserves_rigid_bodies_and_display_frames(tmp_path):
    """刚体与表示枠引用的是 bone index，不该因改名而变。"""
    source = write_model(tmp_path)
    before = read_pmx(source)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    after = read_pmx(out)

    assert len(after.rigid_bodies) == len(before.rigid_bodies)
    assert len(after.display_frames) == len(before.display_frames)


def test_bytes_outside_the_name_fields_are_untouched(tmp_path):
    """核心保证：名称字段之外是**同样的字节**。

    生成方式是拼接而不是"解析→重建"，所以未改动的部分连一个字节都不会变。
    直接逐字节 zip 比对会因为长度变化而错位，因此改为比对
    "第一个改动点之前"与"最后一个改动点之后"两段。
    """
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})

    original = source.read_bytes()
    modified = out.read_bytes()
    bone = read_pmx(source).bones[1]
    jp_start, jp_end = bone.name_jp_range
    en_start, en_end = bone.name_en_range

    assert original[:jp_start] == modified[:jp_start], "名称字段之前的字节变了"
    # 尾部对齐：从两个文件各自末尾往回数同样的长度。
    tail = len(original) - en_end
    assert original[-tail:] == modified[-tail:], "名称字段之后的字节变了"
    assert len(original) != len(modified), "改名改变了长度，应有差异"


# --------------------------------------------------------------------------
# 结构验证（方案 §9）
# --------------------------------------------------------------------------

def test_verify_passes_on_a_correct_alias_model(tmp_path):
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    assert verify_alias_structure(source, out, {1: "PB000001"}) == []


def test_verify_detects_an_unexpected_rename(tmp_path):
    """走 DIRECT 的骨被改了名 → 必须被发现。"""
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    problems = verify_alias_structure(source, out, {0: "PB000000"})
    assert problems, "骨骼 1 被改名但不在预期列表里，应该报错"


def test_verify_reports_a_change_that_was_not_expected(tmp_path):
    """别名模型确实改了骨 1，但校验方以为不该改 → 必须被发现。"""
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {1: "PB000001"})
    problems = verify_alias_structure(source, out, {})
    assert any("不该改名" in p for p in problems)


def test_verify_reports_alias_that_was_not_applied(tmp_path):
    """校验方期待别名，但文件没改 → 必须被发现。"""
    source = write_model(tmp_path)
    out = build_alias_model(source, tmp_path / "alias.pmx", {0: "PB000000"})
    problems = verify_alias_structure(source, out, {0: "PB000000", 1: "PB000001"})
    assert any("不是预期别名" in p for p in problems)


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------

def test_missing_source_raises(tmp_path):
    with pytest.raises(AliasModelError, match="不存在"):
        build_alias_model(tmp_path / "nope.pmx", tmp_path / "o.pmx", {0: "PB0"})


def test_empty_alias_map_raises(tmp_path):
    source = write_model(tmp_path)
    with pytest.raises(AliasModelError, match="为空"):
        build_alias_model(source, tmp_path / "o.pmx", {})


def test_out_of_range_bone_index_raises(tmp_path):
    source = write_model(tmp_path)
    with pytest.raises(AliasModelError, match="不存在"):
        build_alias_model(source, tmp_path / "o.pmx", {999: "PB000999"})


def test_output_is_written_atomically(tmp_path):
    """不能留下半成品 —— 先写 .tmp 再 rename。"""
    source = write_model(tmp_path)
    out = tmp_path / "sub" / "alias.pmx"
    build_alias_model(source, out, {1: "PB000001"})
    assert out.is_file()
    assert not list(out.parent.glob("*.tmp")), "不该残留 .tmp"
