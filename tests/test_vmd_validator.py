"""Unit tests for the four-layer baked-VMD validator (方案 §22 / §25).

All cases are offline: VMD byte strings are synthesised, so no real motion file
and no MMM installation is required.  The tests deliberately include the
"vacuous check" cases, because a subset assertion fed an empty expectation
passes unconditionally and that is the failure mode this module exists to stop.
"""
from __future__ import annotations

import json

import pytest

from mmd_bridge.vmd_validator import (
    PHYSICS_NOT_BAKED_TO_BONES,
    REPAIR_MANIFEST_EMPTY,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    VMD_BONE_NAME_UNREPRESENTABLE,
    VMD_EXPECTATION_EMPTY,
    VMD_EXPORT_MISSING_PHYSICS_BONES,
    VMD_SOURCE_TRACK_LOST,
    check_bone_names_representable,
    validate_baked_vmd,
    write_validation_json,
)
from tests.test_vmd_reader import _bone_frame, _morph_frame, make_vmd


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _write(tmp_path, name: str, data: bytes):
    target = tmp_path / name
    target.write_bytes(data)
    return target


#: 一个明确非单位的姿态。用它构造的轨道才"携带动画" ——
#: 单位姿态的轨道（哪怕几千帧）等价于没有轨道，层 B 不会要求它保留。
MOVING_POSE = (0.0, 1.0, 0.0)
MOVING_ROT = (0.0, 0.0, 0.0, 1.0)


def _motion(bones: list[tuple[str, list[int]]], *,
            pos=MOVING_POSE, rot=MOVING_ROT) -> bytes:
    """Build a VMD whose bone tracks are ``{name: [frames]}``.

    默认写入**非单位姿态**，这样每条轨道都真的携带动画。
    需要"空轨道"（只有单位姿态）时显式传 ``pos=(0,0,0), rot=(0,0,0,1)``。
    """
    frames = []
    for name, numbers in bones:
        for number in numbers:
            frames.append(_bone_frame(name, number, pos=pos, rot=rot))
    return make_vmd(bone_frames=frames)


def _empty_motion(bones: list[tuple[str, list[int]]]) -> bytes:
    """只含单位姿态的轨道 —— 丢了不损失任何动画。"""
    return _motion(bones, pos=(0.0, 0.0, 0.0), rot=(0.0, 0.0, 0.0, 1.0))


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_full_pass_when_all_layers_succeed(tmp_path):
    source = _write(tmp_path, "source.vmd", _motion([
        ("センター", [0, 10, 20]),
        ("左足", [0, 10]),
    ]))
    baked = _write(tmp_path, "baked.vmd", _motion([
        ("センター", [0, 10, 20]),
        ("左足", [0, 10]),
        ("スカート_0_0", [0, 1, 2, 3]),
        ("スカート_0_1", [0, 1, 2, 3]),
    ]))

    report, text = validate_baked_vmd(
        baked, source_path=source,
        expected_bone_names=["スカート_0_0", "スカート_0_1"],
        bake_range=(0, 3))

    assert report.status == STATUS_PASS
    assert report.ok is True
    assert report.verified_bone_count == 2
    assert report.errors == []
    assert "PASS" in text


# --------------------------------------------------------------------------
# Layer A — binary structure / CP932
# --------------------------------------------------------------------------

def test_layer_a_fails_on_unparseable_file(tmp_path):
    baked = _write(tmp_path, "baked.vmd", b"NOT A VMD AT ALL")
    report, _ = validate_baked_vmd(baked)

    assert report.status == STATUS_FAIL
    assert report.ok is False
    assert report.layer("A 二进制结构 / CP932").status == STATUS_FAIL


def test_layer_a_warns_on_truncated_name_but_does_not_fail(tmp_path):
    """VMD's own mid-slot truncation is a warning, not corruption."""
    slot = "右もみあげ１２３４５".encode("cp932")[:15]
    import struct
    frame = (slot + struct.pack("<I", 0) + struct.pack("<3f", 0, 0, 0)
             + struct.pack("<4f", 0, 0, 0, 1) + b"\x00" * 64)
    baked = _write(tmp_path, "baked.vmd", make_vmd(bone_frames=[frame]))

    report, _ = validate_baked_vmd(baked)

    assert report.layer("A 二进制结构 / CP932").status == STATUS_PASS
    assert "VMD_NAME_TRUNCATED" in _codes(report)
    assert report.errors == []          # warning only


