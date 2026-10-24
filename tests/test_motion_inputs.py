from dataclasses import asdict, replace

import pytest

from mmd_bridge.core import Job, Settings, atomic_json, file_hash, read_json, source_input_paths
from mmd_bridge.motion_inputs import prepare_motion_inputs
from mmd_bridge.vmd_reader import read_vmd
from mmd_bridge.vmd_writer import merge_vmds
from tests.test_vmd_reader import (
    _bone_frame, _camera_frame, _ik_frame, _morph_frame, make_vmd,
)


def write_motion(folder, name, **sections):
    path = folder / name
    path.write_bytes(make_vmd(**sections))
    return str(path)


def test_last_file_replaces_whole_named_track_and_preserves_other_tracks(tmp_path):
    main = write_motion(tmp_path, 'body.vmd', bone_frames=[
        _bone_frame('センター', 0), _bone_frame('センター', 100),
        _bone_frame('両目', 0), _bone_frame('両目', 20)], morph_frames=[
        _morph_frame('あ', 0, .1), _morph_frame('あ', 80, .8),
        _morph_frame('笑い', 50, .4)], ik_frames=[_ik_frame(0, True, [('左足ＩＫ', 1)])])
    face = write_motion(tmp_path, 'face.vmd', morph_frames=[_morph_frame('あ', 10, .5)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[
        _bone_frame('両目', 5, pos=(1, 2, 3))[:-64] + bytes(range(64))],
                       morph_frames=[_morph_frame('あ', 30, 0)])
    empty = write_motion(tmp_path, 'empty.vmd')
    before = {p: file_hash(p) for p in (main, face, eye, empty)}
    report = merge_vmds([main, face, eye, empty], tmp_path / 'merged.vmd')
    result = read_vmd(tmp_path / 'merged.vmd')
    assert {(f.name, f.frame) for f in result.bone_frames} == {
        ('センター', 0), ('センター', 100), ('両目', 5)}
    assert [(f.name, f.frame, f.weight) for f in result.morph_frames] == [
        ('あ', 30, 0), ('笑い', 50, pytest.approx(.4))]
    assert result.ik_frames == read_vmd(main).ik_frames
    assert result.bone_tracks['両目'].frames == read_vmd(eye).bone_frames
    assert before == {p: file_hash(p) for p in before}
    assert report['policy'] == 'later_track_wins'
    assert report['replaced_tracks']['morph_frames'] == ['あ', 'あ']


def test_bone_and_morph_same_name_are_separate_tracks_and_order_is_authoritative(tmp_path):
    main = write_motion(tmp_path, 'main.vmd', bone_frames=[_bone_frame('目', 0)],
                         morph_frames=[_morph_frame('目', 0, .2)])
    first = write_motion(tmp_path, 'first.vmd', bone_frames=[_bone_frame('目', 2)])
    last = write_motion(tmp_path, 'last.vmd', bone_frames=[_bone_frame('目', 4)])
    for files, expected in (([first, last], 4), ([last, first], 2)):
        merge_vmds([main, *files], tmp_path / 'out.vmd')
        result = read_vmd(tmp_path / 'out.vmd')
        assert [f.frame for f in result.bone_frames] == [expected]
        assert [f.frame for f in result.morph_frames] == [0]


def test_invalid_merge_cannot_overwrite_source_or_existing_output(tmp_path):
    main = write_motion(tmp_path, 'main.vmd', bone_frames=[_bone_frame('頭', 0)])
    duplicate = write_motion(tmp_path, 'duplicate.vmd', bone_frames=[
        _bone_frame('両目', 0), _bone_frame('両目', 0, pos=(1, 0, 0))])
    original = file_hash(main)
    with pytest.raises(ValueError, match='独立于输入'):
        merge_vmds([main], main)
    output = tmp_path / 'out.vmd'; output.write_bytes(b'preserved')
    with pytest.raises(ValueError, match='重复关键帧'):
        merge_vmds([main, duplicate], output)
    with pytest.raises(ValueError, match='帧号超出范围'):
        merge_vmds([main], output, offsets=[-1])
    assert file_hash(main) == original and output.read_bytes() == b'preserved'


def test_identical_in_file_duplicates_are_deduplicated_without_erasing_tracks(tmp_path):
    main = write_motion(tmp_path, 'main.vmd', bone_frames=[_bone_frame('頭', 0)] * 2,
                        morph_frames=[_morph_frame('あ', 3, .5)] * 2)
    extra = write_motion(tmp_path, 'extra.vmd', bone_frames=[_bone_frame('両目', 5)])
    report = merge_vmds([main, extra], tmp_path / 'out.vmd')
    result = read_vmd(tmp_path / 'out.vmd')
    assert [(f.name, f.frame) for f in result.bone_frames] == [('頭', 0), ('両目', 5)]
    assert len(result.morph_frames) == 1
    assert report['deduplicated_keys'] == [
        {'source_index': 0, 'section': 'bone_frames', 'track': '頭', 'frame': 0},
        {'source_index': 0, 'section': 'morph_frames', 'track': 'あ', 'frame': 3}]


def test_ik_switch_tracks_do_not_erase_unmentioned_switches(tmp_path):
    main = write_motion(tmp_path, 'main.vmd', ik_frames=[
        _ik_frame(0, True, [('左足ＩＫ', 1), ('右足ＩＫ', 2)]),
        _ik_frame(20, True, [('左足ＩＫ', 3), ('右足ＩＫ', 4)])])
    extra = write_motion(tmp_path, 'extra.vmd', ik_frames=[_ik_frame(5, False, [('左足ＩＫ', 8)])])
    merge_vmds([main, extra], tmp_path / 'out.vmd')
    frames = read_vmd(tmp_path / 'out.vmd').ik_frames
    events = {f.frame: {t.name: t.bone_index for t in f.targets} for f in frames}
    assert events[0] == {'右足ＩＫ': 2}
    assert events[5] == {'右足ＩＫ': 2, '左足ＩＫ': 8}
    assert events[20] == {'右足ＩＫ': 4, '左足ＩＫ': 8}
    assert not frames[-1].visible


def test_merge_stays_inside_task_and_reuses_only_matching_receipt(tmp_path):
    main = write_motion(tmp_path, 'body.vmd', bone_frames=[_bone_frame('センター', 0)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 2)])
    cfg = asdict(Settings(motion=main, supplementary_motions=[eye]))
    output, lipsync, report = prepare_motion_inputs(cfg, tmp_path / 'transfer' / 'job')
    assert output == str(tmp_path / 'transfer' / 'job' / 'motion_inputs' / 'merged.vmd')
    assert not lipsync and report['sources'][1]['path'] == eye
    assert read_json(tmp_path / 'transfer' / 'job' / 'motion_inputs' / 'merge.json') == report
    assert prepare_motion_inputs(cfg, tmp_path / 'transfer' / 'job') == (output, '', report)
    (tmp_path / 'eye.vmd').write_bytes(make_vmd(bone_frames=[_bone_frame('両目', 3)]))
    with pytest.raises(ValueError, match='已改变'):
        prepare_motion_inputs(cfg, tmp_path / 'transfer' / 'job')


