import ast
from pathlib import Path
from types import SimpleNamespace


def test_finish_waits_for_pie_teardown_and_does_not_launch_a_second_render():
    tree = ast.parse((Path(__file__).parents[1] / 'mmd_bridge/ue_reference.py').read_text(encoding='utf-8-sig'))
    start = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'start')
    selected = [node for node in start.body if isinstance(node, ast.FunctionDef)
                and node.name in ('finish', 'tick')]
    world, clock, completed = [object()], [0], []
    native = SimpleNamespace(UnrealEditorSubsystem=object(), get_editor_subsystem=lambda cls:
                             SimpleNamespace(get_game_world=lambda: world[0]))
    state = {'executor': object(), 'paused': False, 'cancelled': False}
    namespace = {'u': native, 'state': state, 'time': SimpleNamespace(monotonic=lambda: clock[0]),
                 'finalize': completed.append}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'reference-lifecycle', 'exec'), namespace)
    namespace['finish'](None, True)
    namespace['tick'](0)
    assert not completed and state['executor'] is None
    world[0] = None
    namespace['tick'](0)
    assert completed == [True]
    # A stuck PIE world must produce a bounded diagnostic, never load over PIE.
    world[0] = object()
    namespace['finish'](None, False)
    clock[0] = 121
    namespace['tick'](0)
    assert state['finish_timeout'] and completed == [True, False]
