import copy
from dataclasses import asdict

import pytest

from mmd_bridge import character_material_preset as preset
from mmd_bridge.core import Settings, Job, atomic_json, read_json


def fixture():
    return {'format': preset.FORMAT, 'version': preset.VERSION,
            'slots': [{'index': 0, 'slot_name': 'Face', 'material': 'face'},
                      {'index': 1, 'slot_name': 'Cloth', 'material': 'cloth'}],
            'materials': {'face': {'root': '/Game/User/M_Face', 'instances': []},
                          'cloth': {'root': '/Game/User/M_Cloth', 'instances': []}}}


def test_reordered_mesh_uses_names_instead_of_slot_position():
    target = [{'index': 0, 'slot_name': 'Cloth'}, {'index': 1, 'slot_name': 'Face'},
              {'index': 2, 'slot_name': 'Extra'}]
    assert preset.match_slots(fixture()['slots'], target) == [{'source': 0, 'target': 1}, {'source': 1, 'target': 0}]


def test_original_pmx_identity_survives_renamed_imported_slots():
    source = [{'index': 0, 'slot_name': 'old', 'model_sha256': 'same', 'pmx_index': 12}]
    target = [{'index': 3, 'slot_name': 'new', 'model_sha256': 'same', 'pmx_index': 12},
              {'index': 0, 'slot_name': 'old', 'model_sha256': 'other', 'pmx_index': 12}]
    assert preset.match_slots(source, target) == [{'source': 0, 'target': 3}]


def test_unrelated_model_pmx_indices_cannot_silently_match():
    source = [{'index': 0, 'slot_name': 'Face', 'model_sha256': 'a', 'pmx_index': 0}]
    target = [{'index': 0, 'slot_name': 'Hair', 'model_sha256': 'b', 'pmx_index': 0}]
    with pytest.raises(ValueError, match='未唯一对应'):
        preset.match_slots(source, target)
    assert preset.match_slots(source, target, mapping={'0': 0}) == [{'source': 0, 'target': 0}]


def test_duplicate_names_and_many_to_one_mapping_are_rejected():
    with pytest.raises(ValueError, match='未唯一对应'):
        preset.match_slots([{'index': 0, 'slot_name': 'Same'}],
                           [{'index': 0, 'slot_name': 'Same'}, {'index': 1, 'slot_name': 'Same'}])
    with pytest.raises(ValueError, match='未唯一对应'):
        preset.match_slots(fixture()['slots'], [{'index': 0, 'slot_name': 'A'}], mapping={'0': 0, '1': 0})


@pytest.mark.parametrize('change', ['version', 'root', 'missing_material', 'duplicate_index'])
def test_bad_file_fails_before_any_ue_operation(change):
    data = fixture()
    if change == 'version': data['version'] = 99
    if change == 'root': data['materials']['face']['root'] = '/Script/Engine.Material'
    if change == 'missing_material': del data['materials']['face']
    if change == 'duplicate_index': data['slots'][1]['index'] = 0
    with pytest.raises(ValueError):
        preset.validate(data)


def test_new_job_freezes_file_and_rejects_changed_source_on_resume(tmp_path, monkeypatch):
    source = tmp_path/'look.json'
    atomic_json(source, fixture())
    model, motion = tmp_path/'model.pmx', tmp_path/'motion.vmd'
    model.write_bytes(b'unchanged geometry'); motion.write_bytes(b'unchanged performance')
    monkeypatch.setattr(Settings, 'validate', lambda self: {})
    settings = Settings(model=str(model), motion=str(motion), character_material_preset=str(source))
    job = Job.create(tmp_path/'jobs', settings)
    assert preset.job_preset(job) == fixture()
    assert 'character_material_preset' in job.data['inputs']
    changed = fixture(); changed['materials']['face']['root'] = '/Game/User/M_Face_Changed'
    atomic_json(source, changed)
    assert preset.job_preset(job) == fixture()
    with pytest.raises(ValueError, match='素材已改变'):
        job.verify_inputs()
    snapshot = job.folder/'character_material_preset.json'
    atomic_json(snapshot, changed)
    with pytest.raises(ValueError, match='快照已改变'):
        preset.job_preset(job)


def test_missing_field_keeps_legacy_tasks_opted_out(tmp_path):
    job = Job(tmp_path)
    atomic_json(job.path, {'settings': {'model': 'old.pmx'}})
    assert preset.job_preset(job) is None
    assert Settings().character_material_preset == ''


def test_preset_ui_roundtrip_mapping_and_old_job_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    from mmd_bridge.character_material_preset_dialog import MaterialPresetMappingDialog
    app = W.QApplication.instance() or W.QApplication([])
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    window = Window()
    try:
        filename = tmp_path/'user-look.json'; atomic_json(filename, fixture())
        field = window.inputs['character_material_preset']; field.setText(str(filename))
        saved = asdict(window.settings())
        assert saved['character_material_preset'] == str(filename)
        window.apply_settings({'fps': 24})
        assert field.text() == str(filename)
        window.apply_settings(saved, existing_job=True)
        assert field.text() == str(filename)
        window.apply_settings({'character_material_profile': 'character_pbr'}, existing_job=True)
        assert field.text() == ''
        target = dict(label='Other role', origin='动画序列 → 网格',
                      slots=[{'index': 0, 'slot_name': 'Cloth'}, {'index': 1, 'slot_name': 'Face'}])
        dialog = MaterialPresetMappingDialog(fixture(), target, window)
        assert [combo.currentData() for combo in dialog.choices] == [1, 0]
        dialog.accept()
        assert dialog.mapping == {'0': 1, '1': 0}
        dialog.close()
    finally:
        window.timer.stop(); window.close()
