"""Pure planning: turn an analysis into an explicit, reviewable repair plan.

Kept separate from the patcher so the decision ("which bones go into which
frame") can be unit-tested without touching bytes, and so the GUI can show the
plan for confirmation before anything is written.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pmx_analyzer import PhysicsDisplayFrameAnalysis
from .pmx_model import (
    FRAME_ITEM_BONE,
    PHYSICS_BAKE_FRAME_NAME,
    PMXDisplayFrame,
    PMXDisplayItem,
    PMXModel,
)


@dataclass
class PhysicsDisplayFrameRepairPlan:
    target_frame: str = PHYSICS_BAKE_FRAME_NAME
    create_frame: bool = False
    bones_to_add: list[int] = field(default_factory=list)
    bone_names: list[str] = field(default_factory=list)

    @property
    def bones_to_add_count(self) -> int:
        return len(self.bones_to_add)


def find_or_create_target_frame(model: PMXModel) -> tuple[PMXDisplayFrame, bool]:
    """Reuse an existing PhysicsBake frame, otherwise create one.

    SPEC §8 / 任务书 §11: never create PhysicsBake_2.  Only a genuinely
    malformed model (a PhysicsBake frame that cannot hold bones) falls back to a
    new frame.
    """
    existing = model.frame_by_name(PHYSICS_BAKE_FRAME_NAME)
    if existing is not None:
        return existing, False
    frame = PMXDisplayFrame(
        name_jp=PHYSICS_BAKE_FRAME_NAME,
        name_en=PHYSICS_BAKE_FRAME_NAME,
        special_flag=0,
        items=[],
    )
    return frame, True


def build_repair_plan(model: PMXModel, analysis: PhysicsDisplayFrameAnalysis) -> PhysicsDisplayFrameRepairPlan:
    """Decide the minimal write: only bones absent from *every* display frame."""
    frame, created = find_or_create_target_frame(model)
    already_in_frame = set(frame.bone_indices())

    plan = PhysicsDisplayFrameRepairPlan(create_frame=created)
    for bone in analysis.missing_bones:
        # Defensive: a bone already present in the target frame is never re-added,
        # even if a caller hands us a stale analysis (SPEC §8 "禁止添加重复 Bone").
        if bone.bone_index in already_in_frame:
            continue
        plan.bones_to_add.append(bone.bone_index)
        plan.bone_names.append(bone.bone_name)

    if not plan.bones_to_add:
        plan.create_frame = False
    return plan


def apply_plan_to_frames(
    model: PMXModel,
    plan: PhysicsDisplayFrameRepairPlan,
) -> list[PMXDisplayFrame]:
    """Return the new display-frame list with the plan applied.

    Original frames are reused by reference and only ever appended to, so no
    author entry is moved, reordered or deleted (SPEC §9 / 任务书 §13).
    """
    frames = list(model.display_frames)
    if not plan.bones_to_add:
        return frames

    frame, created = find_or_create_target_frame(model)
    if created:
        frames.append(frame)

    existing = set(frame.bone_indices())
    changed = False
    for index in plan.bones_to_add:
        if index in existing:
            continue
        frame.items.append(PMXDisplayItem(item_type=FRAME_ITEM_BONE, index=index))
        existing.add(index)
        changed = True
    if changed:
        # The frame no longer matches its original bytes, so it must be
        # re-encoded instead of copied verbatim (see pmx_patcher).
        invalidate_raw_range(frame)
    return frames


def invalidate_raw_range(frame: PMXDisplayFrame) -> None:
    """Mark a frame as modified so the patcher re-encodes it."""
    frame.raw_start = -1
    frame.raw_end = -1


def format_plan(plan: PhysicsDisplayFrameRepairPlan) -> str:
    if not plan.bones_to_add:
        return ("[PMX PhysicsBake]\n\nNo repair required.\n"
                "All dynamic physics bones are already registered in Display Frames.")
    lines = [
        "[PMX PhysicsBake Plan]",
        "",
        f"Target Frame: {plan.target_frame} ({'新建' if plan.create_frame else '复用现有'})",
        f"Bones To Add: {plan.bones_to_add_count}",
        "",
    ]
    for index, name in zip(plan.bones_to_add, plan.bone_names):
        lines.append(f"  [{index}] {name}")
    return "\n".join(lines)
