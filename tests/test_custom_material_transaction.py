import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.core import atomic_json, read_json


def transaction(tmp_path, save_results):
    path = Path(__file__).parents[1]/'mmd_bridge/ue_custom_material_edit.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'commit')
    class Asset:
        def __init__(self, name): self.name = name
        def get_path_name(self): return self.name
    assets = {name: Asset(name) for name in ('original', 'candidate', 'later')}
    class Component:
        current = assets['original']
        def get_material(self, slot): return self.current
        def set_material(self, slot, value): self.current = value
        def modify(self): pass
    component = Component()
    saves = iter(save_results)
    editor = SimpleNamespace(save_current_level=lambda: next(saves))
    unreal = SimpleNamespace(find_object=lambda *a: component, load_asset=assets.get,
                            get_editor_subsystem=lambda cls: editor, LevelEditorSubsystem=object())
    ledger = tmp_path/'custom_undo.json'
    scope = {'u': unreal, 'atomic_json': atomic_json, 'read_json': read_json,
             'ledger_path': lambda: ledger, 'level': lambda: 'isolated-level'}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    rows = [{'component': 'component', 'slot': 0, 'original': 'original', 'candidate': 'candidate'}]
    return scope['commit'], component, assets, ledger, rows


def test_success_records_exact_binding_and_separate_ledger(tmp_path):
    commit, component, assets, ledger, rows = transaction(tmp_path, [True])
    unrelated = tmp_path/'character_pbr_undo.json'; unrelated.write_bytes(b'untouched PBR ledger')
    assert commit(rows, 'revision')['count'] == 1
    assert component.current is assets['candidate'] and read_json(ledger)['state'] == 'complete'
    assert unrelated.read_bytes() == b'untouched PBR ledger'


def test_failed_save_restores_binding_and_previous_custom_ledger(tmp_path):
    commit, component, assets, ledger, rows = transaction(tmp_path, [False, True])
    atomic_json(ledger, {'state': 'earlier-record'})
    with pytest.raises(RuntimeError, match='保存'): commit(rows, 'revision')
    assert component.current is assets['original'] and read_json(ledger) == {'state': 'earlier-record'}


def test_failed_rollback_keeps_interrupted_record_for_recovery(tmp_path):
    commit, component, assets, ledger, rows = transaction(tmp_path, [False, False])
    with pytest.raises(RuntimeError): commit(rows, 'revision')
    assert read_json(ledger)['state'] == 'applying'


def test_later_binding_is_never_overwritten(tmp_path):
    commit, component, assets, ledger, rows = transaction(tmp_path, [True])
    component.current = assets['later']
    with pytest.raises(ValueError, match='后续编辑'): commit(rows, 'revision')
    assert component.current is assets['later']