def test_layer_a_fails_on_mis_encoded_name(tmp_path):
    import struct
    bad = b"\x98\x83x\x83\x8b\x83g" + b"\x00" * 8
    frame = (bad + struct.pack("<I", 0) + struct.pack("<3f", 0, 0, 0)
             + struct.pack("<4f", 0, 0, 0, 1) + b"\x00" * 64)
    baked = _write(tmp_path, "baked.vmd", make_vmd(bone_frames=[frame]))

    report, _ = validate_baked_vmd(baked)

    assert report.status == STATUS_FAIL
    assert "VMD_CP932_DECODE_FAILED" in _codes(report)


# --------------------------------------------------------------------------
# Layer B — source motion retained
# --------------------------------------------------------------------------

def test_layer_b_passes_when_source_tracks_survive(tmp_path):
    source = _write(tmp_path, "s.vmd", _motion([("センター", [0, 5])]))
    baked = _write(tmp_path, "b.vmd", _motion([
        ("センター", [0, 5]), ("スカート_0_0", [0, 5])]))

    report, _ = validate_baked_vmd(baked, source_path=source)

    assert report.layer("B 原 Motion 保留").status == STATUS_PASS
    assert VMD_SOURCE_TRACK_LOST not in _codes(report)


def test_layer_b_fails_when_source_tracks_are_lost(tmp_path):
    """The classic failure: only the PhysicsBake group was exported."""
    source = _write(tmp_path, "s.vmd", _motion([
        ("センター", [0, 5]), ("左足", [0, 5]), ("右足", [0, 5])]))
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [0, 5])]))

    report, _ = validate_baked_vmd(baked, source_path=source)

    assert report.status == STATUS_FAIL
    assert report.layer("B 原 Motion 保留").status == STATUS_FAIL
    assert VMD_SOURCE_TRACK_LOST in _codes(report)


def test_layer_b_is_skipped_without_source(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0])]))
    report, _ = validate_baked_vmd(baked)
    assert report.layer("B 原 Motion 保留").status == STATUS_SKIPPED


# --------------------------------------------------------------------------
# Layer C — expected bones present
# --------------------------------------------------------------------------

def test_layer_c_fails_on_missing_physics_bones(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0", "スカート_0_1"])

    assert report.layer("C 期望骨骼存在").status == STATUS_FAIL
    assert VMD_EXPORT_MISSING_PHYSICS_BONES in _codes(report)


def test_layer_c_passes_when_all_expected_present(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([
        ("スカート_0_0", [0, 1]), ("スカート_0_1", [0, 1])]))
    report, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0", "スカート_0_1"])
    assert report.layer("C 期望骨骼存在").status == STATUS_PASS


# --------------------------------------------------------------------------
# Layer D — bake density
# --------------------------------------------------------------------------

def test_layer_d_fails_when_expected_bones_have_no_frames(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1, 2])]))
    report, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0"])

    assert report.layer("D 烘焙帧密度").status == STATUS_FAIL
    assert PHYSICS_NOT_BAKED_TO_BONES in _codes(report)


def test_layer_d_ignores_frames_outside_bake_range(tmp_path):
    """A track that exists but is empty inside the bake range is not baked."""
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [500, 600])]))
    report, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0"], bake_range=(0, 100))

    assert report.layer("D 烘焙帧密度").status == STATUS_FAIL


def test_layer_d_respects_min_frames_per_bone(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [0])]))

    lenient, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0"],
        bake_range=(0, 0), min_frames_per_bone=1)
    assert lenient.layer("D 烘焙帧密度").status == STATUS_PASS

    strict, _ = validate_baked_vmd(
        baked, expected_bone_names=["スカート_0_0"],
        bake_range=(0, 0), min_frames_per_bone=2)
    assert strict.layer("D 烘焙帧密度").status == STATUS_FAIL


