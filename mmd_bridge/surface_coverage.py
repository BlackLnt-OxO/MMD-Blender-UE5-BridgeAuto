"""Scalar coverage algebra shared by shader traversal and regression checks."""


def constant(value):
    return {'op': 'constant', 'value': float(value)}


def number(expr):
    value = expr.get('value')
    return float(value) if expr.get('op') == 'constant' and isinstance(value, (int, float)) else None


def scalar_math(mode, a, b):
    av, bv = number(a), number(b)
    if av is not None and bv is not None:
        return constant({'ADD': lambda: av + bv, 'SUBTRACT': lambda: av - bv,
                         'MULTIPLY': lambda: av * bv, 'MINIMUM': lambda: min(av, bv),
                         'MAXIMUM': lambda: max(av, bv)}[mode]())
    if mode == 'MULTIPLY':
        if av == 0 or bv == 0: return constant(0)
        if av == 1: return b
        if bv == 1: return a
    if mode == 'ADD':
        if av == 0: return b
        if bv == 0: return a
    return {'op': 'math', 'mode': mode, 'a': a, 'b': b, 'clamp': False}


def saturate(expr):
    return scalar_math('MINIMUM', scalar_math('MAXIMUM', expr, constant(0)), constant(1))


def blend(a, b, factor):
    if a == b: return a
    factor = saturate(factor)
    return scalar_math('ADD', scalar_math('MULTIPLY', a,
        scalar_math('SUBTRACT', constant(1), factor)), scalar_math('MULTIPLY', b, factor))


def mix_masks(a, b, factor):
    """Coverage is independent of the surviving surface's thin transmission."""
    opacity = blend(a['opacity'], b['opacity'], factor)
    if number(a['opacity']) == 0:
        transmission = b['transmission']
    elif number(b['opacity']) == 0:
        transmission = a['transmission']
    elif a['transmission'] == b['transmission']:
        transmission = a['transmission']
    else:
        # Conditional scattering of the covered portion, not the empty pixels.
        numerator = blend(scalar_math('MULTIPLY', a['opacity'], a['transmission']),
                          scalar_math('MULTIPLY', b['opacity'], b['transmission']), factor)
        transmission = numerator if number(opacity) == 1 else {
            'op': 'math', 'mode': 'DIVIDE', 'a': numerator,
            'b': scalar_math('MAXIMUM', opacity, constant(1e-6)), 'clamp': True}
    return {'opacity': opacity, 'transmission': transmission,
            'has_transparent': a['has_transparent'] or b['has_transparent'],
            'has_translucent': a['has_translucent'] or b['has_translucent']}
