"""Read saved foliage inputs in an isolated native UE process; never save assets."""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import unreal as u
from mmd_bridge.core import read_json, atomic_json

folder = Path(os.environ['MMDBRIDGE_AUDIT_JOB'])
out = folder / 'audit/native_cutouts.json'
result = {'complete': False, 'read_only': True, 'materials': [], 'issues': []}
try:
    job = read_json(folder / 'job.json')
    assert Path(u.Paths.get_project_file_path()).resolve() == Path(job['settings']['project']).resolve()
    scene = read_json(folder / 'manifest.json')['scene']
    report = read_json(folder / 'ue_report.json')
    visited = set()
    for record in scene['materials']:
        name = record.get('source_name', record['name'])
        if not any(word in name.lower() for word in ('grass', 'leaf', 'leaves')):
            continue
        # Different variants may have different masks or instance overrides.
        identity = record['id']
        if record.get('conversion_status') in ('placeholder', 'failed'):
            continue
        if identity in visited:
            continue
        visited.add(identity)
        path = report['assets']['materials'][record['id']]
        instance = u.load_asset(path)
        if not instance:
            raise RuntimeError('材质实例无法加载: ' + path)
        parent = instance.get_editor_property('parent')
        while isinstance(parent, u.MaterialInstance):
            parent = parent.get_editor_property('parent')
        overrides = instance.get_editor_property('base_property_overrides')
        override = overrides.get_editor_property('override_blend_mode')
        parent_mode = parent.get_editor_property('blend_mode')
        effective_mode = overrides.get_editor_property('blend_mode') if override else parent_mode
        item = {'name': name, 'id': record['id'], 'strategy': record['strategy'],
                'instance': path, 'parent': parent.get_path_name(),
                'parent_blend_mode': str(parent_mode), 'override_blend_mode': override,
                'effective_blend_mode': str(effective_mode),
                'two_sided': parent.get_editor_property('two_sided'),
                'effective_two_sided': (overrides.get_editor_property('two_sided')
                    if overrides.get_editor_property('override_two_sided') else parent.get_editor_property('two_sided')),
                'thin_surface_scattering': u.EditorAssetLibrary.get_metadata_tag(parent, 'MMDBridge.ThinSurfaceScattering')}
        for property_name in ('MP_OPACITY_MASK', 'MP_OPACITY', 'MP_FRONT_MATERIAL', 'MP_NORMAL'):
            node = u.MaterialEditingLibrary.get_material_property_input_node(
                parent, getattr(u.MaterialProperty, property_name))
            item[property_name] = node.get_class().get_name() if node else None
        opacity = record.get('channels', {}).get('opacity', {})
        source_cutout = bool(opacity and (opacity.get('op') != 'constant' or opacity.get('value', 1) < 1))
        item['source_cutout'] = source_cutout
        if source_cutout and effective_mode == u.BlendMode.BLEND_OPAQUE:
            result['issues'].append({'name': name, 'error': '源遮罩被不透明模式覆盖'})
        if effective_mode == u.BlendMode.BLEND_MASKED and not item['MP_OPACITY_MASK']:
            result['issues'].append({'name': name, 'error': 'Masked 材质缺少 Opacity Mask 连接'})
        if record.get('legacy_thin_sheet'):
            expected = job['settings'].get('thin_surface_scattering', 'legacy_diffusion')
            if item['thin_surface_scattering'] != expected or not item['effective_two_sided']:
                result['issues'].append({'name': name, 'error': '薄片散射模式或双面状态不符', 'expected': expected})
        result['materials'].append(item)
    result['complete'] = True
except Exception:
    result['error'] = traceback.format_exc()
    u.log_error(result['error'])
atomic_json(out, result)
print('SCENE_CUTOUT_AUDIT', result['complete'], len(result['materials']), len(result['issues']), flush=True)
