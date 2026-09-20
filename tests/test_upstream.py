import importlib
import sys
from types import SimpleNamespace as N
import pytest
from mmd_bridge.core import Settings, Job, atomic_json, read_json
from mmd_bridge.controller import Controller
from mmd_bridge.material_policy import classify_surface, projected_uv


def test_surface_classification_and_override():
    assert classify_surface([.3, .6, .8], [8, 8])["category"] == "small"
    assert classify_surface([150, 200, 0], planar=True)["strategy"] == "hybrid"
    assert classify_surface([8, 9, 8], [4, 4])["category"] == "small"  # tree, not a tiled surface
    assert classify_surface([1, 1, 1], override="tiled")["category"] == "tiled"
    with pytest.raises(ValueError):
        classify_surface([1, 1, 1], override="typo")
    with pytest.raises(ValueError):
        classify_surface([float('nan'), 1, 1])


def test_shared_projection_has_identical_edge_coordinates():
    low, high = [0, 0, 0], [100, 200, 0]
    # Adjacent meshes have different local coordinates/translations at the same edge.
    left_edge = [0 + 50, 80, 0]
    right_edge = [50 + 0, 80, 0]
    assert projected_uv(left_edge, low, high, [0, 1]) == projected_uv(right_edge, low, high, [0, 1])
    assert projected_uv(left_edge, low, high, [0, 1]) == (.5, .404)
    assert projected_uv(low, low, high, [0, 1]) == (.02, .02)
    assert projected_uv(high, low, high, [0, 1]) == (.98, .98)


def test_new_model_job_never_launches_blender(tmp_path, monkeypatch):
    model = tmp_path / 'a.pmx'
    model.write_bytes(b'PMX ')
    controller = Controller(tmp_path / 'home')
    monkeypatch.setattr(controller, 'launch_blender', lambda *args: pytest.fail('unexpected Blender character export'))
    job = controller.start(Settings(model=str(model)))
    assert job.data['settings']['character_backend'] == 'mmd2unreal'
    assert controller.exports_ready()
    assert read_json(job.folder / 'manifest.json')['files'] == {}
    controller.resume(job.folder)
    assert controller.exports_ready()


def test_resume_scene_reexports_and_preserves_stop_on_running_exporter(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    scene = tmp_path/'scene.blend'
    scene.write_bytes(b'fixture')
    executable = tmp_path/'blender.exe'
    executable.write_bytes(b'fixture')
    job = Job.create(tmp_path/'jobs', Settings(scene=str(scene), blender=str(executable)))
    atomic_json(job.folder/'manifest.json', {'files': {}})
    (job.folder/'stop').touch()
    def running(folder):
        raise ValueError('旧 Blender 导出进程仍在运行')
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', running)
    controller = Controller(tmp_path/'home')
    with pytest.raises(ValueError, match='仍在运行'):
        controller.resume(job.folder)
    assert (job.folder/'stop').exists()
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', lambda folder: None)
    controller.resume(job.folder)
    assert not (job.folder/'stop').exists()
    assert '重新分析场景' in read_json(job.folder/'status.json')['message']
    calls = []
    monkeypatch.setattr(controller, 'launch_blender', lambda mode: calls.append(mode))
    controller.poll()
    controller.poll()
    assert calls == ['scene']


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    assets, calls = {}, []
    class Asset:
        def __init__(self, path): self.path = path
        def get_path_name(self): return self.path
    class Task:
        def __init__(self): self.values = {'imported_object_paths': []}
        def set_editor_property(self, key, value): self.values[key] = value
        def get_editor_property(self, key): return self.values[key]
    def run(tasks):
        for task in tasks:
            calls.append(task.values)
            path = task.values['destination_path'] + '/Model.Model'
            assets[path] = Asset(path)
            task.values['imported_object_paths'] = [path]
    tools = N(import_asset_tasks=run)
    fake = N(SystemLibrary=N(get_engine_version=lambda:'5.8.2'), find_object=lambda *args:object(),
             AssetToolsHelpers=N(get_asset_tools=lambda:tools), AssetImportTask=Task,
             MMDPmxFactory=object, MMDVmdFactory=object,
             SkeletalMesh=Asset, AnimSequence=Asset, LevelSequence=Asset,
             load_asset=lambda path:assets.get(path), log=lambda message:None,
             EditorAssetLibrary=N(list_assets=lambda root, **kwargs:[p for p in assets if p.startswith(root)],
                                  save_loaded_asset=lambda asset:True))
    monkeypatch.setitem(sys.modules, 'unreal', fake)
    module = importlib.import_module('mmd_bridge.ue_mmd2unreal')
    monkeypatch.setattr(module, 'u', fake)
    model = tmp_path/'a.pmx'; model.write_bytes(b'PMX ')
    job = Job.create(tmp_path/'jobs', Settings(model=str(model)))
    return module, job, tools, assets, calls


def test_interactive_factory_and_receipt_resume(upstream):
    module, job, tools, assets, calls = upstream
    receipt = module.import_sources(job)
    assert calls[0]['filename'] == job.data['settings']['model']
    assert calls[0]['automated'] is False
    assert calls[0]['replace_existing'] is False
    assert 'factory' in calls[0]
    assert receipt['sources']['model']['primary']
    module.import_sources(job)
    assert len(calls) == 1
    assets.clear()
    with pytest.raises(module.UpstreamBlocked, match='缺失'):
        module.import_sources(job)


def test_cancel_can_retry_without_empty_receipt(upstream):
    module, job, tools, assets, calls = upstream
    original = tools.import_asset_tasks
    tools.import_asset_tasks = lambda tasks:None
    with pytest.raises(module.ImportCancelled): module.import_sources(job)
    assert not (job.folder/'mmd2unreal_receipt.json').exists()
    tools.import_asset_tasks = original
    assert module.import_sources(job)['sources']['model']['primary']


def test_unconfirmed_assets_block_overwrite(upstream):
    module, job, tools, assets, calls = upstream
    destination = job.data['settings']['content_root']+'/Upstream_'+job.data['id']+'/Model/user'
    assets[destination] = object()
    with pytest.raises(module.UpstreamBlocked, match='未确认资产'): module.import_sources(job)
    assert calls == []


def test_input_change_cannot_reuse_import_receipt(upstream):
    module, job, tools, assets, calls = upstream
    module.import_sources(job)
    from pathlib import Path
    Path(job.data['settings']['model']).write_bytes(b'PMX changed')
    with pytest.raises(ValueError, match='已改变'): module.import_sources(job)
