import math

import pytest

from mmd_bridge.blender_validate import position_error_check, roundtrip_ready
from mmd_bridge.core import atomic_json


def test_position_precision_warnings_are_scaled_without_hiding_large_errors():
    near = (0, 0, 1.6)
    distant = (0, 0, -78.64)
    assert position_error_check(0.00009, near)['severity'] == 'ok'
    assert position_error_check(0.000110126, near)['severity'] == 'error'
    warning = position_error_check(0.000110126, near, distant)
    assert warning['severity'] == 'warning'
    assert warning['limit_m'] < 0.001
    assert position_error_check(0.001, distant)['severity'] == 'error'
    assert position_error_check(0, (0, 0, 0))['severity'] == 'ok'


@pytest.mark.parametrize('error,position', [(math.nan, (0, 0, 1)), (0, (math.inf, 0, 0)), (-1, (0, 0, 0))])
def test_position_precision_rejects_invalid_values(error, position):
    with pytest.raises(ValueError, match='无效|非有限'):
        position_error_check(error, position)


def test_roundtrip_receipt_accepts_warnings_but_rejects_failure_and_stale_files(tmp_path):
    manifest = {'files': {'model': {'sha256': 'model'}, 'animation': {'sha256': 'animation'}}}
    path = tmp_path / 'blender_roundtrip.json'
    assert not roundtrip_ready(tmp_path, manifest)
    proof = {'success': True, 'warnings': ['minor precision'], 'file_hashes': {'model': 'model', 'animation': 'animation'}}
    atomic_json(path, proof)
    assert roundtrip_ready(tmp_path, manifest)
    atomic_json(path, {**proof, 'success': False})
    assert not roundtrip_ready(tmp_path, manifest)
    atomic_json(path, proof)
    manifest['files']['animation']['sha256'] = 'changed'
    assert not roundtrip_ready(tmp_path, manifest)
    atomic_json(path, {'pose_checks': [{'max_position_error_m': 0}]})
    assert roundtrip_ready(tmp_path, manifest)  # Existing successful receipts predate hashes.
    path.write_text('{invalid')
    assert not roundtrip_ready(tmp_path, manifest)


def test_roundtrip_warning_is_yellow_and_hard_failure_is_red():
    from PySide6 import QtGui as G, QtWidgets as W
    from mmd_bridge.app import LogHighlighter
    app = W.QApplication.instance() or W.QApplication([])
    document = G.QTextDocument()
    highlighter = LogHighlighter(document)
    document.setPlainText('[warning] Len: FBX 精度警告，已继续\n[failed] 缺少骨骼')
    highlighter.rehighlight()
    block = document.begin()
    assert block.layout().formats()[0].format.foreground().color().name() == '#ffd166'
    assert block.next().layout().formats()[0].format.foreground().color().name() == '#ff6b6b'
