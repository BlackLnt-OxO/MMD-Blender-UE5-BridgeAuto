"""生成 Alias PMX —— 只替换目标骨骼的名称字段，其余**逐字节不变**。

为什么要字节级替换而不是"解析 → 重建"
--------------------------------------
方案 §9 要求：除名称外，``bone count / index / parent / IK / append / 权重 /
刚体索引 / joint / 表示枠`` 全部保持一致。最可靠的保证方式是
**根本不碰那些字节**。

PMX 的骨骼名是变长 UTF-16LE（带 4 字节长度前缀），改名字会改变文件长度，
后续所有偏移都会移动。所以做法是：

1. 读取时记下每个名称字段的字节范围（:class:`PMXBone.name_jp_range`）；
2. 生成新文件时只把那些范围换成新名字的字节；
3. 其余字节**原样搬运**。

这样结构一致性不是"验证出来的"，而是**构造上就不可能被破坏**。

只改名称字段
------------
方案 §7 明确禁止修改：bone index / parent index / deform order / IK /
append-inherit / vertex weights / rigid body bone index / joint /
display frame bone index / 任何物理参数。

方案 §8 补充：``name_en`` 也要一起改 —— 避免 MMM / 插件 / mmd_tools
某个环节意外选中英文名（实测 MMM 的 ``DisplayName`` 就会随界面语言变化）。
"""
from __future__ import annotations

import struct
from pathlib import Path

from .pmx_binary import PMXFormatError
from .pmx_reader import read_pmx

__all__ = [
    "ALIAS_MODEL_FILE_NAME",
    "AliasModelError",
    "build_alias_model",
]

#: 供 MMM 烘焙用的工作模型名（方案 §7）。
ALIAS_MODEL_FILE_NAME = "mmm_working_alias.pmx"


class AliasModelError(RuntimeError):
    """生成 Alias PMX 失败。"""


def _encode_text(value: str) -> bytes:
    """按 PMX 的文本字段格式编码：4 字节长度前缀 + UTF-16LE。"""
    payload = value.encode("utf-16-le")
    return struct.pack("<i", len(payload)) + payload


def build_alias_model(
    source_pmx: str | Path,
    output_path: str | Path,
    index_to_alias: dict[int, str],
) -> Path:
    """生成别名 PMX。

    Args:
        source_pmx: 源 PMX（通常是修复后的 ``ProcessedPMX.pmx``）。
        output_path: 输出路径。
        index_to_alias: ``bone_index → alias``，由
            :func:`mmd_bridge.vmd_name_strategy.analyze_vmd_name_strategy` 给出。

    Returns:
        输出路径。

    Raises:
        AliasModelError: 源文件读不了、骨骼索引越界、
            或替换范围重叠（后者意味着读取器的偏移记录有 bug）。
    """
    source = Path(source_pmx)
    if not source.is_file():
        raise AliasModelError(f"源 PMX 不存在：{source}")
    if not index_to_alias:
        raise AliasModelError("index_to_alias 为空 —— 无需生成 Alias PMX")

    model = read_pmx(source)
    data = source.read_bytes()

    by_index = {bone.index: bone for bone in model.bones}
    replacements: list[tuple[int, int, bytes]] = []

    for bone_index, alias in sorted(index_to_alias.items()):
        bone = by_index.get(bone_index)
        if bone is None:
            raise AliasModelError(
                f"bone_index={bone_index} 在 {source.name} 里不存在"
                f"（模型共 {len(model.bones)} 根骨）")
        if bone.name_jp_range is None or bone.name_en_range is None:
            raise AliasModelError(
                f"bone_index={bone_index} 没有记录名称字节范围 —— "
                f"读取器可能不是 read_pmx()")
        # 方案 §8：本地名与英文名都改，避免下游意外选中英文名。
        replacements.append((*bone.name_jp_range, _encode_text(alias)))
        replacements.append((*bone.name_en_range, _encode_text(alias)))

    replacements.sort(key=lambda item: item[0])

    # 范围不得重叠 —— 重叠说明偏移记录有 bug，宁可报错也不要写出坏文件。
    for (start, end, _), (next_start, _, _) in zip(replacements, replacements[1:]):
        if end > next_start:
            raise AliasModelError(
                f"名称字段范围重叠：({start}, {end}) 与 ({next_start}, ...) —— "
                f"读取器的偏移记录有误")

    # 拼接：未改动的字节原样搬运。
    pieces: list[bytes] = []
    cursor = 0
    for start, end, payload in replacements:
        if start < cursor:
            raise AliasModelError(f"替换范围 ({start}, {end}) 与前一段重叠")
        pieces.append(data[cursor:start])
        pieces.append(payload)
        cursor = end
    pieces.append(data[cursor:])

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_bytes(b"".join(pieces))
    if output.exists():
        output.unlink()
    temp.rename(output)
    return output


def verify_alias_structure(
    original_pmx: str | Path,
    alias_pmx: str | Path,
    index_to_alias: dict[int, str],
) -> list[str]:
    """验证别名 PMX 除名称外与源文件一致（方案 §9）。

    Returns:
        问题描述列表；**空列表表示通过**。

    因为 :func:`build_alias_model` 是字节级拼接，这里只需确认
    "只有预期的名称字段变了"。做法是重新解析两边，逐项比对结构字段。
    """
    problems: list[str] = []
    before = read_pmx(original_pmx)
    after = read_pmx(alias_pmx)

    if len(before.bones) != len(after.bones):
        problems.append(
            f"骨骼数不一致：{len(before.bones)} → {len(after.bones)}")
        return problems

    if len(before.rigid_bodies) != len(after.rigid_bodies):
        problems.append(
            f"刚体数不一致：{len(before.rigid_bodies)} → {len(after.rigid_bodies)}")

    for old, new in zip(before.bones, after.bones):
        if old.index != new.index:
            problems.append(f"骨骼 index 变了：{old.index} → {new.index}")
        if old.flags != new.flags:
            problems.append(f"骨骼 {old.index} 的 flags 变了")

        alias = index_to_alias.get(old.index)
        if alias is None:
            # 走 DIRECT 的骨 —— 两个名字都必须**原样不动**。
            if new.name_jp != old.name_jp:
                problems.append(
                    f"骨骼 {old.index} 不该改名，但 name_jp 变成 {new.name_jp!r}")
            if new.name_en != old.name_en:
                problems.append(
                    f"骨骼 {old.index} 不该改名，但 name_en 变成 {new.name_en!r}")
        else:
            # 走 ALIAS 的骨 —— 本地名与英文名都要等于别名（方案 §8）。
            if new.name_jp != alias:
                problems.append(
                    f"骨骼 {old.index} 的 name_jp 不是预期别名："
                    f"{new.name_jp!r} != {alias!r}")
            if new.name_en != alias:
                problems.append(
                    f"骨骼 {old.index} 的 name_en 不是预期别名："
                    f"{new.name_en!r} != {alias!r}")

    # 表示枠引用的是骨骼 index，不该因改名而变。
    if len(before.display_frames) != len(after.display_frames):
        problems.append("表示枠数量变了")

    return problems
