import ast
import json
from pathlib import Path

import pytest

from mmd_bridge.core import stable_id


def checkpoint_key():
    path = Path(__file__).parents[1] / 'mmd_bridge/blender_usd.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_scene_checkpoint_key')
    scope = {'json': json, 'stable_id': stable_id}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope[node.name]


def test_reference_memory_preset_does_not_invalidate_frozen_scene_or_legacy_key():
    key = checkpoint_key()
    original = {'scene_scale': 1., 'bake_resolution': 1024, 'ue_render_memory_profile': 'balanced'}
    legacy = stable_id('source-sha', '5.2.2', json.dumps(original, sort_keys=True), 'frozen-render-v4-coverage')
    changed = dict(original, ue_render_memory_profile='low')
    assert key(changed, 'source-sha', '5.2.2', legacy) == legacy
    assert key(original, 'source-sha', '5.2.2') == key(changed, 'source-sha', '5.2.2')


@pytest.mark.parametrize('change', ['source', 'blender', 'geometry', 'material'])
def test_legacy_key_still_rejects_changes_outside_ue_memory_preset(change):
    key = checkpoint_key()
    cfg = {'scene_scale': 1., 'bake_resolution': 1024, 'ue_render_memory_profile': 'balanced'}
    legacy = stable_id('source-sha', '5.2.2', json.dumps(cfg, sort_keys=True), 'frozen-render-v4-coverage')
    source, blender = 'source-sha', '5.2.2'
    if change == 'source': source = 'changed-source'
    if change == 'blender': blender = '5.2.3'
    if change == 'geometry': cfg['scene_scale'] = 2.
    if change == 'material': cfg['bake_resolution'] = 2048
    assert key(cfg, source, blender, legacy) != legacy