def test_legacy_settings_keep_original_two_inputs(tmp_path):
    assert prepare_motion_inputs({'motion': 'main.vmd', 'lipsync': 'face.vmd'}, tmp_path) == (
        'main.vmd', 'face.vmd', None)
    assert not list(tmp_path.iterdir())


def test_offsets_and_interpolation_are_preserved_without_touching_source(tmp_path):
    main = write_motion(tmp_path, 'main.vmd', bone_frames=[_bone_frame('センター', 0)])
    extra = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 3)])
    cfg = asdict(Settings(motion=main, supplementary_motions=[extra], motion_offset=4, lipsync_offset=8))
    output, _, _ = prepare_motion_inputs(cfg, tmp_path / 'job')
    eye = read_vmd(output).bone_tracks['両目'].frames[0]
    assert eye.frame == 7
    assert eye.interpolation == read_vmd(extra).bone_frames[0].interpolation
    with pytest.raises(ValueError, match='整数 VMD 帧'):
        prepare_motion_inputs(cfg | {'fps': 24, 'lipsync_offset': 5}, tmp_path / 'other')


def test_validation_accepts_eye_only_and_checks_every_supplement(tmp_path):
    exe = tmp_path / 'blender.exe'; exe.touch()
    model = tmp_path / 'model.pmx'; model.write_bytes(b'PMX ')
    main = write_motion(tmp_path, 'main.vmd', bone_frames=[_bone_frame('センター', 0)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 3)])
    cfg = Settings(blender=str(exe), model=str(model), motion=main,
                   lipsync=eye, supplementary_motions=[eye])
    assert cfg.validate()['supplementary_motion_0000']['tracks']['bone'] == 1
    with pytest.raises(ValueError, match='没有 morph'):
        replace(cfg, supplementary_motions=[]).validate()
    camera = write_motion(tmp_path, 'camera.vmd', camera_frames=[_camera_frame(2)])
    with pytest.raises(ValueError, match='没有骨骼、表情或 IK'):
        replace(cfg, supplementary_motions=[camera]).validate()
    with pytest.raises(ValueError, match='文件不存在'):
        replace(cfg, supplementary_motions=[str(tmp_path / 'missing.vmd')]).validate()


