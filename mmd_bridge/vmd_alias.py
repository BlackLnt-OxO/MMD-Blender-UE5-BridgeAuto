"""Alias 产物：映射文件与 Alias 源 VMD（方案 §6 / §10.2 / §12）。

为什么需要 Alias 源 VMD
-----------------------
走 Alias 路线时，MMM 加载的是**别名 PMX**（骨骼叫 ``PB000247``）。
如果这时把**原名**的源 VMD 喂进去，MMM 里根本找不到那些骨骼，
动作会静默丢失。

所以源 VMD 里凡是命中别名映射的轨道，都必须先把名字换成别名，
再交给 MMM。

为什么用字节级替换
------------------
VMD 的骨骼帧是**定长 111 字节**（name 15 + frame 4 + pos 12 + rot 16 + interp 64），
骨骼段从偏移 54 开始（30 字节签名 + 20 字节模型名 + 4 字节计数）。

因此第 i 帧的名字就在 ``54 + i*111``。

⇒ 只改这 15 字节，**其余部分逐字节不动** —— 位移、四元数、插值、
以及 Morph / Camera / Light / Shadow / IK 各段全部原样保留（方案 §13）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .vmd_name_strategy import ALIAS_MAP_FILE_NAME, MAX_BONE_NAME_BYTES
from .vmd_reader import (
    BONE_FRAME_SIZE,
    MODEL_NAME_SIZE_V2,
    VMD_MAGIC_SIZE,
    VMDError,
    read_vmd,
)

__all__ = [
    "ALIAS_LIPSYNC_FILE_NAME",
    "ALIAS_VMD_FILE_NAME",
    "AliasArtifactError",
    "build_alias_vmd",
    "read_alias_map",
    "write_alias_map",
]

#: Alias 源 VMD 的默认文件名。
ALIAS_VMD_FILE_NAME = "source_alias.vmd"

#: Alias 表情 VMD 的默认文件名。
#:
#: 表情（morph）轨道本身不按骨骼名索引，但实测有些表情文件里**混有骨骼轨道**
#: （例如口型作者顺手写了头部/身体骨），漏转的话 MMM 里那部分会静默丢失。
ALIAS_LIPSYNC_FILE_NAME = "lipsync_alias.vmd"

#: 骨骼段起始偏移：签名 + 模型名 + 计数。
_BONE_SECTION_START = VMD_MAGIC_SIZE + MODEL_NAME_SIZE_V2 + 4


class AliasArtifactError(RuntimeError):
    """生成 Alias 产物失败。"""


# --------------------------------------------------------------------------
# 映射文件（方案 §6）
# --------------------------------------------------------------------------

def write_alias_map(
    job_folder: str | Path,
    *,
    model_name: str,
    index_to_alias: dict[int, str],
    original_names: dict[int, str],
) -> Path:
    """写出 ``vmd_alias_map.json``。

    **三个字段都要留**：``bone_index`` / ``original_name`` / ``alias``。
    Bone Index 是唯一不会因改名而失效的身份标识，字符串反查只是便利。
    """
    folder = Path(job_folder)
    folder.mkdir(parents=True, exist_ok=True)

    bones = []
    for index in sorted(index_to_alias):
        bones.append({
            "bone_index": index,
            "original_name": original_names.get(index, ""),
            "alias": index_to_alias[index],
        })

    payload = {
        "version": 1,
        "model": model_name,
        "bones": bones,
    }
    target = folder / ALIAS_MAP_FILE_NAME
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def read_alias_map(path: str | Path) -> dict:
    """读回映射文件。

    Returns:
        ``{"model": str, "alias_to_original": dict, "index_to_alias": dict,
        "original_to_alias": dict}``；文件不存在或损坏时返回空结构。
    """
    empty = {"model": "", "alias_to_original": {},
             "index_to_alias": {}, "original_to_alias": {}}
    target = Path(path)
    if not target.is_file():
        return empty
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict):
        return empty

    result = dict(empty)
    result["model"] = str(payload.get("model", ""))
    for entry in payload.get("bones") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry["bone_index"])
            alias = str(entry["alias"])
            original = str(entry["original_name"])
        except (KeyError, TypeError, ValueError):
            continue
        result["alias_to_original"][alias] = original
        result["index_to_alias"][index] = alias
        result["original_to_alias"][original] = alias
    return result


# --------------------------------------------------------------------------
# Alias 源 VMD（方案 §10.2）
# --------------------------------------------------------------------------

def build_alias_vmd(
    source_vmd: str | Path,
    output_path: str | Path,
    original_to_alias: dict[str, str],
) -> tuple[Path, dict]:
    """把源 VMD 里命中映射的骨骼轨道改名为别名。

    Args:
        source_vmd: 原始动作。
        output_path: 输出路径。
        original_to_alias: ``原骨骼名 → 别名``。

    Returns:
        ``(输出路径, 统计)``；统计含 ``renamed``（改了多少帧）、
        ``tracks``（改了多少条轨道）、``unmatched``（物理骨但源里没有的）。

    Raises:
        AliasArtifactError: 源文件读不了，或布局与预期不符。
    """
    source = Path(source_vmd)
    if not source.is_file():
        raise AliasArtifactError(f"源 VMD 不存在：{source}")

    try:
        document = read_vmd(source)
    except VMDError as exc:
        raise AliasArtifactError(f"源 VMD 解析失败：{exc}") from exc

    data = bytearray(source.read_bytes())

    # 布局自检 —— 偏移算错会静默改坏别人的数据，所以宁可先验证。
    expected_min = _BONE_SECTION_START + len(document.bone_frames) * BONE_FRAME_SIZE
    if len(data) < expected_min:
        raise AliasArtifactError(
            f"VMD 长度与骨骼帧数不符：{len(data)} < {expected_min}"
            f"（骨骼帧 {len(document.bone_frames)} 个）")

    # 方向自检 —— 传反了会变成"一帧都没改"的**静默空操作**，
    # 而下游 MMM 会因为找不到骨骼而丢掉整条动作。
    # 所以发现"键一个都不匹配、值却匹配得上"就直接报错。
    track_names = {frame.name for frame in document.bone_frames}
    if original_to_alias and not (set(original_to_alias) & track_names):
        flipped = set(original_to_alias.values()) & track_names
        if flipped:
            raise AliasArtifactError(
                f"original_to_alias 的方向传反了 —— "
                f"键一个都不在 VMD 里，但有 {len(flipped)} 个值匹配得上。"
                f"应当传 {{原骨骼名: 别名}}，而不是 {{别名: 原骨骼名}}")

    renamed = 0
    touched: set[str] = set()
    for position, frame in enumerate(document.bone_frames):
        alias = original_to_alias.get(frame.name)
        if alias is None:
            continue
        offset = _BONE_SECTION_START + position * BONE_FRAME_SIZE
        payload = alias.encode("cp932", errors="strict")
        if len(payload) > MAX_BONE_NAME_BYTES:      # pragma: no cover
            raise AliasArtifactError(
                f"别名 {alias!r} 超过 {MAX_BONE_NAME_BYTES} 字节 —— "
                f"别名生成器有 bug")
        data[offset:offset + MAX_BONE_NAME_BYTES] = payload.ljust(
            MAX_BONE_NAME_BYTES, b"\x00")
        renamed += 1
        touched.add(frame.name)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_bytes(bytes(data))
    if output.exists():
        output.unlink()
    temp.rename(output)

    return output, {
        "renamed_frames": renamed,
        "renamed_tracks": len(touched),
        "unmatched_tracks": sorted(set(original_to_alias) - touched),
        "bone_frames": len(document.bone_frames),
    }
