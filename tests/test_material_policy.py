from mmd_bridge.material_policy import texture_role, cutout_transmission, glass_refraction_mode


def test_translucent_support_does_not_reclassify_existing_opaque_or_glass_materials():
    from mmd_bridge.material_policy import masked_surface
    assert not masked_surface({'channels': {'opacity': {'op': 'constant', 'value': 1}}})
    assert not masked_surface({'surface': 'glass', 'channels': {'opacity': {'op': 'constant', 'value': 1}}})
    assert masked_surface({'channels': {'opacity': {'op': 'texture'}}})
    assert masked_surface({'legacy_thin_sheet': True, 'channels': {'opacity': {'op': 'constant', 'value': 1}}})


def test_satisfied_mygo_main_materials_keep_their_original_mask_decisions():
    from pathlib import Path
    import pytest
    from mmd_bridge.core import read_json
    from mmd_bridge.material_policy import masked_surface
    folder = Path(__file__).resolve().parents[1] / 'work/mygo_program/main_clean_20261007_01/Transfer/MMDBridge_Jobs/Scene_It_sMyGO_ParkingLots_01'
    if not (folder / 'manifest.json').is_file():
        pytest.skip('local source-scene artifact is unavailable')
    records = read_json(folder / 'manifest.json')['scene']['materials']
    protected = [r for r in records if r['conversion_status'] in ('converted', 'approximated')]
    assert len(protected) == 186
    for record in protected:
        opacity = record.get('channels', {}).get('opacity')
        old = bool(opacity and (opacity.get('op') != 'constant' or opacity.get('value', 1) < 1))
        assert masked_surface(record) == old, record['name']
        assert not any(n['type'] == 'ShaderNodeBsdfTranslucent'
                       for n in record.get('material_decision', {}).get('nodes', []))


def test_thin_sheet_detection_uses_effective_closure_not_names_or_unreachable_nodes():
    from mmd_bridge.material_contract import has_translucent_closure
    assert has_translucent_closure({'kind': 'bsdf_translucent'})
    assert has_translucent_closure({'kind': 'mix', 'a': {'kind': 'bsdf_principled'},
                                   'b': {'kind': 'bsdf_translucent'}})
    assert not has_translucent_closure({'kind': 'bsdf_principled', 'name': 'leaf'})
    assert not has_translucent_closure({'kind': 'bsdf_glass'})


def test_new_uniform_coverage_translucent_sheet_keeps_masked_not_glass_reuse():
    record = {'surface': 'pbr', 'legacy_thin_sheet': True, 'thin_surface_transmission': True,
              'channels': {'opacity': {'op': 'constant', 'value': 1},
                           'transmission': {'op': 'constant', 'value': 1}}}
    assert cutout_transmission(record)
    assert not cutout_transmission(dict(record, legacy_thin_sheet=False))


def test_automatic_routes_use_semantics_and_actual_repetition():
    from mmd_bridge.material_policy import scene_material_route
    assert scene_material_route(simple=True,repeat_count=1)['route']=='nodes'
    assert scene_material_route(simple=False,repeat_count=8)['route']=='pbr_bake'
    assert scene_material_route(simple=False,repeat_count=8)['resources']['reuse']=='shared_semantic_fields'
    assert scene_material_route(simple=False,repeat_count=7)['route']=='pbr_bake'
    assert scene_material_route(simple=False,category='chunked')['layout']=='chunked_object_atlas'
    assert scene_material_route(simple=False,transmitting=True)['route']=='hybrid'


def test_large_flat_surfaces_preserve_tiling_before_deciding_atlas_size():
    from mmd_bridge.material_policy import classify_surface
    assert classify_surface([200,200,.01],planar=True)['bake_category']=='tiled'
    assert classify_surface([1,2,3])['bake_category']=='small'


def test_texture_role_uses_channel_semantics_before_color_space():
    srgb = {"color_space": "sRGB"}
    assert texture_role(srgb) == "srgb"
    assert texture_role(srgb, data=True) == "mask"
    assert texture_role(srgb, normal=True, data=True) == "normal"
    assert texture_role({"color_space": "Non-Color"}) == "linear_color"


def test_transmission_does_not_discard_independent_coverage():
    record = {'surface':'pbr','surface_policy':{'size_m':[1,1,.04]},
              'channels':{'opacity':{'op':'texture'},'transmission':{'op':'texture'}}}
    assert cutout_transmission(record)
    assert not cutout_transmission(dict(record, surface='glass'))
    assert not cutout_transmission(dict(record, water=True))
    assert not cutout_transmission(dict(record, surface_policy={'size_m':[1,1,1]}))
    assert not cutout_transmission(dict(record, surface_policy=None))
    assert not cutout_transmission({'surface':'pbr','channels':{'opacity':{'op':'constant','value':1},'transmission':{'op':'texture'}}})
    assert not cutout_transmission({'surface':'pbr','channels':{'opacity':{'op':'texture'},'transmission':{'op':'constant','value':0}}})


def test_flat_pane_and_curved_glass_keep_distinct_optics():
    assert glass_refraction_mode({'surface_policy':{'size_m':[3,.03,1.2]}}) == 'pixel_normal_offset'
    assert glass_refraction_mode({'surface_policy':{'size_m':[1,1,1]}}) == 'index_of_refraction'
    assert glass_refraction_mode({'surface_policy':{'size_m':[.1,.001,.2]}}) == 'index_of_refraction'
    assert glass_refraction_mode({}) == 'index_of_refraction'


def test_combined_tree_bounds_do_not_turn_thin_bsdf_into_glass():
    record={'surface':'pbr','surface_policy':{'size_m':[10,12,18]},
            'thin_surface_transmission':True,
            'channels':{'opacity':{'op':'texture'},'transmission':{'op':'constant','value':.3}}}
    assert cutout_transmission(record)
    assert not cutout_transmission(dict(record,thin_surface_transmission=False))


def test_open_mesh_components_preserve_cutout_scattering_with_volumetric_bounds():
    record = {'name': 'arbitrary_name', 'surface': 'pbr',
              'surface_policy': {'size_m': [3, 4, 5],
                                 'mesh_surface': {'thin_open_surface': True}},
              'channels': {'opacity': {'op': 'texture'}, 'transmission': {'op': 'texture'}}}
    assert cutout_transmission(record)
    assert cutout_transmission(dict(record, name='renamed'))
    assert not cutout_transmission(dict(record, surface='glass'))
    assert not cutout_transmission(dict(record, water=True))
    assert not cutout_transmission(dict(record, surface_policy={'size_m': [3, 4, 5],
                                    'mesh_surface': {'thin_open_surface': False}}))
