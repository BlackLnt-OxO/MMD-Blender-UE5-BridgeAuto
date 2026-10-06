"""Unit tests for the strict VMD reader (方案 §15).

Every case runs offline against synthesised byte strings, so no real motion
file is needed.  The format details asserted here were verified against 66
real VMD files on disk; the two that are easy to get wrong are called out
explicitly:

* the IK target record is **21** bytes, not the 19 that most documentation
  states;
* a name longer than its fixed slot is cut mid-character by VMD itself, which
  is a truncation and not an encoding error.
"""
from __future__ import annotations

import struct

import pytest

from mmd_bridge.vmd_reader import (
    BONE_FRAME_SIZE,
    IK_TARGET_SIZE,
    MAX_BONE_NAME_BYTES,
    VMD_CP932_DECODE_FAILED,
    VMD_NAME_TRUNCATED,
    VMD_BONE_NAME_UNREPRESENTABLE,
    VMDEncodingError,
    VMDTruncatedError,
    bone_name_issue,
    decode_name,
    decode_name_field,
    encode_bone_name,
    parse_vmd,
    read_vmd,
)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

def _name_slot(name: str | bytes, width: int = MAX_BONE_NAME_BYTES) -> bytes:
    raw = name if isinstance(name, bytes) else name.encode("cp932")
    assert len(raw) <= width, f"测试名称过长: {name!r}"
    return raw + b"\x00" * (width - len(raw))


def _bone_frame(name, frame, pos=(0.0, 0.0, 0.0), rot=(0.0, 0.0, 0.0, 1.0)) -> bytes:
    out = _name_slot(name)
    out += struct.pack("<I", frame)
    out += struct.pack("<3f", *pos)
    out += struct.pack("<4f", *rot)
    out += b"\x00" * 64
    assert len(out) == BONE_FRAME_SIZE
    return out


def _morph_frame(name, frame, weight) -> bytes:
    return _name_slot(name) + struct.pack("<I", frame) + struct.pack("<f", weight)


def _camera_frame(frame) -> bytes:
    out = struct.pack("<I", frame) + struct.pack("<f", 30.0)
    out += struct.pack("<3f", 0.0, 0.0, 0.0)
    out += struct.pack("<3f", 0.0, 0.0, 0.0)
    out += b"\x00" * 24
    out += struct.pack("<I", 30) + b"\x00"
    assert len(out) == 61
    return out


def _light_frame(frame) -> bytes:
    return (struct.pack("<I", frame) + struct.pack("<3f", 1.0, 1.0, 1.0)
            + struct.pack("<3f", 0.0, 0.0, 0.0))


def _shadow_frame(frame) -> bytes:
    return struct.pack("<I", frame) + b"\x00" + struct.pack("<f", 1.0)


def _ik_frame(frame, visible, targets) -> bytes:
    out = struct.pack("<I", frame) + bytes([1 if visible else 0])
    out += struct.pack("<I", len(targets))
    for name, index in targets:
        # 21-byte record: name[15] + index u32 + reserved u16
        out += _name_slot(name) + struct.pack("<I", index) + struct.pack("<H", 0)
    return out


def make_vmd(
    *,
    version: str = "0002",
    model_name: str = "test model",
    bone_frames: list[bytes] | None = None,
    morph_frames: list[bytes] | None = None,
    camera_frames: list[bytes] | None = None,
    light_frames: list[bytes] | None = None,
    shadow_frames: list[bytes] | None = None,
    ik_frames: list[bytes] | None = None,
    trailing: bytes = b"",
    omit_from: str | None = None,
) -> bytes:
    """Assemble a VMD byte string.  ``omit_from`` drops optional sections."""
    magic = (b"Vocaloid Motion Data 0002" if version == "0002"
             else b"Vocaloid Motion Data file")
    out = magic + b"\x00" * (30 - len(magic))
    name_width = 20 if version == "0002" else 10
    out += _name_slot(model_name, name_width)

    order = {
        "bone": bone_frames or [],
        "morph": morph_frames or [],
        "camera": camera_frames or [],
        "light": light_frames or [],
        "shadow": shadow_frames or [],
        "ik": ik_frames or [],
    }
    labels = list(order)
    stop = labels.index(omit_from) if omit_from in labels else len(labels)
    for label in labels[:stop]:
        entries = order[label]
        out += struct.pack("<I", len(entries))
        out += b"".join(entries)
    return out + trailing


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

def test_reads_v2_header_and_model_name():
    doc = parse_vmd(make_vmd(model_name="初音ミク"))
    assert doc.version == "0002"
    assert doc.model_name == "初音ミク"


