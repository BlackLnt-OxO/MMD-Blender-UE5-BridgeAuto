import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.character_pbr import ALPHA_GRAPH_VERSION, plan


def test_new_plans_version_the_alpha_graph_without_changing_the_requested_blend():
    record = {'name': 'Skirt', 'pmx_index': 0, 'mmd': {'diffuse_alpha': .8}, 'textures': {},
              'character_material': {'alpha_mode': 'translucent'}}
    for blend in ('opaque', 'masked', 'translucent'):
        cfg = {'character_material_overrides': {'model': {'0': {'alpha_mode': blend}}}}
        prepared = plan(record, cfg, 'model')
        assert prepared['alpha_graph_version'] == ALPHA_GRAPH_VERSION == 1
        assert prepared['alpha_mode'] == blend
        assert prepared['params']['Opacity'] == .8
    assert 'alpha_graph_version' not in record['character_material']


def test_coverage_uses_the_final_mask_input_while_sss_uses_substrate_opacity():
    path = Path(__file__).parents[1]/'mmd_bridge/ue_character_pbr.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_property')
    calls = []
    shader = SimpleNamespace(get_editor_property=lambda name: 'cloth')
    material = object()
    builder = SimpleNamespace(material=material, _pbr_shader=(shader, ''),
        connect=lambda *args: calls.append(('shader', *args)),
        lib=SimpleNamespace(connect_material_property=lambda *args: calls.append(('root', *args)) or True))
    native = SimpleNamespace(MaterialProperty=SimpleNamespace(MP_OPACITY_MASK='root_mask'),
                             MaterialShadingModel=SimpleNamespace(MSM_CLOTH='cloth'))
    scope = {'u': native}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
    source = (object(), 'A')
    scope['_property'](builder, source, 'MP_OPACITY_MASK')
    assert calls == [('root', source[0], 'A', 'root_mask')]
    scope['_property'](builder, source, 'MP_OPACITY')
    assert calls[-1] == ('shader', source, (shader, ''), 'Opacity')


@pytest.mark.parametrize('blend', ['opaque', 'masked', 'translucent'])
def test_alpha_revision_changes_the_parent_path_and_binding_without_migrating_old_records(blend):
    from tests.test_skin_surface_versioned import functions
    scope = functions()
    loaded, verified = [], []
    asset = object()
    scope['u'] = SimpleNamespace(load_asset=lambda path: loaded.append(path) or asset,
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda *args: 'owner'))
    manager = SimpleNamespace(root='/Game/Fixture', owner='owner', _created_this_pass=set(),
                              verify_asset=verified.append)
    old = {'character_material': {'pbr': {'version': 9, 'shading_version': 11, 'defaults_version': 6,
        'type': 'cloth', 'alpha_mode': blend}}, 'mmd': {'two_sided': False}, 'textures': {}}
    import copy
    new = copy.deepcopy(old)
    new['character_material']['pbr']['alpha_graph_version'] = ALPHA_GRAPH_VERSION
    assert scope['master'](manager, old) is asset
    assert scope['master'](manager, new) is asset
    base = f'/Game/Fixture/Materials/M_CharacterPBR_cloth_{blend}_One_v9_S11_D6'
    assert loaded == [base, base+'_AM1']
    assert verified == [asset, asset]
    assert scope['signature'](old) != scope['signature'](new)
    assert 'alpha_graph_version' not in old['character_material']['pbr']
