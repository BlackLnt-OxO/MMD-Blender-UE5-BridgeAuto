from pathlib import Path
import pytest
from mmd_bridge.bake_cache import cleanup_scene_bakes, material_texture_paths


def scene_fixture(tmp_path):
    root = tmp_path/'Scene'; root.mkdir()
    (root/'Scene.usda').write_text('#usda 1.0')
    textures = root/'Textures'; textures.mkdir()
    used = textures/'T_Bake_field_current_01.exr'; used.write_bytes(b'current')
    old = textures/'Bake_field_previous_01.exr'; old.write_bytes(b'old bake')
    source = textures/'user_texture.exr'; source.write_bytes(b'user source')
    expression = {'op':'hsv', 'color':{'op':'texture', 'image':{'id':'current','path':str(used),'color_space':'Non-Color'}}}
    return {'materials':[{'channels':{'base_color':expression}}],
            'usd':{'root_layer':str(root/'Scene.usda'), 'files':[{'path':str(used)}]}}, used, old, source


def test_cleanup_keeps_nested_graph_references_and_source_files(tmp_path):
    scene, used, old, source = scene_fixture(tmp_path)
    assert material_texture_paths(scene['materials']) == {used.resolve()}
    result = cleanup_scene_bakes(scene)
    assert used.exists() and source.exists() and not old.exists()
    assert result['removed_bytes'] == len(b'old bake')
    assert cleanup_scene_bakes(scene)['removed_files'] == 0


def test_incomplete_references_prevent_cleanup(tmp_path):
    scene, used, old, source = scene_fixture(tmp_path)
    used.unlink()
    with pytest.raises(ValueError, match='引用不完整'):
        cleanup_scene_bakes(scene)
    assert old.exists() and source.exists()


def test_constant_only_scene_can_remove_old_bakes(tmp_path):
    scene, used, old, source = scene_fixture(tmp_path)
    scene['materials'] = [{'channels':{'base_color':{'op':'constant','value':[1,1,1]}}}]
    scene['usd']['files'] = []
    result = cleanup_scene_bakes(scene)
    assert result['removed_files'] == 2 and source.exists()
