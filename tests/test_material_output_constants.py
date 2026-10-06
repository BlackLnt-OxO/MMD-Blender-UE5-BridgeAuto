from types import SimpleNamespace as NS

from mmd_bridge.material_graph import constant_socket, effective_graph


def socket(node, value, *, output=False, source=None):
    s = NS(node=node, default_value=value, is_output=output, type='VALUE',
           is_linked=source is not None, identifier='Value', id_data=NS(animation_data=None),
           is_unavailable=False, links=[])
    s.as_pointer = lambda: id(s)
    if source is not None: s.links = [NS(from_socket=source)]
    return s


def test_unconnected_texture_output_default_is_not_a_constant_or_cache_signature():
    node = NS(type='TEX_IMAGE', bl_idname='ShaderNodeTexImage', mute=False, image=None,
              inputs=[], id_data=NS(animation_data=None))
    out = socket(node, .5, output=True)
    assert constant_socket(out) is None
    assert 'constant' not in effective_graph(out)


def test_math_output_evaluates_inputs_instead_of_its_unconnected_default():
    node = NS(type='MATH', bl_idname='ShaderNodeMath', mute=False, operation='MULTIPLY',
              use_clamp=False, id_data=NS(animation_data=None))
    node.inputs = [socket(node, .2), socket(node, .8)]
    out = socket(node, .5, output=True)
    assert abs(constant_socket(out)-.16) < 1e-8
    value = NS(type='VALUE', bl_idname='ShaderNodeValue', mute=False, inputs=[], id_data=NS(animation_data=None))
    node.inputs[1] = socket(node, 0, source=socket(value, .3, output=True))
    assert abs(constant_socket(out)-.06) < 1e-8


def test_multiply_with_variable_texture_remains_unknown():
    image = NS(type='TEX_IMAGE', bl_idname='ShaderNodeTexImage', mute=False, image=None,
               inputs=[], id_data=NS(animation_data=None))
    node = NS(type='MATH', bl_idname='ShaderNodeMath', mute=False, operation='MULTIPLY',
              use_clamp=False, id_data=NS(animation_data=None))
    node.inputs = [socket(node, .2), socket(node, .5, source=socket(image, .5, output=True))]
    assert constant_socket(socket(node, .5, output=True)) is None
