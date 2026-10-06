import importlib
import sys
import types


def test_collision_respects_per_prototype_opt_out(monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    usd = importlib.import_module("mmd_bridge.ue_usd")

    class Mesh:
        def __init__(self, name):
            self.name = name
            self.body_properties = {"never_needs_cooked_collision_data": True,
                                    "collision_trace_flag": "default"}
            self.body = types.SimpleNamespace(
                get_editor_property=lambda key: self.body_properties[key],
                set_editor_property=lambda key, value: self.body_properties.__setitem__(key, value))

        def get_path_name(self):
            return self.name

        def get_editor_property(self, key):
            assert key == "body_setup"
            return self.body

    class Component:
        def __init__(self, mesh):
            self.mesh = mesh
            self.enabled = None
            self.profile = None

        def get_editor_property(self, key):
            assert key == "static_mesh"
            return self.mesh

        def set_collision_enabled(self, value):
            self.enabled = value

        def set_collision_profile_name(self, value):
            self.profile = value

    solid, excluded = Mesh("solid"), Mesh("excluded")
    solid_component, excluded_component = Component(solid), Component(excluded)
    components = {"/solid/Mesh": solid_component, "/excluded/Mesh": excluded_component}
    saved = []
    fake_unreal = types.SimpleNamespace(
        StaticMesh=Mesh, StaticMeshComponent=Component,
        CollisionTraceFlag=types.SimpleNamespace(CTF_USE_COMPLEX_AS_SIMPLE="complex"),
        CollisionEnabled=types.SimpleNamespace(QUERY_AND_PHYSICS="enabled", NO_COLLISION="disabled"),
        EditorAssetLibrary=types.SimpleNamespace(save_loaded_asset=lambda mesh: saved.append(mesh.name) or True),
    )
    monkeypatch.setattr(usd, "u", fake_unreal)
    actor = types.SimpleNamespace(get_generated_component=lambda path: components.get(path))
    scene = {"prototypes": [
        {"id": "a", "prim_path": "/proto_a", "collision": True},
        {"id": "b", "prim_path": "/proto_b", "collision": False},
    ], "instances": [
        {"prototype_id": "a", "prim_path": "/solid"},
        {"prototype_id": "b", "prim_path": "/excluded"},
    ]}
    paths = {"/solid": [solid], "/excluded": [excluded]}
    report = {"complex_collision_assets": 0}
    usd.configure_stage_collision(actor, scene, paths, report, lambda *args: None)

    assert saved == ["solid"]
    assert report["complex_collision_assets"] == report["collision_components"] == 1
    assert solid.body_properties == {"never_needs_cooked_collision_data": False,
                                     "collision_trace_flag": "complex"}
    assert solid_component.enabled == "enabled" and solid_component.profile == "BlockAll"
    assert excluded_component.enabled == "disabled"


def test_generated_assets_uses_mesh_child_when_parent_only_returns_material(monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    usd = importlib.import_module("mmd_bridge.ue_usd")

    class Mesh:
        def get_path_name(self):
            return "/Game/Mesh"

    mesh = Mesh()
    monkeypatch.setattr(usd.u, "StaticMesh", Mesh, raising=False)
    actor = types.SimpleNamespace(get_generated_assets=lambda path: [mesh] if path == "/item/Mesh" else [object()])
    assets, by_path = usd._generated_assets(actor, {"prototypes": [], "instances": [{"prim_path": "/item"}]})
    assert assets == [mesh] and by_path["/item"] == [mesh]


def test_generated_assets_uses_live_component_when_usd_asset_links_are_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    usd = importlib.import_module("mmd_bridge.ue_usd")

    class Mesh:
        def get_path_name(self):
            return "/Game/Generated"

    class Component:
        def get_editor_property(self, key):
            assert key == "static_mesh"
            return mesh

    mesh = Mesh()
    monkeypatch.setattr(usd, "u", types.SimpleNamespace(StaticMesh=Mesh, StaticMeshComponent=Component))
    actor = types.SimpleNamespace(
        get_generated_assets=lambda path: [],
        get_generated_component=lambda path: Component() if path == "/item/Mesh" else None)
    assets, by_path = usd._generated_assets(actor, {"prototypes": [], "instances": [{"prim_path": "/item"}]})
    assert assets == [mesh] and by_path["/item"] == [mesh]