def test_resume_detects_changes_reordering_and_removal_per_character(tmp_path):
    first = write_motion(tmp_path, 'first.vmd', bone_frames=[_bone_frame('両目', 2)])
    second = write_motion(tmp_path, 'second.vmd', morph_frames=[_morph_frame('あ', 4, .5)])
    cfg = asdict(Settings(motion=first, supplementary_motions=[first, second],
                          additional_characters=[{'motion': second, 'supplementary_motions': [second]}]))
    data = {'settings': cfg, 'inputs': {k: file_hash(p) for k, p in source_input_paths(cfg).items()},
            'character_inputs': [{k: file_hash(p) for k, p in source_input_paths(c).items()}
                                 for c in cfg['additional_characters']]}
    job = Job(tmp_path / 'job')
    atomic_json(job.path, data)
    job.verify_inputs()
    for paths in ([second, first], [first], []):
        changed = {**data, 'settings': cfg | {'supplementary_motions': paths}}
        atomic_json(job.path, changed)
        with pytest.raises(ValueError, match='已改变'):
            job.verify_inputs()
    atomic_json(job.path, data)
    (tmp_path / 'second.vmd').write_bytes(make_vmd(morph_frames=[_morph_frame('あ', 5, .5)]))
    with pytest.raises(ValueError, match='已改变'):
        job.verify_inputs()


def mode1_settings(tmp_path, **updates):
    from tests.test_pmx_physics_displayframe import make_pmx
    source = tmp_path / 'model_source'; source.mkdir()
    model = source / 'actor.pmx'; model.write_bytes(make_pmx())
    exe = tmp_path / 'blender.exe'; exe.touch()
    runtime = tmp_path / 'runtime'
    (runtime / 'mmdbridge_scripts').mkdir(parents=True)
    for name in ('MikuMikuDance_CHS.exe', 'd3d9.dll', 'mmdbridge_scripts/mmdbridge_vmd_mode1.py'):
        (runtime / name).touch()
    main = write_motion(tmp_path, 'body.vmd', bone_frames=[_bone_frame('頭', 0), _bone_frame('頭', 3)])
    return Settings(**(dict(blender=str(exe), model=str(model), motion=main,
                           mmdbridge_path=str(runtime), character_backend='mmdbridge_fbx',
                           flow='character', character_output_dir=str(tmp_path)) | updates))


