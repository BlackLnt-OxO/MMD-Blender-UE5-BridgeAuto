"""Unit tests for the PMX physics display-frame preflight / repair module.

Covers the ten cases required by 任务书 §28.  All of them run offline against
synthesised in-memory PMX models, so no real model file is needed.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from mmd_bridge import pmx_physics_displayframe as pbf
from mmd_bridge.pmx_analyzer import (
    WARNING_BONE_NOT_VISIBLE,
    WARNING_INVALID_BONE_INDEX,
    WARNING_NEGATIVE_RIGID_BODY_BONE,
    analyze_model,
)
from mmd_bridge.pmx_binary import ByteWriter
from mmd_bridge.pmx_model import (
    BONE_FLAG_ENABLED,
    BONE_FLAG_VISIBLE,
    PHYSICS_BAKE_FRAME_NAME,
    PMXModel,
)
from mmd_bridge.pmx_planner import apply_plan_to_frames, build_repair_plan
from mmd_bridge.pmx_reader import _read_morphs, _read_vertices, read_pmx
from mmd_bridge.pmx_binary import ByteReader, ByteWriter
from mmd_bridge.pmx_model import PMXHeader


def test_pmx_binary_text_encoding_follows_header():
    text = "芭芭拉"
    writer = ByteWriter("utf-8")
    writer.write_text(text)
    data = writer.build()
    assert data == struct.pack("<i", len(text.encode("utf-8"))) + text.encode("utf-8")
    reader = ByteReader(data)
    reader.text_encoding = "utf-8"
    assert reader.read_text() == text


def test_pmx_reader_records_qdef_without_changing_its_payload():
    data = struct.pack("<i", 1) + bytes(32) + bytes([4]) + bytes(32) + bytes(4)
    assert _read_vertices(ByteReader(data), PMXHeader(version=2.1)) == {
        "BDEF1": 0, "BDEF2": 0, "BDEF4": 0, "SDEF": 0, "QDEF": 1}


def test_pmx_reader_skips_pmx21_flip_and_impulse_morphs():
    writer = ByteWriter()
    writer.write_int(2, 4)
    for kind, payload_size in ((9, 8), (10, 29)):
        writer.write_text("morph")
        writer.write_text("morph")
        writer.write_int(0, 1)
        writer.write_int(kind, 1)
        writer.write_int(1, 4)
        writer.write_raw(bytes(payload_size))
    raw = writer.build()
    reader = ByteReader(raw)
    _read_morphs(reader, PMXHeader(version=2.1))
    assert reader.pos == len(raw)


# --------------------------------------------------------------------------
# Builders: synthesise a complete, valid PMX file in memory.
# --------------------------------------------------------------------------

def _bone(name: str, *, visible: bool = True) -> bytes:
    flags = BONE_FLAG_ENABLED | (BONE_FLAG_VISIBLE if visible else 0)
    writer = ByteWriter()
    writer.write_text(name)          # name JP
    writer.write_text(name)          # name EN
    writer.write_vec([0.0, 0.0, 0.0])
    writer.write_index(-1, 2)        # parent
    writer.write_int(0, 4)           # deform layer
    writer.write_int(flags, 2)
    writer.write_vec([0.0, 0.0, 0.0])  # tail offset (flags & 0x0001 == 0)
    return writer.build()


def _rigid_body(name: str, related_bone: int, physics_mode: int) -> bytes:
    writer = ByteWriter()
    writer.write_text(name)
    writer.write_text(name)
    writer.write_index(related_bone, 2)
    writer.write_int(0, 1)           # collision group
    writer.write_int(0, 2)           # no-collision mask
    writer.write_int(0, 1)           # shape = 0 (sphere)
    writer.write_vec([1.0, 1.0, 1.0])   # size
    writer.write_vec([0.0, 0.0, 0.0])   # position
    writer.write_vec([0.0, 0.0, 0.0])   # rotation
    writer.write_float(1.0)          # mass
    writer.write_float(0.5)          # linear damping
    writer.write_float(0.5)          # angular damping
    writer.write_float(0.0)          # restitution
    writer.write_float(0.5)          # friction
    writer.write_int(physics_mode, 1)
    return writer.build()


def _display_frame(name: str, bone_indices: list[int], morph_indices: list[int] | None = None) -> bytes:
    writer = ByteWriter()
    writer.write_text(name)
    writer.write_text(name)
    writer.write_int(0, 1)           # special flag
    items = [("bone", index) for index in bone_indices]
    items += [("morph", index) for index in (morph_indices or [])]
    writer.write_int(len(items), 4)
    for kind, index in items:
        if kind == "bone":
            writer.write_int(0, 1)
            writer.write_index(index, 2)
        else:
            writer.write_int(1, 1)
            writer.write_index(index, 2)
    return writer.build()


def make_pmx(
    *,
    bones: list[tuple[str, bool]] | None = None,
    rigid_bodies: list[tuple[str, int, int]] | None = None,
    display_frames: list[tuple[str, list[int]]] | None = None,
    version: float = 2.0,
) -> bytes:
    """Assemble a minimal but structurally valid PMX 2.0 file."""
    bones = bones if bones is not None else [("センター", True), ("下半身", True)]
    rigid_bodies = rigid_bodies if rigid_bodies is not None else []
    display_frames = display_frames if display_frames is not None else [("Root", [0])]

    out = bytearray()
    out += b"PMX "
    out += struct.pack("<f", version)
    # globals marker (always 8), encoding, additional UVs, then 6 index sizes.
    out += bytes([8, 0, 0, 2, 2, 2, 2, 2, 2])

    # model info: name JP, name EN, comment JP, comment EN (four strings)
    for text in ("test", "test", "", ""):
        encoded = text.encode("utf-16-le")
        out += struct.pack("<i", len(encoded)) + encoded

    out += struct.pack("<i", 0)     # vertices
    out += struct.pack("<i", 0)     # faces
    out += struct.pack("<i", 0)     # textures
    out += struct.pack("<i", 0)     # materials
    if version >= 2.1:
        pass

    out += struct.pack("<i", len(bones))
    for name, visible in bones:
        out += _bone(name, visible=visible)

    out += struct.pack("<i", 0)     # morphs

    out += struct.pack("<i", len(display_frames))
    for name, indices in display_frames:
        out += _display_frame(name, indices)

    out += struct.pack("<i", len(rigid_bodies))
    for name, related, mode in rigid_bodies:
        out += _rigid_body(name, related, mode)
    return bytes(out)


def model_from(**kwargs) -> PMXModel:
    import tempfile
    from pathlib import Path

    raw = make_pmx(**kwargs)
    with tempfile.NamedTemporaryFile(suffix=".pmx", delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    try:
        return read_pmx(temp)
    finally:
        temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Case 1 / Case 2 / Case 3 — mode filtering
# --------------------------------------------------------------------------

def test_case1_mode1_missing_is_added():
    model = model_from(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.physics_bone_count == 1
    assert analysis.missing_physics_bone_count == 1
    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == [1]
    assert plan.create_frame is True


def test_case2_mode2_missing_is_added():
    model = model_from(
        bones=[("センター", True), ("髪01", True)],
        rigid_bodies=[("髪01", 1, 2)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.missing_physics_bone_count == 1
    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == [1]


def test_case3_mode0_is_ignored():
    model = model_from(
        bones=[("センター", True), ("頭", True)],
        rigid_bodies=[("頭コリジョン", 1, 0)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.physics_bone_count == 0
    assert analysis.missing_physics_bone_count == 0
    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == []
    assert plan.create_frame is False


# --------------------------------------------------------------------------
# Case 4 / Case 8 — already displayed
# --------------------------------------------------------------------------

def test_case4_bone_already_in_other_frame_is_not_re_added():
    model = model_from(
        bones=[("センター", True), ("髪01", True)],
        rigid_bodies=[("髪01", 1, 1)],
        display_frames=[("Root", [0]), ("髪", [1])],
    )
    analysis = analyze_model(model)
    assert analysis.physics_bone_count == 1
    assert analysis.displayed_physics_bone_count == 1
    assert analysis.missing_physics_bone_count == 0
    assert analysis.needs_repair() is False
    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == []


def test_case8_nothing_missing_means_no_frame_created():
    model = model_from(
        bones=[("センター", True)],
        rigid_bodies=[],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    plan = build_repair_plan(model, analysis)
    assert plan.create_frame is False
    assert apply_plan_to_frames(model, plan) == model.display_frames


# --------------------------------------------------------------------------
# Case 5 / Case 6 — invalid references
# --------------------------------------------------------------------------

def test_case5_related_bone_minus_one_is_ignored_with_warning():
    model = model_from(
        bones=[("センター", True)],
        rigid_bodies=[("浮遊剛体", -1, 1)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.physics_bone_count == 0
    assert any(WARNING_NEGATIVE_RIGID_BODY_BONE in w for w in analysis.warnings)


def test_case6_out_of_range_bone_index_warns_and_is_not_written():
    model = model_from(
        bones=[("センター", True)],
        rigid_bodies=[("壊れた剛体", 300, 1)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.missing_physics_bone_count == 0
    assert any(WARNING_INVALID_BONE_INDEX in w for w in analysis.warnings)
    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == []


# --------------------------------------------------------------------------
# Case 7 — reuse an existing PhysicsBake frame
# --------------------------------------------------------------------------

def test_case7_existing_physicsbake_frame_is_reused():
    model = model_from(
        bones=[("センター", True), ("スカート01", True), ("スカート02", True)],
        rigid_bodies=[("スカート01", 1, 1), ("スカート02", 2, 1)],
        display_frames=[("Root", [0]), (PHYSICS_BAKE_FRAME_NAME, [1])],
    )
    analysis = analyze_model(model)
    # Bone 1 is already in PhysicsBake; only bone 2 is genuinely missing.
    assert analysis.missing_physics_bone_count == 1
    plan = build_repair_plan(model, analysis)
    assert plan.create_frame is False
    assert plan.bones_to_add == [2]

    frames = apply_plan_to_frames(model, plan)
    bake_frames = [f for f in frames if f.name_jp == PHYSICS_BAKE_FRAME_NAME]
    assert len(bake_frames) == 1, "不得创建第二个 PhysicsBake"


# --------------------------------------------------------------------------
# Case 9 — invisible bone: warn, still add, keep the flag
# --------------------------------------------------------------------------

def test_case9_invisible_bone_warns_but_is_added_and_flag_untouched():
    model = model_from(
        bones=[("センター", True), ("髪先_L", False)],
        rigid_bodies=[("髪先_L", 1, 2)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.missing_physics_bone_count == 1
    assert analysis.invisible_missing_count == 1
    assert any(WARNING_BONE_NOT_VISIBLE in w for w in analysis.warnings)

    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == [1]

    frames = apply_plan_to_frames(model, plan)
    bake = next(f for f in frames if f.name_jp == PHYSICS_BAKE_FRAME_NAME)
    assert 1 in bake.bone_indices()
    # The flag itself must be untouched.
    assert model.bones[1].visible is False


# --------------------------------------------------------------------------
# Case 10 — one bone driven by several rigid bodies appears once
# --------------------------------------------------------------------------

def test_case10_bone_driven_by_multiple_rigid_bodies_appears_once():
    model = model_from(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("rb_a", 1, 1), ("rb_b", 1, 2), ("rb_c", 1, 1)],
        display_frames=[("Root", [0])],
    )
    analysis = analyze_model(model)
    assert analysis.physics_bone_count == 1
    assert analysis.missing_physics_bone_count == 1
    info = analysis.physics_bones[0]
    assert sorted(info.physics_modes) == [1, 1, 2]

    plan = build_repair_plan(model, analysis)
    assert plan.bones_to_add == [1]
    frames = apply_plan_to_frames(model, plan)
    bake = next(f for f in frames if f.name_jp == PHYSICS_BAKE_FRAME_NAME)
    assert bake.bone_indices().count(1) == 1


# --------------------------------------------------------------------------
# Round trip / file level behaviour
# --------------------------------------------------------------------------

def test_repair_writes_new_file_and_never_overwrites_source(tmp_path):
    source = tmp_path / "character.pmx"
    raw = make_pmx(
        bones=[("センター", True), ("スカート01", True), ("スカート02", True)],
        rigid_bodies=[("スカート01", 1, 1), ("スカート02", 2, 1)],
        display_frames=[("Root", [0])],
    )
    source.write_bytes(raw)

    result, report = pbf.repair(source)
    assert result.success and result.modified
    assert result.added_bone_indices == [1, 2]
    assert result.output_path.endswith("_PhysicsBakeFixed.pmx")
    assert source.read_bytes() == raw, "原始 PMX 必须保持不变"

    repaired = model_from_bytes((tmp_path / "character_PhysicsBakeFixed.pmx").read_bytes())
    assert repaired.frame_by_name(PHYSICS_BAKE_FRAME_NAME) is not None
    assert repaired.frame_by_name(PHYSICS_BAKE_FRAME_NAME).bone_indices() == [1, 2]
    # Nothing else may change.
    assert len(repaired.bones) == 3
    assert len(repaired.rigid_bodies) == 2
    assert repaired.bones[1].name_jp == "スカート01"


def test_repair_suffix_bytes_are_preserved_verbatim(tmp_path):
    source = tmp_path / "m.pmx"
    raw = make_pmx(
        bones=[("センター", True), ("髪01", True)],
        rigid_bodies=[("髪01", 1, 1)],
        display_frames=[("Root", [0])],
    )
    source.write_bytes(raw)

    model = pbf.read_pmx(source)
    original = source.read_bytes()
    expected_suffix = original[model.rigid_body_start:]

    pbf.repair(source)
    repaired_bytes = (tmp_path / "m_PhysicsBakeFixed.pmx").read_bytes()
    assert repaired_bytes.endswith(expected_suffix)
    assert repaired_bytes.startswith(original[:model.display_frame_start])
    # The display-frame section length grew, which is the only intended change.
    assert len(repaired_bytes) > len(original)


def test_repair_is_idempotent(tmp_path):
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])],
    ))
    first, _ = pbf.repair(source)
    second, report = pbf.repair(first.output_path)
    assert second.modified is False, "第二次运行不应再次修改"
    assert "No repair required" in report


def test_repair_keeps_existing_frames_untouched(tmp_path):
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("下半身", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 2, 1)],
        display_frames=[("Root", [0]), ("下半身", [1])],
    ))
    result, _ = pbf.repair(source)
    repaired = model_from_bytes(Path(result.output_path).read_bytes())
    lower = repaired.frame_by_name("下半身")
    assert lower is not None
    assert lower.bone_indices() == [1], "原有显示枠条目不得被移动或删除"
    assert [f.name_jp for f in repaired.display_frames] == ["Root", "下半身", "PhysicsBake"]


def test_analyze_does_not_write_any_file(tmp_path):
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])],
    ))
    before = sorted(p.name for p in tmp_path.iterdir())
    result, report = pbf.analyze(source)
    assert result.success
    assert result.modified is False
    assert result.missing_physics_bone_count == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert "Preflight" in report


# --------------------------------------------------------------------------
# Empty-set hazard: analyze() must surface the plan, never an empty expectation
# --------------------------------------------------------------------------

def test_analyze_returns_added_bone_names_not_empty(tmp_path):
    """Regression: analyze() used to leave the expectation list empty.

    An empty ``added_bone_names`` makes the downstream
    ``expected ⊆ actual`` assertion pass unconditionally, so a repair would be
    reported as verified while nothing was checked.
    """
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True), ("スカート02", True)],
        rigid_bodies=[("スカート01", 1, 1), ("スカート02", 2, 1)],
        display_frames=[("Root", [0])],
    ))
    result, _ = pbf.analyze(source)

    assert result.missing_physics_bone_count == 2
    assert result.added_bone_names == ["スカート01", "スカート02"]
    assert result.added_bone_indices == [1, 2]
    # The plan must be visible without calling build_repair_plan() by hand.
    assert result.target_frame == PHYSICS_BAKE_FRAME_NAME
    assert result.frame_created is True


def test_analyze_returns_empty_list_only_when_nothing_is_missing(tmp_path):
    """An empty list is legitimate *only* when there is nothing to add."""
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0]), ("物理", [1])],
    ))
    result, _ = pbf.analyze(source)

    assert result.missing_physics_bone_count == 0
    assert result.added_bone_names == []


def test_assert_expectation_not_empty_raises_on_vacuous_plan():
    with pytest.raises(pbf.RepairManifestError) as excinfo:
        pbf.assert_expectation_not_empty(349, [])
    assert pbf.REPAIR_MANIFEST_EMPTY in str(excinfo.value)
    assert "349" in str(excinfo.value)


def test_assert_expectation_not_empty_allows_genuine_empty():
    """No missing bones means an empty expectation is correct, not a hazard."""
    pbf.assert_expectation_not_empty(0, [])
    pbf.assert_expectation_not_empty(4, ["左AH1"])


def test_build_repair_manifest_carries_bone_names(tmp_path):
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True), ("スカート02", True)],
        rigid_bodies=[("スカート01", 1, 1), ("スカート02", 2, 2)],
        display_frames=[("Root", [0])],
    ))
    manifest = pbf.build_repair_manifest(source)

    assert manifest["added_bone_names"] == ["スカート01", "スカート02"]
    assert manifest["added_bone_indices"] == [1, 2]
    assert manifest["target_display_frame"] == PHYSICS_BAKE_FRAME_NAME
    assert manifest["missing_physics_bone_count"] == 2
    assert manifest["source_model"] == str(source)


def test_build_repair_manifest_rejects_vacuous_plan(tmp_path, monkeypatch):
    """The guard must fire if the plan and the analysis ever disagree again."""
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])],
    ))
    real_analyze = pbf.analyze

    def analyze_with_empty_names(path):
        result, report = real_analyze(path)
        result.added_bone_names = []      # simulate the old defect
        result.added_bone_indices = []
        return result, report

    monkeypatch.setattr(pbf, "analyze", analyze_with_empty_names)
    with pytest.raises(pbf.RepairManifestError) as excinfo:
        pbf.build_repair_manifest(source)
    assert pbf.REPAIR_MANIFEST_EMPTY in str(excinfo.value)


# --------------------------------------------------------------------------
# Raw-byte preservation: untouched frames must survive verbatim
# --------------------------------------------------------------------------

def test_original_frames_kept_byte_for_byte(tmp_path):
    """Case A: appending PhysicsBake must not re-encode author frames."""
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("下半身", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 2, 1)],
        display_frames=[("Root", [0]), ("下半身", [1])],
    ))
    model = pbf.read_pmx(source)
    original = source.read_bytes()

    raw_ranges = [(f.raw_start, f.raw_end) for f in model.display_frames]
    assert all(start >= 0 for start, _ in raw_ranges)

    result, _ = pbf.repair(source)
    repaired = Path(result.output_path).read_bytes()

    # The section prefix (everything before the frame count) must be unchanged.
    prefix = original[:model.display_frame_start]
    assert repaired.startswith(prefix)

    # The original frame records must appear verbatim, in order, starting right
    # after the (bumped) frame-count field.  Only the count may differ, and the
    # new PhysicsBake record is appended after the author's frames.
    original_frames = original[model.display_frame_start + 4:model.rigid_body_start]
    repaired_body = repaired[model.display_frame_start + 4:]
    assert repaired_body.startswith(original_frames), "原有显示枠字节必须原样保留且顺序不变"

    count = struct.unpack("<i", repaired[model.display_frame_start:model.display_frame_start + 4])[0]
    assert count == len(model.display_frames) + 1


def test_existing_physicsbake_frame_is_rewritten_in_place(tmp_path):
    """Case B: only the PhysicsBake record may change; others stay verbatim."""
    source = tmp_path / "m.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("髪01", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 2, 1)],
        display_frames=[("Root", [0]), ("髪", [1]), (PHYSICS_BAKE_FRAME_NAME, [])],
    ))
    model = pbf.read_pmx(source)
    original = source.read_bytes()
    untouched = [f for f in model.display_frames if f.name_jp != PHYSICS_BAKE_FRAME_NAME]

    result, _ = pbf.repair(source)
    repaired = Path(result.output_path).read_bytes()
    for frame in untouched:
        raw = original[frame.raw_start:frame.raw_end]
        assert raw in repaired, f"显示枠「{frame.name_jp}」字节被改动"

    reloaded = model_from_bytes(repaired)
    bake = reloaded.frame_by_name(PHYSICS_BAKE_FRAME_NAME)
    assert bake is not None and bake.bone_indices() == [2]
    assert len([f for f in reloaded.display_frames
                if f.name_jp == PHYSICS_BAKE_FRAME_NAME]) == 1
    assert [f.name_jp for f in reloaded.display_frames] == ["Root", "髪", PHYSICS_BAKE_FRAME_NAME]



def test_rejects_file_without_pmx_magic(tmp_path):
    source = tmp_path / "bad.pmx"
    source.write_bytes(b"NOPE" + b"\x00" * 64)
    result, report = pbf.analyze(source)
    assert result.success is False
    assert result.errors
    assert "PMX" in report


def test_bone_index_width_follows_header(tmp_path):
    """Display frame entries must use the header's bone index size, not int32."""
    source = tmp_path / "narrow.pmx"
    source.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])],
    ))
    model = pbf.read_pmx(source)
    assert model.header.bone_index_size == 2

    result, _ = pbf.repair(source)
    repaired = pbf.read_pmx(result.output_path)
    bake = repaired.frame_by_name(PHYSICS_BAKE_FRAME_NAME)
    assert bake is not None and bake.bone_indices() == [1]
    # With a 2-byte bone index, a 1-entry frame costs 2 bytes for the index.
    assert repaired.header.bone_index_size == 2


def model_from_bytes(raw: bytes) -> PMXModel:
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".pmx", delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    try:
        return read_pmx(temp)
    finally:
        temp.unlink(missing_ok=True)