# --------------------------------------------------------------------------
# The vacuous-check guard
# --------------------------------------------------------------------------

def test_empty_expectation_is_refused_not_silently_passed(tmp_path):
    """∅ ⊆ anything is always true — asking with no names must not pass."""
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, _ = validate_baked_vmd(baked, expected_bone_names=[])

    assert report.status == STATUS_FAIL
    assert report.ok is False
    assert VMD_EXPECTATION_EMPTY in _codes(report)
    assert report.layer("C 期望骨骼存在").status == STATUS_SKIPPED


def test_omitting_expectation_yields_inconclusive_not_pass(tmp_path):
    """Not being asked to verify is different from failing to verify."""
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, _ = validate_baked_vmd(baked)

    assert report.status == "inconclusive"
    assert report.ok is False
    assert VMD_EXPECTATION_EMPTY not in _codes(report)


def test_manifest_claiming_missing_bones_without_names_is_refused(tmp_path):
    """Defence in depth against the empty-set hazard inside the manifest."""
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    manifest = {"missing_physics_bone_count": 349, "added_bone_names": []}

    report, _ = validate_baked_vmd(baked, manifest=manifest)

    assert report.status == STATUS_FAIL
    assert REPAIR_MANIFEST_EMPTY in _codes(report)


def test_manifest_with_matching_names_passes(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([
        ("スカート_0_0", [0, 1]), ("スカート_0_1", [0, 1])]))
    manifest = {
        "missing_physics_bone_count": 2,
        "added_bone_names": ["スカート_0_0", "スカート_0_1"],
    }

    report, _ = validate_baked_vmd(baked, manifest=manifest)

    assert report.status == STATUS_PASS
    assert report.verified_bone_count == 2


def test_clean_model_manifest_with_zero_missing_is_allowed(tmp_path):
    """Nothing to add is a legitimate empty expectation, not a hazard."""
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    manifest = {"missing_physics_bone_count": 0, "added_bone_names": []}

    report, _ = validate_baked_vmd(baked, manifest=manifest)

    assert REPAIR_MANIFEST_EMPTY not in _codes(report)
    assert report.status == "inconclusive"      # nothing was claimed either


# --------------------------------------------------------------------------
# Pre-flight name checks (方案 §10)
# --------------------------------------------------------------------------

def test_preflight_accepts_names_within_15_bytes():
    assert check_bone_names_representable(["スカート_0_0", "左AH1"]) == []


def test_preflight_flags_over_length_name_that_splits_a_character():
    """超长本身不报错，但截断切在多字节字符中间就是乱码。

    `"あ" * 9` 编码后 18 字节，第 15 字节落在第 8 个「あ」的中间。
    （纯 ASCII 超长名会被放行 —— VMD 截断后是干净的。）
    """
    issues = check_bone_names_representable(["あ" * 9])
    assert [i.code for i in issues] == [VMD_BONE_NAME_UNREPRESENTABLE]
    assert "切断多字节字符" in issues[0].message


def test_preflight_flags_unencodable_name():
    issues = check_bone_names_representable(["骨骼_😀"])
    assert [i.code for i in issues] == [VMD_BONE_NAME_UNREPRESENTABLE]
    assert "CP932" in issues[0].message


def test_preflight_flags_truncation_collision():
    """Two names sharing their first 15 bytes would be indistinguishable."""
    a = "スカート_10_100"          # 15 bytes
    b = "スカート_10_1000"         # 16 bytes, truncates to the same 15
    issues = check_bone_names_representable([a, b])

    codes = [i.code for i in issues]
    assert VMD_BONE_NAME_UNREPRESENTABLE in codes
    collision = [i for i in issues if "冲突" in i.message]
    assert collision, "应报告截断后的名称冲突"
    assert a in collision[0].message and b in collision[0].message


