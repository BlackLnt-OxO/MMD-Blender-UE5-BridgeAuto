import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge import character_custom_materials as custom
from mmd_bridge.core import Settings, file_hash


def fixture(tmp_path):
    model = tmp_path/'source.pmx'; model.write_bytes(b'original pmx')
    image = tmp_path/'hair.png'; image.write_bytes(b'original atlas')
    digest = file_hash(model)
    row = {'model_sha256': digest, 'model': str(model), 'pmx_index': 3, 'name': 'skin', 'name_e': '',
           'diffuse': [.8, .7, .6, .4], 'specular': [.1, .2, .3], 'shininess': 6., 'ambient': [.2]*3,
           'sphere_mode': 0, 'sources': {'base_color': {'path': str(image), 'reference': 'hair.png',
                'status': 'available', 'kind': 'pmx'}, 'sphere': {'path': '', 'status': 'disabled'}}}
    cfg = {'model': str(model), 'project': str(tmp_path/'A.uproject'), 'character_material_profile': 'generic',
           'character_master_material': '/Game/M', 'eye_master_material': '', 'character_custom_materials': {}}
    entry = custom.model_entry(cfg['character_custom_materials'], digest, cfg, create=True)
    entry['parents']['/Game/M'] = {'inventory': {'signature': 'read-native-parent', 'parameters': [
        {'name': 'Arbitrary_Color', 'kind': 'texture', 'default': '/Game/Default', 'supported': True},
        {'name': 'Extra', 'kind': 'texture', 'default': '/Game/Extra', 'supported': True},
        {'name': 'Opacity', 'kind': 'scalar', 'default': 1.},
        {'name': 'BaseColorTint', 'kind': 'vector', 'default': [1]*4},
        {'name': 'Specular', 'kind': 'scalar', 'default': .5},
        {'name': 'Roughness', 'kind': 'scalar', 'default': .5},
        {'name': 'SpecularColor', 'kind': 'vector', 'default': [1]*4},
        {'name': 'Shininess', 'kind': 'scalar', 'default': 1.},
        {'name': 'AmbientColor', 'kind': 'vector', 'default': [0]*4}]}, 'textures': {}}
    return cfg, row, entry


def binding(source='base_color', channel='rgb', invert=False):
    return {'source': source, 'channel': channel, 'invert': invert, 'color_space': 'srgb'}


def test_unmapped_keeps_defaults_and_never_invents_white_or_pbr_values(tmp_path):
    cfg, row, _ = fixture(tmp_path)
    result = custom.plan(cfg, row)
    assert result['textures'] == {}
    assert result['numeric']['Opacity']['value'] == .4
    assert result['numeric']['SpecularColor']['value'] == [.1, .2, .3]
    assert result['numeric']['Shininess']['value'] == 6.
    assert 'Roughness' not in result['numeric'] and 'Specular' not in result['numeric']


