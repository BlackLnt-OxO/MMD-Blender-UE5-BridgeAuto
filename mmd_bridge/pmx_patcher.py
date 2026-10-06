"""Section-aware PMX patcher for bone names and Display Frames.

Unmodified byte ranges are copied from the original file unchanged.  Only
selected bone-name slots and the Display Frame section are re-encoded.

Inside the Display Frame section the writer is deliberately as lazy as possible,
so untouched frames never get re-encoded:

  Case A — no existing PhysicsBake frame
      frame-count is bumped by one and the original frame bytes are appended
      verbatim, then the new PhysicsBake record is written after them.
      Original frames are preserved bit-for-bit.

  Case B — a PhysicsBake frame already exists (reuse)
      every original frame is copied verbatim; only the PhysicsBake record is
      re-emitted with the extra bone items appended.  Its position is kept.

  Case C — no bones to add
      the file is not written at all (handled by the caller).

Regenerating the whole section would also work, but it would silently normalise
any author frame that is not encoded exactly the way this reader would encode it.
Copying raw bytes cannot do that.
"""
from __future__ import annotations

import struct
from pathlib import Path

from .pmx_binary import ByteWriter, PMXFormatError
from .pmx_model import (
    FRAME_ITEM_BONE,
    FRAME_ITEM_MORPH,
    PMXDisplayFrame,
    PMXModel,
)


def serialize_frame(model: PMXModel, frame: PMXDisplayFrame) -> bytes:
    """Encode one display-frame record (no leading count field)."""
    header = model.header
    writer = ByteWriter("utf-8" if header.encoding else "utf-16-le")
    writer.write_text(frame.name_jp)
    writer.write_text(frame.name_en)
    writer.write_int(frame.special_flag, 1)
    writer.write_int(len(frame.items), 4)
    for item in frame.items:
        if item.item_type == FRAME_ITEM_BONE:
            writer.write_int(FRAME_ITEM_BONE, 1)
            writer.write_index(item.index, header.bone_index_size)
        elif item.item_type == FRAME_ITEM_MORPH:
            writer.write_int(FRAME_ITEM_MORPH, 1)
            writer.write_index(item.index, header.morph_index_size)
        else:
            raise PMXFormatError(f"无法序列化未知显示枠条目类型: {item.item_type}")
    return writer.build()


def serialize_display_frames(model: PMXModel, frames: list[PMXDisplayFrame]) -> bytes:
    """Encode the whole Display Frame section from parsed data.

    Kept as an explicit fallback for models whose frames carry no raw range.
    """
    writer = ByteWriter()
    writer.write_int(len(frames), 4)
    for frame in frames:
        writer.write_raw(serialize_frame(model, frame))
    return writer.build()


def patch_display_frames(
    original: bytes,
    model: PMXModel,
    frames: list[PMXDisplayFrame],
) -> bytes:
    """Rebuild the Display Frame section, reusing original frame bytes."""
    original_by_identity = {id(frame): frame for frame in model.display_frames}

    writer = ByteWriter()
    # The frame count field lives immediately before the first frame record.
    writer.write_int(len(frames), 4)

    for frame in frames:
        # A frame is copied verbatim only if it came from the parsed model *and*
        # still matches its original bytes.  Modified or new frames are encoded.
        if id(frame) in original_by_identity and frame.has_raw_range:
            writer.write_raw(original[frame.raw_start:frame.raw_end])
        else:
            writer.write_raw(serialize_frame(model, frame))
    return writer.build()


def patch_pmx_bytes(original: bytes, model: PMXModel, frames: list[PMXDisplayFrame],
                    bone_names: dict[int, str] | None = None) -> bytes:
    """Patch bone names and display frames; copy every other byte unchanged."""
    if model.display_frame_start > model.rigid_body_start:
        raise PMXFormatError("区段偏移异常：Display Frame 起点晚于 RigidBody 起点")
    if not original[model.rigid_body_start:]:
        raise PMXFormatError("区段偏移异常：RigidBody 起点之后没有内容")
    changes = [(model.display_frame_start, model.rigid_body_start,
                patch_display_frames(original, model, frames))]
    encoding = "utf-16-le" if model.header.encoding == 0 else "utf-8"
    for index, fixed in (bone_names or {}).items():
        if not 0 <= index < len(model.bones):
            raise PMXFormatError(f"Bone Index 越界: {index}")
        bone = model.bones[index]
        if fixed == bone.name_jp:
            continue
        if bone.name_jp_range is None:
            raise PMXFormatError(f"Bone #{index} 缺少名称原始区段")
        encoded = fixed.encode(encoding)
        changes.append((*bone.name_jp_range, struct.pack("<i", len(encoded)) + encoded))
    changes.sort(key=lambda item: item[0])
    pieces, cursor = [], 0
    for start, end, payload in changes:
        if not cursor <= start <= end <= len(original):
            raise PMXFormatError(f"PMX 区段重叠或越界: {start}..{end}")
        pieces.extend((original[cursor:start], payload))
        cursor = end
    pieces.append(original[cursor:])
    return b"".join(pieces)


def default_output_path(source: str | Path, suffix: str = "_PhysicsBakeFixed") -> Path:
    """Never overwrite the source model (SPEC §17 / 任务书 §19)."""
    path = Path(source)
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def write_repaired_pmx(
    source: str | Path,
    model: PMXModel,
    frames: list[PMXDisplayFrame],
    output: str | Path | None = None,
) -> Path:
    """Patch and write.  Refuses to write onto the original model."""
    source_path = Path(source).resolve()
    target = Path(output).resolve() if output else default_output_path(source_path).resolve()
    if target == source_path:
        raise ValueError("拒绝覆盖原始 PMX；请指定不同的输出路径")
    original = source_path.read_bytes()
    repaired = patch_pmx_bytes(original, model, frames)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(repaired)
    return target


def write_processed_pmx(source: str | Path, model: PMXModel,
                        frames: list[PMXDisplayFrame], bone_names: dict[int, str],
                        output: str | Path) -> Path:
    source_path, target = Path(source).resolve(), Path(output).resolve()
    if source_path == target:
        raise ValueError("拒绝覆盖原始 PMX")
    data = patch_pmx_bytes(source_path.read_bytes(), model, frames, bone_names)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return target
