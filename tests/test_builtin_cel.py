import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from mmd_bridge.character_cel import plan, reference, settings_signature, PREFIX
from mmd_bridge.character_geometry import geometry_policy
from mmd_bridge.core import Settings, file_hash, resource_root
from mmd_bridge.orchestration import module_settings


def record(name='Hair', sphere=None, mode=0):
    return {'name': name, 'pmx_index': 3, 'textures': {'sphere': sphere} if sphere else {},
            'mmd': {'sphere_texture_type': mode, 'diffuse_color': [.7, .8, .9], 'diffuse_alpha': .5}}


def test_color_modes_preserve_curves_second_shadows_and_disable_defaults():
    ref = reference()
    before = copy.deepcopy(ref)
    b = plan(record(), {'character_cel_color_mode': 'role'}, 'one', ref)
    a = plan(record(), {'character_cel_color_mode': 'neutral'}, 'one', ref)
    assert a['parameters']['scalars'] == b['parameters']['scalars'] == ref['roles']['hair']['scalars']
    for key in ('ShadowColor_2nd', 'ShadowColor2nd'):
        assert a['parameters']['vectors'][key] == b['parameters']['vectors'][key] == ref['roles']['hair']['vectors'][key]
    assert a['parameters']['vectors']['ShadowColor'] == [.6, .6, .6, 0]
    assert b['parameters']['vectors']['ShadowColor'] == ref['roles']['hair']['vectors']['ShadowColor']
    for p in (a, b):
        assert p['parameters']['vectors']['BaseColor'] == [1, 1, 1, 0]
        assert p['parameters']['vectors']['MatCapColor'] == [.9, .9, .9, ref['roles']['hair']['vectors']['MatCapColor'][3]]
        assert not p['parameters']['switches']['Brush?']
        assert not p['parameters']['switches']['启用边缘光']
    assert ref == before


@pytest.mark.parametrize('mode', [1, 2])
def test_enabled_pmx_sphere_has_priority_and_preserves_mode(tmp_path, mode):
    texture = tmp_path/'sphere.spa'; texture.write_bytes(b'image')
    p = plan(record(sphere={'path': str(texture)}, mode=mode), {}, 'one')
    assert p['matcap']['source'] == 'pmx'
    assert p['matcap']['mode'] == mode


def test_disabled_missing_and_additional_uv_are_distinct(tmp_path):
    missing = {'path': str(tmp_path/'missing.sph')}
    assert plan(record(sphere=missing), {}, 'one')['matcap']['source'] == 'cel_hair'
    for mode in (1, 2, 3):
        p = plan(record(sphere=missing, mode=mode), {}, 'one')
        assert p['matcap']['source'] == 'disabled'
        assert p['status'] == 'needs_review' and p['issues']
    assert plan(record('Cloth'), {}, 'one')['matcap']['source'] == 'disabled'


def test_unknown_role_and_manual_role_are_source_scoped():
    cfg = {'character_material_overrides': {'one': {'3': {'type': 'skin'}}}}
    a = plan(record('Unrecognized'), cfg, 'one')
    b = plan(record('Unrecognized'), cfg, 'two')
    assert a['role'] == 'skin' and a['classification']['method'] == 'manual'
    assert b['status'] == 'needs_review'
    assert b['parameters']['vectors']['ShadowColor'] == [.6, .6, .6, 0]


def test_cel_config_follows_children_and_invalidates_color_cache():
    settings = Settings(character_material_profile='builtin_cel', character_cel_color_mode='neutral',
                        character_backend='mmdbridge_fbx', flow='character', model='first.pmx',
                        additional_characters=[{'model': 'second.pmx'}])
    children = list(module_settings(settings))
    assert len(children) == 2
    assert all(cfg.character_cel_color_mode == 'neutral' for _, cfg in children)
    assert settings_signature(asdict(settings)) != settings_signature(asdict(replace(settings, character_cel_color_mode='role')))
    assert settings_signature(asdict(Settings())) is None
    assert geometry_policy(asdict(settings))['mode'] == settings.character_geometry_mode


