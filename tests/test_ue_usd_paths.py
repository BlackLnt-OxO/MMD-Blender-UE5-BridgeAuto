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
