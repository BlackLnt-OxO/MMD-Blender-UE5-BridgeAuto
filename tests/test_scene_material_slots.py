import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def bind_source_materials():
    path = Path(__file__).parents[1] / 'mmd_bridge/blender_usd.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_bind_source_materials')
    scope = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope[node.name]


@pytest.mark.parametrize('initial', [[], ['old'], ['old', 'old', 'old'], ['old', 'old', 'old', 'extra']])
def test_bindings_fill_missing_slots_preserve_empty_positions_and_face_indices(initial):
    mesh = SimpleNamespace(materials=list(initial), polygon_material_indices=[2, 0, 1])
    source = ['red', None, 'blue']
    bind_source_materials()(mesh, source)
    assert mesh.materials == source + (['extra'] if len(initial) > 3 else [])
    assert mesh.polygon_material_indices == [2, 0, 1]
    assert source == ['red', None, 'blue']


def test_absent_source_slots_do_not_clear_existing_materials():
    mesh = SimpleNamespace(materials=['existing'])
    bind_source_materials()(mesh, [])
    assert mesh.materials == ['existing']
