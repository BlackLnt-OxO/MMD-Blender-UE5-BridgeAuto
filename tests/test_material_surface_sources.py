import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from mmd_bridge.core import Job, atomic_json


def source(tmp_path, monkeypatch):
    project = tmp_path/'Project.uproject'; project.write_text('{}')
    content = tmp_path/'Content'; scene = content/'Scenes/Pool'; scene.mkdir(parents=True)
    job = Job(tmp_path/'job')
    atomic_json(job.path, {'asset_id': 'Pool', 'settings': {'project': str(project)}})
    atomic_json(scene/'.mmd_bridge_namespace.json', {'asset_id': 'Pool', 'job': str(job.folder)})
    asset = '/Game/Scenes/Pool/Materials/MI_Leaf.MI_Leaf'
    atomic_json(job.folder/'ue_report.json', {'root': '/Game/Scenes/Pool', 'assets': {'materials': {'leaf': asset}}})
    atomic_json(job.folder/'manifest.json', {'scene': {'materials': [{'id': 'leaf', 'name': 'Leaf'}]}})
    tags = {'MMDBridge.Owner': 'Pool', 'MMDBridge.SourceMaterial': 'leaf'}
    ue = SimpleNamespace(Paths=SimpleNamespace(project_content_dir=lambda: str(content),
        get_project_file_path=lambda: str(project), convert_relative_path_to_full=lambda path: path),
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda material, name: tags.get(name, '')))
    monkeypatch.setitem(sys.modules, 'unreal', ue)
    spec = importlib.util.spec_from_file_location('mmd_bridge._surface_source_test',
        Path(__file__).parents[1]/'mmd_bridge/ue_material_surface.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    material = SimpleNamespace(get_path_name=lambda: asset)
    return module, job, scene, tags, material


def test_namespace_and_source_id_locate_record_without_a_selected_task(tmp_path, monkeypatch):
    module, job, _, _, material = source(tmp_path, monkeypatch)
    matched, _, record = module.source_record(material)
    assert matched.folder == job.folder and record['id'] == 'leaf'


def test_nested_override_finds_original_scene_namespace(tmp_path, monkeypatch):
    module, job, _, tags, material = source(tmp_path, monkeypatch)
    tags['MMDBridge.SurfaceSource'] = 'leaf'
    material.get_path_name = lambda: '/Game/Scenes/Pool/MaterialSurfaces/S_Test/Materials/MI_New.MI_New'
    assert module.source_record(material)[0].folder == job.folder


@pytest.mark.parametrize('changed', ['owner', 'root', 'project', 'missing'])
def test_conflicting_or_missing_records_are_not_guessed(tmp_path, monkeypatch, changed):
    module, job, scene, tags, material = source(tmp_path, monkeypatch)
    if changed == 'owner': tags['MMDBridge.Owner'] = 'Other'
    elif changed == 'root':
        atomic_json(job.folder/'ue_report.json', {'root': '/Game/Scenes/Other', 'assets': {'materials': {}}})
    elif changed == 'project':
        atomic_json(job.path, {'asset_id': 'Pool', 'settings': {'project': str(tmp_path/'Other.uproject')}})
    else: (scene/'.mmd_bridge_namespace.json').unlink()
    with pytest.raises(ValueError): module.source_record(material)


def test_duplicate_material_with_only_a_source_tag_is_not_accepted(tmp_path, monkeypatch):
    module, _, _, _, material = source(tmp_path, monkeypatch)
    material.get_path_name = lambda: '/Game/Scenes/Pool/Materials/MI_Copy.MI_Copy'
    with pytest.raises(ValueError, match='绑定不一致'): module.source_record(material)


def test_snapshot_reads_manifest_once_and_next_snapshot_refreshes_it(tmp_path, monkeypatch):
    module, job, _, _, material = source(tmp_path, monkeypatch)
    read = module.read_json
    calls = []
    def counting(path):
        if Path(path).name == 'manifest.json': calls.append(path)
        return read(path)
    monkeypatch.setattr(module, 'read_json', counting)
    cache = {}
    module.source_record(material, cache); module.source_record(material, cache)
    assert len(calls) == 1
    atomic_json(job.folder/'manifest.json', {'scene': {'materials': [{'id': 'leaf', 'name': 'Updated'}]}})
    assert module.source_record(material, {})[2]['name'] == 'Updated'
    assert len(calls) == 2
