"""Complete VMD writer for bone, morph, camera, light, shadow and IK frames."""
from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

from .vmd_reader import (
    BONE_INTERPOLATION_SIZE, CAMERA_INTERPOLATION_SIZE, MAX_BONE_NAME_BYTES,
    MODEL_NAME_SIZE_V1, MODEL_NAME_SIZE_V2, VMD_MAGIC_SIZE, VMD_MAGIC_V1,
    VMD_MAGIC_V2, VMDDocument, decode_name_field, read_vmd,
)


def _slot(name: str, raw: bytes, width: int) -> bytes:
    if len(raw) == width and decode_name_field(raw).name == name:
        return raw
    encoded = name.encode("cp932")
    if len(encoded) > width:
        raise ValueError(f"VMD 名称超出 {width} 字节: {name!r}")
    return encoded.ljust(width, b"\0")


def serialize_vmd(document: VMDDocument) -> bytes:
    if document.version not in ("0001", "0002"):
        raise ValueError(f"不支持的 VMD 版本: {document.version}")
    if not 2 <= document.sections_present <= 6:
        raise ValueError("VMD section 数必须在 2..6")
    magic = document.raw_magic or (VMD_MAGIC_V2 if document.version == "0002" else VMD_MAGIC_V1).ljust(
        VMD_MAGIC_SIZE, b"\0")
    width = MODEL_NAME_SIZE_V2 if document.version == "0002" else MODEL_NAME_SIZE_V1
    sections: list[list[bytes]] = []
    bone = []
    for frame in document.bone_frames:
        if len(frame.interpolation) != BONE_INTERPOLATION_SIZE:
            raise ValueError("Bone 插值块长度不是 64 字节")
        bone.append(_slot(frame.name, frame.raw_name, MAX_BONE_NAME_BYTES) +
                    struct.pack("<I3f4f", frame.frame, *frame.position, *frame.rotation) +
                    frame.interpolation)
    sections.append(bone)
    sections.append([_slot(frame.name, frame.raw_name, MAX_BONE_NAME_BYTES) +
                     struct.pack("<If", frame.frame, frame.weight)
                     for frame in document.morph_frames])
    camera = []
    for frame in document.camera_frames:
        if len(frame.interpolation) != CAMERA_INTERPOLATION_SIZE:
            raise ValueError("Camera 插值块长度不是 24 字节")
        camera.append(struct.pack("<If3f3f", frame.frame, frame.distance,
                                  *frame.position, *frame.rotation) +
                      frame.interpolation + struct.pack("<IB", frame.fov, frame.perspective))
    sections.append(camera)
    sections.append([struct.pack("<I3f3f", frame.frame, *frame.color, *frame.position)
                     for frame in document.light_frames])
    sections.append([struct.pack("<IBf", frame.frame, frame.mode, frame.distance)
                     for frame in document.shadow_frames])
    ik = []
    for frame in document.ik_frames:
        payload = [struct.pack("<IBI", frame.frame, int(frame.visible), len(frame.targets))]
        for target in frame.targets:
            if len(target.reserved) != 2:
                raise ValueError("IK 保留字段长度不是 2 字节")
            payload.append(_slot(target.name, target.raw_name, MAX_BONE_NAME_BYTES) +
                           struct.pack("<I", target.bone_index) + target.reserved)
        ik.append(b"".join(payload))
    sections.append(ik)
    if any(sections[document.sections_present:]):
        raise ValueError("VMD 存在未声明的可选区段数据")
    result = bytearray(magic + _slot(document.model_name, document.raw_model_name, width))
    for frames in sections[:document.sections_present]:
        result += struct.pack("<I", len(frames))
        result += b"".join(frames)
    result += document.raw_trailing
    return bytes(result)


