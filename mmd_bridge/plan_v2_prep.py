"""File-only preparation for the MMD Mode 2 bake path."""
from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .bone_name_fix import fix_bone_names
from .effective_inputs import write_effective_inputs
from .pmx_analyzer import analyze_model
from .pmx_patcher import write_processed_pmx
from .pmx_planner import apply_plan_to_frames, build_repair_plan
from .pmx_reader import read_pmx
from .vmd_codec import rewrite_bone_names
from .vmd_reader import read_vmd
from .vmd_validator import validate_baked_vmd, write_validation_json
from .vmd_writer import merge_missing_morphs


@dataclass(frozen=True)
class PreparedCharacterV2:
    source_model: Path
    source_motion: Path
    model: Path
    motion: Path
    lipsync: Path | None
    name_map: Path
    physics_bone_count: int
    repaired_bone_count: int
    renamed_bone_count: int
    frame_range: tuple[int, int]
    expected_bones: tuple[str, ...]
    vertex_weight_counts: dict[str, int]

    @property
    def requires_mmd_bake(self) -> bool:
        return self.physics_bone_count > 0


def prepare_character_v2(source_pmx: str | Path, source_vmd: str | Path,
                         source_lipsync: str | Path | None,
                         work_dir: str | Path) -> PreparedCharacterV2:
    source_pmx, source_vmd = Path(source_pmx).resolve(), Path(source_vmd).resolve()
    source_lipsync = Path(source_lipsync).resolve() if source_lipsync else None
    work_dir = Path(work_dir).resolve()
    if work_dir.is_relative_to(source_pmx.parent):
        raise ValueError("工作目录不能位于源模型目录内部")
    if (work_dir / "effective.json").exists():
        raise ValueError("任务已提交 effective.json；请使用新工作目录")
    input_dir = work_dir / "input"
    if input_dir.exists():
        raise ValueError("任务输入目录已存在；请使用新工作目录")

    model = read_pmx(source_pmx)
    motion = read_vmd(source_vmd)
    lipsync = read_vmd(source_lipsync) if source_lipsync else None
    source_tracks = set(motion.bone_tracks) | (set(lipsync.bone_tracks) if lipsync else set())
    mappings = fix_bone_names(model, source_tracks)
    by_index = {entry.bone_index: entry.fixed for entry in mappings}
    counts = Counter(entry.original for entry in mappings)
    rename = {entry.original: entry.fixed for entry in mappings
              if counts[entry.original] == 1 and entry.fixed != entry.original}

    analysis = analyze_model(model)
    plan = build_repair_plan(model, analysis)
    frames = apply_plan_to_frames(model, plan)

    # ponytail: copy the source folder to retain relative textures; copy only
    # referenced paths when the PMX reader exposes them.
    shutil.copytree(source_pmx.parent, input_dir, dirs_exist_ok=True)
    processed_model = write_processed_pmx(source_pmx, model, frames, by_index,
                                          input_dir / "model.pmx")
    verified = read_pmx(processed_model)
    if analyze_model(verified).missing_physics_bone_count:
        raise ValueError("Processed PMX 回读后仍有物理骨缺少显示枠")
    if any(verified.bones[entry.bone_index].name_jp != entry.fixed for entry in mappings):
        raise ValueError("Processed PMX 回读后骨骼名称不一致")

    processed_motion = rewrite_bone_names(source_vmd, input_dir / "motion.vmd", rename)
    processed_lipsync = (rewrite_bone_names(source_lipsync, input_dir / "lipsync.vmd", rename)
                         if source_lipsync else None)
    name_map = input_dir / "bone_name_map.json"
    name_map.write_text(json.dumps([entry.__dict__ for entry in mappings],
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    expected_indices = dict.fromkeys(info.bone_index for info in analysis.physics_bones)
    return PreparedCharacterV2(source_pmx, source_vmd, processed_model,
                               processed_motion, processed_lipsync, name_map,
                               analysis.physics_bone_count, len(plan.bones_to_add),
                               sum(entry.fixed != entry.original for entry in mappings),
                               motion.bone_frame_range,
                               tuple(by_index[index] for index in expected_indices),
                               model.vertex_weight_counts)


def finalize_character_v2(prepared: PreparedCharacterV2,
                          baked_vmd: str | Path | None,
                          job_dir: str | Path) -> Path:
    """Validate the full MMD result, then atomically commit effective.json."""
    job_dir = Path(job_dir).resolve()
    if (job_dir / "effective.json").exists():
        raise ValueError("任务已有 effective.json，拒绝重复提交")
    if prepared.requires_mmd_bake:
        if baked_vmd is None:
            raise ValueError("模型含物理骨，缺少 MMD 烘焙输出")
        baked_vmd = Path(baked_vmd).resolve()
        final_vmd = merge_missing_morphs(baked_vmd, prepared.motion,
                                         prepared.lipsync, job_dir / "final.vmd")
        report, _ = validate_baked_vmd(
            final_vmd, source_path=prepared.motion,
            expected_bone_names=prepared.expected_bones,
            bake_range=prepared.frame_range, model_path=prepared.model,
            bone_name_map=prepared.name_map)
        write_validation_json(report, job_dir / "validation.json")
        if not report.ok:
            raise ValueError(f"烘焙结果校验失败: {[issue.code for issue in report.errors]}")
    else:
        # No physics to bake: the rewritten source is the final animation.
        read_vmd(prepared.motion)
        final_vmd = prepared.motion
    return write_effective_inputs(
        job_dir, source_model=str(prepared.source_model),
        source_vmd=str(prepared.source_motion), effective_model=str(prepared.model),
        effective_motion=str(final_vmd), repaired=bool(prepared.repaired_bone_count or
                                                      prepared.renamed_bone_count),
        mmm_skipped=not prepared.requires_mmd_bake,
        mmm_skipped_reason="zero_physics_bones" if not prepared.requires_mmd_bake else "",
        expected_bone_count=len(prepared.expected_bones),
        effective_lipsync=str(prepared.lipsync or "") if not prepared.requires_mmd_bake else "")
