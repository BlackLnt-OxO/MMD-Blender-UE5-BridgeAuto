import copy

from tests.test_character_custom_materials import fixture, binding


def test_custom_analysis_keeps_pbr_geometry_and_input_settings_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    cfg, row, entry = fixture(tmp_path)
    cfg['character_material_overrides'] = {'pbr': {'0': {'params': {'SkinRosiness': .8}}}}
    cfg['character_geometry_overrides'] = {'geometry': {'0': {'local': 'include'}}}
    original = copy.deepcopy(cfg)
    app = W.QApplication.instance() or W.QApplication([])
    snapshot = {'model': cfg['model'], 'sha256': row['model_sha256'], 'materials': [row],
                'texture_count': 1, 'custom_mapping_snapshot': 1}
    dialog = CharacterPBRDialog(snapshot, cfg)
    try:
        page = dialog.custom_editor
        name, source, *_ = page.fields[0]
        source.setCurrentIndex(source.findData('base_color'))
        page.save()
        assert dialog.result() == W.QDialog.Accepted
        assert dialog.overrides == cfg['character_material_overrides']
        assert dialog.geometry_overrides == cfg['character_geometry_overrides']
        assert cfg == original
        saved = dialog.custom_materials['models'][row['model_sha256']]['projects']
        assert next(iter(saved.values()))['parents']['/Game/M']['textures'][name]['source'] == 'base_color'
    finally:
        dialog.close()


def test_per_slot_default_and_cancel_do_not_change_original_common_rule(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    from mmd_bridge.character_custom_materials import plan
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    original = copy.deepcopy(cfg)
    app = W.QApplication.instance() or W.QApplication([])
    snapshot = {'model': cfg['model'], 'sha256': row['model_sha256'], 'materials': [row],
                'texture_count': 1, 'custom_mapping_snapshot': 1}
    dialog = CharacterPBRDialog(snapshot, cfg)
    try:
        page = dialog.custom_editor
        page.scope.setCurrentIndex(page.scope.findData('slot'))
        _, source, *_ = page.fields[0]
        source.setCurrentIndex(source.findData(None))
        page.store()
        assert not plan(page.cfg, row)['textures']
        dialog.reject()
        assert cfg == original and plan(cfg, row)['textures']
    finally:
        dialog.close()


def test_unread_imported_rule_survives_dialog_and_is_not_silently_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M'] = {'textures': {'ImportedName': binding()}}
    app = W.QApplication.instance() or W.QApplication([])
    dialog = CharacterPBRDialog({'model': cfg['model'], 'sha256': row['model_sha256'],
        'materials': [row], 'custom_mapping_snapshot': 1}, cfg)
    try:
        page = dialog.custom_editor
        page.store()
        assert page.entry['parents']['/Game/M']['textures']['ImportedName'] == binding()
        assert page.table.item(0, 0).text() == 'ImportedName'
        assert '重新读取' in page.preview.toPlainText()
    finally:
        dialog.close()


def test_window_persists_custom_namespace_without_changing_pbr_and_clears_old_presets(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    from mmd_bridge.core import read_json
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    cfg, row, entry = fixture(tmp_path)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window(); window.timer.stop()
    try:
        window.apply_settings(cfg)
        pbr = copy.deepcopy(window.character_material_overrides)
        window.remember_character_rows()
        saved = read_json(window.dialog_paths_file)
        assert saved['character_custom_materials'] == cfg['character_custom_materials']
        assert window.settings().character_custom_materials == cfg['character_custom_materials']
        window.apply_settings({'character_material_profile': 'character_pbr'}, existing_job=True)
        assert window.settings().character_custom_materials == {}
        assert window.character_material_overrides == pbr
    finally:
        window.close()