def test_mode1_preparation_uses_one_merged_input_and_extends_morph_duration(tmp_path):
    from mmd_bridge.mmd_avi import prepare_job, run_bake
    face = write_motion(tmp_path, 'face.vmd', morph_frames=[_morph_frame('あ', 120, .5)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 6)])
    cfg = mode1_settings(tmp_path, supplementary_motions=[face, eye])
    job = Job.create(tmp_path / 'transfer', cfg)
    prepared = prepare_job(job)
    assert prepared.lipsync is None
    assert prepared.source_motion.as_posix() == cfg.motion.replace('\\', '/')
    assert prepared.motion_merge_version == 1 and prepared.frame_range == (0, 120)
    assert read_vmd(prepared.motion).bone_tracks['両目'].frames[0].frame == 6
    assert prepare_job(job) == prepared
    run_bake(job)  # Synthetic PMX has no physics: no DCC is launched.
    effective = read_json(job.folder / 'effective.json')
    assert effective['effective_lipsync'] == ''
    assert read_vmd(effective['effective_motion']).morph_frames[0].frame == 120


def test_multi_role_routes_keep_supplement_order_and_separate_merge_files(tmp_path, monkeypatch):
    from mmd_bridge import mmd_avi
    from mmd_bridge.orchestration import module_settings
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 4)])
    last = write_motion(tmp_path, 'last_eye.vmd', bone_frames=[_bone_frame('両目', 9)])
    cfg = mode1_settings(tmp_path, supplementary_motions=[eye, last])
    cfg = replace(cfg, additional_characters=[{'model': cfg.model, 'motion': cfg.motion,
                                              'supplementary_motions': [last, eye]}])
    parts = dict(module_settings(cfg))
    assert parts['character_0001'].supplementary_motions == [eye, last]
    assert parts['character_0002'].supplementary_motions == [last, eye]
    parent = Job.create(tmp_path / 'transfer', cfg)
    children = []
    def fake_native_run(child):
        children.append(child)
        mmd_avi.prepare_job(child)
        child.status('mmd_bake_done', 'fixture prepared')
    monkeypatch.setattr(mmd_avi, 'run_bake', fake_native_run)
    mmd_avi.run_multi_bake(parent)
    assert len(children) == 2
    assert [read_vmd(child.folder / 'motion_inputs' / 'merged.vmd').bone_tracks['両目'].first_frame
            for child in children] == [9, 4]
    assert all(child.data['shared_bake_end'] == 9 for child in children)
    assert children[0].folder != children[1].folder


def test_secondary_role_without_supplements_does_not_inherit_primary_expression(tmp_path, monkeypatch):
    from mmd_bridge import mmd_avi
    from mmd_bridge.orchestration import module_settings
    face = write_motion(tmp_path, 'face.vmd', morph_frames=[_morph_frame('あ', 4, .5)])
    cfg = mode1_settings(tmp_path, lipsync=face, supplementary_motions=[face])
    cfg = replace(cfg, additional_characters=[{'model': cfg.model, 'motion': cfg.motion}])
    assert not dict(module_settings(cfg))['character_0002'].supplementary_motions
    parent = Job.create(tmp_path / 'transfer', cfg)
    children = []
    def prepare(child):
        children.append(child)
        mmd_avi.prepare_job(child)
        child.status('mmd_bake_done', 'fixture prepared')
    monkeypatch.setattr(mmd_avi, 'run_bake', prepare)
    mmd_avi.run_multi_bake(parent)
    assert children[0].data['settings']['supplementary_motions'] == [face]
    assert children[1].data['settings']['lipsync'] == ''
    assert children[1].data['settings']['supplementary_motions'] == []
    assert not (children[1].folder / 'motion_inputs').exists()


def test_composite_parent_keeps_fingerprints_owned_by_independent_modules(tmp_path):
    from mmd_bridge.orchestration import create_parent
    parent = create_parent(Settings(character_backend='mmdbridge_fbx', model='missing.pmx',
        motion='missing.vmd', supplementary_motions=['missing_eye.vmd'], character_output_dir=str(tmp_path)))
    assert parent.data['inputs'] == {}
    parent.verify_inputs()  # Each module diagnoses its own missing inputs.
