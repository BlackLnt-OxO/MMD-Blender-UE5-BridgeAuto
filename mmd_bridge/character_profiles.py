"""Explicit character shader contracts; usable outside Blender and Unreal."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

VERSION = 1
FORMATS = ('none', 'direction_atlas', 'threshold_lr')
ROLES = ('face', 'skin', 'hair', 'eye', 'cloth_1', 'cloth_2', 'cloth_3')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def validate(profile):
    if profile.get('version') != VERSION:
        raise ValueError('不支持的角色母材质配置版本')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(profile.get('id', ''))):
        raise ValueError('角色母材质配置缺少有效 id')
    if not isinstance(profile.get('parents'), list) or not profile['parents']:
        raise ValueError('角色母材质配置缺少 parents')
    for parent in profile['parents']:
        if not isinstance(parent, str) or not parent.startswith('/Game/') or '..' in parent:
            raise ValueError('角色母材质配置包含无效父材质路径')
    eye_parent=profile.get('default_eye_parent')
    if eye_parent is not None and (not isinstance(eye_parent,str) or not eye_parent.startswith('/Game/') or '..' in eye_parent):
        raise ValueError('默认眼睛母材质路径无效')
    outline = profile.get('outline')
    if outline is not None:
        if not isinstance(outline,dict) or not isinstance(outline.get('parent'),str) or not outline['parent'].startswith('/Game/') or '..' in outline['parent']:
            raise ValueError('描边母材质路径无效')
        for key in ('scale','face_scale'):
            value=outline.get(key,.1)
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=2:
                raise ValueError('描边 '+key+' 需为 0 至 2 的有限数值')
    sdf = profile.get('sdf', {})
    if sdf.get('format', 'none') not in FORMATS:
        raise ValueError('不支持的 SDF 数据格式')
    if sdf.get('format', 'none') != 'none':
        size = sdf.get('tile_size', 512)
        if type(size) is not int or size < 16 or size > 512 or size & (size-1):
            raise ValueError('SDF tile_size 需为 16 至 512 的二次幂')
        if sdf.get('gutter',2)!=2:
            raise ValueError('当前方向图集边界保护必须为 2 像素')
        if sdf.get('format') == 'threshold_lr':
            if sdf.get('interpolation','pchip') not in ('linear','pchip'):
                raise ValueError('单张 SDF interpolation 需为 linear 或 pchip')
            resolution = sdf.get('resolution',1024)
            if type(resolution) is not int or not 16 <= resolution <= 4096 or resolution & (resolution-1):
                raise ValueError('单张 SDF resolution 需为 16 至 4096 的二次幂')
            for key,maximum in (('contrast',12),('smooth_sigma',16),('edge_pixels',16)):
                value = sdf.get(key,0)
                if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                    raise ValueError('单张 SDF '+key+' 数值超出范围')
            for key,default in (('nose_cutout',135),('cheek_triangle',98)):
                cutout = sdf.get(key,{})
                if not isinstance(cutout,dict) or type(cutout.get('enabled',False)) is not bool:
                    raise ValueError(key+' 需为对象，enabled 需为布尔值')
                end_angle = cutout.get('end_angle',default)
                if not isinstance(end_angle,(int,float)) or not math.isfinite(end_angle) or not 0<=end_angle<=180:
                    raise ValueError(key+'.end_angle 需为 0 至 180 度')
        if type(sdf.get('samples',64)) is not int or sdf.get('samples',64)<1:
            raise ValueError('SDF samples 必须为正整数')
        threshold=sdf.get('threshold',.05)
        if not isinstance(threshold,(int,float)) or not math.isfinite(threshold) or threshold<0:
            raise ValueError('SDF threshold 必须为非负有限数值')
    for kind in ('textures', 'scalars', 'vectors', 'switches'):
        if not isinstance(profile.get(kind, {}), dict):
            raise ValueError('母材质参数映射必须为对象: ' + kind)
        for semantic, binding in profile.get(kind, {}).items():
            if not isinstance(binding, dict) or not isinstance(binding.get('parameter'), str) or not binding['parameter']:
                raise ValueError('母材质参数映射缺少 parameter: ' + semantic)
            if kind=='textures' and binding.get('color_space','data') not in ('srgb','data','normal','half_float'):
                raise ValueError('不支持的贴图颜色空间: '+semantic)
            if 'value' in binding and kind in ('scalars','vectors','switches'):
                value=binding['value']
                values=value if kind=='vectors' else [value]
                if kind=='vectors' and (not isinstance(values,list) or len(values) not in (3,4)):
                    raise ValueError('向量默认值需为 3 或 4 个数值: '+semantic)
                if kind=='switches' and type(value) is not bool:
                    raise ValueError('开关默认值需为布尔值: '+semantic)
                if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
                    raise ValueError('参数默认值必须为有限数值: '+semantic)
    if not isinstance(profile.get('required',[]),list) or any(not isinstance(v,str) or not v for v in profile.get('required',[])):
        raise ValueError('required 必须为参数名数组')
    for target, recipe in profile.get('packed_textures', {}).items():
        if not isinstance(recipe, dict) or set(recipe.get('channels', {})) != set('rgba'):
            raise ValueError('打包纹理必须明确四个通道: ' + target)
        for expr in recipe['channels'].values():
            validate_expression(expr)
    for rule in profile.get('source_rules', []):
        if not rule.get('semantic') or not rule.get('pattern'):
            raise ValueError('附带贴图规则缺少 semantic / pattern')
        re.compile(rule['pattern'])
    for role, pattern in profile.get('role_rules', {}).items():
        if role not in ROLES:
            raise ValueError('未知角色材质部位: ' + role)
        re.compile(pattern)
    for value in profile.get('region_codes', {}).values():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('Lightmap 分区编码必须位于 0 至 1')
    presets=profile.get('role_presets',{})
    if not isinstance(presets,dict):
        raise ValueError('role_presets 必须为对象')
    for role,preset in presets.items():
        if role not in ROLES+('default',) or not isinstance(preset,dict) or set(preset)-{'scalars','vectors','switches'}:
            raise ValueError('无效的材质部位预设: '+role)
        for kind,parameters in preset.items():
            if not isinstance(parameters,dict):
                raise ValueError('部位参数预设必须为对象')
            for parameter,value in parameters.items():
                values=value if kind=='vectors' else [value]
                if not isinstance(parameter,str) or not parameter or (kind=='vectors' and (not isinstance(values,list) or len(values) not in (3,4))):
                    raise ValueError('部位预设缺少有效参数或向量: '+parameter)
                if kind=='switches' and type(value) is not bool:
                    raise ValueError('部位预设开关必须为布尔值')
                if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
                    raise ValueError('部位预设数值必须有限')
    return profile


def validate_expression(expr):
    if not isinstance(expr, dict):
        raise ValueError('通道表达式必须为对象')
    if set(expr) - {'value', 'source', 'channel', 'invert', 'scale'}:
        raise ValueError('通道表达式包含未知操作')
    if ('value' in expr) == ('source' in expr):
        raise ValueError('通道表达式需且仅需指定 value 或 source')
    if expr.get('channel', 'r') not in ('r', 'g', 'b', 'a', 'luminance'):
        raise ValueError('未知贴图源通道')
    for key in ('value', 'scale'):
        if key in expr and (not isinstance(expr[key], (int, float)) or not math.isfinite(expr[key])):
            raise ValueError('通道数值必须有限')


def load_profiles(resource_root, user_dir=''):
    folders = [Path(resource_root) / 'profiles' / 'character_materials']
    if user_dir:
        folders.append(Path(user_dir))
    profiles = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob('*.json')):
            profile = validate(json.loads(path.read_text(encoding='utf-8-sig')))
            if profile['id'] in profiles:
                raise ValueError('重复的角色母材质配置 id: ' + profile['id'])
            profiles[profile['id']] = profile
    return profiles


def resolve_profile(profiles, parent, selected='auto'):
    if selected == 'generic' or not parent:
        return None
    if selected != 'auto':
        if selected not in profiles:
            raise ValueError('角色母材质配置不存在: ' + selected)
        profile = profiles[selected]
        if parent not in profile['parents']:
            raise ValueError('所选配置与母材质路径不对应: ' + parent)
        return profile
    matches = [p for p in profiles.values() if parent in p['parents']]
    if len(matches) > 1:
        raise ValueError('母材质对应多个配置，请明确选择: ' + parent)
    return matches[0] if matches else None


def material_role(record, profile=None):
    override = record.get('character_material', {}).get('role')
    if override in ROLES:
        return override
    mmd = record.get('mmd', {})
    text = ' '.join(str(v) for v in (record.get('name', ''), record.get('source_name', ''),
                                     mmd.get('name_j', ''), mmd.get('name_e', ''))).lower()
    defaults = {
        'eye': r'(^|[^a-z])(eyes?|eyeballs?|iris|pupils?|sclera)([^a-z]|$)|eye[_ .-]*(white|ball|highlights?|lights?|shadow)|眼|瞳|目白|白目|黒目|目玉|目星|虹彩|(^|\s)目($|\s)',
        'hair': r'hair|髪|髮|头发|頭髮|发',
        'face': r'(^|[^a-z])face([^a-z]|$)|顔|颜|面[0-9０-９]*($|\s)',
        'skin': r'skin|肌|皮肤|皮膚',
    }
    if re.search(r'brow|lash|eyelid|blindfold|eye[_ .-]*mask|glasses|goggles|眼皮|眼睑|眼瞼|眼罩|眼镜|眼鏡|眉|睫|まゆ|まつげ|まぶた|二重|口|mouth|teeth|歯', text):
        return 'cloth_1'
    rules = dict(defaults)
    rules.update((profile or {}).get('role_rules', {}))
    return next((role for role, pattern in rules.items() if re.search(pattern, text, re.I)), 'cloth_1')


def is_eye_material(record):
    return material_role({k:v for k,v in record.items() if k!='character_material'})=='eye'


def requested_character_parent(settings, record, profiles):
    if not is_eye_material(record):
        return settings.get('character_master_material','')
    selected=settings.get('eye_master_material','')
    if selected:
        return selected
    profile=resolve_profile(profiles,settings.get('character_master_material',''),settings.get('character_material_profile','auto'))
    return profile.get('default_eye_parent','') if profile else ''


def source_values(record, profile):
    role = material_role(record, profile)
    codes = profile.get('region_codes', {})
    mode = int(record.get('mmd', {}).get('sphere_texture_type') or 0)
    has_sphere = bool(record.get('textures', {}).get('sphere')) and mode in (1, 2)
    return {'region_code': float(codes.get(role, codes.get('cloth_1', 0))),
            'sphere_g_weight': float(has_sphere and mode == 1),
            'sphere_b_weight': float(has_sphere and mode == 2), 'sphere_gate': 1.0,
            'normal_x': 0.0, 'normal_y': 0.0, 'ao_neutral': 1.0, 'matcap_neutral': 0.0}


def outline_values(record, profile, default_edges=False):
    mmd=record.get('mmd',{});settings=profile['outline']
    enabled=bool(mmd.get('enabled_toon_edge',True))
    color=list(mmd.get('edge_color',[0,0,0,1]))
    weight=float(mmd.get('edge_weight',1))
    if default_edges:
        text=' '.join(str(record.get(k,'')) for k in ('name','source_name')).lower()
        enabled=material_role(record,profile)!='eye' and not re.search(r'brow|lash|眉|睫|まゆ|まつげ|二重|mouth|teeth|口|歯',text)
        color=[0,0,0,1];weight=1.
    if len(color)!=4 or not all(math.isfinite(v) for v in color) or not math.isfinite(weight):
        raise ValueError('PMX 描边颜色或宽度无效')
    color[3]*=int(bool(enabled))
    scale=settings.get('face_scale',.04) if material_role(record,profile)=='face' else settings.get('scale',.1)
    return color,scale*min(100,max(0,weight))


def ramp_lit_gain(color):
    """Keep a PMX texture's lit color; retain the curve's relative shadow tint."""
    if len(color)!=3 or any(not math.isfinite(v) or v<=1e-4 for v in color):
        raise ValueError('Ramp 亮面颜色为零或无效，不能自动归一化')
    return [1/v for v in color]+[0.0]


def channel_value(expr, constants, pixels=None):
    """Evaluate a declared scalar or one source texel; never infer from color."""
    validate_expression(expr)
    if 'value' in expr:
        value = float(expr['value'])
    elif expr['source'] in constants:
        value = float(constants[expr['source']])
    else:
        if pixels is None or expr['source'] not in pixels:
            raise ValueError('缺少打包通道来源: ' + expr['source'])
        color = pixels[expr['source']]
        channel = expr.get('channel', 'r')
        value = (sum(float(color[i])*w for i, w in enumerate((.2126, .7152, .0722)))
                 if channel == 'luminance' else float(color['rgba'.index(channel)]))
    if expr.get('invert'):
        value = 1-value
    value *= float(expr.get('scale', 1))
    if not math.isfinite(value):
        raise ValueError('打包通道含非有限数值')
    return max(0., min(1., value))


def discover_sources(model, record, profile):
    """Only unique explicit rules bind files; ambiguous candidates remain evidence."""
    folder = Path(model).resolve().parent
    base = record.get('mmd', {}).get('texture_rel_path', '')
    stem = Path(str(base).replace('\\', '/')).stem
    files = sorted(p for p in folder.rglob('*') if p.is_file() and
                   p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tga', '.bmp', '.exr', '.tif', '.tiff'))
    bound, candidates = {}, []
    for rule in profile.get('source_rules', []):
        pattern = rule['pattern'].replace('{base}', re.escape(stem))
        matches = [p for p in files if re.fullmatch(pattern, p.name, re.I)]
        if len(matches) == 1:
            bound[rule['semantic']] = str(matches[0])
        elif matches:
            candidates.append({'semantic': rule['semantic'], 'reason': 'ambiguous',
                               'paths': [str(p) for p in matches]})
    assigned = set(bound.values())
    for path in files:
        if str(path) not in assigned and re.search(r'(?:light.?map|mask|normal|[_-]ao(?:\.|_)|[_-]orm(?:\.|_))',path.name,re.I):
            candidates.append({'reason':'unregistered_semantics','paths':[str(path)]})
    return bound, candidates


def binding_signature(record, profile, parent):
    from .core import file_hash
    source_hashes={semantic:file_hash(spec['path']) for semantic,spec in record.get('textures',{}).items()
                   if spec.get('path') and Path(spec['path']).is_file()}
    return fingerprint({'profile': profile, 'parent': parent,
                        'source_hashes':source_hashes,
                        'mmd': record.get('mmd', {}), 'textures': record.get('textures', {}),
                        'channels': record.get('channels', {}),
                        'character_material': record.get('character_material', {})})
