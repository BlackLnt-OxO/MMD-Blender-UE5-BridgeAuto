"""Read saved native material contracts for two imports, without saving assets."""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import unreal as u
from mmd_bridge.core import read_json, atomic_json, file_hash

config = read_json(Path(os.environ['MMDBRIDGE_NATIVE_COMPARE']))
result = {'complete': False, 'read_only': True, 'differences': [], 'materials': []}


def value(item):
    if isinstance(item, u.Texture):
        return {'class': item.get_class().get_name(),
                'source_hash': u.EditorAssetLibrary.get_metadata_tag(item, 'MMDBridge.SourceHash'),
                'srgb': item.get_editor_property('srgb'),
                'compression_settings': str(item.get_editor_property('compression_settings'))}
    if isinstance(item, (u.LinearColor, u.Vector)):
        fields = ('r', 'g', 'b', 'a') if isinstance(item, u.LinearColor) else ('x', 'y', 'z')
        return [getattr(item, field) for field in fields]
    return item


def contract(asset):
    item = {'instance_parameters': {}, 'parents': []}
    current = asset
    while isinstance(current, u.MaterialInstance):
        params = {}
        for kind in ('scalar', 'vector', 'texture'):
            values = []
            for entry in current.get_editor_property(kind + '_parameter_values'):
                info = entry.get_editor_property('parameter_info')
                values.append({'name': str(info.get_editor_property('name')),
                    'association': str(info.get_editor_property('association')),
                    'index': info.get_editor_property('index'),
                    'value': value(entry.get_editor_property('parameter_value'))})
            params[kind] = sorted(values, key=lambda entry: (entry['name'], entry['association'], entry['index']))
        overrides = current.get_editor_property('base_property_overrides')
        params['overrides'] = {}
        for name in ('blend_mode', 'two_sided', 'opacity_mask_clip_value'):
            enabled = overrides.get_editor_property('override_' + name)
            params['overrides'][name] = {'enabled': enabled,
                'value': str(overrides.get_editor_property(name)) if enabled else None}
        item['parents'].append(params)
        current = current.get_editor_property('parent')
    item['native'] = {name: str(current.get_editor_property(name)) for name in
        ('blend_mode', 'two_sided', 'opacity_mask_clip_value', 'shading_model', 'use_material_attributes')}
    item['expressions'] = u.MaterialEditingLibrary.get_num_material_expressions(current)
    item['metadata'] = {name: u.EditorAssetLibrary.get_metadata_tag(current, 'MMDBridge.' + name)
        for name in ('SurfaceGraph', 'CutoutTransmission', 'ThinSurfaceScattering',
                     'MaterialStrategy', 'TextureParameters', 'BuildState', 'MaskDistance', 'ShaderCoverage')}
    item['inputs'] = {}
    for name in ('MP_BASE_COLOR', 'MP_METALLIC', 'MP_ROUGHNESS', 'MP_NORMAL',
                 'MP_OPACITY', 'MP_OPACITY_MASK', 'MP_FRONT_MATERIAL'):
        node = u.MaterialEditingLibrary.get_material_property_input_node(current, getattr(u.MaterialProperty, name))
        item['inputs'][name] = node.get_class().get_name() if node else None
    return item


try:
    baseline = Path(config['baseline_job'])
    candidate = Path(config['candidate_job'])
    assert baseline != candidate
    old = read_json(baseline / 'ue_report.json')['assets']['materials']
    new = read_json(candidate / 'ue_report.json')['assets']['materials']
    records = read_json(baseline / 'manifest.json')['scene']['materials']
    builder = None
    if config.get('build_frozen_control'):
        import importlib.util
        import copy
        module = Path(config['control_module'])
        assert file_hash(module) == config['control_module_sha256']
        spec = importlib.util.spec_from_file_location('mmd_bridge.ue_materials_frozen_control', module)
        frozen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(frozen)
        settings = read_json(baseline / 'job.json')['settings']
        root = config['control_root']
        content = Path(u.Paths.project_content_dir()) / root.removeprefix('/Game/')
        assert not content.exists(), 'control requires an absent namespace'
        result['read_only'] = False
        result['scope'] = 'three drifted baseline assets rebuilt from frozen b46cdc8 renderer; no scene writes'
        builder = frozen.Materials(root, 'FrozenMyGOControl_b46cdc8', {'warnings': [], 'failures': []}, settings)
        builder.function_definitions = {definition['id']: definition for definition in
            read_json(baseline / 'manifest.json')['scene'].get('function_definitions', [])}
    for record in records:
        if record.get('conversion_status') not in ('converted', 'approximated'):
            continue
        identity = record['id']
        if builder and identity not in config['control_ids']:
            continue
        first = builder.build(copy.deepcopy(record), baseline) if builder else u.load_asset(old[identity])
        second = u.load_asset(new[identity])
        assert first and second, identity
        a, b = contract(first), contract(second)
        entry = {'id': identity, 'name': record['name'], 'equal': a == b, 'baseline': a, 'candidate': b}
        result['materials'].append(entry)
        if a != b:
            result['differences'].append({'id': identity, 'name': record['name']})
    result['complete'] = True
    result['equal'] = not result['differences']
except Exception:
    result['error'] = traceback.format_exc()
    u.log_error(result['error'])
atomic_json(Path(config['control_output'] if config.get('build_frozen_control') else config['output']), result)
print('NATIVE_MATERIAL_COMPARE', result['complete'], len(result['materials']), len(result['differences']), flush=True)
