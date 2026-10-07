"""Regression evidence for one common converter; never select a scene-specific recipe.

The contract compares outputs, not scene names. A clean reimport may use a new
directory/namespace, but a changed graph, texture, coordinate carrier or binding
must be reviewed before a common-main change can be released.
"""
import hashlib
import json
from pathlib import Path

from .core import read_json, file_hash

SETTINGS = (
    'material_workflow', 'material_strategy', 'material_complex_strategy',
    'material_bake_fallback', 'material_conversion_timeout', 'bake_resolution',
    'scene_frame', 'scene_import_profile', 'scene_scale', 'scene_master_material',
    'thin_surface_scattering', 'restore_scene_appearance', 'volume_resolution',
    'scene_render_pipeline', 'scene_viewport_ocio', 'fog_density_scale',
    'world_volume_distance', 'scene_exposure_offset', 'scene_light_scale',
)
MATERIAL_FIELDS = (
    'source_material_id', 'source_name', 'source_objects', 'strategy', 'surface',
    'channels', 'surface_graph', 'closure_features', 'special_masks',
    'thin_surface_transmission', 'legacy_thin_sheet', 'coverage_source', 'water', 'basic_pbr_bake',
    'baked_channels', 'bake_resolution', 'bake_layout', 'bake_margin_pixels',
    'conversion_status', 'material_workflow',
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def capture(folder, *, export_only=False):
    """Read outputs; export-only evidence never claims a native UE check."""
    folder = Path(folder)
    job = read_json(folder / 'job.json')
    manifest = read_json(folder / 'manifest.json')
    scene = manifest['scene']
    report = {} if export_only else read_json(folder / 'ue_report.json')
    if report.get('checks', {}).get('material_compile_errors'):
        raise ValueError('不能将材质编译失败的导入登记为回归基准')
    functions = {f['id']: f for f in scene.get('function_definitions', [])}
    image_hashes = {}

    def normalize(value, stack=()):
        if isinstance(value, list):
            return [normalize(v, stack) for v in value]
        if not isinstance(value, dict):
            return value
        if 'path' in value and 'color_space' in value:
            path = str(Path(value['path']))
            if path not in image_hashes:
                if not Path(path).is_file():
                    raise ValueError('回归贴图缺失: ' + path)
                image_hashes[path] = file_hash(Path(path))
            return {**{k: normalize(v, stack) for k, v in value.items()
                       if k not in ('id', 'name', 'asset_name', 'path')},
                    'content_sha256': image_hashes[path]}
        result = {k: normalize(v, stack) for k, v in value.items() if k != 'function'}
        if value.get('op', value.get('kind')) == 'function_call':
            identity = value['function']
            if identity in stack or identity not in functions:
                raise ValueError('回归函数依赖不完整或递归: ' + identity)
            function = functions[identity]
            result['function'] = normalize({k: v for k, v in function.items()
                                            if k not in ('id', 'name')}, (*stack, identity))
        elif 'function' in value:
            result['function'] = value['function']
        return result

    materials = {}
    for record in scene.get('materials', []):
        key = record['id']
        if key in materials:
            raise ValueError('重复材质记录 ID: ' + key)
        payload = {k: record[k] for k in MATERIAL_FIELDS if k in record}
        if 'basic_pbr_bake' in payload:
            payload['basic_pbr_bake'] = {k: v for k, v in payload['basic_pbr_bake'].items()
                                         if k not in ('seconds', 'reason', 'error')}
        payload['source_objects'] = sorted(payload.get('source_objects', []))
        materials[key] = {'name': record.get('source_name', record.get('name', key)),
                          'signature': _digest(normalize(payload))}
    # Prototype IDs hash positions, topology, material slots, UV layers and
    # source coordinate attributes. Instances include per-instance shader data.
    geometry_files = {}
    for resource in scene.get('usd', {}).get('files', []):
        relative = resource.get('relative', '').replace('\\', '/')
        if relative == 'Geometry.usda' or relative.startswith('Geometry/'):
            path = Path(resource['path'])
            if not path.is_file():
                raise ValueError('回归几何资源缺失: ' + str(path))
            geometry_files[relative] = file_hash(path)
    geometry_format = 'file_bytes'
    receipt_path = folder / 'audit/geometry_contract.json'
    if receipt_path.is_file():
        receipt = read_json(receipt_path)
        if receipt.get('schema') != 1 or set(receipt.get('files', {})) != set(geometry_files):
            raise ValueError('USD 几何语义回执不完整')
        for relative, digest in geometry_files.items():
            if receipt['files'][relative]['file_sha256'] != digest:
                raise ValueError('USD 几何在语义回执生成后被修改: ' + relative)
        geometry_files = {k: v['semantic_sha256'] for k, v in receipt['files'].items()}
        geometry_format = 'usd_semantics_import_root_normalized'
    geometry = {'prototypes': sorted(scene.get('prototypes', []), key=lambda p: p['id']),
                'instances': scene.get('instances', []),
                'scatter_batches': scene.get('scatter_batches', []),
                'geometry_files': geometry_files}
    settings = job['settings']
    return {'schema': 4, 'source_sha256': job['inputs']['scene'],
            'settings': {k: settings.get(k) for k in SETTINGS},
            'blender_version': scene.get('blender_version'),
            'engine_version': report.get('engine_version'),
            'materials': materials, 'geometry_signature': _digest(geometry), 'geometry_format': geometry_format,
            'texture_count': len(image_hashes),
            'runtime_code_hashes': report.get('runtime_code_hashes', {}),
            'visual_status': 'needs_review', 'job': str(folder.resolve()),
            'native_root': report.get('root')}


def compare_exports(baseline, candidate):
    result = compare(baseline, candidate)
    differences = [item for item in result['differences'] if item['scope'] != 'engine_version']
    independent = baseline.get('job') != candidate.get('job')
    return {'output_contract_equal': not differences, 'differences': differences,
            'independent_export': independent, 'export_gate_passed': not differences and independent,
            'native_validation': 'not_run', 'visual_status': 'needs_review',
            'baseline_job': baseline.get('job'), 'candidate_job': candidate.get('job')}


def compare(baseline, candidate):
    differences = []
    for key in ('schema', 'source_sha256', 'settings', 'blender_version',
                'engine_version', 'geometry_format', 'geometry_signature'):
        if baseline.get(key) != candidate.get(key):
            differences.append({'scope': key})
    old, new = baseline['materials'], candidate['materials']
    for identity in sorted(set(old) | set(new)):
        if old.get(identity) != new.get(identity):
            differences.append({'scope': 'material', 'id': identity,
                                'name': new.get(identity, old.get(identity))['name']})
    runtime_changed = baseline.get('runtime_code_hashes') != candidate.get('runtime_code_hashes')
    independent_import = bool(baseline.get('native_root') and candidate.get('native_root') and
        baseline['native_root'] != candidate['native_root'] and
        baseline.get('job') != candidate.get('job'))
    return {'output_contract_equal': not differences, 'differences': differences,
            'renderer_changed': runtime_changed,
            'independent_import': independent_import,
            'technical_gate_passed': not differences and not runtime_changed and independent_import,
            'visual_status': 'needs_review' if differences or runtime_changed else 'baseline_unchanged',
            'baseline_job': baseline.get('job'), 'candidate_job': candidate.get('job')}
