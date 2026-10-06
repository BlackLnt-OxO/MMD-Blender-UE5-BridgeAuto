"""Pure analysis: which simulated physics bones are unreachable in MMM?

Implements the core formula from SPEC §21 / 任务书 §40:

    PhysicsBones        = RelatedBone(RigidBody.PhysicsMode in {1, 2})
    DisplayedBones      = every DisplayFrame item with Type == 0
    MissingPhysicsBones = PhysicsBones - DisplayedBones

No filesystem access, no names used for classification.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pmx_model import (
    BAKEABLE_PHYSICS_MODES,
    FRAME_ITEM_BONE,
    PMXModel,
)

WARNING_INVALID_BONE_INDEX = "WARNING_INVALID_BONE_INDEX"
WARNING_INVALID_DISPLAY_FRAME_ITEM = "WARNING_INVALID_DISPLAY_FRAME_ITEM"
WARNING_NEGATIVE_RIGID_BODY_BONE = "WARNING_NEGATIVE_RIGID_BODY_BONE"
WARNING_BONE_NOT_VISIBLE = "WARNING_BONE_NOT_VISIBLE"


@dataclass
class PhysicsBoneInfo:
    """One simulated bone, with every rigid body that drives it."""

    bone_index: int
    bone_name: str
    rigid_body_indices: list[int] = field(default_factory=list)
    rigid_body_names: list[str] = field(default_factory=list)
    physics_modes: list[int] = field(default_factory=list)
    is_displayed: bool = False
    is_visible: bool = True

    @property
    def primary_rigid_body_name(self) -> str:
        return self.rigid_body_names[0] if self.rigid_body_names else ""

    @property
    def mode_label(self) -> str:
        return "/".join(str(mode) for mode in sorted(set(self.physics_modes)))


@dataclass
class PhysicsDisplayFrameAnalysis:
    physics_bones: list[PhysicsBoneInfo] = field(default_factory=list)
    displayed_bones: set[int] = field(default_factory=set)
    missing_bones: list[PhysicsBoneInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def physics_bone_count(self) -> int:
        return len(self.physics_bones)

    @property
    def displayed_physics_bone_count(self) -> int:
        return sum(1 for bone in self.physics_bones if bone.is_displayed)

    @property
    def missing_physics_bone_count(self) -> int:
        return len(self.missing_bones)

    @property
    def invisible_missing_count(self) -> int:
        return sum(1 for bone in self.missing_bones if not bone.is_visible)

    def needs_repair(self) -> bool:
        return bool(self.missing_bones)


def collect_displayed_bones(model: PMXModel) -> tuple[set[int], list[str]]:
    """Every bone index reachable from *any* display frame, plus warnings."""
    displayed: set[int] = set()
    warnings: list[str] = []
    bone_count = len(model.bones)
    for frame in model.display_frames:
        for item in frame.items:
            if item.item_type != FRAME_ITEM_BONE:
                continue
            if item.index < 0 or item.index >= bone_count:
                warnings.append(
                    f"{WARNING_INVALID_DISPLAY_FRAME_ITEM}: 显示枠「{frame.name_jp}」"
                    f"引用了非法 Bone Index {item.index}（Bone 数 {bone_count}）")
                continue
            displayed.add(item.index)
    return displayed, warnings


def collect_physics_bones(model: PMXModel) -> tuple[dict[int, PhysicsBoneInfo], list[str]]:
    """Mode 1/2 rigid bodies grouped by their related bone."""
    grouped: dict[int, PhysicsBoneInfo] = {}
    warnings: list[str] = []
    bone_count = len(model.bones)
    for body in model.rigid_bodies:
        if body.physics_mode not in BAKEABLE_PHYSICS_MODES:
            continue
        related = body.related_bone_index
        if related < 0:
            warnings.append(
                f"{WARNING_NEGATIVE_RIGID_BODY_BONE}: 刚体「{body.name_jp}」"
                f"(Mode {body.physics_mode}) 的 RelatedBone = {related}，已忽略")
            continue
        if related >= bone_count:
            warnings.append(
                f"{WARNING_INVALID_BONE_INDEX}: 刚体「{body.name_jp}」(Mode {body.physics_mode}) "
                f"引用了越界 Bone Index {related}（Bone 数 {bone_count}），禁止修复")
            continue
        info = grouped.get(related)
        if info is None:
            bone = model.bones[related]
            info = PhysicsBoneInfo(
                bone_index=related,
                bone_name=bone.name_jp or bone.name_en,
                is_visible=bone.visible,
            )
            grouped[related] = info
        info.rigid_body_indices.append(body.index)
        info.rigid_body_names.append(body.name_jp or body.name_en)
        info.physics_modes.append(body.physics_mode)
    return grouped, warnings


def analyze_model(model: PMXModel) -> PhysicsDisplayFrameAnalysis:
    """Full preflight analysis. Read-only and side-effect free."""
    physics, warnings = collect_physics_bones(model)
    displayed, display_warnings = collect_displayed_bones(model)
    warnings.extend(display_warnings)

    analysis = PhysicsDisplayFrameAnalysis(
        displayed_bones=displayed,
        warnings=warnings,
    )
    for index in sorted(physics):
        info = physics[index]
        info.is_displayed = index in displayed
        analysis.physics_bones.append(info)
        if not info.is_displayed:
            analysis.missing_bones.append(info)
            if not info.is_visible:
                # SPEC §10 / 任务书 §14: report only, never auto-fix the flag.
                warnings.append(
                    f"{WARNING_BONE_NOT_VISIBLE}: Bone #{index}「{info.bone_name}」"
                    f"(Mode {info.mode_label}) 缺少显示枠且 Visible=False；仅警告，不修改 Flag")
    return analysis


def format_analysis(analysis: PhysicsDisplayFrameAnalysis, model: PMXModel) -> str:
    """Human-readable report mirroring SPEC §13 / 任务书 §27."""
    lines = [
        "[PMX PhysicsBake Preflight]",
        "",
        f"Model: {model.path}",
        f"Bones: {model.bone_count}",
        f"RigidBodies: {model.rigid_body_count}",
        "",
        f"Dynamic Physics Bones: {analysis.physics_bone_count}",
        f"Already in Display Frames: {analysis.displayed_physics_bone_count}",
        f"Missing Display Frame: {analysis.missing_physics_bone_count}",
    ]
    if analysis.invisible_missing_count:
        lines.append(f"Visible=False: {analysis.invisible_missing_count}")
    if analysis.missing_bones:
        lines.append("")
        for bone in analysis.missing_bones:
            marker = "[FIX]" if bone.is_visible else "[WARNING]"
            lines.append(
                f"{marker} Bone #{bone.bone_index} 「{bone.bone_name}」 "
                f"RigidBody={bone.primary_rigid_body_name} "
                f"Mode={bone.mode_label} DisplayFrame=Missing "
                f"Visible={bone.is_visible}")
    else:
        lines.append("")
        lines.append("No repair required. "
                     "All dynamic physics bones are already registered in Display Frames.")
    if analysis.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in analysis.warnings:
            lines.append(f"  - {warning}")
    return "\n".join(lines)
