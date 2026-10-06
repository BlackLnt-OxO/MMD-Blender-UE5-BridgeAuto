"""Lossless VMD bone-name rewriting; all other frame bytes stay untouched."""
from __future__ import annotations

from pathlib import Path

from .vmd_reader import (
    BONE_FRAME_SIZE, MAX_BONE_NAME_BYTES, MODEL_NAME_SIZE_V1,
    MODEL_NAME_SIZE_V2, VMD_MAGIC_SIZE, encode_bone_name, read_vmd,
)


def rewrite_bone_names(source: str | Path, output: str | Path,
                       names: dict[str, str]) -> Path:
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError("拒绝覆盖源 VMD")
    document = read_vmd(source)
    raw = bytearray(source.read_bytes())
    start = VMD_MAGIC_SIZE + (MODEL_NAME_SIZE_V2 if document.version == "0002"
                              else MODEL_NAME_SIZE_V1) + 4
    end = start + len(document.bone_frames) * BONE_FRAME_SIZE
    if end > len(raw):
        raise ValueError("VMD Bone 区段越界")
    for index, frame in enumerate(document.bone_frames):
        fixed = names.get(frame.name)
        if fixed is None or fixed == frame.name:
            continue
        offset = start + index * BONE_FRAME_SIZE
        raw[offset:offset + MAX_BONE_NAME_BYTES] = encode_bone_name(fixed).ljust(
            MAX_BONE_NAME_BYTES, b"\0")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(raw)
    rewritten = read_vmd(temporary)
    if len(rewritten.bone_frames) != len(document.bone_frames) or any(
            after.name != names.get(before.name, before.name)
            for before, after in zip(document.bone_frames, rewritten.bone_frames)):
        temporary.unlink(missing_ok=True)
        raise ValueError("VMD 改名后回读校验失败")
    temporary.replace(output)
    return output