def write_vmd(document: VMDDocument, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(serialize_vmd(document))
    temporary.replace(path)
    return path


def merge_vmds(paths, output_path, *, offsets=None):
    """Later nonempty tracks replace entire earlier tracks, never absent ones.

    Bone and morph tracks are keyed independently by name. Preserve the winning
    track's raw interpolation/name bytes, including explicit zero-valued keys.
    """
    paths = [Path(path).resolve() for path in paths]
    output_path = Path(output_path).resolve()
    if not paths or output_path in paths:
        raise ValueError('合并输出必须独立于输入 VMD')
    offsets = list(offsets) if offsets is not None else [0] * len(paths)
    if len(offsets) != len(paths) or any(type(offset) is not int for offset in offsets):
        raise ValueError('每个 VMD 必须有一个整数帧偏移')
    documents = [read_vmd(path) for path in paths]
    names = ('bone_frames', 'morph_frames', 'camera_frames', 'light_frames', 'shadow_frames', 'ik_frames')
    merged = {name: {} for name in names}
    replaced = {name: [] for name in names}
    ik_tracks = {}
    visibility = {}
    ik_documents = []
    deduplicated = []
    for source_index, (document, offset) in enumerate(zip(documents, offsets)):
        if document.raw_trailing:
            raise ValueError(f'VMD 含无法解释的尾部数据，不能安全合并: {document.path}')
        for name in names:
            seen = {}
            tracks = {}
            for frame in getattr(document, name):
                number = frame.frame + offset
                if not 0 <= number <= 0xffffffff:
                    raise ValueError(f'VMD 偏移后帧号超出范围: {document.path}, {number}')
                key = (frame.name, number) if name in names[:2] else number
                value = replace(frame, frame=number)
                if key in seen:
                    if seen[key] != value:
                        raise ValueError(f'VMD 文件内有数值冲突的重复关键帧: {document.path}, {name}, {key}')
                    deduplicated.append({'source_index': source_index, 'section': name,
                                         'track': getattr(frame, 'name', name), 'frame': number})
                    continue
                seen[key] = value
                track = frame.name if name in names[:2] else name
                tracks.setdefault(track, []).append(value)
            for track, frames in tracks.items():
                if track in merged[name]:
                    replaced[name].append(track)
                merged[name][track] = frames
            if name == 'ik_frames' and tracks:
                frames = tracks[name]
                ik_documents.append(frames)
                visibility = {frame.frame: frame.visible for frame in frames}
                targets = {}
                for frame in frames:
                    for target in frame.targets:
                        keys = targets.setdefault(target.name, {})
                        if frame.frame in keys:
                            raise ValueError(f'VMD 文件内有重复 IK 关键帧: {document.path}, {target.name}')
                        keys[frame.frame] = target
                ik_tracks.update(targets)
    if len(ik_documents) > 1:
        # Visibility and each named IK switch are separate tracks. Carry their
        # state over union frame times so unrelated switches remain effective.
        from .vmd_reader import VMDIKFrame
        numbers = sorted(set(visibility).union(*(set(keys) for keys in ik_tracks.values())))
        visible, state, frames = True, {}, []
        for number in numbers:
            visible = visibility.get(number, visible)
            for track, keys in ik_tracks.items():
                if number in keys:
                    state[track] = keys[number]
            frames.append(VMDIKFrame(number, visible, tuple(state.values())))
        merged['ik_frames'] = {'ik_frames': frames}
    result = replace(documents[0], sections_present=max(doc.sections_present for doc in documents),
                     **{name: sorted((frame for frames in tracks.values() for frame in frames),
                                     key=lambda f: (f.frame, getattr(f, 'name', '')))
                        for name, tracks in merged.items()})
    write_vmd(result, output_path)
    verified = read_vmd(output_path)
    if any(getattr(verified, name) != getattr(result, name) for name in names):
        raise ValueError('合并 VMD 回读后关键帧不一致')
    return {'version': 1, 'policy': 'later_track_wins',
            'counts': {name: len(getattr(verified, name)) for name in names},
            'replaced_tracks': replaced, 'deduplicated_keys': deduplicated}


def merge_missing_morphs(baked_path: str | Path, source_path: str | Path,
                         lipsync_path: str | Path | None,
                         output_path: str | Path) -> Path:
    """Restore missing morph keys; lipsync wins over main motion at equal keys."""
    inputs = [baked_path, source_path, *([lipsync_path] if lipsync_path else [])]
    if Path(output_path).resolve() in {Path(path).resolve() for path in inputs}:
        raise ValueError("合并输出不能覆盖输入 VMD")
    baked = read_vmd(baked_path)
    source_frames = read_vmd(source_path).morph_frames
    lip_frames = read_vmd(lipsync_path).morph_frames if lipsync_path else []
    for label, frames in (("baked", baked.morph_frames), ("source", source_frames),
                          ("lipsync", lip_frames)):
        if len({(frame.name, frame.frame) for frame in frames}) != len(frames):
            raise ValueError(f"{label} VMD 同一 Morph 同一帧有重复关键帧")
    expected = {(frame.name, frame.frame): frame for frame in source_frames}
    if lipsync_path:
        expected.update({(frame.name, frame.frame): frame for frame in lip_frames})
    existing = {(frame.name, frame.frame): frame for frame in baked.morph_frames}
    existing.update(expected)
    result = replace(baked, morph_frames=list(existing.values()))
    output = write_vmd(result, output_path)
    actual = {(frame.name, frame.frame): frame.weight
              for frame in read_vmd(output).morph_frames}
    if not {key: frame.weight for key, frame in expected.items()}.items() <= actual.items():
        raise ValueError("Morph 轨道合并后回读失败")
    return output
