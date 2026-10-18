import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mmd_bridge.character_profiles import ALPHA_VERSION, character_alpha_mode, character_alpha_ready


def histogram(**values):
    result = [0]*256
    for value, count in values.items():
        result[int(value)] = count
    return result


@pytest.mark.parametrize('samples,opacity,expected', [
    ({'255': 100}, 1, 'opaque'),
    ({'0': 83, '255': 14, '128': 3}, 1, 'masked'),
    ({'0': 57, '255': 39, '128': 4}, 1, 'masked'),
    ({'255': 91, '128': 9}, 1, 'masked'),
    ({'255': 66, '245': 34}, 1, 'masked'),
    ({'255': 70, '128': 30}, 1, 'translucent'),
    ({'68': 100}, 1, 'translucent'),
    ({'225': 100}, 1, 'translucent'),
    ({'0': 83, '255': 17}, .5, 'translucent'),
    ({'255': 100}, 0, 'masked'),
])
def test_character_alpha_distinguishes_cutouts_and_continuous_layers(samples, opacity, expected):
    assert character_alpha_mode(histogram(**samples), opacity) == expected


@pytest.mark.parametrize('samples,opacity', [([], 1), ([0]*256, 1), ([-1]*256, 1),
                                            ([float('nan')]*256, 1), ([1]*256, float('nan'))])
def test_character_alpha_rejects_invalid_evidence(samples, opacity):
    with pytest.raises(ValueError):
        character_alpha_mode(samples, opacity)


def test_legacy_character_alpha_requires_metadata_refresh():
    manifest = {'materials': [{'character_material': {'profile': 'ZZZ_Render_PMX', 'alpha_mode': 'translucent'}}]}
    assert not character_alpha_ready(manifest)
    manifest['materials'][0]['character_material']['alpha_mode_version'] = ALPHA_VERSION
    assert character_alpha_ready(manifest)
    assert character_alpha_ready({'materials': [{'name': 'unregistered'}]})


def bounds_function(native):
    path = Path(__file__).parents[1]/'mmd_bridge/ue_character_materials.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == 'ensure_character_bounds')
    namespace = {'u': native}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['ensure_character_bounds']


def test_bounds_repair_preserves_existing_physics_and_protects_name_conflicts():
    calls = []
    physics = NS(get_path_name=lambda: '/Game/Role/SK_Character_PhysicsAsset')
    mesh = NS(get_path_name=lambda: '/Game/Role/SK_Character.SK_Character',
              get_editor_property=lambda name: physics)
    manager = NS(verify_asset=lambda asset: calls.append(('verify', asset)), report={})
    repair = bounds_function(NS())
    repair(manager, mesh)
    assert calls == [('verify', mesh)]
    assert manager.report['checks']['character_bounds'][0]['physics_asset'] == physics.get_path_name()
    mesh.get_editor_property = lambda name: None
    def refuse_existing(asset):
        if asset is physics:
            raise ValueError('not owned')
    manager.verify_asset = refuse_existing
    repair = bounds_function(NS(load_asset=lambda path: physics))
    with pytest.raises(ValueError, match='not owned'):
        repair(manager, mesh)
