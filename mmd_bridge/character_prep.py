"""Stage 2.4 — character model preprocessing that yields the *effective* model.

This is the entry point of the new character main flow.  It runs **before** MMM
and decides three things the rest of the pipeline must not re-decide:

1. whether the model needs a display-frame repair, and if so writes
   ``ProcessedPMX.pmx``;
2. which bone set the downstream assertions must verify
   (``repair_expected_bones`` for the repair, ``bake_probe_bones`` for the bake);
3. whether MMM can be involved at all — a model with **no physics bones** is
   bypassed entirely, and a model whose physics bones **cannot be represented in
   VMD** is refused before MMM ever starts.

Design notes
------------
**The bypass is not a failure.**  A model with ``physics_bone_count == 0`` has
nothing to bake, so MMM is skipped and the original motion flows straight to
Blender.  Reporting this as ``MMM_BAKE_PROBE_UNAVAILABLE`` would send the reader
looking for a verification problem that does not exist.

**The bypass criterion is strictly ``physics_bone_count``.**  A model with
physics bones that are *all* missing from every display frame
(``displayed_physics_bone_count == 0``) is the *primary* repair target, not a
bypass case.  Confusing the two would skip the very models this project exists
to fix.

**Representability is a format limit, not a verification gap.**  VMD stores bone
names in 15-byte Shift-JIS slots.  A physics bone whose name exceeds that (or
whose truncation collides with another bone's) cannot have its bake result
written to VMD at all — so the flow must stop before MMM, with a diagnosis that
says so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .effective_inputs import (
    EFFECTIVE_FILE_NAME,
    read_effective_inputs,
    resolve_character_inputs,
    write_effective_inputs as write_effective,
)
from .pmx_alias import (
    ALIAS_MODEL_FILE_NAME,
    AliasModelError,
    build_alias_model,
    verify_alias_structure,
)
from .vmd_alias import (
    ALIAS_LIPSYNC_FILE_NAME,
    ALIAS_VMD_FILE_NAME,
    AliasArtifactError,
    build_alias_vmd,
    write_alias_map,
)
from .vmd_name_strategy import analyze_vmd_name_strategy
from .pmx_analyzer import PhysicsDisplayFrameAnalysis, analyze_model
from .pmx_physics_displayframe import (
    PhysicsDisplayFrameRepairResult,
    assert_expectation_not_empty,
    default_output_path,
    repair,
)
from .pmx_reader import read_pmx
from .vmd_reader import bone_name_issue
from .vmd_validator import (
    ValidationIssue,
)

__all__ = [
    "EFFECTIVE_FILE_NAME",
    "MAX_BAKE_PROBES",
    "NO_PHYSICS_BONES",
    "PHYSICS_BONES_NOT_VMD_REPRESENTABLE",
    "VMD_ALIAS_GENERATION_FAILED",
    "PMX_REPAIR_VERIFY_FAILED",
    "CharacterPrepError",
    "CharacterPrepResult",
    "prepare_character_model",
    "read_effective_inputs",
    "resolve_character_inputs",
    "select_bake_probes",
    "write_effective_inputs",
]

#: 下游（Blender）读取实际输入的唯一来源（方案 §4 / §37）。
#: 真正的读写实现在 :mod:`mmd_bridge.effective_inputs`，此处仅再导出。
EFFECTIVE_FILE_NAME = EFFECTIVE_FILE_NAME

#: Refusal codes (方案 §8 / §39).
PHYSICS_BONES_NOT_VMD_REPRESENTABLE = "PHYSICS_BONES_NOT_VMD_REPRESENTABLE"
#: 名称不可表示不再是错误 —— 只有"生成别名产物失败"才是。
VMD_ALIAS_GENERATION_FAILED = "VMD_ALIAS_GENERATION_FAILED"
PMX_REPAIR_VERIFY_FAILED = "PMX_REPAIR_VERIFY_FAILED"

#: Recorded in the manifest when MMM is skipped because there is nothing to bake.
NO_PHYSICS_BONES = "NO_PHYSICS_BONES"

#: Upper bound on probe bones.  The evidence value flattens out long before
#: this, and every probe adds work to validator layer D.
MAX_BAKE_PROBES = 64


class CharacterPrepError(RuntimeError):
    """The character flow cannot proceed.  Carries a stable ``code``."""

    def __init__(self, code: str, message: str,
                 issues: list[ValidationIssue] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.issues = issues or []


@dataclass
class CharacterPrepResult:
    """Everything the MMM stage and Blender need to know about the model."""

    source_model: str = ""
    effective_model: str = ""
    #: Motion that Blender should consume.  The MMM stage overwrites this with
    #: ``baked.vmd``; the bypass path leaves it as the user's source VMD.
    effective_motion: str = ""

    physics_bone_count: int = 0
    #: Displayed / missing counts **as found in the source model** — together they
    #: explain why a repair was needed.  After a verified repair,
    #: ``missing_after_repair`` is 0 and every physics bone is displayed, so the
    #: effective model's figures are derivable and are not duplicated here.
    displayed_physics_bone_count: int = 0
    missing_physics_bone_count: int = 0
    #: Missing count re-measured on the repaired file; ``None`` when no repair ran.
    missing_after_repair: int | None = None

    #: Bones PhysicsBakeFix added — proves the repair reached MMM.
    repair_expected_bones: list[str] = field(default_factory=list)
    #: Bones that prove MMM's physics bake actually produced keyframes.
    bake_probe_bones: list[str] = field(default_factory=list)

    #: True when MMM must be skipped entirely.
    mmm_skipped: bool = False
    mmm_skipped_reason: str = ""

    #: True when the model's physics bones could not be written to VMD as-is,
    #: so an Alias working model was generated (方案《VMD 骨骼名限制自动 Alias》).
    uses_vmd_alias: bool = False
    #: ``mmm_working_alias.pmx`` —— MMM 要加载的工作模型（仅走 Alias 时有值）。
    alias_model: str = ""
    #: ``source_alias.vmd`` —— 源动作的别名版，MMM 要加载它而不是原动作。
    alias_motion: str = ""
    #: 表情 VMD 的别名版；``Settings.lipsync`` 有值时才有意义。
    alias_lipsync: str = ""
    #: ``vmd_alias_map.json`` —— 别名 ↔ 原名 的映射。
    alias_map_path: str = ""
    #: ``bone_index → alias``；内部追踪优先用它，而不是字符串反查。
    alias_index_map: dict[int, str] = field(default_factory=dict)
    #: ``alias → 原骨骼名``。
    alias_name_map: dict[str, str] = field(default_factory=dict)
    #: 名称路由的统计（direct / aliased / 各类原因计数）。
    alias_summary: dict[str, Any] = field(default_factory=dict)

    repair_manifest: dict[str, Any] = field(default_factory=dict)
    analysis_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def repaired(self) -> bool:
        return bool(self.repair_manifest.get("modified"))

    @property
    def expected_bones(self) -> list[str]:
        """The union the VMD validator's layers C/D must be given.

        Never empty for a non-bypassed model — an empty expectation would make
        ``∅ ⊆ actual`` pass unconditionally, which is the defect this project
        already had to fix once.
        """
        return sorted(set(self.repair_expected_bones) | set(self.bake_probe_bones))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_model": self.source_model,
            "effective_model": self.effective_model,
            "effective_motion": self.effective_motion,
            "physics_bone_count": self.physics_bone_count,
            "displayed_physics_bone_count": self.displayed_physics_bone_count,
            "missing_physics_bone_count": self.missing_physics_bone_count,
            "missing_after_repair": self.missing_after_repair,
            "repair_expected_bones": self.repair_expected_bones,
            "bake_probe_bones": self.bake_probe_bones,
            "mmm_skipped": self.mmm_skipped,
            "mmm_skipped_reason": self.mmm_skipped_reason,
            "repaired": self.repaired,
            "repair_manifest": self.repair_manifest,
            "analysis_summary": self.analysis_summary,
        }

    def summary(self) -> str:
        lines = ["[Character Prep]",
                 f"  source    : {self.source_model}",
                 f"  effective : {self.effective_model}"]
        if self.mmm_skipped:
            lines.append(f"  MMM       : 跳过（{self.mmm_skipped_reason}）")
            lines.append("  说明：该模型没有物理骨，无需烘焙；直接使用原始动作进入 Blender。")
        else:
            lines.append(f"  physics   : {self.physics_bone_count} "
                         f"(已显示 {self.displayed_physics_bone_count})")
            if self.repaired:
                lines.append(f"  repair    : 需要（源缺失 {self.missing_physics_bone_count}"
                             f" → 修复后 {self.missing_after_repair}）")
            else:
                lines.append("  repair    : 不需要（源模型缺失 0）")
            lines.append(f"  expected  : 修复骨 {len(self.repair_expected_bones)}"
                         f" + 探针骨 {len(self.bake_probe_bones)}"
                         f" = 去重后 {len(self.expected_bones)}")
        return "\n".join(lines)


def select_bake_probes(
    analysis: PhysicsDisplayFrameAnalysis,
    *,
    limit: int = MAX_BAKE_PROBES,
    index_to_alias: dict[int, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Pick bones that can prove the physics bake happened.

    Candidates are ``PhysicsBones ∩ DisplayedBones``; they are filtered for VMD
    representability **first**, and only the surviving set is sampled.  Sampling
    before filtering would waste slots on names that can never be written.

    ``index_to_alias`` 让走 Alias 的骨骼也能入选 —— 判定用的是它们的
    **最终名字**（别名），而不是原名。返回的也是最终名字，
    因为断言要对着烘焙后的 VMD 做，而那里存的是别名。

    Returns ``(probes, unusable_candidate_names)``.
    """
    aliases = index_to_alias or {}
    candidates = [bone for bone in analysis.physics_bones if bone.is_displayed]
    usable: list[str] = []
    unusable: list[str] = []
    for bone in sorted(candidates, key=lambda b: b.bone_index):
        final = aliases.get(bone.bone_index, bone.bone_name)
        if bone_name_issue(final) is None:
            usable.append(final)
        else:
            unusable.append(final)
    return usable[:limit], unusable


