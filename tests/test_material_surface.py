import copy
import pytest
from mmd_bridge.material_surface import foliage_atlas_opacity, repair_foliage_atlas, surface_override
from mmd_bridge.material_contract import closure_features
from mmd_bridge.material_policy import masked_surface


def atlas():
    def tex(role):
        return {'op': 'texture', 'image': {'id': role, 'name': 'plant_2K_' + role + '.jpg'},
                'output': 'Color', 'uv': {'op': 'uv', 'index': 0}}
    c = {'base_color': tex('Albedo'), 'transmission': tex('Translucency'),
         'emission_strength': tex('Opacity'), 'emission': {'op': 'constant', 'value': [0, 0, 0, 1]},
         'opacity': {'op': 'constant', 'value': 1}, 'ior': {'op': 'constant', 'value': 1.45}}
    return {'id': 'plant', 'asset_name': 'M_Plant', 'surface': 'pbr', 'channels': c,
            'surface_graph': {'kind': 'bsdf_principled', 'channels': copy.deepcopy(c)},
            'strategy_reasons': [], 'surface_policy': {'mesh_surface': {'thin_open_surface': True}}}


def test_paired_atlas_repairs_coverage_and_scattering_without_mutating_source():
    original = atlas(); before = copy.deepcopy(original)
    fixed = repair_foliage_atlas(original)
    assert original == before
    assert fixed['id'] == original['id']
    assert masked_surface(fixed)
    assert 'transmission' not in closure_features(fixed['surface_graph'])
    assert fixed['channels']['opacity'] == original['channels']['emission_strength']
    assert fixed['surface_graph']['b']['kind'] == 'bsdf_translucent'


@pytest.mark.parametrize('change', ['closed', 'glass', 'emissive', 'mismatched', 'missing', 'layers'])
def test_other_surfaces_are_not_automatically_reclassified(change):
    r = atlas()
    if change == 'closed': r['surface_policy']['mesh_surface']['thin_open_surface'] = False
    if change == 'glass': r['surface_graph']['kind'] = 'bsdf_glass'
    if change == 'emissive': r['channels']['emission']['value'] = [1, 0, 0]
    if change == 'mismatched': r['channels']['transmission']['image']['name'] = 'other_Translucency.jpg'
    if change == 'missing': del r['channels']['emission_strength']
    if change == 'layers': r['surface_graph'] = {'kind': 'mix', 'a': r['surface_graph'], 'b': {'kind': 'bsdf_glass', 'channels': {}}}
    assert repair_foliage_atlas(r) is r


def test_manual_masked_removes_refraction_but_opaque_does_not_require_a_mask():
    r = atlas(); before = copy.deepcopy(r)
    masked = surface_override(r, 'masked')
    assert masked_surface(masked) and masked['id'] != r['id']
    assert 'transmission' not in masked['closure_features']
    assert r == before
    r['channels'].pop('emission_strength')
    with pytest.raises(ValueError, match='遮罩'): surface_override(r, 'masked')
    assert surface_override(r, 'opaque')['channels']['opacity']['value'] == 1


def test_manual_ambiguous_opacity_maps_are_rejected():
    r = atlas()
    r['channels']['other'] = {'op': 'texture', 'image': {'id': 'other', 'name': 'other_Opacity.png'}}
    with pytest.raises(ValueError, match='唯一'): surface_override(r, 'masked')


def test_forced_transmission_is_active_even_when_source_weight_is_zero():
    r = atlas()
    r['surface_graph']['channels']['transmission'] = {'op': 'constant', 'value': 0}
    assert 'transmission' in surface_override(r, 'transmission')['closure_features']
