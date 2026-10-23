import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from mmd_bridge.core import Job, atomic_json, file_hash, read_json


def transaction(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'unreal', SimpleNamespace())
    path = Path(__file__).parents[1]/'mmd_bridge/ue_material_surface.py'
    spec = importlib.util.spec_from_file_location('mmd_bridge._surface_transaction_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    job = Job(tmp_path/'job')
    folder = job.folder/'MaterialSurfaces/S_Test'; folder.mkdir(parents=True)
    root = tmp_path/'Scene.usda'; original = b'#usda 1.0\r\n( subLayers = [@Geometry.usda@] )\r\n'
    root.write_bytes(original)
    layer = tmp_path/'MaterialSurfaces/S_Test.usda'; layer.parent.mkdir(); layer.write_bytes(b'#usda 1.0')
    material = tmp_path/'original.uasset'; material.write_bytes(b'protected')
    row = {'prim_path': '/World/Plant/Mesh', 'slot': 0, 'label': 'Plant',
           'original_asset': '/Game/Original.Original', 'candidate_asset': '/Game/Candidate.Candidate'}
    data = {'root_layer': str(root), 'root_hash': file_hash(root), 'targets': [row],
            'mode': 'masked', 'protected_assets': {row['original_asset']: file_hash(material)}}
    atomic_json(folder/'build.json', data)
    atomic_json(folder/'layer.json', {'path': str(layer), 'sha256': file_hash(layer), 'relative': 'MaterialSurfaces/S_Test.usda'})
    stage = SimpleNamespace(set_root_layer=lambda value: None)
    def component(stage, row):
        asset = row['candidate_asset'] if b'@MaterialSurfaces' in root.read_bytes() else row['original_asset']
        return SimpleNamespace(get_material=lambda slot: SimpleNamespace(get_path_name=lambda: asset))
    monkeypatch.setattr(module, 'context', lambda job, **kwargs: ({}, {}, stage, root))
    monkeypatch.setattr(module, 'saved_guard', lambda *args: None)
    monkeypatch.setattr(module, 'component', component)
    monkeypatch.setattr(module, 'asset_file', lambda asset: material)
    monkeypatch.setattr(module, 'save_stage_level', lambda *args: True)
    return module, job, folder, root, original


def test_commit_and_undo_preserve_exact_crlf_bytes_and_shared_asset(tmp_path, monkeypatch):
    module, job, folder, root, original = transaction(tmp_path, monkeypatch)
    assert module.commit(job, folder)['count'] == 1
    assert root.read_bytes() != original
    assert module.commit(job, folder, undo=True)['undone']
    assert root.read_bytes() == original


def test_failed_binding_rolls_back_without_marking_committed(tmp_path, monkeypatch):
    module, job, folder, root, original = transaction(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'component', lambda *args: SimpleNamespace(
        get_material=lambda slot: SimpleNamespace(get_path_name=lambda: '/Game/Original.Original')))
    with pytest.raises(ValueError, match='绑定未通过'): module.commit(job, folder)
    assert root.read_bytes() == original
    assert not (folder/'receipt.json').exists()
    assert read_json(folder/'journal.json')['state'] == 'rolled_back'


def test_usd_failed_level_save_restores_root_without_committing(tmp_path,monkeypatch):
    module,job,folder,root,original=transaction(tmp_path,monkeypatch)
    replies=iter([False,True])
    monkeypatch.setattr(module,'save_stage_level',lambda *args:next(replies))
    with pytest.raises(ValueError,match='保存 USD'):module.commit(job,folder)
    assert root.read_bytes()==original and not (folder/'receipt.json').exists()
    assert read_json(folder/'journal.json')['state']=='rolled_back'


def test_planned_root_with_strong_override_restores_original_bytes(tmp_path, monkeypatch):
    module,job,folder,root,original=transaction(tmp_path,monkeypatch)
    planned=folder/'root_candidate.usda'
    planned.write_bytes(original+b'\n# explicit slot override @MaterialSurfaces@')
    layer=read_json(folder/'layer.json');layer['root_candidate_hash']=file_hash(planned)
    atomic_json(folder/'layer.json',layer)
    assert module.commit(job,folder)['count']==1
    assert root.read_bytes()==planned.read_bytes()
    assert module.commit(job,folder,undo=True)['undone']
    assert root.read_bytes()==original


def test_changed_planned_root_never_overwrites_live_root(tmp_path, monkeypatch):
    module,job,folder,root,original=transaction(tmp_path,monkeypatch)
    planned=folder/'root_candidate.usda';planned.write_bytes(original)
    layer=read_json(folder/'layer.json');layer['root_candidate_hash']=file_hash(planned)
    atomic_json(folder/'layer.json',layer);planned.write_bytes(b'changed')
    with pytest.raises(ValueError,match='候选根层已改变'): module.commit(job,folder)
    assert root.read_bytes()==original


def test_interrupted_commit_can_restore_without_a_receipt(tmp_path, monkeypatch):
    module, job, folder, root, original = transaction(tmp_path, monkeypatch)
    module.commit(job, folder)
    receipt = read_json(folder/'receipt.json'); (folder/'receipt.json').unlink()
    atomic_json(folder/'journal.json', {'state': 'applying', 'before': file_hash(folder/'root_before.usda'),
                                      'after': receipt['root_hash_after']})
    assert module.commit(job, folder, undo=True)['undone']
    assert root.read_bytes() == original


def test_later_edit_and_corrupt_restore_point_are_not_overwritten(tmp_path, monkeypatch):
    module, job, folder, root, original = transaction(tmp_path, monkeypatch)
    module.commit(job, folder)
    after = root.read_bytes()
    root.write_bytes(after + b'\n# user edit')
    with pytest.raises(ValueError, match='后续编辑'): module.commit(job, folder, undo=True)
    assert root.read_bytes().endswith(b'# user edit')
    root.write_bytes(after); (folder/'root_before.usda').write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='还原点'): module.commit(job, folder, undo=True)
    assert root.read_bytes() == after


