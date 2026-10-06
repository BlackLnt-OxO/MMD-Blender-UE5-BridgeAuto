import pytest
from mmd_bridge.surface_coverage import constant, mix_masks


def evaluate(expr, alpha):
    if expr['op'] == 'constant': return expr['value']
    if expr['op'] == 'texture': return alpha
    a, b = (evaluate(expr[k], alpha) for k in ('a', 'b'))
    result = {'ADD': lambda: a+b, 'SUBTRACT': lambda: a-b, 'MULTIPLY': lambda: a*b,
              'MINIMUM': lambda: min(a,b), 'MAXIMUM': lambda: max(a,b),
              'DIVIDE': lambda: a/b}[expr['mode']]()
    return min(1,max(0,result)) if expr.get('clamp') else result


def masks(alpha, transmission=0, transparent=False, translucent=False):
    return {'opacity':constant(alpha),'transmission':constant(transmission),
            'has_transparent':transparent,'has_translucent':translucent}


@pytest.mark.parametrize('swapped', [False, True])
@pytest.mark.parametrize('alpha', [0, .25, 1, -1, 2])
def test_cutout_background_and_thin_transmission_are_independent(swapped, alpha):
    a,b = masks(0,transparent=True),masks(1,.35,translucent=True)
    if swapped: a,b = b,a
    result=mix_masks(a,b,{'op':'texture'})
    expected=min(1,max(0,alpha))
    assert evaluate(result['opacity'],alpha) == pytest.approx(1-expected if swapped else expected)
    assert evaluate(result['transmission'],alpha) == pytest.approx(.35)


def test_nested_shader_alpha_multiplies_outer_coverage():
    leaf=mix_masks(masks(0,transparent=True),masks(.6,.4),{'op':'texture'})
    result=mix_masks(masks(0,transparent=True),leaf,constant(.5))
    assert evaluate(result['opacity'],.5) == pytest.approx(.15)
    assert evaluate(result['transmission'],.5) == pytest.approx(.4)


def test_scattering_is_conditioned_on_surviving_coverage():
    result=mix_masks(masks(.5,1,translucent=True),masks(1,0),constant(.5))
    assert evaluate(result['opacity'],0) == pytest.approx(.75)
    assert evaluate(result['transmission'],0) == pytest.approx(1/3)
