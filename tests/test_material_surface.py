import copy
import pytest
from mmd_bridge.material_surface import foliage_atlas_opacity, repair_foliage_atlas, surface_override, selected_surface_mode
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


@pytest.mark.parametrize('facts,expected', [
    ({'blend_mode':'BLEND_OPAQUE'}, 'opaque'),
    ({'blend_mode':'BLEND_MASKED'}, 'masked'),
    ({'blend_mode':'BLEND_MASKED', 'thin_scattering':'legacy_diffusion'}, 'thin'),
    ({'blend_mode':'BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE'}, 'transmission'),
    ({}, 'opaque'),
])
def test_default_surface_mode_uses_current_material(facts, expected):
    rows = [{'facts':facts}, {'facts':facts}, {'reason':'unknown', 'facts':{'blend_mode':'BLEND_OPAQUE'}}]
    assert selected_surface_mode(rows) == expected


def test_mixed_selection_defaults_to_opaque():
    assert selected_surface_mode([{'facts':{'blend_mode':'BLEND_MASKED'}},
                                  {'facts':{'blend_mode':'BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE'}}]) == 'opaque'


def test_manual_glass_preserves_closure_and_separates_geometry_profiles():
    record = {'id':'shared_glass', 'surface':'glass', 'surface_policy':None, 'channels':{
        'ior':{'op':'constant','value':1.7}, 'roughness':{'op':'constant','value':.4}}}
    before = copy.deepcopy(record)
    thin = surface_override(record, 'transmission', geometry={'planar_pane':True})
    thick = surface_override(record, 'transmission', geometry={'planar_pane':False})
    assert thin['surface_graph']['kind'] == thick['surface_graph']['kind'] == 'bsdf_glass'
    assert thin['surface_graph']['channels']['ior'] == record['channels']['ior']
    assert thin['surface_graph']['channels']['roughness'] == record['channels']['roughness']
    assert thin['id'] != thick['id'] and record == before


@pytest.mark.parametrize('kind', ['bsdf_refraction','bsdf_transparent'])
def test_new_transmission_closures_can_be_forced_opaque_without_source_changes(kind):
    record = {'id':'new_closure','surface':'pbr','channels':{'opacity':{'op':'constant','value':1}},
              'surface_graph':{'kind':kind,'channels':{'base_color':{'op':'constant','value':[.4,.7,1,1]}}}}
    before = copy.deepcopy(record)
    changed = surface_override(record,'opaque')
    assert record == before
    assert not set(changed['closure_features']) & {'transmission','straight_transmission'}
