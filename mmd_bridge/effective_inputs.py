"""``effective_model`` / ``effective_motion`` —— 下游唯一的输入契约。

方案 §4 / §37：

.. code-block:: text

    需要修复 → effective_model = ProcessedPMX.pmx
    无需修复 → effective_model = 原 PMX
    经过 MMM → effective_motion = baked.vmd
    零物理骨 → effective_motion = 源 VMD

规则本身很简单，但**必须只有一个地方实现**。在此之前，"到底用原 PMX 还是
Fixed PMX" 的判断散落在各个模块里，任何一处漏改都会让某条分支悄悄用错输入。

本模块**刻意不依赖任何 PMX/VMD 代码**：它会被 Blender 进程导入，
把整个 PMX 解析链拖进去既浪费又增加耦合。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "EFFECTIVE_FILE_NAME",
    "read_effective_inputs",
    "resolve_character_inputs",
    "write_effective_inputs",
]

#: 任务目录下的文件名。
EFFECTIVE_FILE_NAME = "effective.json"


def write_effective_inputs(
    job_folder: str | Path,
    *,
    source_model: str = "",
    effective_model: str = "",
    source_vmd: str = "",
    effective_motion: str = "",
    repaired: bool = False,
    mmm_skipped: bool = False,
    mmm_skipped_reason: str = "",
    expected_bone_count: int = 0,
    #: 可选的表情 VMD（MMM 里在主动作之后导入；Blender 里单独导入）。
    effective_lipsync: str = "",
    # --- VMD 名称别名（方案《VMD 骨骼名限制自动 Alias》§13）-------------
    uses_vmd_alias: bool = False,
    alias_map: str = "",
    alias_model: str = "",
) -> Path:
    """把实际输入固化到 ``<job>/effective.json``。

    写的是**事实**而不是意图：下游只需照做，不需要重新推导。

    ``uses_vmd_alias`` 为真时，``effective_model`` 指向**别名工作模型**、
    ``effective_motion`` 指向**别名动作**，两者必须成对使用 ——
    混用原名模型会匹配不上，动作会静默丢失。
    """
    folder = Path(job_folder)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_model": str(source_model),
        "source_vmd": str(source_vmd),
        "effective_model": str(effective_model or source_model),
        "effective_motion": str(effective_motion or source_vmd),
        "repaired": bool(repaired),
        "mmm_skipped": bool(mmm_skipped),
        "mmm_skipped_reason": str(mmm_skipped_reason),
        "expected_bone_count": int(expected_bone_count),
        "effective_lipsync": str(effective_lipsync),
        "uses_vmd_alias": bool(uses_vmd_alias),
        "alias_map": str(alias_map),
        "alias_model": str(alias_model),
    }
    target = folder / EFFECTIVE_FILE_NAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, target)
    return target


def read_effective_inputs(job_folder: str | Path) -> dict[str, Any]:
    """读取 ``<job>/effective.json``；不存在或损坏时返回空 dict。"""
    target = Path(job_folder) / EFFECTIVE_FILE_NAME
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_character_inputs(cfg: dict[str, Any],
                             job_folder: str | Path) -> tuple[str, str, str]:
    """解析角色通路真正要用的 ``(model, motion, note)``。

    ``note`` 为空表示沿用原始输入；否则说明被替换成了什么，供日志与 manifest 记录。

    没有 ``effective.json`` 时**完全退回旧行为** —— 因此对尚未接入 MMM 的
    历史任务（以及尚未跑过 MMM 阶段的新任务）没有任何影响。
    """
    model = str(cfg.get("model", "") or "")
    motion = str(cfg.get("motion", "") or "")

    effective = read_effective_inputs(job_folder)
    if not effective:
        return model, motion, ""

    new_model = str(effective.get("effective_model", "") or "")
    new_motion = str(effective.get("effective_motion", "") or "")

    notes = []
    if new_model and new_model != model:
        model = new_model
        notes.append("model=effective_model")
    if new_motion and new_motion != motion:
        motion = new_motion
        notes.append("motion=effective_motion")
    if effective.get("mmm_skipped"):
        notes.append(f"mmm_skipped={effective.get('mmm_skipped_reason', '')}")
    return model, motion, "; ".join(notes)
