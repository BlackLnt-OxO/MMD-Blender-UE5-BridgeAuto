"""Tests for the ``effective_model`` / ``effective_motion`` contract (方案 §4 / §37).

This file is the single source of truth for "which PMX and which VMD does the
downstream actually consume".  The cases that matter are the ones where the
answer *changes* — a repair, a bake, or a bypass — plus the fallback that keeps
jobs which never ran the MMM stage working exactly as before.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmd_bridge.effective_inputs import (
    EFFECTIVE_FILE_NAME,
    read_effective_inputs,
    resolve_character_inputs,
    write_effective_inputs,
)


# --------------------------------------------------------------------------
# write / read
# --------------------------------------------------------------------------

def test_write_then_read_round_trip(tmp_path):
    write_effective_inputs(
        tmp_path,
        source_model="D:/src.pmx",
        effective_model="D:/ProcessedPMX.pmx",
        source_vmd="D:/src.vmd",
        effective_motion="D:/baked.vmd",
        repaired=True,
        expected_bone_count=349,
    )
    payload = read_effective_inputs(tmp_path)
    assert payload["effective_model"] == "D:/ProcessedPMX.pmx"
    assert payload["effective_motion"] == "D:/baked.vmd"
    assert payload["source_model"] == "D:/src.pmx"
    assert payload["source_vmd"] == "D:/src.vmd"
    assert payload["repaired"] is True
    assert payload["expected_bone_count"] == 349


def test_write_creates_job_folder(tmp_path):
    target = write_effective_inputs(tmp_path / "deep" / "job",
                                    source_model="a.pmx")
    assert target.is_file()
    assert target.name == EFFECTIVE_FILE_NAME


def test_effective_model_falls_back_to_source(tmp_path):
    """没传 effective_model 时应等于源模型，而不是空字符串。"""
    write_effective_inputs(tmp_path, source_model="D:/src.pmx",
                           effective_model="", source_vmd="D:/src.vmd")
    payload = read_effective_inputs(tmp_path)
    assert payload["effective_model"] == "D:/src.pmx"
    assert payload["effective_motion"] == "D:/src.vmd"


def test_read_returns_empty_dict_when_absent(tmp_path):
    assert read_effective_inputs(tmp_path) == {}


def test_read_returns_empty_dict_on_corrupt_file(tmp_path):
    (tmp_path / EFFECTIVE_FILE_NAME).write_text("{not json", encoding="utf-8")
    assert read_effective_inputs(tmp_path) == {}


def test_read_returns_empty_dict_when_not_an_object(tmp_path):
    (tmp_path / EFFECTIVE_FILE_NAME).write_text("[1,2,3]", encoding="utf-8")
    assert read_effective_inputs(tmp_path) == {}


# --------------------------------------------------------------------------
# resolve —— the part the pipeline depends on
# --------------------------------------------------------------------------

def test_resolve_falls_back_when_no_effective_file(tmp_path):
    """没有 effective.json 时行为必须与改造前完全一致。"""
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, note = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/src.pmx"
    assert motion == "D:/src.vmd"
    assert note == ""


def test_resolve_uses_effective_model_after_repair(tmp_path):
    write_effective_inputs(tmp_path,
                           source_model="D:/src.pmx",
                           effective_model="D:/job/ProcessedPMX.pmx",
                           source_vmd="D:/src.vmd",
                           effective_motion="D:/src.vmd")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, note = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/job/ProcessedPMX.pmx"
    assert motion == "D:/src.vmd"
    assert "model=effective_model" in note
    assert "motion=" not in note


def test_resolve_uses_baked_vmd_after_mmm(tmp_path):
    write_effective_inputs(tmp_path,
                           source_model="D:/src.pmx",
                           effective_model="D:/src.pmx",
                           source_vmd="D:/src.vmd",
                           effective_motion="D:/job/mmm/baked.vmd")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, note = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/src.pmx"
    assert motion == "D:/job/mmm/baked.vmd"
    assert "motion=effective_motion" in note


def test_resolve_keeps_source_vmd_on_bypass(tmp_path):
    """零物理骨旁路：动作仍是源 VMD，但注释要说明 MMM 被跳过。"""
    write_effective_inputs(tmp_path,
                           source_model="D:/src.pmx",
                           effective_model="D:/src.pmx",
                           source_vmd="D:/src.vmd",
                           effective_motion="D:/src.vmd",
                           mmm_skipped=True,
                           mmm_skipped_reason="NO_PHYSICS_BONES")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, note = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/src.pmx"
    assert motion == "D:/src.vmd"
    assert "mmm_skipped=NO_PHYSICS_BONES" in note


def test_resolve_has_no_note_when_nothing_changed(tmp_path):
    write_effective_inputs(tmp_path,
                           source_model="D:/src.pmx",
                           effective_model="D:/src.pmx",
                           source_vmd="D:/src.vmd",
                           effective_motion="D:/src.vmd")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    _, _, note = resolve_character_inputs(cfg, tmp_path)
    assert note == ""


def test_resolve_ignores_empty_effective_fields(tmp_path):
    """effective.json 里字段为空时不能把有效输入清成空串。"""
    (tmp_path / EFFECTIVE_FILE_NAME).write_text(
        json.dumps({"effective_model": "", "effective_motion": ""}),
        encoding="utf-8")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, _ = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/src.pmx"
    assert motion == "D:/src.vmd"


def test_resolve_tolerates_missing_cfg_keys(tmp_path):
    model, motion, note = resolve_character_inputs({}, tmp_path)
    assert model == "" and motion == "" and note == ""


def test_resolve_handles_corrupt_effective_file(tmp_path):
    (tmp_path / EFFECTIVE_FILE_NAME).write_text("<<<", encoding="utf-8")
    cfg = {"model": "D:/src.pmx", "motion": "D:/src.vmd"}
    model, motion, note = resolve_character_inputs(cfg, tmp_path)
    assert model == "D:/src.pmx"
    assert note == ""


# --------------------------------------------------------------------------
# character_prep integration
# --------------------------------------------------------------------------

def test_character_prep_writes_effective_inputs(tmp_path):
    from mmd_bridge.character_prep import prepare_character_model
    from tests.test_pmx_physics_displayframe import make_pmx

    pmx = tmp_path / "m.pmx"
    pmx.write_bytes(make_pmx(
        bones=[("センター", True), ("スカート01", True)],
        rigid_bodies=[("スカート01", 1, 1)],
        display_frames=[("Root", [0])]))
    vmd = tmp_path / "source.vmd"

    job = tmp_path / "job"
    prep, _ = prepare_character_model(pmx, source_vmd=vmd,
                                      output_dir=job / "pmx")
    from mmd_bridge.character_prep import write_effective_inputs as write_prep
    write_prep(job, prep)

    payload = read_effective_inputs(job)
    assert payload["source_model"] == str(pmx)
    assert payload["source_vmd"] == str(vmd)
    assert payload["effective_model"].endswith("ProcessedPMX.pmx")
    assert payload["repaired"] is True
    assert payload["expected_bone_count"] == 1


def test_character_prep_effective_motion_can_be_overridden(tmp_path):
    from mmd_bridge.character_prep import (
        CharacterPrepResult,
        write_effective_inputs as write_prep,
    )
    result = CharacterPrepResult(
        source_model="D:/src.pmx", effective_model="D:/src.pmx",
        effective_motion="D:/src.vmd")
    write_prep(tmp_path, result, effective_motion="D:/job/mmm/baked.vmd")
    payload = read_effective_inputs(tmp_path)
    assert payload["effective_motion"] == "D:/job/mmm/baked.vmd"
    assert payload["source_vmd"] == "D:/src.vmd", "源动作仍要保留记录"
