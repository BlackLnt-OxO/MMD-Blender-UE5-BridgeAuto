from mmd_bridge.material_contract import closure_features, has_translucent_closure, closure_refraction


def test_inactive_transmission_and_scattering_do_not_change_surface_classification():
    opaque = {'kind':'bsdf_diffuse', 'channels':{}}
    glass = {'kind':'bsdf_glass','channels':{'ior':{'op':'constant','value':1.45}}}
    leaf = {'kind':'bsdf_translucent','channels':{}}
    for unused in (glass, leaf):
        graph = {'kind':'mix','a':opaque,'b':unused,'factor':{'op':'constant','value':0}}
        assert not closure_features(graph) and not has_translucent_closure(graph)
        assert closure_refraction(graph) is None
    assert closure_features({'kind':'mix','a':opaque,'b':glass,'factor':{'op':'constant','value':1}})=={'transmission'}


def test_dynamic_mixtures_keep_both_semantics_and_source_ior():
    glass = {'kind':'bsdf_glass','channels':{'ior':{'op':'constant','value':1.45}}}
    graph = {'kind':'mix','a':glass,'b':{'kind':'bsdf_translucent','channels':{}},'factor':{'op':'texture'}}
    assert closure_features(graph)=={'transmission'} and has_translucent_closure(graph)
    assert closure_refraction(graph)==glass['channels']['ior']


def test_straight_transparency_is_separate_from_refracting_transmission():
    graph = {'kind':'bsdf_transparent','channels':{'base_color':{'op':'constant','value':[.2,.5,1,1]}}}
    assert closure_features(graph)=={'straight_transmission'}
    assert closure_refraction(graph) is None and not has_translucent_closure(graph)


def test_nested_holes_are_factored_out_without_double_attenuating_surviving_bsdf():
    from mmd_bridge.surface_coverage import project_coverage
    leaf = {'kind':'bsdf_diffuse','channels':{'base_color':{'op':'constant','value':[.2,.4,.1,1]}}}
    hole = {'kind':'bsdf_transparent','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}}}
    graph = {'kind':'mix','a':leaf,'b':hole,'factor':{'op':'constant','value':.25}}
    nested = {'kind':'mix','a':hole,'b':graph,'factor':{'op':'constant','value':.5}}
    projected, coverage = project_coverage(nested)
    assert projected == leaf and coverage == {'op':'constant','value':.375}
    assert graph['b'] == hole


def test_partial_alpha_mixture_preserves_conditional_surface_weights():
    from mmd_bridge.surface_coverage import project_coverage
    a = {'kind':'bsdf_principled','channels':{'opacity':{'op':'constant','value':.2}}}
    b = {'kind':'bsdf_diffuse','channels':{}}
    projected, coverage = project_coverage({'kind':'mix','a':a,'b':b,'factor':{'op':'constant','value':.5}})
    assert coverage['value'] == .6
    assert projected['factor']['a']['value'] == .5 and projected['factor']['b']['value'] == .6
    assert projected['a']['channels']['opacity']['value'] == 1
    assert a['channels']['opacity']['value'] == .2


def test_inactive_invalid_transparent_add_is_not_traversed():
    from mmd_bridge.surface_coverage import project_coverage
    opaque = {'kind':'bsdf_diffuse','channels':{}}
    hole = {'kind':'bsdf_transparent','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}}}
    invalid = {'kind':'add','a':opaque,'b':hole}
    projected, coverage = project_coverage({'kind':'mix','a':opaque,'b':invalid,'factor':{'op':'constant','value':0}})
    assert projected == opaque and coverage['value'] == 1


def test_active_transparent_add_reports_unrepresentable_coverage():
    import pytest
    from mmd_bridge.surface_coverage import project_coverage
    hole = {'kind':'bsdf_transparent','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}}}
    with pytest.raises(ValueError, match='Add Shader'):
        project_coverage({'kind':'add','a':{'kind':'bsdf_diffuse','channels':{}},'b':hole})
