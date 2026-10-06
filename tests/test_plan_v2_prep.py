import json

import pytest

from mmd_bridge.plan_v2_prep import finalize_character_v2, prepare_character_v2
from mmd_bridge.pmx_analyzer import analyze_model
from mmd_bridge.pmx_reader import read_pmx
from mmd_bridge.vmd_reader import read_vmd
from tests.test_pmx_physics_displayframe import make_pmx
from tests.test_vmd_reader import _bone_frame, _morph_frame, make_vmd


def test_prepare_repairs_model_and_rewrites_motion_without_commit(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    model = source_dir / "原模型.pmx"
    motion = tmp_path / "source.vmd"
    lipsync = tmp_path / "lip.vmd"
    model.write_bytes(make_pmx(bones=[("センター", True), ("发型", True)],
                               rigid_bodies=[("髪", 1, 1)], display_frames=[("Root", [0])]))
    motion.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", 2)]))
    lipsync.write_bytes(make_vmd(morph_frames=[_morph_frame("笑い", 3, 0.5)]))
    job = tmp_path / "job"
    result = prepare_character_v2(model, motion, lipsync, job)
    assert result.requires_mmd_bake
    assert result.repaired_bone_count == 1
    assert analyze_model(read_pmx(result.model)).missing_physics_bone_count == 0
    assert read_vmd(result.motion).bone_frames[0].name == "センター"
    assert len(read_vmd(result.lipsync).morph_frames) == 1
    assert result.name_map.is_file()
    assert not (job / "effective.json").exists()
    assert model.read_bytes() != result.model.read_bytes()


def test_finalize_commits_only_after_full_bake_and_restores_lipsync(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    model = source / "model.pmx"
    model.write_bytes(make_pmx(bones=[("センター", True), ("髪", True)],
                               rigid_bodies=[("髪", 1, 1)], display_frames=[("Root", [0])]))
    motion = tmp_path / "motion.vmd"
    motion.write_bytes(make_vmd(bone_frames=[
        *(_bone_frame("センター", frame) for frame in (0, 1, 2)),
        _bone_frame("別モデル骨", 1, pos=(0, 1, 0)),
    ]))
    lipsync = tmp_path / "lip.vmd"
    lipsync.write_bytes(make_vmd(morph_frames=[_morph_frame("笑い", 1, 0.5)]))
    job = tmp_path / "job"
    prepared = prepare_character_v2(model, motion, lipsync, job)
    baked = tmp_path / "baked.vmd"
    baked.write_bytes(make_vmd(bone_frames=[
        *(_bone_frame("センター", frame) for frame in (0, 1, 2)),
        *(_bone_frame("髪", frame) for frame in (0, 1, 2)),
    ]))
    marker = finalize_character_v2(prepared, baked, job)
    effective = json.loads(marker.read_text(encoding="utf-8"))
    assert effective["effective_motion"] == str(job / "final.vmd")
    assert effective["effective_lipsync"] == ""
    assert {(frame.name, frame.frame) for frame in read_vmd(job / "final.vmd").morph_frames} == {("笑い", 1)}
    assert json.loads((job / "validation.json").read_text(encoding="utf-8"))["ok"]


def test_finalize_rejects_short_bake_without_commit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    model = source / "model.pmx"
    model.write_bytes(make_pmx(bones=[("センター", True), ("髪", True)],
                               rigid_bodies=[("髪", 1, 1)], display_frames=[("Root", [0])]))
    motion = tmp_path / "motion.vmd"
    motion.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", frame)
                                            for frame in (0, 1, 2)]))
    job = tmp_path / "job"
    prepared = prepare_character_v2(model, motion, None, job)
    baked = tmp_path / "short.vmd"
    baked.write_bytes(make_vmd(bone_frames=[
        *(_bone_frame("センター", frame) for frame in (0, 1)),
        *(_bone_frame("髪", frame) for frame in (0, 1)),
    ]))
    with pytest.raises(ValueError, match="VMD_BAKE_RANGE_INCOMPLETE"):
        finalize_character_v2(prepared, baked, job)
    assert not (job / "effective.json").exists()


def test_finalize_rejects_name_map_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    model = source / "model.pmx"
    model.write_bytes(make_pmx(bones=[("センター", True), ("髪", True)],
                               rigid_bodies=[("髪", 1, 1)], display_frames=[("Root", [0])]))
    motion = tmp_path / "motion.vmd"
    motion.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", frame)
                                            for frame in (0, 1)]))
    job = tmp_path / "job"
    prepared = prepare_character_v2(model, motion, None, job)
    entries = json.loads(prepared.name_map.read_text(encoding="utf-8"))
    entries[1]["fixed"] = "wrong"
    prepared.name_map.write_text(json.dumps(entries), encoding="utf-8")
    baked = tmp_path / "baked.vmd"
    baked.write_bytes(make_vmd(bone_frames=[
        *(_bone_frame("センター", frame) for frame in (0, 1)),
        *(_bone_frame("髪", frame) for frame in (0, 1)),
    ]))
    with pytest.raises(ValueError, match="BONE_NAME_MAP_MISMATCH"):
        finalize_character_v2(prepared, baked, job)
    assert not (job / "effective.json").exists()
