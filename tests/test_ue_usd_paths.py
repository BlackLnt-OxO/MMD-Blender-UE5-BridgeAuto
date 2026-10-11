import importlib
import sys
import types


def test_reopened_stage_finds_meshes_below_transform_components(monkeypatch):
    class Mesh:
        def get_path_name(self):
            return '/Game/Task/Scene/Generated/Tree'
    class MeshComponent:
        def get_editor_property(self, name):
            assert name == 'static_mesh'
            return mesh
    mesh = Mesh()
    child = MeshComponent()
    root = types.SimpleNamespace(get_children_components=lambda recursive: [child])
    native = types.SimpleNamespace(StaticMesh=Mesh, StaticMeshComponent=MeshComponent)
    monkeypatch.setitem(sys.modules, 'unreal', native)
    usd = importlib.import_module('mmd_bridge.ue_usd')
    monkeypatch.setattr(usd, 'u', native)
    actor = types.SimpleNamespace(get_generated_assets=lambda path: [],
        get_generated_component=lambda path: root if path == '/World/Tree' else None)
    assets, by_path = usd._generated_assets(actor, {'prototypes': [{'prim_path': '/Definitions/Tree'}],
        'instances': [{'prim_path': '/World/Tree'}, {'prim_path': '/World/Tree'}]})
    assert assets == [mesh]
    assert by_path['/World/Tree'] == [mesh]


def test_usd_package_stays_in_content_root(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    from mmd_bridge.core import file_hash
    ue_usd = importlib.import_module("mmd_bridge.ue_usd")
    source = tmp_path / "source" / "Scene.usda"
    source.parent.mkdir()
    source.write_text("#usda 1.0\n", encoding="utf-8")
    scene = {"usd": {"files": [{"path": str(source), "relative": "Scene.usda",
                                 "sha256": file_hash(source)}]}}
    root, _, _ = ue_usd.deploy_package(scene, tmp_path / "project", "Scene_street_01",
                                       "/Game/TEST_ON_927/Street")
    assert root == (tmp_path / "project" / "Content" / "TEST_ON_927" / "Street" /
                    "A_Scene_street_01" / "Source" / "Scene" / "Scene.usda")


def test_source_named_scene_namespace_deploys_without_legacy_prefix(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'unreal', types.SimpleNamespace())
    from mmd_bridge.core import file_hash, ue_asset_folder, ue_asset_root
    ue_usd = importlib.import_module('mmd_bridge.ue_usd')
    source = tmp_path / 'Scene.usda'; source.write_text('#usda 1.0\n', encoding='utf-8')
    data = {'asset_id': '港口_01', 'asset_folder': '港口_01',
            'settings': {'content_root': '/Game/Scenes'}}
    scene = {'usd': {'files': [{'path': str(source), 'relative': 'Scene.usda',
                              'sha256': file_hash(source)}]}}
    root, _, _ = ue_usd.deploy_package(scene, tmp_path / 'project', data['asset_id'],
                                       '/Game/Scenes', ue_asset_folder(data))
    assert root == tmp_path / 'project/Content/Scenes/港口_01/Source/Scene/Scene.usda'
    assert ue_asset_root(data) == '/Game/Scenes/港口_01'
    assert ue_asset_root({'asset_id': 'Scene_old_01_hash', 'settings': {'content_root': '/Game/Old'}}) == '/Game/Old/A_Scene_old_01_hash'


def test_resume_reuses_identical_loaded_payload_and_preserves_user_layer(tmp_path, monkeypatch):
    from mmd_bridge.core import file_hash
    monkeypatch.setitem(sys.modules, 'unreal', types.SimpleNamespace())
    usd = importlib.import_module('mmd_bridge.ue_usd')
    source = tmp_path / 'source'; source.mkdir()
    files = []
    for relative, body in [('Scene.usda', b'#usda 1.0\n'), ('Geometry/Payload_01.usdc', b'geometry')]:
        path = source / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(body)
        files.append(dict(path=str(path), relative=relative, sha256=file_hash(path)))
    scene = {'usd': {'files': files}}
    root, copied, _ = usd.deploy_package(scene, tmp_path / 'project', 'Scene_railway_01', '/Game/Scenes')
    root.write_text('#usda 1.0\n# user override\n', encoding='utf-8')
    def locked_copy(*args, **kwargs):
        raise PermissionError('loaded USD payload denies write access')
    monkeypatch.setattr(usd.shutil, 'copy2', locked_copy)
    _, copied, preserved = usd.deploy_package(scene, tmp_path / 'project', 'Scene_railway_01', '/Game/Scenes')
    assert copied == [] and preserved and 'user override' in root.read_text(encoding='utf-8')
    # A changed source with a fresh export hash still requires deployment.
    payload = source / 'Geometry/Payload_01.usdc'; payload.write_bytes(b'new geometry')
    files[1]['sha256'] = file_hash(payload)
    import pytest
    with pytest.raises(PermissionError):
        usd.deploy_package(scene, tmp_path / 'project', 'Scene_railway_01', '/Game/Scenes')