def _representability_targets(
    analysis: PhysicsDisplayFrameAnalysis,
    repair_expected_bones: list[str],
) -> list[tuple[int, str]]:
    """Every physics bone that must survive a VMD round-trip.

    Covers both rules of 方案 §7 without branching:

    * ``repair_expected_bones`` — the bones the repair added;
    * every *displayed* physics bone — which, for a ``missing == 0`` model, is
      the whole physics bone set, and is the case that actually breaks
      (a model can be repair-free and still unrepresentable).

    **返回 ``(bone_index, name)`` 而不是单纯的名字。**
    Alias 必须按 Bone Index 分配 —— 它是唯一不会因改名失效的身份标识；
    只拿名字的话，同名骨骼会互相覆盖（方案 §6）。
    """
    by_name = {bone.bone_name: bone.bone_index for bone in analysis.physics_bones}
    pairs = {(bone.bone_index, bone.bone_name)
             for bone in analysis.physics_bones if bone.is_displayed}
    for name in repair_expected_bones:
        index = by_name.get(name)
        if index is not None:
            pairs.add((index, name))
    return sorted(pairs)


def prepare_character_model(
    pmx_path: str | Path,
    *,
    source_vmd: str | Path | None = None,
    source_lipsync: str | Path | None = None,
    output_dir: str | Path | None = None,
    write_manifest: bool = True,
    max_bake_probes: int = MAX_BAKE_PROBES,
) -> tuple[CharacterPrepResult, str]:
    """Analyse, optionally repair, and gate a character PMX.

    Args:
        pmx_path: the user-selected model.
        source_vmd: the user-selected motion; only recorded, never modified.
        output_dir: where ``ProcessedPMX.pmx`` and ``repair_manifest.json`` go.
        write_manifest: set False for a dry run that touches no files.
        max_bake_probes: cap on ``bake_probe_bones``.

    Returns:
        ``(result, human_readable_text)``.

    Raises:
        CharacterPrepError: with code ``PHYSICS_BONES_NOT_VMD_REPRESENTABLE``
            when a physics bone cannot be stored in VMD, or
            ``PMX_REPAIR_VERIFY_FAILED`` when a repair did not take effect.
    """
    source = Path(pmx_path)
    result = CharacterPrepResult(
        source_model=str(source),
        effective_model=str(source),
        effective_motion=str(source_vmd) if source_vmd else "",
    )

    # -- analyse ----------------------------------------------------------
    model = read_pmx(source)
    analysis = analyze_model(model)

    result.physics_bone_count = analysis.physics_bone_count
    result.displayed_physics_bone_count = analysis.displayed_physics_bone_count
    result.missing_physics_bone_count = analysis.missing_physics_bone_count
    result.analysis_summary = {
        "bone_count": len(model.bones),
        "rigid_body_count": len(model.rigid_bodies),
        "physics_bone_count": analysis.physics_bone_count,
        "displayed_physics_bone_count": analysis.displayed_physics_bone_count,
        "missing_physics_bone_count": analysis.missing_physics_bone_count,
    }

    # -- zero-physics bypass ----------------------------------------------
    # Nothing to bake.  Deliberately NOT reported as a probe failure: the flow
    # is healthy, it simply has no physics stage.
    if analysis.physics_bone_count == 0:
        result.mmm_skipped = True
        result.mmm_skipped_reason = NO_PHYSICS_BONES
        result.repair_manifest = {
            "source_model": str(source),
            "processed_model": "",
            "target_display_frame": "",
            "frame_created": False,
            "modified": False,
            "physics_bone_count": 0,
            "displayed_physics_bone_count": 0,
            "missing_physics_bone_count": 0,
            "added_bone_indices": [],
            "added_bone_names": [],
            "bake_probe_bone_names": [],
            "mmm_skipped": True,
            "mmm_skipped_reason": NO_PHYSICS_BONES,
            "source_vmd": str(source_vmd) if source_vmd else "",
        }
        if write_manifest and output_dir:
            _write_manifest(result, output_dir)
        return result, result.summary()

    # -- repair -----------------------------------------------------------
    repair_result: PhysicsDisplayFrameRepairResult | None = None
    if analysis.needs_repair():
        target = Path(output_dir) / "ProcessedPMX.pmx" if output_dir \
            else default_output_path(source)
        repair_result, _ = repair(source, output_path=target,
                                  model=model, analysis=analysis)
        if repair_result.errors:
            raise CharacterPrepError(
                PMX_REPAIR_VERIFY_FAILED,
                f"PMX 修复失败：{repair_result.errors[0]}")
        assert_expectation_not_empty(repair_result.missing_physics_bone_count,
                                     repair_result.added_bone_names)
        result.effective_model = repair_result.output_path
        result.repair_expected_bones = list(repair_result.added_bone_names)

        # Verify the repair actually took effect on the written file.
        repaired_model = read_pmx(repair_result.output_path)
        repaired_analysis = analyze_model(repaired_model)
        if repaired_analysis.missing_physics_bone_count != 0:
            raise CharacterPrepError(
                PMX_REPAIR_VERIFY_FAILED,
                f"修复后仍有 {repaired_analysis.missing_physics_bone_count} "
                f"根物理骨未进入显示枠（{repair_result.output_path}）")
        model, analysis = repaired_model, repaired_analysis
        result.missing_after_repair = analysis.missing_physics_bone_count

    # -- VMD 名称路由（方案《VMD 骨骼名限制自动 Alias》§18）----------------
    #
    # 以前这里是**拒绝器**：名字写不进 VMD 就整个模型不处理。
    # 但 VMD 的 15 字节 / CP932 限制只是**中间传输格式**的限制，
    # 不该升级成整个流程的兼容性限制。现在改成**路由器**：
    #   DIRECT          → 原名可直接写，走原流程
    #   ALIAS_REQUIRED  → 生成别名工作模型 + 别名源动作，MMM 用它们烘焙
    #
    # 实测：芭芭拉.pmx 这类用中文骨骼名的模型以前 100% 被拒，
    # 现在 34 根骨走别名、其余 126 根走原名。
    targets = _representability_targets(analysis, result.repair_expected_bones)
    name_result = analyze_vmd_name_strategy(targets)
    result.alias_summary = name_result.to_dict()

    if name_result.uses_alias:
        job_dir = Path(output_dir) if output_dir else Path(default_output_path(source)).parent
        source_model = Path(result.effective_model or source)
        try:
            alias_model = build_alias_model(
                source_model, job_dir / ALIAS_MODEL_FILE_NAME,
                name_result.index_to_alias)
            problems = verify_alias_structure(
                source_model, alias_model, name_result.index_to_alias)
            if problems:
                raise AliasModelError(
                    "别名 PMX 结构验证失败：" + "；".join(problems[:3]))
        except (AliasModelError, OSError) as exc:
            raise CharacterPrepError(
                VMD_ALIAS_GENERATION_FAILED,
                f"生成 VMD 别名工作模型失败：{exc}") from exc

        index_to_name = {bone.index: bone.name_jp for bone in model.bones}
        result.alias_map_path = str(write_alias_map(
            job_dir, model_name=Path(result.effective_model or source).name,
            index_to_alias=name_result.index_to_alias,
            original_names=index_to_name))
        result.alias_model = str(alias_model)
        result.alias_index_map = dict(name_result.index_to_alias)
        result.alias_name_map = dict(name_result.alias_map)
        result.uses_vmd_alias = True

        # 源动作也要改成别名版 —— 否则 MMM 在别名模型上找不到那些骨骼，
        # 动作会**静默丢失**（方案 §10.2）。
        original_to_alias = {original: alias
                             for alias, original in name_result.alias_map.items()}
        if source_vmd is not None and Path(source_vmd).is_file():
            try:
                alias_motion, _ = build_alias_vmd(
                    source_vmd, job_dir / ALIAS_VMD_FILE_NAME, original_to_alias)
                result.alias_motion = str(alias_motion)
            except (AliasArtifactError, OSError) as exc:
                raise CharacterPrepError(
                    VMD_ALIAS_GENERATION_FAILED,
                    f"生成别名源动作失败：{exc}") from exc

        # 表情 VMD 同样要转。morph 轨道不按骨骼名索引，但实测有些表情文件里
        # **混有骨骼轨道**（作者顺手写了头部/身体骨），漏转的话那部分会静默丢失。
        if source_lipsync is not None and Path(source_lipsync).is_file():
            try:
                alias_lipsync, _ = build_alias_vmd(
                    source_lipsync, job_dir / ALIAS_LIPSYNC_FILE_NAME,
                    original_to_alias)
                result.alias_lipsync = str(alias_lipsync)
            except (AliasArtifactError, OSError) as exc:
                raise CharacterPrepError(
                    VMD_ALIAS_GENERATION_FAILED,
                    f"生成别名表情动作失败：{exc}") from exc

    # -- probes -----------------------------------------------------------
    probes, unusable = select_bake_probes(
        analysis, limit=max_bake_probes,
        index_to_alias=name_result.index_to_alias)
    result.bake_probe_bones = probes
    if unusable and not probes:
        # Representability already passed for every *displayed* bone, so this
        # can only happen when nothing is displayed at all — which the repair
        # above would have fixed.  Kept as a guard, not a normal path.
        raise CharacterPrepError(
            "MMM_BAKE_PROBE_UNAVAILABLE",
            f"{len(unusable)} 根已显示物理骨的名称无法在 VMD 中表示，"
            "无法构造 Bake 验证探针")

    # -- manifest ---------------------------------------------------------
    if repair_result is not None:
        manifest = dict(repair_result.to_dict())
    else:
        manifest = {
            "source_model": str(source),
            "processed_model": "",
            "target_display_frame": "PhysicsBake",
            "frame_created": False,
            "modified": False,
            "physics_bone_count": analysis.physics_bone_count,
            "displayed_physics_bone_count": analysis.displayed_physics_bone_count,
            "missing_physics_bone_count": 0,
            "added_bone_indices": [],
            "added_bone_names": [],
        }
    manifest["bake_probe_bone_names"] = probes
    manifest["mmm_skipped"] = False
    manifest["mmm_skipped_reason"] = ""
    # 记录源动作：Validator 的层 B（原 Motion 保留）需要它，
    # 而 repair manifest 本身只描述 PMX，不知道 VMD 从哪来。
    manifest["source_vmd"] = str(source_vmd) if source_vmd else ""
    result.repair_manifest = manifest

    if write_manifest and output_dir:
        _write_manifest(result, output_dir)

    return result, result.summary()


def _write_manifest(result: CharacterPrepResult, output_dir: str | Path) -> Path:
    target = Path(output_dir) / "repair_manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.repair_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return target


def write_effective_inputs(job_folder: str | Path,
                           result: CharacterPrepResult, *,
                           effective_motion: str | Path | None = None) -> Path:
    """把本结果的 ``effective_model`` / ``effective_motion`` 固化到任务目录。

    ``effective_motion`` 由 MMM 阶段在导出 ``baked.vmd`` 后填入；
    不传则沿用结果里的值（零物理骨旁路时是源 VMD）。

    实际写文件的是 :mod:`mmd_bridge.effective_inputs` —— 那份契约
    会被 Blender 进程导入，因此刻意不依赖本模块的 PMX 解析链。
    """
    return write_effective(
        job_folder,
        source_model=result.source_model,
        effective_model=result.effective_model,
        source_vmd=result.effective_motion,
        effective_motion=str(effective_motion or result.effective_motion),
        repaired=result.repaired,
        mmm_skipped=result.mmm_skipped,
        mmm_skipped_reason=result.mmm_skipped_reason,
        expected_bone_count=len(result.expected_bones),
    )