def test_reads_v1_header_with_10_byte_model_name():
    doc = parse_vmd(make_vmd(version="0001", model_name="旧式"))
    assert doc.version == "0001"
    assert doc.model_name == "旧式"


def test_rejects_non_vmd_magic():
    with pytest.raises(VMDTruncatedError, match="Header 魔数"):
        parse_vmd(b"NOT A VMD FILE" + b"\x00" * 40)


def test_rejects_file_that_ends_before_bone_section():
    magic = b"Vocaloid Motion Data 0002" + b"\x00" * 5
    with pytest.raises(VMDTruncatedError, match="Bone"):
        parse_vmd(magic + _name_slot("m", 20))


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def test_reads_bone_frames_and_builds_tracks():
    data = make_vmd(bone_frames=[
        _bone_frame("センター", 0, pos=(1.0, 2.0, 3.0)),
        _bone_frame("センター", 10),
        _bone_frame("左足", 5),
    ])
    doc = parse_vmd(data)

    assert doc.bone_frame_count == 3
    assert doc.bone_track_count == 2
    assert doc.bone_names == ["センター", "左足"]

    track = doc.bone_track("センター")
    assert track.count == 2
    assert track.first_frame == 0
    assert track.last_frame == 10
    assert track.frames[0].position == (1.0, 2.0, 3.0)
    assert len(track.frames[0].interpolation) == 64


def test_reads_all_optional_sections():
    data = make_vmd(
        bone_frames=[_bone_frame("センター", 0)],
        morph_frames=[_morph_frame("まばたき", 3, 1.0)],
        camera_frames=[_camera_frame(7)],
        light_frames=[_light_frame(9)],
        shadow_frames=[_shadow_frame(11)],
        ik_frames=[_ik_frame(0, True, [("左足ＩＫ", 3), ("右足ＩＫ", 5)])],
    )
    doc = parse_vmd(data)

    assert doc.morph_frames[0].name == "まばたき"
    assert doc.morph_frames[0].weight == 1.0
    assert doc.camera_frames[0].frame == 7
    assert doc.light_frames[0].frame == 9
    assert doc.shadow_frames[0].frame == 11
    assert doc.ik_frames[0].visible is True
    assert [t.name for t in doc.ik_frames[0].targets] == ["左足ＩＫ", "右足ＩＫ"]
    assert doc.ik_frames[0].targets[1].bone_index == 5
    assert doc.max_frame == 11
    assert doc.trailing_bytes == 0


def test_optional_sections_may_be_absent():
    """Older exporters stop after morph; that is valid, not truncated."""
    doc = parse_vmd(make_vmd(
        bone_frames=[_bone_frame("センター", 0)],
        morph_frames=[_morph_frame("あ", 1, 0.5)],
        omit_from="camera"))
    assert doc.bone_frame_count == 1
    assert doc.camera_frames == []
    assert doc.trailing_bytes == 0


def test_bone_section_count_larger_than_payload_is_truncation():
    data = make_vmd(bone_frames=[_bone_frame("センター", 0)])
    # Rewrite the bone count to claim more frames than exist.
    corrupted = bytearray(data)
    struct.pack_into("<I", corrupted, 50, 99)
    with pytest.raises(VMDTruncatedError, match="Bone"):
        parse_vmd(bytes(corrupted))


def test_trailing_bytes_are_recorded_not_hidden():
    doc = parse_vmd(make_vmd(trailing=b"\xAA" * 8))
    assert doc.trailing_bytes == 8


# --------------------------------------------------------------------------
# IK stride — 21 bytes, empirically verified
# --------------------------------------------------------------------------

def test_ik_target_stride_is_21_bytes():
    """The 19-byte reading leaves 2 bytes per target unaccounted for."""
    data = make_vmd(ik_frames=[
        _ik_frame(0, True, [("左足ＩＫ", 3), ("右足ＩＫ", 5), ("左つま先ＩＫ", 7)])])
    doc = parse_vmd(data)

    assert IK_TARGET_SIZE == 21
    assert len(doc.ik_frames) == 1
    assert len(doc.ik_frames[0].targets) == 3
    # Landing exactly at EOF is what proves the stride is right.
    assert doc.trailing_bytes == 0
    assert doc.ik_error == ""


def test_ik_failure_is_recorded_and_does_not_discard_bone_data():
    """Bone data is what validation needs; an IK surprise must not lose it."""
    data = make_vmd(
        bone_frames=[_bone_frame("センター", 0), _bone_frame("センター", 1)],
        ik_frames=[_ik_frame(0, True, [("左足ＩＫ", 3)])])
    corrupted = bytearray(data)
    # Blow up the IK target count so the section cannot be parsed.
    struct.pack_into("<I", corrupted, len(corrupted) - 21 - 4, 999999)
    doc = parse_vmd(bytes(corrupted))

    assert doc.bone_frame_count == 2          # bone data survived
    assert doc.ik_frames == []
    assert "IK" in doc.ik_error


