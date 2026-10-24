"""Source-only custom shader mappings, isolated from all PBR overrides."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from .core import file_hash, resource_root
from .character_profiles import fingerprint, is_eye_material, load_profiles, resolve_profile

VERSION = 1
SOURCES = {'base_color': '基础色', 'sphere': 'Sphere / MatCap', 'toon': 'Toon',
           'normal': '法线', 'roughness': '粗糙度', 'metallic': '金属度', 'ao': 'AO',
           'opacity': '透明遮罩', 'emission': '自发光', 'control': '自定义控制图'}
COLOR_SPACES = ('srgb', 'data', 'normal')
NUMERIC = {
    'diffuse_color': ('vector', ('diffusecolor', 'basecolortint', 'colortint', 'pmxdiffusecolor')),
    'opacity': ('scalar', ('opacity', 'alpha', 'pmxopacity', '不透明度', '透明度')),
    'specular_color': ('vector', ('specularcolor', 'pmxspecularcolor', '镜面颜色')),
    'shininess': ('scalar', ('shininess', 'specularpower', 'pmxshininess', '镜面指数')),
    'ambient_color': ('vector', ('ambientcolor', 'pmxambientcolor', '环境色')),
}


def asset_path(value):
    value = str(value).strip()
    if not value.startswith('/') or '..' in value or '\\' in value or ':' in value:
        raise ValueError('母材质需要有效 UE 资产路径: ' + value)
    return value.split('.', 1)[0]


def project_key(cfg):
    value = cfg.get('project', '')
    return str(Path(value).resolve()).casefold() if value else ''


def uses_custom_editor(cfg):
    selected = cfg.get('character_material_profile', 'auto')
    if selected == 'generic':
        return True
    if selected != 'auto':
        return False
    profiles = load_profiles(resource_root(), cfg.get('character_material_profiles_dir', ''))
    return resolve_profile(profiles, cfg.get('character_master_material', ''), 'auto') is None


def model_entry(data, model_hash, cfg, *, create=False):
    if not data:
        if not create:
            return {}
        data.update(version=VERSION, models={})
    if data.get('version') != VERSION:
        raise ValueError('不支持的自选母材质映射版本')
    models = data.setdefault('models', {}) if create else data.get('models', {})
    model = models.setdefault(model_hash, {'projects': {}}) if create else models.get(model_hash, {})
    projects = model.setdefault('projects', {}) if create else model.get('projects', {})
    return (projects.setdefault(project_key(cfg), {'parents': {}, 'slots': {}})
            if create else projects.get(project_key(cfg), {}))


def default_parent(cfg, source):
    record = {'name': source['name'], 'mmd': {'name_j': source['name'], 'name_e': source.get('name_e', '')}}
    entry = model_entry(cfg.get('character_custom_materials', {}), source['model_sha256'], cfg)
    defaults = entry.get('default_parents', {})
    return ((defaults.get('eye', cfg.get('eye_master_material', '')) if is_eye_material(record) else '')
            or defaults.get('character', cfg.get('character_master_material', '')))


def selection(cfg, source):
    entry = model_entry(cfg.get('character_custom_materials', {}), source['model_sha256'], cfg)
    slot = entry.get('slots', {}).get(str(source['pmx_index']), {})
    parent = slot.get('parent') or default_parent(cfg, source)
    parent = asset_path(parent) if parent else ''
    common = entry.get('parents', {}).get(parent, {})
    mappings = copy.deepcopy(common.get('textures', {}))
    for target, binding in slot.get('parent_textures', {}).get(parent, {}).items():
        if binding is None:
            mappings.pop(target, None)
        else:
            mappings[target] = copy.deepcopy(binding)
    return parent, mappings, slot, common.get('inventory', {})


def enabled(cfg, source):
    if not uses_custom_editor(cfg):
        return False
    return bool(model_entry(cfg.get('character_custom_materials', {}), source['model_sha256'], cfg))


def validate_binding(binding):
    if not isinstance(binding, dict) or set(binding) - {'source', 'channel', 'invert', 'color_space'}:
        raise ValueError('无效的贴图对应关系')
    if binding.get('source') not in SOURCES or binding.get('channel', 'rgb') not in ('rgb', 'r', 'g', 'b', 'a'):
        raise ValueError('未知贴图来源或通道')
    if type(binding.get('invert', False)) is not bool or binding.get('color_space', 'srgb') not in COLOR_SPACES:
        raise ValueError('无效的贴图反转或颜色空间')
    if binding.get('color_space') == 'normal' and (binding.get('channel', 'rgb') != 'rgb' or binding.get('invert')):
        raise ValueError('法线贴图需使用完整 RGB，不能按单通道反转')


def validate(data):
    if not data:
        return
    if not isinstance(data, dict) or data.get('version') != VERSION or not isinstance(data.get('models'), dict):
        raise ValueError('无效的自选母材质配置')
    for digest, model in data['models'].items():
        if not re.fullmatch('[a-f0-9]{64}', digest) or not isinstance(model, dict) or not isinstance(model.get('projects'), dict):
            raise ValueError('自选映射缺少原 PMX 身份')
        for entry in model['projects'].values():
            if not isinstance(entry, dict) or not isinstance(entry.get('parents'), dict) or not isinstance(entry.get('slots'), dict):
                raise ValueError('无效的自选映射工程配置')
            for path in entry.get('default_parents', {}).values():
                if path:
                    asset_path(path)
            for parent, rule in entry['parents'].items():
                asset_path(parent)
                _validate_textures(rule.get('textures', {}))
                if not isinstance(rule.get('inventory', {}), dict):
                    raise ValueError('无效的母材质参数快照')
            for index, slot in entry['slots'].items():
                if not str(index).isdigit() or not isinstance(slot, dict):
                    raise ValueError('无效的原材质槽位')
                if slot.get('parent'):
                    asset_path(slot['parent'])
                for parent, textures in slot.get('parent_textures', {}).items():
                    asset_path(parent)
                    _validate_textures(textures, allow_default=True)
                for semantic, spec in slot.get('sources', {}).items():
                    if semantic not in SOURCES or not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
                        raise ValueError('无效的手选来源贴图')


def _validate_textures(values, allow_default=False):
    if not isinstance(values, dict):
        raise ValueError('贴图映射必须为对象')
    for name, binding in values.items():
        if not isinstance(name, str) or not name:
            raise ValueError('缺少目标 Texture Parameter')
        if binding is not None or not allow_default:
            validate_binding(binding)


def inspect_model(path):
    from .pmx_reader import read_pmx
    path = Path(path).resolve()
    model = read_pmx(path)
    digest = file_hash(path)
    rows = []
    toon_root = resource_root() / 'vendor/blender_mmd_tools-4.5.13/mmd_tools/externals/MikuMikuDance'
    for raw in model.materials:
        row = copy.deepcopy(raw)
        sources = {}
        for semantic, key in (('base_color', 'texture_index'), ('sphere', 'sphere_index'), ('toon', 'toon_index')):
            index = row.get(key, -1)
            shared = semantic == 'toon' and row.get('shared_toon', False)
            reference = ('toon%02d.bmp' % (index + 1)) if shared else (model.textures[index] if 0 <= index < len(model.textures) else '')
            target = (toon_root if shared else path.parent) / reference.replace('\\', '/')
            active = semantic != 'sphere' or row['sphere_mode'] != 0
            status = ('unreferenced' if not reference else 'disabled' if not active else
                      'available' if target.is_file() else 'missing')
            sources[semantic] = {'reference': reference, 'path': str(target.resolve()) if reference else '',
                                 'status': status, 'kind': 'shared_toon' if shared else 'pmx'}
        row.update(model_sha256=digest, model=str(path), sources=sources)
        rows.append(row)
    return {'model': str(path), 'sha256': digest, 'materials': rows, 'texture_count': len(model.textures),
            'custom_mapping_snapshot': VERSION}


def native_values(source):
    result = {'diffuse_color': source['diffuse'][:3], 'opacity': source['diffuse'][3],
              'specular_color': source['specular'], 'shininess': source['shininess'],
              'ambient_color': source['ambient']}
    for value in result.values():
        if any(not math.isfinite(float(v)) for v in (value if isinstance(value, list) else [value])):
            raise ValueError('PMX 材质包含非有限数值')
    return result


def numeric_assignments(source, parameters):
    values, assigned, ambiguous = native_values(source), {}, []
    for semantic, (kind, aliases) in NUMERIC.items():
        matches = [p['name'] for p in parameters if p['kind'] == kind and p.get('supported', True)
                   and re.sub(r'[\s_\-]', '', p['name']).casefold() in aliases]
        if len(matches) == 1:
            assigned[matches[0]] = {'kind': kind, 'value': values[semantic], 'source': semantic}
        elif matches:
            ambiguous.append({'source': semantic, 'parameters': matches})
    return assigned, ambiguous


def plan(cfg, source):
    parent, mappings, slot, inventory = selection(cfg, source)
    if not parent:
        raise ValueError('请为自选材质指定 UE 父材质')
    textures, retained = {}, []
    parameters = {p['name']: p for p in inventory.get('parameters', []) if p['kind'] == 'texture'}
    for target, binding in mappings.items():
        validate_binding(binding)
        parameter = parameters.get(target)
        if not parameter or not parameter.get('supported', True):
            raise ValueError('请重新读取母材质参数，目标不存在或不支持: ' + target)
        if parameter.get('color_space') and parameter['color_space'] != binding.get('color_space', 'srgb'):
            raise ValueError('贴图用途与母材质采样类型不兼容: ' + target + '，需要 ' + parameter['color_space'])
        semantic = binding['source']
        spec = copy.deepcopy(source['sources'].get(semantic, {'status': 'unreferenced', 'path': ''}))
        manual = slot.get('sources', {}).get(semantic)
        if manual:
            spec = dict(manual, status='available', kind='manual')
        if spec['status'] in ('unreferenced', 'disabled'):
            retained.append({'parameter': target, 'source': semantic, 'reason': spec['status']})
            continue
        if not spec.get('path') or not Path(spec['path']).is_file():
            raise ValueError(f"原槽 {source['pmx_index']} {source['name']} 的 {semantic} 缺图: {spec.get('reference', spec.get('path'))}")
        textures[target] = dict(binding, path=spec['path'], source_sha256=file_hash(spec['path']))
    if not inventory.get('signature'):
        raise ValueError('请连接 UE 并读取所选母材质的参数')
    numeric, ambiguous = numeric_assignments(source, inventory.get('parameters', []))
    identity = fingerprint({'version': VERSION, 'source': source, 'parent': parent,
                            'textures': textures, 'numeric': numeric, 'inventory': inventory['signature']})
    return {'version': VERSION, 'parent': parent, 'source': copy.deepcopy(source), 'textures': textures,
            'numeric': numeric, 'ambiguous_numeric': ambiguous, 'retained_defaults': retained,
            'inventory_signature': inventory['signature'], 'signature': identity}


def export_template(entry):
    return {'format': 'MMDBridgeCustomMapping', 'version': VERSION,
            'default_parents': copy.deepcopy(entry.get('default_parents', {})),
            'parents': {path: {'textures': copy.deepcopy(rule.get('textures', {}))}
                        for path, rule in entry.get('parents', {}).items()}}


def import_template(entry, template):
    if template.get('format') != 'MMDBridgeCustomMapping' or template.get('version') != VERSION:
        raise ValueError('不是受支持的自选母材质映射文件')
    if set(template) != {'format', 'version', 'parents', 'default_parents'} or not isinstance(template['parents'], dict):
        raise ValueError('映射文件不得包含模型贴图路径或槽位绑定')
    result = copy.deepcopy(entry)
    if not isinstance(template['default_parents'], dict) or set(template['default_parents']) - {'character', 'eye'}:
        raise ValueError('无效的默认父材质')
    for value in template['default_parents'].values():
        if value:
            asset_path(value)
    result['default_parents'] = copy.deepcopy(template['default_parents'])
    for parent, rule in template['parents'].items():
        path = asset_path(parent)
        if not isinstance(rule, dict) or set(rule) != {'textures'}:
            raise ValueError('无效的映射规则')
        _validate_textures(rule['textures'])
        old = result.setdefault('parents', {}).get(path, {})
        result['parents'][path] = dict(old, textures=copy.deepcopy(rule['textures']))
    return result


def checkpoint_signature(cfg):
    """Material-only contract; never mix this namespace into PBR geometry inputs."""
    model = cfg.get('_custom_source_model') or cfg.get('model', '')
    if not cfg.get('character_custom_materials') or not model or not Path(model).is_file() or not uses_custom_editor(cfg):
        return None
    entry = model_entry(cfg.get('character_custom_materials', {}), file_hash(model), cfg)
    if not entry:
        return None
    snapshot = inspect_model(model)
    return fingerprint([VERSION, [plan(cfg, row)['signature'] for row in snapshot['materials']]])
