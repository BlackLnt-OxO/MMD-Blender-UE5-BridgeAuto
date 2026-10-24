import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtWidgets as W

from mmd_bridge.app import CharacterFileField, Window
from mmd_bridge.core import read_json
from mmd_bridge.motion_files_dialog import MotionFilesDialog
from tests.test_motion_inputs import write_motion
from tests.test_vmd_reader import _bone_frame, _morph_frame


def test_multiselect_order_config_switch_and_restart_per_character(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    face = write_motion(tmp_path, 'face.vmd', morph_frames=[_morph_frame('あ', 2, .5)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 3)])
    other = write_motion(tmp_path, 'other_eye.vmd', bone_frames=[_bone_frame('両目', 6)])
    monkeypatch.setattr(W.QFileDialog, 'getOpenFileNames', lambda *a: ([face, eye], ''))
    window = Window(); window.timer.stop()
    try:
        next(b for b in window.findChildren(W.QPushButton) if b.property('input_key') == 'lipsync').click()
        assert window.settings().supplementary_motions == [face, eye]
        assert window.inputs['lipsync'].text() == face
        window.multi_character_button.setChecked(True)
        assert window.character_values(0)['supplementary_motions'] == [face, eye]
        field = window.character_table.cellWidget(1, 2).findChild(CharacterFileField)
        field.set_paths([other, face])
        assert window.settings().additional_characters[0]['supplementary_motions'] == [other, face]
        # Loading another preset while multi-character mode is already active
        # must not restore the old first row through persistence callbacks.
        window.apply_settings({'lipsync': eye, 'supplementary_motions': [eye, face],
            'additional_characters': [{'model': 'other.pmx', 'motion': other,
                                       'lipsync': face, 'supplementary_motions': [face, other]}]})
        assert window.settings().supplementary_motions == [eye, face]
        assert window.settings().additional_characters[0]['supplementary_motions'] == [face, other]
        assert window.flow_settings('scene').supplementary_motions == []
        saved = read_json(window.dialog_paths_file)
        assert saved['supplementary_motions'] == [eye, face]
    finally:
        window.close()
    restarted = Window(); restarted.timer.stop()
    try:
        assert restarted.settings().supplementary_motions == [eye, face]
        assert restarted.settings().additional_characters[0]['supplementary_motions'] == [face, other]
        restarted.character_table.cellWidget(0, 2).findChild(CharacterFileField).set_paths([])
        assert restarted.settings().supplementary_motions == []
    finally:
        restarted.close()
    cleared = Window(); cleared.timer.stop()
    try:
        assert cleared.settings().supplementary_motions == []
        assert cleared.settings().lipsync == ''
    finally:
        cleared.close()


def test_motion_list_shows_real_tracks_and_preserves_explicit_order(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    face = write_motion(tmp_path, 'face.vmd', morph_frames=[_morph_frame('あ', 2, .5)])
    eye = write_motion(tmp_path, 'eye.vmd', bone_frames=[_bone_frame('両目', 3)])
    dialog = MotionFilesDialog([face, eye])
    try:
        assert '表情 1' in dialog.files.item(0).text()
        assert '骨骼 1' in dialog.files.item(1).text()
        dialog.files.setCurrentRow(1)
        dialog.move(-1)
        assert dialog.paths() == [eye, face]
        dialog.remove_selected()
        assert dialog.paths() == [face]
    finally:
        dialog.close()


def test_official_mmd2unreal_keeps_legacy_single_expression_input(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window(); window.timer.stop()
    try:
        window.apply_settings({'character_backend': 'mmd2unreal', 'lipsync': 'face.vmd'})
        assert window.settings().lipsync == 'face.vmd'
        assert window.settings().supplementary_motions == []
        window.inputs['lipsync'].set_paths(['face.vmd', 'eye.vmd'])
        assert window.settings().supplementary_motions == ['face.vmd', 'eye.vmd']
    finally:
        window.close()