# --------------------------------------------------------------------------
# Name decoding
# --------------------------------------------------------------------------

def test_strict_decode_accepts_plain_japanese():
    assert decode_name(_name_slot("スカート_10_30")) == "スカート_10_30"


def test_truncated_name_is_reported_not_rejected():
    """VMD cuts a name that overflows the slot, leaving an orphan lead byte."""
    full = "右もみあげ１２３４５".encode("cp932")      # > 15 bytes
    slot = full[:15]
    assert len(slot) == 15 and slot[-1] >= 0x81       # orphan lead byte

    field = decode_name_field(slot)
    assert field.issue == VMD_NAME_TRUNCATED
    assert field.name.startswith("右もみあげ")
    assert "截断" in field.detail


def test_truncated_name_does_not_raise_in_reader():
    slot = "右もみあげ１２３４５".encode("cp932")[:15]
    doc = parse_vmd(make_vmd(bone_frames=[
        slot + struct.pack("<I", 0) + struct.pack("<3f", 0, 0, 0)
        + struct.pack("<4f", 0, 0, 0, 1) + b"\x00" * 64]))
    assert len(doc.truncated_names) == 1
    assert doc.decode_failures == []


def test_nul_padded_invalid_sequence_is_a_decode_failure():
    """A slot with NUL padding is not 'full', so this is mis-encoding, not truncation."""
    field = decode_name_field(b"\x98\x83x\x83\x8b\x83g\x00\x00\x00\x00\x00\x00")
    assert field.issue == VMD_CP932_DECODE_FAILED


def test_strict_decode_raises_on_mis_encoded_name():
    with pytest.raises(VMDEncodingError) as excinfo:
        decode_name(b"\x98\x83x\x83\x8b\x83g\x00\x00\x00\x00\x00\x00")
    assert excinfo.value.code == VMD_CP932_DECODE_FAILED


def test_decode_failure_is_recorded_on_document():
    # 15-byte slot, NUL padded, first sequence invalid in CP932.
    bad = b"\x98\x83x\x83\x8b\x83g" + b"\x00" * 8
    assert len(bad) == MAX_BONE_NAME_BYTES
    doc = parse_vmd(make_vmd(bone_frames=[
        bad + struct.pack("<I", 0) + struct.pack("<3f", 0, 0, 0)
        + struct.pack("<4f", 0, 0, 0, 1) + b"\x00" * 64]))
    assert len(doc.decode_failures) == 1
    assert doc.decode_failures[0].section == "Bone"


# --------------------------------------------------------------------------
# Encoding our own names (方案 §10)
# --------------------------------------------------------------------------

def test_encode_bone_name_accepts_15_byte_name():
    name = "スカート_10_10"          # 14 bytes
    assert len(encode_bone_name(name)) == 14
    assert bone_name_issue(name) is None


def test_encode_bone_name_rejects_16_byte_name():
    name = "スカート_10_1000"        # 8 + 8 = 16 bytes
    assert len(name.encode("cp932")) == 16
    assert bone_name_issue(name) == VMD_BONE_NAME_UNREPRESENTABLE
    with pytest.raises(VMDEncodingError, match="超过"):
        encode_bone_name(name)


def test_encode_bone_name_rejects_non_cp932_character():
    name = "骨骼_😀"
    assert bone_name_issue(name) == VMD_BONE_NAME_UNREPRESENTABLE
    with pytest.raises(VMDEncodingError, match="CP932"):
        encode_bone_name(name)


def test_max_bone_name_bytes_matches_vmd_slot():
    assert MAX_BONE_NAME_BYTES == 15
    assert bone_name_issue("a" * 15) is None
    assert bone_name_issue("a" * 16) == VMD_BONE_NAME_UNREPRESENTABLE


# --------------------------------------------------------------------------
# read_vmd on disk
# --------------------------------------------------------------------------

def test_read_vmd_from_disk(tmp_path):
    target = tmp_path / "m.vmd"
    target.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", 0)]))
    doc = read_vmd(target)
    assert doc.path == str(target)
    assert doc.bone_frame_count == 1
    assert doc.summary()["bone_tracks"] == 1


def test_read_vmd_reports_missing_file(tmp_path):
    with pytest.raises(Exception, match="无法读取"):
        read_vmd(tmp_path / "nope.vmd")
