import pytest

from mmd_bridge.material_plan import plan_material, fallback_plan


def static_facts(**changes):
    return dict(fully_adapted=False, features=[], domains=['uv'],
                cache_fields=['Base Color', 'Roughness', 'Normal'], **changes)


@pytest.mark.parametrize('count', [1, 8, 100000])
def test_repetition_changes_resources_without_changing_surface_semantics(count):
    plan = plan_material(static_facts(), repeat_count=count)
    assert plan['route'] == 'pbr_bake'
    assert plan['resources']['repeat_count'] == count
    assert plan['resources']['reuse'] == ('shared_semantic_fields' if count >= 8 else 'content_addressed')


def test_supported_checker_and_closures_do_not_create_bake_maps():
    plan = plan_material({'fully_adapted':True, 'features':['layers'], 'domains':['view']})
    assert plan['route'] == 'nodes'
    assert plan['preserve'] == ['layers']


def test_one_unsupported_field_and_tiled_surface_remain_local_bakes():
    facts = static_facts()
    facts['cache_fields'] = ['Normal']
    assert plan_material(facts)['route'] == 'hybrid'
    plan = plan_material(static_facts(), category='tiled')
    assert plan['route'] == 'hybrid'
    assert plan['layout'] == 'tiled_fields'


@pytest.mark.parametrize('features,domains', [(['layers'],['uv']), ([],['view']),
    (['coat'],['uv']), (['thin_transmission'],['uv']), (['subsurface'],['object'])])
def test_live_and_layered_semantics_precede_whole_bake(features, domains):
    facts = static_facts()
    facts.update(features=features, domains=domains)
    assert plan_material(facts)['route'] == 'hybrid'


def test_wet_ground_never_silently_projects_fresnel_after_timeout():
    plan = plan_material({'features':['layers'], 'domains':['view','uv'], 'cache_fields':['wetness']})
    assert fallback_plan(plan, scope='conversion_timeout')['route'] == 'local_fields'
    assert fallback_plan(plan, local_attempted=True)['route'] == 'blocked'
    explicit = plan_material(plan['facts'], requested='bake')
    assert explicit['route'] == 'pbr_bake'
    assert set(explicit['losses']) == {'layers','view'}


@pytest.mark.parametrize('facts', [dict(data_errors=['Generated mismatch']),
    dict(domains=['time']), dict(domains=['unknown'],blocked_fields=['Script'])])
def test_data_or_dynamic_failures_cannot_be_hidden_by_forced_baking(facts):
    plan = plan_material(facts, requested='bake')
    assert plan['route'] == 'blocked'
    assert fallback_plan(plan)['route'] == 'blocked'


@pytest.mark.parametrize('feature', ['thin_transmission', 'refraction'])
def test_transmission_fallback_preserves_cutout_coverage(feature):
    plan = plan_material({'features':[feature], 'coverage':True, 'domains':['uv']})
    assert fallback_plan(plan)['route'] == 'local_fields'
    fallback = fallback_plan(plan, local_attempted=True)
    assert fallback['route'] == 'pbr_bake'
    assert fallback['opaque_fallback'] and fallback['preserve_coverage']


def test_strict_nodes_do_not_activate_automatic_bakes():
    plan = plan_material(static_facts(), requested='nodes')
    assert plan['route'] == 'nodes'
    assert fallback_plan(plan)['route'] == 'blocked'


def test_unsupported_ray_mix_is_only_projected_by_explicit_transmission_degradation():
    facts={'domains':['ray'], 'blocked_fields':['mix'], 'features':['refraction']}
    plan=plan_material(facts)
    assert plan['route']=='hybrid'
    step=fallback_plan(plan,local_attempted=True)
    assert step['opaque_fallback'] and step['project_view_mix']
    facts['features']=[]
    assert plan_material(facts)['route']=='blocked'
