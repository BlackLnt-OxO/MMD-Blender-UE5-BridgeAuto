"""Public entry point for PMX physics display-frame preflight and repair.

Background
----------
Some PMX models drive skirt / hair / breast / accessory bones with Bullet rigid
bodies, yet those bones are never listed in any PMX display frame (表示枠).
MMD and MMM still simulate them, but MikuMikuMoving cannot expose them in its
timeline — so 物理焼き込み has nowhere to register keyframes, and the physics
animation is lost on the way to VMD → mmd_tools → Blender → UE.

This module finds exactly those bones and adds them to a dedicated
``PhysicsBake`` display frame, changing nothing else in the model.

The detection rule is structural, never name-based (SPEC §3):
``RelatedBone(RigidBody.PhysicsMode ∈ {1,2})`` minus every bone referenced by
any display frame.

Empty-set hazard
----------------
Downstream stages (the MMM physics-bake plugin) assert that every bone this
module planned to add really did reach the model::

    expected_added_bones ⊆ actual_bones      # MMM_DISPLAY_FRAME_REPAIR_EFFECTIVE

That assertion is only meaningful when ``expected_added_bones`` is non-empty,
because ``∅ ⊆ anything`` is *always true*.  An empty expectation therefore
does not fail the check — it silently turns the check into ``if True``.

``analyze()`` used to leave ``added_bone_names`` empty even when it had already
computed the plan, which is exactly that hazard: a 349-bone repair would be
reported as "349 confirmed" while verifying nothing.  Two defences now exist:

1. ``analyze()`` populates ``added_bone_names`` / ``added_bone_indices``;
2. :func:`build_repair_manifest` raises :class:`RepairManifestError` when the
   analysis found missing bones but the expectation list is empty.

Usage::

    from mmd_bridge import pmx_physics_displayframe as pbf

    analysis, report = pbf.analyze("character.pmx")
    if analysis.missing_physics_bone_count:
        result, report = pbf.repair("character.pmx")  # -> *_PhysicsBakeFixed.pmx
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pmx_analyzer import (
    PhysicsBoneInfo,
    PhysicsDisplayFrameAnalysis,
    analyze_model,
    format_analysis,
)
from .pmx_model import PHYSICS_BAKE_FRAME_NAME, PMXModel
from .pmx_patcher import default_output_path, write_repaired_pmx
from .pmx_planner import (
    PhysicsDisplayFrameRepairPlan,
    apply_plan_to_frames,
    build_repair_plan,
    format_plan,
)
from .pmx_reader import PMXFormatError, read_pmx

__all__ = [
    "PMXFormatError",
    "PhysicsBoneInfo",
    "PhysicsDisplayFrameAnalysis",
    "PhysicsDisplayFrameRepairPlan",
    "PhysicsDisplayFrameRepairResult",
    "REPAIR_MANIFEST_EMPTY",
    "RepairManifestError",
    "analyze",
    "analyze_model",
    "build_repair_manifest",
    "default_output_path",
    "format_analysis",
    "format_plan",
    "read_pmx",
    "repair",
]

#: Error code emitted when a repair plan would claim success with no bones to verify.
REPAIR_MANIFEST_EMPTY = "REPAIR_MANIFEST_EMPTY"


class RepairManifestError(RuntimeError):
    """A repair manifest cannot be trusted because its expectation list is empty.

    Raised instead of letting ``∅ ⊆ actual`` pass for a real verification — see
    the module docstring's "Empty-set hazard" section.
    """


def assert_expectation_not_empty(
    missing_count: int, added_bone_names: list[str]
) -> None:
    """Refuse to hand out a vacuous expectation list.

    ``missing_count > 0`` means the analysis found bones that must be added; if
    ``added_bone_names`` is nevertheless empty, the plan and the analysis
    disagree and any downstream ``⊆`` assertion would pass unconditionally.
    """
    if missing_count > 0 and not added_bone_names:
        raise RepairManifestError(
            f"{REPAIR_MANIFEST_EMPTY}: 检测到 {missing_count} 根待修复物理骨，"
            "但修复计划未给出任何骨骼名称；"
            "此时下游的 expected ⊆ actual 断言会因空集恒真而静默通过。")



@dataclass
class PhysicsDisplayFrameRepairResult:
    """Unified result structure (SPEC §21 / 任务书 §21).

    Field names follow the project's snake_case convention; the semantic
    contract of the spec's PascalCase list is preserved one-to-one.
    """

    input_path: str = ""
    output_path: str = ""
    model_name: str = ""

    bone_count: int = 0
    rigid_body_count: int = 0
    physics_bone_count: int = 0
    displayed_physics_bone_count: int = 0
    missing_physics_bone_count: int = 0

    added_bone_indices: list[int] = field(default_factory=list)
    added_bone_names: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    target_frame: str = PHYSICS_BAKE_FRAME_NAME
    frame_created: bool = False
    modified: bool = False
    success: bool = False
    mode: str = "analyze"
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_path": self.input_path,
            "output_path": self.output_path,
            "model_name": self.model_name,
            "bone_count": self.bone_count,
            "rigid_body_count": self.rigid_body_count,
            "physics_bone_count": self.physics_bone_count,
            "displayed_physics_bone_count": self.displayed_physics_bone_count,
            "missing_physics_bone_count": self.missing_physics_bone_count,
            "added_bone_indices": self.added_bone_indices,
            "added_bone_names": self.added_bone_names,
            "warnings": self.warnings,
            "errors": self.errors,
            "target_frame": self.target_frame,
            "frame_created": self.frame_created,
            "modified": self.modified,
            "success": self.success,
            "mode": self.mode,
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        if self.errors:
            return f"失败：{self.errors[0]}"
        if not self.modified:
            return "物理骨显示枠检查通过"
        frame = "已创建" if self.frame_created else "已复用"
        return f"检测到 {self.missing_physics_bone_count} 根物理骨未加入显示枠；{frame} PhysicsBake"


def _fill_common(result: PhysicsDisplayFrameRepairResult, model: PMXModel,
                 analysis: PhysicsDisplayFrameAnalysis, mode: str) -> None:
    result.input_path = str(model.path)
    result.model_name = Path(model.path).name
    result.bone_count = model.bone_count
    result.rigid_body_count = model.rigid_body_count
    result.physics_bone_count = analysis.physics_bone_count
    result.displayed_physics_bone_count = analysis.displayed_physics_bone_count
    result.missing_physics_bone_count = analysis.missing_physics_bone_count
    result.warnings = list(analysis.warnings)
    result.mode = mode
    result.timestamp = time.time()


def analyze(pmx_path: str | Path) -> tuple[PhysicsDisplayFrameRepairResult, str]:
    """Analyze only — never writes a model file (SPEC §18 / 任务书 §20).

    The returned result carries the repair plan's bone list, so callers do not
    have to re-derive it (and cannot accidentally assert against an empty set).
    """
    result = PhysicsDisplayFrameRepairResult()
    try:
        model = read_pmx(pmx_path)
    except (PMXFormatError, OSError) as exc:
        result.errors.append(str(exc))
        result.timestamp = time.time()
        return result, f"读取失败：{exc}"

    analysis = analyze_model(model)
    _fill_common(result, model, analysis, "analyze")
    result.success = True
    result.target_frame = PHYSICS_BAKE_FRAME_NAME
    text = format_analysis(analysis, model)
    if analysis.missing_physics_bone_count:
        plan = build_repair_plan(model, analysis)
        # Surface the plan in the result: an empty expectation list downstream
        # would make ``expected ⊆ actual`` pass unconditionally.
        result.target_frame = plan.target_frame
        result.frame_created = plan.create_frame
        result.added_bone_indices = list(plan.bones_to_add)
        result.added_bone_names = list(plan.bone_names)
        assert_expectation_not_empty(
            analysis.missing_physics_bone_count, result.added_bone_names)
        text = text + "\n\n" + format_plan(plan)
    return result, text


def repair(
    pmx_path: str | Path,
    output_path: str | Path | None = None,
    *,
    model: PMXModel | None = None,
    analysis: PhysicsDisplayFrameAnalysis | None = None,
) -> tuple[PhysicsDisplayFrameRepairResult, str]:
    """Detect and repair, writing a new PMX alongside the original.

    The source model is never overwritten.  When nothing is missing the file is
    not written at all and ``modified`` stays False.
    """
    result = PhysicsDisplayFrameRepairResult()
    try:
        model = model or read_pmx(pmx_path)
    except (PMXFormatError, OSError) as exc:
        result.errors.append(str(exc))
        result.timestamp = time.time()
        return result, f"读取失败：{exc}"

    analysis = analysis or analyze_model(model)
    _fill_common(result, model, analysis, "repair")

    if not analysis.needs_repair():
        result.success = True
        result.modified = False
        result.output_path = ""
        return result, format_analysis(analysis, model)

    plan = build_repair_plan(model, analysis)
    result.target_frame = plan.target_frame
    result.frame_created = plan.create_frame
    result.added_bone_indices = list(plan.bones_to_add)
    result.added_bone_names = list(plan.bone_names)
    assert_expectation_not_empty(
        analysis.missing_physics_bone_count, result.added_bone_names)

    try:
        frames = apply_plan_to_frames(model, plan)
        target = output_path or default_output_path(pmx_path)
        written = write_repaired_pmx(pmx_path, model, frames, target)
    except (PMXFormatError, OSError, ValueError) as exc:
        result.errors.append(str(exc))
        result.timestamp = time.time()
        return result, f"修复失败：{exc}"

    result.output_path = str(written)
    result.modified = True
    result.success = True

    lines = [format_analysis(analysis, model), "", "[PMX PhysicsBake Repair]", ""]
    lines.append(f"Target Frame: {plan.target_frame} "
                 f"({'created' if plan.create_frame else 'reused'})")
    for index, name in zip(result.added_bone_indices, result.added_bone_names):
        lines.append(f"  [{index}] {name}")
    lines.append("")
    lines.append(f"Output: {written}")
    lines.append("原始 PMX 未被修改。")
    return result, "\n".join(lines)


def build_repair_manifest(
    pmx_path: str | Path,
    *,
    output_path: str | Path | None = None,
    result: PhysicsDisplayFrameRepairResult | None = None,
) -> dict[str, Any]:
    """Build the hand-off manifest consumed by the MMM physics-bake stage (方案 §9).

    The manifest is the *only* channel through which the MMM plugin learns which
    bones it must find in the ``PhysicsBake`` display frame, so an empty
    ``added_bone_names`` here would disable that assertion entirely.  This
    function therefore refuses to emit a vacuous manifest.

    Raises:
        RepairManifestError: analysis found missing bones but produced no names.
    """
    if result is None:
        result, _ = analyze(pmx_path)

    if result.errors:
        raise RepairManifestError(
            f"无法生成修复清单：{result.errors[0]}")

    assert_expectation_not_empty(
        result.missing_physics_bone_count, result.added_bone_names)

    return {
        "source_model": str(result.input_path or pmx_path),
        "processed_model": str(output_path or result.output_path or ""),
        "target_display_frame": result.target_frame,
        "frame_created": result.frame_created,
        "modified": result.modified,
        "missing_physics_bone_count": result.missing_physics_bone_count,
        "physics_bone_count": result.physics_bone_count,
        "added_bone_indices": list(result.added_bone_indices),
        "added_bone_names": list(result.added_bone_names),
    }