def test_preflight_reports_each_offender_not_just_the_first():
    issues = check_bone_names_representable(
        ["あ" * 9, "い" * 9, "ok"])
    assert len([i for i in issues if "切断多字节字符" in i.message]) == 2


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_report_to_dict_is_json_serialisable(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, _ = validate_baked_vmd(baked, expected_bone_names=["センター"])

    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert payload["status"] == STATUS_PASS
    assert len(payload["layers"]) == 4
    assert payload["verified_bone_count"] == 1


def test_write_validation_json_creates_file(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, _ = validate_baked_vmd(baked, expected_bone_names=["センター"])

    out = write_validation_json(report, tmp_path / "mmm" / "validation.json")

    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["baked_path"] == str(baked)


def test_summary_text_lists_every_layer(tmp_path):
    baked = _write(tmp_path, "b.vmd", _motion([("センター", [0, 1])]))
    report, text = validate_baked_vmd(baked, expected_bone_names=["センター"])

    for label in ("A 二进制结构", "B 原 Motion", "C 期望骨骼", "D 烘焙帧密度"):
        assert label in text


# --------------------------------------------------------------------------
# 层 B：只对"携带动画"的轨道做要求（实测踩过的坑）
# --------------------------------------------------------------------------

def test_layer_b_ignores_dropped_empty_tracks(tmp_path):
    """全单位姿态的轨道丢了不算失败 —— 没有关键帧就是 rest pose。

    实测场景：Prhythmatic 源动作 283 轨，MMM 导出后只剩 76 轨。
    逐帧检查发现丢的 207 条**全是单位姿态**，不损失任何动画。
    按轨道名硬比会把一个完全正确的烘焙判 FAIL。
    """
    source = _write(tmp_path, "s.vmd", _empty_motion([
        ("センター", [0]), ("前髪2", [0]), ("両目先", [0])]))
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [0, 5])]))

    report, text = validate_baked_vmd(baked, source_path=source)

    assert VMD_SOURCE_TRACK_LOST not in _codes(report)
    layer_b = report.layer("B 原 Motion 保留")
    assert layer_b.status == STATUS_PASS
    assert "空轨道" in layer_b.detail
    assert layer_b.data["empty_dropped"] == 3


def test_layer_b_still_fails_when_real_motion_is_lost(tmp_path):
    """真正携带动画的轨道丢了，必须失败。"""
    source = _write(tmp_path, "s.vmd", _motion([
        ("センター", [0, 5]), ("左足", [0, 5])]))
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [0, 5])]))

    report, _ = validate_baked_vmd(baked, source_path=source)

    assert VMD_SOURCE_TRACK_LOST in _codes(report)
    assert report.layer("B 原 Motion 保留").status == STATUS_FAIL


def test_layer_b_mixed_empty_and_meaningful(tmp_path):
    """空轨道可以丢，有动画的不行 —— 两者必须分别判定。"""
    source = _write(tmp_path, "s.vmd", _motion([
        ("センター", [0, 5]),          # 有动画
        ("両目先", [0]),               # 空
    ]))
    baked = _write(tmp_path, "b.vmd", _motion([("スカート_0_0", [0, 5])]))

    report, _ = validate_baked_vmd(baked, source_path=source)

    assert VMD_SOURCE_TRACK_LOST in _codes(report)
    assert "'センター'" in report.layer("B 原 Motion 保留").data["lost"][0] or \
        "センター" in str(report.layer("B 原 Motion 保留").data["lost"])


def test_layer_b_reports_meaningful_track_count(tmp_path):
    source = _write(tmp_path, "s.vmd", _motion([
        ("センター", [0, 5]), ("左足", [0, 5])]))
    baked = _write(tmp_path, "b.vmd", _motion([
        ("センター", [0, 5]), ("左足", [0, 5])]))

    report, _ = validate_baked_vmd(baked, source_path=source)

    layer_b = report.layer("B 原 Motion 保留")
    assert layer_b.status == STATUS_PASS
    assert layer_b.data["meaningful_tracks"] == 2
    assert layer_b.data["empty_dropped"] == 0