def test_bundled_assets_are_complete_and_match_provenance():
    folder = resource_root()/'resources/character_materials/BuiltinCel'
    data = json.loads((folder/'provenance.json').read_text(encoding='utf-8'))
    assert len(data['files']) >= 12
    for name, digest in data['files'].items():
        assert file_hash(folder/name) == digest
    assert (folder/'Content'/PREFIX.removeprefix('/Game/')/'Character/Materials/Master/M_NPR.uasset').is_file()


def test_geometry_uses_cel_source_type_without_pbr_shading_data():
    from mmd_bridge.character_geometry_roles import material_traits, purpose
    from mmd_bridge.character_geometry import target_material_indices
    rows = [{'asset_name': name, 'name': 'Unrecognized', 'character_material': {
        'cel': {'classification': {'type': name}}}} for name in ('skin', 'hair', 'metal')]
    assert target_material_indices(['skin', 'hair', 'metal'], rows) == [0]
    assert material_traits(rows[1])['type'] == 'hair'
    assert purpose(rows[1])['role'] == 'excluded'
    rows[0]['character_material']['alpha_mode'] = 'masked'
    assert material_traits(rows[0])['alpha_mode'] == 'masked'


def test_desktop_cel_option_and_color_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    try:
        assert window.inputs['character_material_profile'].currentData() == 'character_pbr'
        window.apply_settings({'character_material_profile': 'builtin_cel', 'character_cel_color_mode': 'neutral'})
        assert window.settings().character_cel_color_mode == 'neutral'
        assert window.inputs['character_cel_color_mode'].isEnabled()
        assert window.inputs['character_geometry_mode'].isEnabled()
        assert not window.inputs['character_master_material'].isEnabled()
        window.apply_settings({'character_material_profile': 'character_pbr'})
        assert not window.inputs['character_cel_color_mode'].isEnabled()
    finally:
        window.close()


def test_install_preserves_enabled_project_bytes_and_rejects_asset_conflicts(tmp_path, monkeypatch):
    from mmd_bridge import character_resources as resources
    monkeypatch.setattr(resources, 'load_profiles', lambda *args: {})
    root = tmp_path/'tool'
    relative = PREFIX.removeprefix('/Game/')+'Test.uasset'
    template = root/'resources/character_materials/BuiltinCel/Content'/relative
    template.parent.mkdir(parents=True)
    template.write_bytes(b'template')
    modules = root/'plugins/unreal/MMDBridgeCharacter/Binaries/Win64/UnrealEditor.modules'
    modules.parent.mkdir(parents=True)
    modules.write_text('{"BuildId":"test"}', encoding='utf-8')
    unreal = tmp_path/'engine/UnrealEditor.exe'
    unreal.parent.mkdir()
    unreal.with_name('UnrealEditor.modules').write_text('{"BuildId":"test"}', encoding='utf-8')
    project = tmp_path/'project/Test.uproject'
    project.parent.mkdir()
    original = b'{\r\n\t"Plugins": [{"Name":"MMDBridgeCharacter","Enabled":true}]\r\n}'
    project.write_bytes(original)
    old_relative = 'MMDBridge/BuiltinCel/v1/Character/Materials/Master/M_NPR.uasset'
    old_template = root/'resources/character_materials/BuiltinCel/Content'/old_relative
    old_template.parent.mkdir(parents=True)
    old_template.write_bytes(b'previous template')
    old_target = project.parent/'Content'/old_relative
    old_target.parent.mkdir(parents=True)
    old_target.write_bytes(b'user edited v1')
    settings = {'character_material_profile': 'builtin_cel'}
    assert resources.install(settings, root, project, str(unreal))
    assert project.read_bytes() == original
    assert old_target.read_bytes() == b'user edited v1'
    target = project.parent/'Content'/relative
    target.write_bytes(b'user edit')
    with pytest.raises(ValueError, match='拒绝覆盖'):
        resources.install(settings, root, project, str(unreal))
    assert target.read_bytes() == b'user edit'
