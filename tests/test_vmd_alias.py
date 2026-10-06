"""Tests for the alias artifacts: map file and alias source VMD (方案 §6 / §10.2).

The dangerous failure mode here is **silent**: if the alias VMD ends up with the
original names (or the map is passed the wrong way round), MMM simply won't find
those bones and the motion disappears without an error.  Most of these tests
exist to make that loud.
"""
from __future__ import annotations

import json

import pytest

from mmd_bridge.vmd_alias import (
    ALIAS_VMD_FILE_NAME,
    AliasArtifactError,
    build_alias_vmd,
    read_alias_map,
    write_alias_map,
)
from mmd_bridge.vmd_name_strategy import ALIAS_MAP_FILE_NAME
from mmd_bridge.vmd_reader import read_vmd
from tests.test_vmd_reader import _bone_frame, make_vmd


def make_motion(tmp_path, name="m.vmd", bones=None):
    path = tmp_path / name
    frames = []
    for bone, numbers in (bones or [("センター", [0, 10]), ("左足", [0, 10])]):
        for number in numbers:
            frames.append(_bone_frame(bone, number))
    path.write_bytes(make_vmd(bone_frames=frames))
    return path


# --------------------------------------------------------------------------
# 映射文件（方案 §6）
# --------------------------------------------------------------------------

def test_alias_map_keeps_index_name_and_alias(tmp_path):
    write_alias_map(tmp_path, model_name="m.pmx",
                    index_to_alias={247: "PB000247"},
                    original_names={247: "左发_0_1"})
    payload = json.loads(
        (tmp_path / ALIAS_MAP_FILE_NAME).read_text(encoding="utf-8"))
    entry = payload["bones"][0]
    assert entry["bone_index"] == 247
    assert entry["original_name"] == "左发_0_1"
    assert entry["alias"] == "PB000247"
    assert payload["version"] == 1
    assert payload["model"] == "m.pmx"


def test_read_alias_map_round_trips(tmp_path):
    write_alias_map(tmp_path, model_name="m.pmx",
                    index_to_alias={247: "PB000247", 251: "PB000251"},
                    original_names={247: "A", 251: "B"})
    loaded = read_alias_map(tmp_path / ALIAS_MAP_FILE_NAME)
    assert loaded["alias_to_original"] == {"PB000247": "A", "PB000251": "B"}
    assert loaded["index_to_alias"] == {247: "PB000247", 251: "PB000251"}
    assert loaded["original_to_alias"] == {"A": "PB000247", "B": "PB000251"}


def test_read_alias_map_handles_missing_file(tmp_path):
    loaded = read_alias_map(tmp_path / "nope.json")
    assert loaded["alias_to_original"] == {}


def test_read_alias_map_handles_corrupt_file(tmp_path):
    target = tmp_path / "bad.json"
    target.write_text("{not json", encoding="utf-8")
    assert read_alias_map(target)["alias_to_original"] == {}


def test_read_alias_map_skips_broken_entries(tmp_path):
    target = tmp_path / "partial.json"
    target.write_text(json.dumps({
        "version": 1,
        "bones": [{"bone_index": 1, "original_name": "A", "alias": "PB000001"},
                  {"alias": "missing-index"}],
    }), encoding="utf-8")
    loaded = read_alias_map(target)
    assert loaded["alias_to_original"] == {"PB000001": "A"}


# --------------------------------------------------------------------------
# Alias 源 VMD（方案 §10.2）
# --------------------------------------------------------------------------

def test_alias_vmd_renames_the_matching_tracks(tmp_path):
    source = make_motion(tmp_path)
    out, stats = build_alias_vmd(source, tmp_path / "alias.vmd",
                                 {"センター": "PB000001"})
    assert stats["renamed_frames"] == 2
    assert stats["renamed_tracks"] == 1

    names = {f.name for f in read_vmd(out).bone_frames}
    assert "PB000001" in names
    assert "センター" not in names
    assert "左足" in names, "未命中的轨道必须原样保留"


def test_alias_vmd_keeps_the_file_size_identical(tmp_path):
    """骨骼帧定长 111 字节，改名不该改变文件长度。"""
    source = make_motion(tmp_path)
    out, _ = build_alias_vmd(source, tmp_path / "alias.vmd",
                             {"センター": "PB000001"})
    assert out.stat().st_size == source.stat().st_size


def test_alias_vmd_preserves_frame_numbers_and_values(tmp_path):
    """改名只动 15 字节的名字槽，帧号/位移/旋转/插值必须原样。"""
    source = make_motion(tmp_path)
    before = read_vmd(source)
    out, _ = build_alias_vmd(source, tmp_path / "alias.vmd",
                             {"センター": "PB000001"})
    after = read_vmd(out)

    assert len(after.bone_frames) == len(before.bone_frames)
    for old, new in zip(before.bone_frames, after.bone_frames):
        assert new.frame == old.frame
        assert new.position == old.position
        assert new.rotation == old.rotation
        assert bytes(new.interpolation) == bytes(old.interpolation)


def test_alias_vmd_with_no_matching_tracks_is_a_noop(tmp_path):
    """§10.1：源 VMD 不含物理骨轨道 —— 文件内容应逐字节不变。"""
    source = make_motion(tmp_path)
    out, stats = build_alias_vmd(source, tmp_path / "alias.vmd",
                                 {"不存在的骨": "PB000999"})
    assert stats["renamed_frames"] == 0
    assert stats["unmatched_tracks"] == ["不存在的骨"]
    assert out.read_bytes() == source.read_bytes()


def test_flipped_map_is_rejected_loudly(tmp_path):
    """方向传反会变成"一帧都没改"的静默空操作 —— 必须报错。

    这个坑真实发生过：调用方传了 ``alias_map``（别名→原名），
    而函数要的是 ``原名→别名``，结果 0 帧被改，且没有任何提示。
    """
    source = make_motion(tmp_path)
    flipped = {"PB000001": "センター", "PB000002": "左足"}
    with pytest.raises(AliasArtifactError, match="方向传反"):
        build_alias_vmd(source, tmp_path / "alias.vmd", flipped)


def test_missing_source_vmd_raises(tmp_path):
    with pytest.raises(AliasArtifactError, match="不存在"):
        build_alias_vmd(tmp_path / "nope.vmd", tmp_path / "o.vmd",
                        {"センター": "PB000001"})


def test_alias_vmd_is_written_atomically(tmp_path):
    source = make_motion(tmp_path)
    out = tmp_path / "sub" / ALIAS_VMD_FILE_NAME
    build_alias_vmd(source, out, {"センター": "PB000001"})
    assert out.is_file()
    assert not list(out.parent.glob("*.tmp"))


def test_alias_name_longer_than_the_slot_is_rejected(tmp_path):
    """别名生成器有 bug 时不该写出坏文件。"""
    source = make_motion(tmp_path)
    with pytest.raises(AliasArtifactError, match="超过"):
        build_alias_vmd(source, tmp_path / "alias.vmd",
                        {"センター": "X" * 20})