def test_identity_pose_detection():
    """单位姿态判定：位置全 0 且旋转为单位四元数（w=±1 都算）。"""
    from mmd_bridge.vmd_reader import VMDBoneFrame
    from mmd_bridge.vmd_validator import is_identity_pose

    def frame(pos, rot):
        return VMDBoneFrame(name="b", frame=0, position=pos, rotation=rot,
                            interpolation=b"\x00" * 64)

    assert is_identity_pose(frame((0, 0, 0), (0, 0, 0, 1))) is True
    assert is_identity_pose(frame((0, 0, 0), (0, 0, 0, -1))) is True
    assert is_identity_pose(frame((0, 0.001, 0), (0, 0, 0, 1))) is False
    assert is_identity_pose(frame((0, 0, 0), (0, 0, 0.001, 1))) is False
    assert is_identity_pose(frame((0, 0, 0), (0, 0, 0, 0.999))) is False


def test_track_carries_motion_scans_all_frames():
    """只有中间某一帧有动作也算携带动画。"""
    from mmd_bridge.vmd_reader import VMDBoneFrame, VMDBoneTrack
    from mmd_bridge.vmd_validator import track_carries_motion

    def frame(number, pos):
        return VMDBoneFrame(name="b", frame=number, position=pos,
                            rotation=(0, 0, 0, 1), interpolation=b"\x00" * 64)

    empty = VMDBoneTrack(name="b", frames=[frame(0, (0, 0, 0)),
                                           frame(5, (0, 0, 0))])
    assert track_carries_motion(empty) is False

    one_move = VMDBoneTrack(name="b", frames=[frame(0, (0, 0, 0)),
                                              frame(5, (0, 2, 0)),
                                              frame(9, (0, 0, 0))])
    assert track_carries_motion(one_move) is True


# --------------------------------------------------------------------------
# 超长骨骼名：只有"截断切断字符"和"截断冲突"才是错
# --------------------------------------------------------------------------

def test_overlong_ascii_name_is_allowed_when_truncation_is_clean():
    """实测踩过的坑：纯 ASCII 超长名被一律拒绝，但 VMD 会截断，完全可用。

    `TDA Miku.pmx` 的 `SIDE BANGS A_0_1` 编码后 16 字节（只超 1 字节），
    截断到 15 字节得到 `SIDE BANGS A_0_` —— 干净且不与其它名字冲突。
    早期版本因此把整个模型拒了，而 MMM 自己完全能导出。
    """
    from mmd_bridge.vmd_validator import check_bone_names_representable
    assert check_bone_names_representable(
        ["SIDE BANGS A_0_1", "SIDE BANGS A_1_1"]) == []


def test_overlong_name_that_collides_is_still_rejected():
    """截断后同名 → VMD 无法区分，烘焙结果会张冠李戴。"""
    from mmd_bridge.vmd_validator import check_bone_names_representable
    issues = check_bone_names_representable(
        ["Bone_Bronya_C9_Hair_L_01", "Bone_Bronya_C9_Hair_L_02"])
    assert issues
    assert any("冲突" in i.message for i in issues)


def test_overlong_name_that_splits_a_character_is_rejected():
    """日文超长名截断会切在多字节字符中间 → 残留半个字符，解码是乱码。

    实测样本：`スカート_0_10_服` 编码后 16 字节，第 15 字节是 `服` 的前半。
    """
    from mmd_bridge.vmd_validator import check_bone_names_representable
    issues = check_bone_names_representable(["あ" * 9])   # 18 字节
    assert issues
    assert any("切断多字节字符" in i.message for i in issues)


def test_unencodable_name_is_still_rejected():
    """简体字不在 CP932 里 —— 这是硬错误，手动用 MMM 也存不进去。"""
    from mmd_bridge.vmd_validator import check_bone_names_representable
    issues = check_bone_names_representable(["左发_0_1"])
    assert issues
    assert any("CP932" in i.message for i in issues)


def test_normal_names_pass():
    from mmd_bridge.vmd_validator import check_bone_names_representable
    assert check_bone_names_representable(["スカート_0_0", "センター"]) == []