def test_common_rule_uses_each_slots_own_atlas_and_slot_can_restore_default(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    other = copy.deepcopy(row); other['pmx_index'] = 4
    image = tmp_path/'other.png'; image.write_bytes(b'other atlas')
    other['sources']['base_color']['path'] = str(image)
    assert custom.plan(cfg, other)['textures']['Arbitrary_Color']['path'] == str(image)
    assert custom.plan(cfg, row)['textures']['Arbitrary_Color']['path'] != str(image)
    entry['slots']['3'] = {'parent_textures': {'/Game/M': {'Arbitrary_Color': None}}}
    assert not custom.plan(cfg, row)['textures']
    assert custom.plan(cfg, other)['textures']


def test_parent_override_does_not_reuse_the_other_parents_parameters(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    entry['parents']['/Game/Other'] = {'inventory': {'signature': 'other', 'parameters': []}}
    entry['slots']['3'] = {'parent': '/Game/Other'}
    assert custom.plan(cfg, row)['parent'] == '/Game/Other'
    assert custom.plan(cfg, row)['textures'] == {}


def test_slot_overrides_are_isolated_when_parent_parameter_names_match(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/Other'] = copy.deepcopy(entry['parents']['/Game/M'])
    entry['slots']['3'] = {'parent_textures': {'/Game/M': {'Arbitrary_Color': binding()}}}
    assert custom.plan(cfg, row)['textures']
    entry['slots']['3']['parent'] = '/Game/Other'
    assert not custom.plan(cfg, row)['textures']
    entry['slots']['3']['parent'] = '/Game/M'
    assert custom.plan(cfg, row)['textures']


def test_mapping_cannot_silently_change_parent_sampler_interpretation(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['inventory']['parameters'][0]['color_space'] = 'data'
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    with pytest.raises(ValueError, match='采样类型不兼容'): custom.plan(cfg, row)


def test_disabled_or_unreferenced_sources_inherit_but_referenced_missing_is_error(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['textures'] = {'Extra': binding('sphere')}
    result = custom.plan(cfg, row)
    assert result['textures'] == {} and result['retained_defaults'][0]['reason'] == 'disabled'
    row['sources']['sphere'] = {'path': str(tmp_path/'missing.sph'), 'status': 'missing', 'reference': 'missing.sph'}
    with pytest.raises(ValueError, match='缺图'): custom.plan(cfg, row)


def test_one_source_can_feed_two_parameters_with_distinct_channels(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding(), 'Extra': binding(channel='a', invert=True)}
    result = custom.plan(cfg, row)
    assert result['textures']['Extra']['channel'] == 'a' and result['textures']['Extra']['invert']
    assert result['textures']['Extra']['path'] == result['textures']['Arbitrary_Color']['path']


def test_config_isolated_by_model_and_project_and_does_not_consume_pbr_maps(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    cfg['character_material_overrides'] = {row['model_sha256']: {'3': {'textures': {'base_color': {'path': 'pbr-only.png'}},
                                                           'params': {'Roughness': .2}}}}
    protected = copy.deepcopy(cfg['character_material_overrides'])
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    assert custom.plan(cfg, row)['textures']['Arbitrary_Color']['path'].endswith('hair.png')
    assert cfg['character_material_overrides'] == protected
    assert custom.model_entry(cfg['character_custom_materials'], 'f'*64, cfg) == {}
    assert custom.model_entry(cfg['character_custom_materials'], row['model_sha256'], dict(cfg, project='elsewhere.uproject')) == {}


def test_numeric_aliases_are_exact_and_ambiguous_matches_remain_default(tmp_path):
    _, row, _ = fixture(tmp_path)
    parameters = [{'kind': 'scalar', 'name': n} for n in ['Opacity', 'Alpha', 'MyOpacityMaskStrength', 'Metallic']]
    assigned, ambiguous = custom.numeric_assignments(row, parameters)
    assert assigned == {} and ambiguous[0]['parameters'] == ['Opacity', 'Alpha']


def test_template_is_portable_rules_without_source_paths_or_slot_bindings(tmp_path):
    cfg, row, entry = fixture(tmp_path)
    entry['default_parents'] = {'character': '/Game/M', 'eye': ''}
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    entry['slots']['3'] = {'sources': {'normal': {'path': 'private.png'}}, 'parent': '/Game/Other'}
    template = custom.export_template(entry)
    assert 'private.png' not in str(template) and 'slots' not in template and 'inventory' not in str(template)
    destination = {'parents': {}, 'slots': {'9': {'parent': '/Game/Keep'}}}
    loaded = custom.import_template(destination, template)
    assert destination['parents'] == {} and loaded['slots'] == destination['slots']
    assert loaded['parents']['/Game/M']['textures'] == entry['parents']['/Game/M']['textures']
    with pytest.raises(ValueError): custom.import_template(destination, dict(template, slots={}))


def test_mapping_and_source_changes_invalidate_only_custom_checkpoint(tmp_path, monkeypatch):
    cfg, row, entry = fixture(tmp_path)
    monkeypatch.setattr(custom, 'inspect_model', lambda _: {'sha256': row['model_sha256'], 'materials': [row]})
    entry['parents']['/Game/M']['textures'] = {'Arbitrary_Color': binding()}
    before = custom.checkpoint_signature(cfg)
    Path(row['sources']['base_color']['path']).write_bytes(b'edited source')
    assert custom.checkpoint_signature(cfg) != before
    assert custom.checkpoint_signature(dict(cfg, character_material_profile='character_pbr')) is None
    assert custom.checkpoint_signature(dict(cfg, character_custom_materials={})) is None


@pytest.mark.parametrize('bad', [binding(channel='z'), binding(source='unknown'),
    dict(binding(), color_space='normal', channel='a'), dict(binding(), path='must-not-export')])
def test_bad_mapping_is_rejected(bad):
    with pytest.raises(ValueError): custom.validate_binding(bad)


def test_reader_uses_actual_references_even_when_filename_says_hair(tmp_path, monkeypatch):
    cfg, row, _ = fixture(tmp_path)
    raw = {k: v for k, v in row.items() if k not in ('sources', 'model', 'model_sha256')}
    raw.update(texture_index=0, sphere_index=1, sphere_mode=0, toon_index=0, shared_toon=True)
    monkeypatch.setattr('mmd_bridge.pmx_reader.read_pmx', lambda _: SimpleNamespace(materials=[raw], textures=['hair.png', 'unused.sph']))
    result = custom.inspect_model(cfg['model'])['materials'][0]
    assert result['sources']['base_color']['reference'] == 'hair.png'
    assert result['sources']['sphere']['status'] == 'disabled'
    assert result['sources']['toon']['reference'] == 'toon01.bmp'


def test_legacy_settings_and_registered_profiles_do_not_enable_new_route(tmp_path):
    cfg, row, _ = fixture(tmp_path)
    assert not custom.enabled(dict(cfg, character_custom_materials={}), row)
    assert not custom.uses_custom_editor(dict(cfg, character_material_profile='character_pbr'))
    assert not custom.uses_custom_editor(dict(cfg, character_material_profile='ZZZ_Render_PMX'))
    custom.validate({})
    assert Settings().character_custom_materials == {}


def test_new_custom_jobs_have_explicit_version_while_other_routes_and_old_jobs_keep_contract(tmp_path, monkeypatch):
    from mmd_bridge.core import Job, atomic_json
    cfg, row, entry = fixture(tmp_path)
    monkeypatch.setattr(Settings, 'validate', lambda self: {})
    for profile, backend, expected in [('generic', 'mmdbridge_fbx', 1), ('generic', 'fbx_legacy', 1),
            ('generic', 'mmd2unreal', None), ('character_pbr', 'mmdbridge_fbx', None),
            ('ue_official_toon', 'mmdbridge_fbx', None), ('ZZZ_Render_PMX', 'mmdbridge_fbx', None)]:
        job = Job.create(tmp_path/'jobs', Settings(model=cfg['model'], character_material_profile=profile, character_backend=backend))
        assert job.data.get('custom_material_binding_version') == expected
    legacy = tmp_path/'legacy'; legacy.mkdir()
    atomic_json(legacy/'job.json', {'schema': 2, 'settings': {'character_material_profile': 'generic'}})
    assert 'custom_material_binding_version' not in Job(legacy).data
