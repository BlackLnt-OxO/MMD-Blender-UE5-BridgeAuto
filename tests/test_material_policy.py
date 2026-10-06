from mmd_bridge.material_policy import texture_role, cutout_transmission, glass_refraction_mode


def test_automatic_routes_use_semantics_and_actual_repetition():
    from mmd_bridge.material_policy import scene_material_route
    assert scene_material_route(simple=True,repeat_count=1)['route']=='nodes'
    assert scene_material_route(simple=False,repeat_count=8)['route']=='hybrid'
    assert scene_material_route(simple=False,repeat_count=7)['route']=='pbr_bake'
    assert scene_material_route(simple=False,category='chunked')['layout']=='chunked_object_atlas'
    assert scene_material_route(simple=False,transmitting=True)['route']=='hybrid'


def test_large_flat_complex_surfaces_have_chunked_bake_category():
    from mmd_bridge.material_policy import classify_surface
    assert classify_surface([200,200,.01],planar=True)['bake_category']=='chunked'
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
