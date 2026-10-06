import pytest

from mmd_bridge.bone_name_fix import (
    VMD_DUPLICATE_SOURCE_BONE_AMBIGUOUS, BoneNameError, fix_bone_names,
)
from mmd_bridge.pmx_model import PMXBone, PMXModel
from mmd_bridge.pmx_patcher import write_processed_pmx
from mmd_bridge.pmx_reader import read_pmx
from mmd_bridge.pmx_analyzer import analyze_model
from mmd_bridge.pmx_planner import build_repair_plan, apply_plan_to_frames
from tests.test_pmx_physics_displayframe import make_pmx


def test_bone_name_fix_is_unique_and_representable():
    model = PMXModel(bones=[PMXBone(0, "センター"), PMXBone(1, "发型"),
                            PMXBone(2, "发型"), PMXBone(3, "very long bone name")])
    fixed = fix_bone_names(model)
    names = [entry.fixed for entry in fixed]
    assert names[0] == "センター"
    assert len(set(names)) == len(names)
    assert all(0 < len(name.encode("cp932")) <= 15 for name in names)


def test_duplicate_source_track_is_refused():
    model = PMXModel(bones=[PMXBone(0, "骨A"), PMXBone(1, "骨A")])
    with pytest.raises(BoneNameError) as error:
        fix_bone_names(model, {"骨A"})
    assert error.value.code == VMD_DUPLICATE_SOURCE_BONE_AMBIGUOUS


def test_pmx_writer_combines_name_and_display_frame_patch(tmp_path):
    source = tmp_path / "source.pmx"
    source.write_bytes(make_pmx(bones=[("センター", True), ("发型", True)],
                                rigid_bodies=[("髪", 1, 1)], display_frames=[("Root", [0])]))
    model = read_pmx(source)
    mapping = fix_bone_names(model)
    plan = build_repair_plan(model, analyze_model(model))
    frames = apply_plan_to_frames(model, plan)
    target = write_processed_pmx(source, model, frames,
                                 {item.bone_index: item.fixed for item in mapping},
                                 tmp_path / "model.pmx")
    repaired = read_pmx(target)
    assert repaired.bones[1].name_jp == mapping[1].fixed
    assert analyze_model(repaired).missing_physics_bone_count == 0
    assert source.read_bytes() != target.read_bytes()
