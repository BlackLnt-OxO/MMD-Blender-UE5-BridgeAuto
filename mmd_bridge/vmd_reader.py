"""Strict VMD (Vocaloid Motion Data) reader — plan §15 / §22 layer A.

Why a new reader
----------------
``mmd_bridge.core.inspect_vmd()`` decodes bone names with
``errors="replace"``, which silently turns undecodable names into U+FFFD.  A
VMD whose bone names cannot be represented in CP932 is therefore indistinguishable
from a healthy one, and the loss only surfaces much later as "the physics
animation is missing" with no failing check to point at.

This module never repairs a decode error.  Every Shift-JIS field is read with
``errors="strict"`` and a failure raises :class:`VMDEncodingError` carrying the
offending bytes, so the caller can report ``VMD_CP932_DECODE_FAILED`` instead of
proceeding with corrupted names.

Layout (little-endian throughout)
---------------------------------
======================  =====  ==============================================
Section                 Bytes  Contents
======================  =====  ==============================================
Header                  30     ``"Vocaloid Motion Data 0002"``, NUL padded
Model name              20     VMD 2.0; 10 for the legacy VMD 1.0 header
Bone frames             var    name[15] + frame u32 + pos 3f + rot 4f + ip[64]
Morph frames            var    name[15] + frame u32 + weight f
Camera frames           var    frame u32 + dist f + pos 3f + rot 3f + ip[24]
                               + fov u32 + perspective u8
Light frames            var    frame u32 + colour 3f + pos 3f
Shadow frames           var    frame u32 + mode u8 + distance f
IK / display frames     var    frame u32 + visible u8 + n u32 + n*(name[15]+u32)
======================  =====  ==============================================

VMD stores the bone/morph name on *every* frame, not once per track, so
:class:`VMDBoneFrame` carries its own ``name`` and the per-bone view is derived
from it.  Numeric payloads are plain IEEE-754 floats.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Format constants
# --------------------------------------------------------------------------

VMD_MAGIC_SIZE = 30
VMD_MAGIC_V2 = b"Vocaloid Motion Data 0002"
VMD_MAGIC_V1 = b"Vocaloid Motion Data file"

MODEL_NAME_SIZE_V2 = 20
MODEL_NAME_SIZE_V1 = 10

#: VMD stores every name in a fixed 15-byte Shift-JIS slot.
MAX_BONE_NAME_BYTES = 15
BONE_NAME_ENCODING = "cp932"

BONE_FRAME_SIZE = 111
MORPH_FRAME_SIZE = 23
CAMERA_FRAME_SIZE = 61
LIGHT_FRAME_SIZE = 28
SHADOW_FRAME_SIZE = 9

#: IK target record: ``name[15] + index u32 + reserved u16``.
#:
#: The 19-byte reading (``name[15] + index u32``) is what most format
#: documentation states, but every one of the 25 real VMD files on hand that
#: contains an IK section lands exactly at EOF only with 21.  With 19 each file
#: leaves a residue of ``2 * target_count`` bytes (8, 18, 22, 32, 34, 161 …),
#: which is precisely the pattern observed.  21 is therefore the real stride.
IK_TARGET_SIZE = 21

BONE_INTERPOLATION_SIZE = 64
CAMERA_INTERPOLATION_SIZE = 24

#: Error codes (方案 §25) raised or reported by this module.
VMD_INVALID = "VMD_INVALID"
VMD_CP932_DECODE_FAILED = "VMD_CP932_DECODE_FAILED"
VMD_BONE_NAME_UNREPRESENTABLE = "VMD_BONE_NAME_UNREPRESENTABLE"
#: A name longer than its fixed slot, cut mid-character by VMD itself.
VMD_NAME_TRUNCATED = "VMD_NAME_TRUNCATED"

__all__ = [
    "BONE_FRAME_SIZE",
    "CAMERA_FRAME_SIZE",
    "IK_TARGET_SIZE",
    "LIGHT_FRAME_SIZE",
    "MAX_BONE_NAME_BYTES",
    "MORPH_FRAME_SIZE",
    "SHADOW_FRAME_SIZE",
    "VMD_BONE_NAME_UNREPRESENTABLE",
    "VMD_CP932_DECODE_FAILED",
    "VMD_INVALID",
    "VMD_NAME_TRUNCATED",
    "VMDBoneFrame",
    "VMDBoneTrack",
    "VMDCameraFrame",
    "VMDDocument",
    "VMDEncodingError",
    "VMDError",
    "VMDIKFrame",
    "VMDIKTarget",
    "VMDLightFrame",
    "VMDMorphFrame",
    "VMDMorphTrack",
    "VMDNameIssue",
    "VMDShadowFrame",
    "VMDTruncatedError",
    "NameField",
    "bone_name_bytes",
    "bone_name_issue",
    "decode_name",
    "decode_name_field",
    "encode_bone_name",
    "parse_vmd",
    "read_vmd",
]


class VMDError(ValueError):
    """Base class for malformed-VMD failures."""

    code = VMD_INVALID


class VMDTruncatedError(VMDError):
    """The byte stream ends inside a section that the header promised."""

    code = VMD_INVALID


class VMDEncodingError(VMDError):
    """A Shift-JIS field could not be decoded with ``errors="strict"``."""

    code = VMD_CP932_DECODE_FAILED

    def __init__(self, message: str, raw: bytes = b"", offset: int = 0):
        super().__init__(message)
        self.raw = raw
        self.offset = offset


# --------------------------------------------------------------------------
# Name helpers
# --------------------------------------------------------------------------

def _is_lead_byte(value: int) -> bool:
    """True for a Shift-JIS lead byte, i.e. one that needs a trail byte."""
    return 0x81 <= value <= 0x9F or 0xE0 <= value <= 0xEF


@dataclass(frozen=True, slots=True)
class NameField:
    """Result of decoding one fixed-width name slot."""

    name: str
    issue: str | None = None
    detail: str = ""
    raw: bytes = b""

    def __bool__(self) -> bool:
        return bool(self.name)


def decode_name(raw: bytes, *, offset: int = 0, what: str = "名称") -> str:
    """Decode one NUL-padded Shift-JIS field, refusing to guess.

    ``errors="strict"`` is deliberate: ``"replace"`` would hide exactly the
    corruption this reader exists to catch.

    Note this rejects a *truncated* name too (see :func:`decode_name_field`);
    use that function when reading real files, which legitimately contain them.
    """
    field = decode_name_field(raw, offset=offset, what=what)
    if field.issue:
        raise VMDEncodingError(
            f"{field.issue}: {what} 解码问题（偏移 {offset}）：{field.detail}",
            raw=raw.split(b"\x00", 1)[0], offset=offset)
    return field.name


def decode_name_field(raw: bytes, *, offset: int = 0,
                      what: str = "名称") -> NameField:
    """Decode a name slot, tolerating VMD's own mid-character truncation.

    VMD stores names in a fixed slot (15 bytes for bone/morph/IK, 20 for the
    model name).  A name longer than the slot is cut mid-character, which leaves
    an orphan lead byte at the very end and makes the whole field undecodable.

    That is a property of the format, not corruption — 14 of the 66 real VMD
    files on hand contain such names — so it is reported as
    :data:`VMD_NAME_TRUNCATED` with the decodable prefix returned, instead of
    being rejected.

    The discriminator is that a truncated field is **completely full**: VMD only
    cuts a character when it has run out of room, so there is no NUL padding.
    A field that *is* NUL-padded but still fails to decode is genuinely
    mis-encoded (e.g. UTF-8 written into a Shift-JIS slot) and stays an error.
    """
    trimmed = raw.split(b"\x00", 1)[0]
    try:
        return NameField(trimmed.decode(BONE_NAME_ENCODING, errors="strict"), raw=raw)
    except UnicodeDecodeError as exc:
        slot_full = b"\x00" not in raw
        if slot_full and trimmed:
            prefix, orphan = _strip_orphan_leads(trimmed)
            if prefix and len(orphan) <= 2:
                try:
                    decoded = prefix.decode(BONE_NAME_ENCODING, errors="strict")
                except UnicodeDecodeError:
                    pass
                else:
                    return NameField(
                        decoded, VMD_NAME_TRUNCATED,
                        f"{what} 超出定长槽位被截断，尾部残留半个字符 "
                        f"{orphan.hex(' ')}；已保留可解码前缀 {decoded!r}", raw)
        return NameField(
            trimmed.decode(BONE_NAME_ENCODING, errors="replace"),
            VMD_CP932_DECODE_FAILED,
            f"{what} 无法用 CP932 严格解码（原始字节 {trimmed!r}）：{exc}", raw)


def _strip_orphan_leads(raw: bytes) -> tuple[bytes, bytes]:
    """Split trailing orphan lead bytes off *raw*.

    Returns ``(prefix, orphan)`` where *orphan* is the shortest trailing run of
    lead bytes whose removal lets *prefix* decode cleanly.
    """
    orphan = b""
    while raw and _is_lead_byte(raw[-1]):
        orphan = raw[-1:] + orphan
        raw = raw[:-1]
    return raw, orphan


def encode_bone_name(name: str) -> bytes:
    """Encode a bone name into its 15-byte VMD slot.

    Raises:
        VMDEncodingError: the name is not representable in CP932, or longer
            than 15 bytes.  Both would be silently truncated by VMD itself.
    """
    try:
        encoded = name.encode(BONE_NAME_ENCODING, errors="strict")
    except UnicodeEncodeError as exc:
        raise VMDEncodingError(
            f"{VMD_BONE_NAME_UNREPRESENTABLE}: 骨骼名 {name!r} 无法编码为 CP932：{exc}",
            raw=name.encode("utf-8", "replace")) from exc
    if len(encoded) > MAX_BONE_NAME_BYTES:
        raise VMDEncodingError(
            f"{VMD_BONE_NAME_UNREPRESENTABLE}: 骨骼名 {name!r} 编码后 "
            f"{len(encoded)} 字节，超过 VMD 的 {MAX_BONE_NAME_BYTES} 字节上限，"
            "写入时会被静默截断")
    return encoded


def bone_name_issue(name: str) -> str | None:
    """Return an error code if *name* cannot round-trip through a VMD slot.

    Non-raising counterpart of :func:`encode_bone_name`, for validators that
    collect every offending name instead of stopping at the first.
    """
    try:
        encode_bone_name(name)
    except VMDEncodingError:
        return VMD_BONE_NAME_UNREPRESENTABLE
    return None


def bone_name_bytes(name: str) -> bytes:
    """CP932 byte length of *name*, whether or not it fits the slot."""
    return name.encode(BONE_NAME_ENCODING, errors="replace")


# --------------------------------------------------------------------------
# Frame structures
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VMDBoneFrame:
    """One bone keyframe.

    ``interpolation`` is kept raw: it is an opaque 64-byte curve description
    that MMD, MMM and mmd_tools each interpret themselves, so re-encoding it
    would risk changing playback timing.
    """

    name: str
    frame: int
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    interpolation: bytes
    raw_name: bytes = b""


@dataclass(frozen=True, slots=True)
class VMDMorphFrame:
    name: str
    frame: int
    weight: float
    raw_name: bytes = b""


@dataclass(frozen=True, slots=True)
class VMDCameraFrame:
    frame: int
    distance: float
    position: tuple[float, float, float]
    rotation: tuple[float, float, float]
    interpolation: bytes
    fov: int
    perspective: int


@dataclass(frozen=True, slots=True)
class VMDLightFrame:
    frame: int
    color: tuple[float, float, float]
    position: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class VMDShadowFrame:
    frame: int
    mode: int
    distance: float


@dataclass(frozen=True, slots=True)
class VMDIKTarget:
    name: str
    bone_index: int
    raw_name: bytes = b""
    reserved: bytes = b"\0\0"


@dataclass(frozen=True, slots=True)
class VMDIKFrame:
    frame: int
    visible: bool
    targets: tuple[VMDIKTarget, ...]


@dataclass
class VMDBoneTrack:
    """Every keyframe of a single bone, in file order."""

    name: str
    frames: list[VMDBoneFrame] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def first_frame(self) -> int:
        return min((f.frame for f in self.frames), default=0)

    @property
    def last_frame(self) -> int:
        return max((f.frame for f in self.frames), default=0)

    def frames_in_range(self, start: int, end: int) -> list[VMDBoneFrame]:
        return [f for f in self.frames if start <= f.frame <= end]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
        }


@dataclass
class VMDMorphTrack:
    name: str
    frames: list[VMDMorphFrame] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def first_frame(self) -> int:
        return min((f.frame for f in self.frames), default=0)

    @property
    def last_frame(self) -> int:
        return max((f.frame for f in self.frames), default=0)


@dataclass(frozen=True, slots=True)
class VMDNameIssue:
    """One name slot that could not be decoded cleanly."""

    code: str
    detail: str
    offset: int
    section: str


@dataclass
class VMDDocument:
    """A fully parsed VMD file."""

    path: str = ""
    version: str = "0002"
    model_name: str = ""
    raw_model_name: bytes = b""
    raw_magic: bytes = b""
    sections_present: int = 6
    raw_trailing: bytes = b""

    #: Bone keyframes in file order.
    bone_frames: list[VMDBoneFrame] = field(default_factory=list)
    morph_frames: list[VMDMorphFrame] = field(default_factory=list)
    camera_frames: list[VMDCameraFrame] = field(default_factory=list)
    light_frames: list[VMDLightFrame] = field(default_factory=list)
    shadow_frames: list[VMDShadowFrame] = field(default_factory=list)
    ik_frames: list[VMDIKFrame] = field(default_factory=list)

    #: Derived per-bone view, built during parsing.  Insertion-ordered.
    bone_tracks: dict[str, VMDBoneTrack] = field(default_factory=dict)
    morph_tracks: dict[str, VMDMorphTrack] = field(default_factory=dict)

    #: Every name slot that was truncated or undecodable.  Never silently
    #: dropped: the validator turns these into explicit findings.
    name_issues: list[VMDNameIssue] = field(default_factory=list)

    #: Set when the IK section could not be parsed.  The IK section is not used
    #: by any validation layer, so a failure here must not discard the bone and
    #: morph data that *is* needed.
    ik_error: str = ""

    #: Bytes left over after the last known section (suspicious but not fatal).
    trailing_bytes: int = 0
    #: Size of the source file, for reporting.
    file_size: int = 0

    def record_name(self, field: NameField, section: str, offset: int) -> str:
        """Use a decoded name slot, remembering any issue it carried."""
        if field.issue:
            self.name_issues.append(VMDNameIssue(
                code=field.issue, detail=field.detail,
                offset=offset, section=section))
        return field.name

    @property
    def truncated_names(self) -> list[VMDNameIssue]:
        return [i for i in self.name_issues if i.code == VMD_NAME_TRUNCATED]

    @property
    def decode_failures(self) -> list[VMDNameIssue]:
        return [i for i in self.name_issues
                if i.code == VMD_CP932_DECODE_FAILED]

    @property
    def bone_names(self) -> list[str]:
        """Unique bone names, in first-appearance order."""
        return list(self.bone_tracks)

    @property
    def bone_frame_count(self) -> int:
        return len(self.bone_frames)

    @property
    def bone_track_count(self) -> int:
        return len(self.bone_tracks)

    @property
    def max_frame(self) -> int:
        """Highest frame number anywhere in the file."""
        highest = 0
        for collection in (self.bone_frames, self.morph_frames,
                           self.camera_frames, self.light_frames,
                           self.shadow_frames, self.ik_frames):
            for frame in collection:
                if frame.frame > highest:
                    highest = frame.frame
        return highest

    @property
    def bone_frame_range(self) -> tuple[int, int]:
        """``(first, last)`` frame across all bone tracks, or ``(0, 0)``."""
        if not self.bone_frames:
            return (0, 0)
        numbers = [f.frame for f in self.bone_frames]
        return (min(numbers), max(numbers))

    def bone_track(self, name: str) -> VMDBoneTrack | None:
        return self.bone_tracks.get(name)

    def frames_in_range(self, name: str, start: int, end: int) -> list[VMDBoneFrame]:
        track = self.bone_tracks.get(name)
        return track.frames_in_range(start, end) if track else []

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "model_name": self.model_name,
            "file_size": self.file_size,
            "bone_frames": len(self.bone_frames),
            "bone_tracks": len(self.bone_tracks),
            "morph_frames": len(self.morph_frames),
            "morph_tracks": len(self.morph_tracks),
            "camera_frames": len(self.camera_frames),
            "light_frames": len(self.light_frames),
            "shadow_frames": len(self.shadow_frames),
            "ik_frames": len(self.ik_frames),
            "ik_error": self.ik_error,
            "max_frame": self.max_frame,
            "bone_frame_range": list(self.bone_frame_range),
            "trailing_bytes": self.trailing_bytes,
            "name_issues": len(self.name_issues),
            "truncated_names": len(self.truncated_names),
            "decode_failures": len(self.decode_failures),
        }


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

class _Cursor:
    """Bounds-checked sequential reader.

    Fixed-width numeric fields are unpacked straight out of the buffer rather
    than through a slice: a 3 M-frame VMD performs ~15 M field reads, and the
    intermediate ``bytes`` objects dominate the runtime otherwise.
    """

    def __init__(self, data: bytes, path: str = ""):
        self.data = data
        self.pos = 0
        self.path = path

    def _ensure(self, count: int, what: str) -> int:
        end = self.pos + count
        if end > len(self.data):
            raise VMDTruncatedError(
                f"{VMD_INVALID}: VMD {what} 截断——需要在偏移 {self.pos} 处读取 "
                f"{count} 字节，文件仅 {len(self.data)} 字节"
                + (f"（{self.path}）" if self.path else ""))
        return end

    def _take(self, count: int, what: str) -> bytes:
        end = self._ensure(count, what)
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u8(self, what: str) -> int:
        end = self._ensure(1, what)
        value = self.data[self.pos]
        self.pos = end
        return value

    def u32(self, what: str) -> int:
        end = self._ensure(4, what)
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos = end
        return value

    def f32(self, what: str) -> float:
        end = self._ensure(4, what)
        value = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos = end
        return value

    def fvec(self, count: int, what: str) -> tuple[float, ...]:
        end = self._ensure(4 * count, what)
        values = struct.unpack_from("<" + "f" * count, self.data, self.pos)
        self.pos = end
        return values

    def name(self, what: str) -> NameField:
        offset = self.pos
        return decode_name_field(self._take(MAX_BONE_NAME_BYTES, what),
                                 offset=offset, what=what)

    def skip(self, count: int, what: str) -> None:
        self.pos = self._ensure(count, what)

    def remaining(self) -> int:
        return len(self.data) - self.pos


# --------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------

def _read_header(cursor: _Cursor) -> tuple[str, NameField, bytes]:
    magic = cursor._take(VMD_MAGIC_SIZE, "Header")
    if magic.startswith(VMD_MAGIC_V2):
        version, name_size = "0002", MODEL_NAME_SIZE_V2
    elif magic.startswith(VMD_MAGIC_V1):
        version, name_size = "0001", MODEL_NAME_SIZE_V1
    else:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: 不是有效 VMD——Header 魔数不匹配：{magic[:30]!r}")

    offset = cursor.pos
    field = decode_name_field(cursor._take(name_size, "ModelName"),
                              offset=offset, what="模型名")
    return version, field, magic


def _read_bone_frames(cursor: _Cursor, document: VMDDocument) -> None:
    count = cursor.u32("Bone 计数")
    if count > cursor.remaining() // BONE_FRAME_SIZE:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: VMD Bone 段声明 {count} 帧，但剩余 "
            f"{cursor.remaining()} 字节不足（每帧 {BONE_FRAME_SIZE} 字节）")

    for _ in range(count):
        name_offset = cursor.pos
        field = cursor.name("骨骼名")
        name = document.record_name(field, "Bone", name_offset)
        frame = cursor.u32("Bone 帧号")
        position = cursor.fvec(3, "Bone 位移")
        rotation = cursor.fvec(4, "Bone 旋转")
        interpolation = cursor._take(BONE_INTERPOLATION_SIZE, "Bone 插值")
        entry = VMDBoneFrame(
            name=name, frame=frame,
            position=position,      # type: ignore[arg-type]
            rotation=rotation,      # type: ignore[arg-type]
            interpolation=interpolation, raw_name=field.raw)
        document.bone_frames.append(entry)
        document.bone_tracks.setdefault(
            name, VMDBoneTrack(name=name)).frames.append(entry)


def _read_morph_frames(cursor: _Cursor, document: VMDDocument) -> None:
    count = cursor.u32("Morph 计数")
    if count > cursor.remaining() // MORPH_FRAME_SIZE:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: VMD Morph 段声明 {count} 帧，但剩余 "
            f"{cursor.remaining()} 字节不足（每帧 {MORPH_FRAME_SIZE} 字节）")

    for _ in range(count):
        name_offset = cursor.pos
        field = cursor.name("表情名")
        name = document.record_name(field, "Morph", name_offset)
        frame = cursor.u32("Morph 帧号")
        weight = cursor.f32("Morph 权重")
        entry = VMDMorphFrame(name=name, frame=frame, weight=weight, raw_name=field.raw)
        document.morph_frames.append(entry)
        document.morph_tracks.setdefault(
            name, VMDMorphTrack(name=name)).frames.append(entry)


def _read_camera_frames(cursor: _Cursor, document: VMDDocument) -> None:
    count = cursor.u32("Camera 计数")
    if count > cursor.remaining() // CAMERA_FRAME_SIZE:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: VMD Camera 段声明 {count} 帧，但剩余 "
            f"{cursor.remaining()} 字节不足（每帧 {CAMERA_FRAME_SIZE} 字节）")

    for _ in range(count):
        frame = cursor.u32("Camera 帧号")
        distance = cursor.f32("Camera 距离")
        position = cursor.fvec(3, "Camera 位置")
        rotation = cursor.fvec(3, "Camera 旋转")
        interpolation = cursor._take(CAMERA_INTERPOLATION_SIZE, "Camera 插值")
        fov = cursor.u32("Camera FOV")
        perspective = cursor.u8("Camera 透视")
        document.camera_frames.append(VMDCameraFrame(
            frame=frame, distance=distance,
            position=position,      # type: ignore[arg-type]
            rotation=rotation,      # type: ignore[arg-type]
            interpolation=interpolation, fov=fov, perspective=perspective))


def _read_light_frames(cursor: _Cursor, document: VMDDocument) -> None:
    count = cursor.u32("Light 计数")
    if count > cursor.remaining() // LIGHT_FRAME_SIZE:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: VMD Light 段声明 {count} 帧，但剩余 "
            f"{cursor.remaining()} 字节不足（每帧 {LIGHT_FRAME_SIZE} 字节）")

    for _ in range(count):
        frame = cursor.u32("Light 帧号")
        color = cursor.fvec(3, "Light 颜色")
        position = cursor.fvec(3, "Light 位置")
        document.light_frames.append(VMDLightFrame(
            frame=frame,
            color=color,            # type: ignore[arg-type]
            position=position))     # type: ignore[arg-type]


def _read_shadow_frames(cursor: _Cursor, document: VMDDocument) -> None:
    count = cursor.u32("Shadow 计数")
    if count > cursor.remaining() // SHADOW_FRAME_SIZE:
        raise VMDTruncatedError(
            f"{VMD_INVALID}: VMD Shadow 段声明 {count} 帧，但剩余 "
            f"{cursor.remaining()} 字节不足（每帧 {SHADOW_FRAME_SIZE} 字节）")

    for _ in range(count):
        frame = cursor.u32("Shadow 帧号")
        mode = cursor.u8("Shadow 模式")
        distance = cursor.f32("Shadow 距离")
        document.shadow_frames.append(VMDShadowFrame(
            frame=frame, mode=mode, distance=distance))


def _read_ik_frames(cursor: _Cursor, document: VMDDocument) -> None:
    """Parse the IK / display section, tolerating failure.

    No validation layer depends on IK data, so a structural surprise here must
    not throw away the bone and morph data that *is* needed.  Failures are
    recorded in ``document.ik_error`` rather than raised.
    """
    count = cursor.u32("IK 计数")
    for index in range(count):
        if cursor.remaining() < 9:
            document.ik_error = (
                f"IK 段第 {index} 帧头部截断于偏移 {cursor.pos}")
            return
        frame = cursor.u32("IK 帧号")
        visible = bool(cursor.u8("IK 可见性"))
        target_count = cursor.u32("IK 目标数")
        if target_count > cursor.remaining() // IK_TARGET_SIZE:
            document.ik_error = (
                f"IK 段第 {index} 帧声明 {target_count} 个目标，"
                f"但剩余 {cursor.remaining()} 字节不足"
                f"（每目标 {IK_TARGET_SIZE} 字节）")
            return
        targets = []
        for _ in range(target_count):
            name_offset = cursor.pos
            field = cursor.name("IK 目标名")
            name = document.record_name(field, "IK", name_offset)
            bone_index = cursor.u32("IK 目标骨骼索引")
            reserved = cursor._take(2, "IK 保留字段")
            targets.append(VMDIKTarget(name=name, bone_index=bone_index,
                                       raw_name=field.raw, reserved=reserved))
        document.ik_frames.append(VMDIKFrame(
            frame=frame, visible=visible, targets=tuple(targets)))


#: Sections in the fixed order VMD stores them.  The first two are mandatory;
#: older exporters omit everything from Camera onwards.
_SECTIONS = (
    ("Bone", _read_bone_frames),
    ("Morph", _read_morph_frames),
    ("Camera", _read_camera_frames),
    ("Light", _read_light_frames),
    ("Shadow", _read_shadow_frames),
    ("IK", _read_ik_frames),
)
_OPTIONAL_FROM_INDEX = 2


def parse_vmd(data: bytes, path: str = "") -> VMDDocument:
    """Parse a VMD byte string.

    The header, model name, bone and morph sections are mandatory.  Camera,
    light, shadow and IK sections are read when present; older exporters omit
    them, so a file ending after the morph section is valid.

    Raises:
        VMDTruncatedError: the stream ends inside a promised section.
        VMDEncodingError: a name field is not valid CP932.
    """
    document = VMDDocument(path=path, file_size=len(data))
    cursor = _Cursor(data, path)

    document.version, model_field, document.raw_magic = _read_header(cursor)
    document.model_name = document.record_name(model_field, "Header",
                                               MODEL_NAME_SIZE_V1)
    document.raw_model_name = model_field.raw

    for index, (label, reader) in enumerate(_SECTIONS):
        if cursor.remaining() == 0:
            if index >= _OPTIONAL_FROM_INDEX:
                break
            raise VMDTruncatedError(
                f"{VMD_INVALID}: VMD 在 {label} 段之前结束"
                + (f"（{path}）" if path else ""))
        if cursor.remaining() < 4:
            raise VMDTruncatedError(
                f"{VMD_INVALID}: VMD {label} 段计数截断于偏移 {cursor.pos}")
        reader(cursor, document)
        document.sections_present = index + 1

    document.trailing_bytes = cursor.remaining()
    document.raw_trailing = cursor.data[cursor.pos:]
    return document


def read_vmd(path: str | Path) -> VMDDocument:
    """Read and fully parse a VMD file from disk."""
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise VMDError(f"{VMD_INVALID}: 无法读取 VMD 文件 {source}：{exc}") from exc
    return parse_vmd(data, path=str(source))