def native_transaction(tmp_path, monkeypatch):
    module, job, folder, root, original = transaction(tmp_path, monkeypatch)
    data = read_json(folder/'build.json')
    data['level'] = '/Game/Other/Map'
    data['targets'][0].update(binding='native', component_path='Actor.Mesh', level=data['level'])
    atomic_json(folder/'build.json', data)
    assets = {name: SimpleNamespace(get_path_name=lambda name=name: name)
              for name in ('/Game/Original.Original', '/Game/Candidate.Candidate', '/Game/User.User')}
    state = {'asset': assets['/Game/Original.Original'], 'saved': 0}
    def save(): state['saved'] += 1; return True
    cmp = SimpleNamespace(get_material=lambda slot: state['asset'], modify=lambda: None,
                          set_material=lambda slot, asset: state.update(asset=asset))
    monkeypatch.setattr(module, 'current_level', lambda: data['level'])
    monkeypatch.setattr(module, 'component', lambda stage, row: cmp)
    module.u.get_editor_subsystem = lambda cls: SimpleNamespace(save_current_level=save)
    module.u.LevelEditorSubsystem = object
    module.u.load_asset = lambda path: assets.get(path)
    return module, job, folder, root, original, state, assets


def test_native_copy_switch_and_undo_save_only_instance_bindings(tmp_path, monkeypatch):
    module, job, folder, root, original, state, assets = native_transaction(tmp_path, monkeypatch)
    assert module.commit(job, folder)['count'] == 1
    assert state['asset'] is assets['/Game/Candidate.Candidate'] and state['saved'] == 1
    assert module.commit(job, folder, undo=True)['undone']
    assert state['asset'] is assets['/Game/Original.Original'] and state['saved'] == 2
    assert root.read_bytes() == original


def test_native_later_slot_edit_is_never_overwritten(tmp_path, monkeypatch):
    module, job, folder, _, _, state, assets = native_transaction(tmp_path, monkeypatch)
    module.commit(job, folder)
    state['asset'] = assets['/Game/User.User']
    with pytest.raises(ValueError, match='后续编辑'): module.commit(job, folder, undo=True)
    assert state['asset'] is assets['/Game/User.User'] and state['saved'] == 1


def test_native_failed_save_restores_original_binding(tmp_path, monkeypatch):
    module, job, folder, root, original, state, assets = native_transaction(tmp_path, monkeypatch)
    replies = iter([False, True])
    module.u.get_editor_subsystem = lambda cls: SimpleNamespace(save_current_level=lambda: next(replies))
    with pytest.raises(ValueError, match='保存'): module.commit(job, folder)
    assert state['asset'] is assets['/Game/Original.Original'] and root.read_bytes() == original
    assert read_json(folder/'journal.json')['state'] == 'rolled_back'
