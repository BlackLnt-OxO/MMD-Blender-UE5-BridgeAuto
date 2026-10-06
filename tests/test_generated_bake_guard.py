from types import SimpleNamespace

from mmd_bridge.scene_shader_data import pin_generated, has_generated_layers


def test_missing_original_generated_carrier_does_not_rewrite_source_graph():
    untouched=object()
    tree=SimpleNamespace(nodes=[untouched])
    mesh=SimpleNamespace(uv_layers={'MMDBridge_Generated_XY':object()})
    pin_generated(tree,mesh)
    assert tree.nodes==[untouched]


def test_both_generated_carriers_are_required_for_restore():
    assert not has_generated_layers(SimpleNamespace(uv_layers={}))
    assert not has_generated_layers(SimpleNamespace(uv_layers={'MMDBridge_Generated_XY': object()}))
    assert has_generated_layers(SimpleNamespace(uv_layers={
        'MMDBridge_Generated_XY': object(), 'MMDBridge_Generated_ZW': object()}))
