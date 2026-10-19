"""Deterministic, engine-independent character PBR inputs and material policy."""
from __future__ import annotations

import copy
import math
import re
import unicodedata
from pathlib import Path

from .core import file_hash, resource_root, stable_id
from .character_profiles import fingerprint

VERSION = 9
SHADING_VERSION = 9
DEFAULTS_VERSION = 3
PRESET = 'character_pbr'
LABEL = '内置PBR角色'
SKIN_SCATTERING = {
    'legacy_v8': {'face': .3, 'skin': .4, 'surface_albedo': (.8, .56, .48),
                  'mean_free_path_color': (1., .32, .17)},
    'balanced_v1': {'face': .27, 'skin': .35, 'surface_albedo': (.8, .62, .56),
                    'mean_free_path_color': (1., .55, .435)},
}
TYPES = {
    'skin': '皮肤', 'face': '面部', 'hair': '头发', 'iris': '虹膜',
    'sclera': '眼白', 'eye_highlight': '眼睛高光', 'eye_shadow': '眼影',
    'lash': '眉毛／睫毛', 'mouth': '口腔', 'teeth': '牙齿',
    'cloth': '布料', 'satin': '缎面／领带', 'hosiery': '丝袜',
    'leather': '皮革', 'metal': '金属', 'plastic': '塑料',
    'rubber': '橡胶', 'glass': '玻璃／宝石', 'generic': '通用PBR',
}
SEMANTICS = ('base_color', 'normal', 'roughness', 'metallic', 'ao', 'opacity',
             'emission', 'emission_mask', 'sphere', 'direction', 'iris_mask', 'sss_mask')
PARAMETERS = {
    'Roughness': (.02, 1., .55), 'Metallic': (0., 1., 0.), 'Specular': (0., 1., .4),
    'DetailStrength': (0., 1., .08), 'DetailTiling': (1., 300., 45.),
    'SSSStrength': (0., 1., .45), 'Anisotropy': (-.95, .95, .7),
    'HairDirection': (-360., 360., 90.), 'MatCapStrength': (0., 2., 1.),
    'ClearCoat': (0., 1., 0.), 'CoatRoughness': (.02, 1., .08),
    'Opacity': (0., 1., 1.), 'EmissiveStrength': (0., 100., 0.),
    'IrisDepth': (0., .03, 0.), 'Fuzz': (0., 1., 0.),
    'FabricDirection': (-360., 360., 90.),
    'HosieryCoverage': (0., 1., .5), 'HosieryEdgeDensity': (0., 1., 1.),
    'HosieryThickness': (0., 3., 1.),
    'FuzzBrightness': (0., 1., .08), 'FabricRoughnessVariation': (0., .5, .22),
    'SkinRosiness': (0., 1., .3),
    'SSSDetailPreservation': (0., 1., 1.),
    'SSSMaskFloor': (0., 1., .9),
    'WetEnabled': (0., 1., 0.), 'WetAmount': (0., 1., 1.),
    'WetTiling': (.1, 100., 8.), 'WetRotation': (-360., 360., 0.),
    'WetNormalStrength': (0., 2., 1.), 'WetRoughness': (.02, .5, .04),
}
DEFAULTS = {
    'skin': {'Roughness': .48, 'Specular': .35, 'DetailStrength': .22, 'SSSStrength': .4},
    'face': {'Roughness': .5, 'Specular': .32, 'DetailStrength': .14, 'SSSStrength': .3},
    'hair': {'Roughness': .38, 'Specular': .4, 'DetailStrength': .06, 'DetailTiling': 2.},
    'iris': {'Roughness': .28, 'ClearCoat': 1., 'CoatRoughness': .06, 'DetailStrength': 0., 'EmissiveStrength': .35},
    'sclera': {'Roughness': .38, 'ClearCoat': .65, 'CoatRoughness': .1, 'DetailStrength': 0., 'EmissiveStrength': .2},
    'eye_highlight': {'Roughness': .18, 'ClearCoat': .5, 'DetailStrength': 0., 'EmissiveStrength': .3},
    'eye_shadow': {'Roughness': .8, 'Specular': .08, 'DetailStrength': 0.},
    'lash': {'Roughness': .75, 'Specular': .12, 'DetailStrength': 0.},
    'mouth': {'Roughness': .4, 'DetailStrength': 0.},
    'teeth': {'Roughness': .35, 'Specular': .35, 'ClearCoat': .3, 'CoatRoughness': .2, 'DetailStrength': 0.},
    'cloth': {'Roughness': .84, 'Specular': .22, 'DetailStrength': .32, 'Fuzz': .7, 'DetailTiling': 95.},
    'satin': {'Roughness': .56, 'Specular': .3, 'DetailStrength': .18,
              'Anisotropy': .35, 'DetailTiling': 95., 'FabricRoughnessVariation': .16},
    'hosiery': {'Roughness': .4, 'Specular': .45, 'DetailStrength': .12,
                'Anisotropy': .45, 'DetailTiling': 135.},
    'leather': {'Roughness': .4, 'DetailStrength': .15, 'DetailTiling': 75.},
    'metal': {'Roughness': .27, 'Metallic': 1., 'DetailStrength': .025},
    'plastic': {'Roughness': .32, 'ClearCoat': .25, 'DetailStrength': .02},
    'rubber': {'Roughness': .78, 'Specular': .25, 'DetailStrength': .05},
    'glass': {'Roughness': .08, 'ClearCoat': 1., 'DetailStrength': 0.},
    'generic': {},
}


def detail_map_names(kind):
    """Separate surface microstructure; water remains an optional skin layer."""
    if kind in ('skin', 'face'):
        return {'detail_normal': 'skin_normal.png', 'detail_roughness': 'skin_roughness.png',
                'wet_normal': 'wet_normal.png', 'wet_mask': 'wet_mask.png'}
    if kind == 'hosiery':
        return {'detail_normal': 'hosiery_normal.png', 'detail_roughness': 'hosiery_roughness.png',
                'hosiery_mask': 'hosiery_mask.png'}
    if kind in ('cloth', 'satin', 'leather'):
        return {'detail_normal': kind+'_normal.png', 'detail_roughness': kind+'_roughness.png'}
    return {'detail_normal': 'hair_normal.png' if kind == 'hair' else 'micro_normal.png'}


def parameter_defaults(kind, scattering='legacy_v8'):
    result = {key: default for key, (_, _, default) in PARAMETERS.items()}
    result.update(DEFAULTS[kind])
    if kind in ('skin', 'face'):
        result['SSSStrength'] = SKIN_SCATTERING[scattering][kind]
    return result


def detail_bindings(kind, folder=None):
    folder = Path(folder) if folder else resource_root()/'resources/character_materials/Character_PBR'
    result = {}
    for semantic, name in detail_map_names(kind).items():
        path = folder/name
        digest = file_hash(path)
        result[semantic] = {'path': str(path), 'id': stable_id(name, digest), 'sha256': digest,
                            'name': path.stem, 'asset_name': 'T_PBR_'+path.stem,
                            'color_space': 'Non-Color', 'lookup': False, 'generated_bake': True}
    return result
LIP_PATTERN = (r'(^|[^a-z])lips?(?![ _.-]*(?:stick|gloss)\b)([^a-z]|$)'
               r'|嘴唇|唇(?!膏|釉)|くちびる|リップ(?!スティック|グロス)')
RULES = (
    ('eye_shadow', r'eye.?shadow|目影|目陰|眼影|hairshadow|hair.?shadow|[髪髮发]影'),
    ('lash', r'eyebrow|eyelash|eyeline|eyelid|(^|[^a-z])brows?([^a-z]|$)|眉|睫|まつげ|まゆ|二重|目線'),
    ('eye_highlight', r'eye.?lights?|eye.?highlights?|^eye[ _.-]*hi[0-9]*$|^hl[0-9]*$|目光|目星|ハイライト'),
    ('sclera', r'eye.?white|sclera|白目|白い目|目白|眼白'),
    ('iris', r'(^|[^a-z])(eyes?|pupils?|iris)([^a-z]|$)|瞳|虹彩|眼睛|(^|\s)目($|\s)'),
    ('teeth', r'teeth|tooth|齿|齒|歯|牙'),
    ('face', LIP_PATTERN),
    ('mouth', r'mouth|tongue|舌|口腔|腔内|口舌|(^|\s)口($|\s)'),
    ('metal', r'metal|silver|gold|金属|金屬|銀|铝|鋁|钢|鋼|铁|鐵|メタル|シルバー|ゴールド'),
    ('glass', r'glass|lens|jewel|gem|镜片|鏡片|透明罩|宝石|寶石|ガラス|レンズ'),
    ('plastic', r'plastic|headphone|耳机|耳機|电池|電池|塑料|发饰|髮飾|hairpin|clips|プラスチック'),
    # A button's surrounding garment name does not identify its substance.
    ('generic', r'(^|[^a-z])buttons?([^a-z]|$)|钮扣|鈕扣|纽扣|釦|^(shoes?|boots?)([ _.-]*[0-9]+)?$'),
    ('hair', r'hair|bangs|bunches|髪|髮|头发|頭髮|(^|\s)发[0-9]*($|\s)'),
    ('face', r'(^|[^a-z])face[0-9]*([^a-z]|$)|顔|颜|脸|臉|面[１２12]?($|\s)'),
    ('skin', r'(^|[^a-z])(skin|body|arm|arms|nails?)([^a-z]|$)|肌|皮肤|皮膚|身体|身體|(^|\s)体($|\s)'),
    ('leather', r'leather|皮革|皮靴|皮鞋|皮带|皮帶|レザー'),
    ('rubber', r'rubber|sole|橡胶|橡膠|鞋底|ゴム'),
    ('hosiery', r'stockings?|hosiery|pantyhose|(^|[^a-z])tights?([^a-z]|$)|丝袜|絲襪|连裤袜|連褲襪'),
    ('satin', r'satin|silk|(^|[^a-z])tie([^a-z]|$)|缎面|緞面|丝绸|絲綢|领带|領帶'),
    ('cloth', r'cloth|shirt|skirt|shorts?|pants|trousers?|sweatshirt|tie|coat|glove|stocking|ribbon|garter|pocket|jacket|cape|(^|[^a-z])top([^a-z]|$)|衣|裙|袜|襪|裤|褲|布|袖|手套|手袋|领带|領帶|上着|外套|披风|披風|シャツ|ショーツ|スウェット|クローク|リボン|蝴蝶[结結]'),
)
SUFFIXES = {
    'normal': ('normal', 'n', 'norm'), 'roughness': ('roughness', 'rough', 'r'),
    'metallic': ('metallic', 'metalness', 'metal', 'm'), 'ao': ('ao', 'ambientocclusion'),
    'opacity': ('opacity', 'alpha', 'cutout'), 'emission': ('emissive', 'emission', 'e'),
    'emission_mask': ('emissionmask', 'emissivemask', 'glowmask'),
    'direction': ('flow', 'direction', 'tangent'), 'iris_mask': ('irismask',),
    'sss_mask': ('sssmask', 'subsurfacemask'),
}


def classification_evidence(record, *, use_surface_evidence=True):
    """Prefer original part names over generic English exporter/shader labels."""
    mmd = record.get('mmd', {})
    fields = (('name', record.get('name', '')), ('mmd.name_j', mmd.get('name_j', '')),
              ('mmd.name_e', mmd.get('name_e', '')))
    matches, seen = [], set()
    for field, raw in fields:
        value = unicodedata.normalize('NFKC', str(raw)).strip()
        value = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])', ' ', value)
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        for kind, pattern in RULES:
            match = re.search(pattern, value, re.I)
            if match:
                matches.append({'field': field, 'value': str(raw), 'type': kind,
                                'matched': match.group(0).strip()})
    chosen = matches[0] if matches else {'field': None, 'value': '', 'type': 'generic', 'matched': ''}
    result = dict(chosen, method='name_rule' if matches else 'unrecognized')
    if chosen['type'] == 'face' and re.search(LIP_PATTERN, chosen['matched'], re.I):
        result['surface_role'] = 'lips'
    result['conflicts'] = [match for match in matches if match['type'] != chosen['type']]
    kind = chosen['type']
    if (kind == 'skin' and re.search(r'robot|机器人|機器人|机械|機械', record.get('model_name', ''), re.I)
            and re.fullmatch(r'body|arms?|身体|身體|体', chosen['matched'], re.I)):
        kind = 'generic'
        result.update(type=kind, method='context_review', automatic_type='skin',
                      review_reason='机械模型的身体名称不能确定物理材质，请确认金属、塑料或其他类型')
    stats = record.get('character_material', {}).get('uv_color_statistics', {})
    if (use_surface_evidence and kind == 'iris' and re.fullmatch(r'eyes?[ _.-]*[0-9]*', record.get('name',''), re.I)
            and stats.get('samples',0) >= 128 and stats.get('bright_neutral_ratio',0) > .95
            and max(stats.get('std',[1])) < .05):
        kind = 'sclera'
        result.update(method='uv_eye_color', type=kind)
    result['requires_review'] = kind == 'generic' or bool(result['conflicts'])
    if re.search(r'スーツ|(^|[^a-z])suits?([^a-z]|$)', ' '.join(str(value) for _, value in fields), re.I):
        result['review_reason'] = '服装名称未说明是布料、皮革还是塑料，请选择实际材质'
        result['requires_review'] = True
    return result


def classify(record, *, use_surface_evidence=True):
    return classification_evidence(record, use_surface_evidence=use_surface_evidence)['type']


def review_reasons(pbr):
    """Classification uncertainty is separate from missing-resource fallback."""
    evidence = pbr.get('classification', {})
    reasons = []
    if evidence.get('method') != 'manual_override' and evidence.get('requires_review', pbr.get('type') == 'generic'):
        reasons.append(evidence.get('review_reason') or ('名称命中冲突，请确认材质类型' if evidence.get('conflicts') else '名称未明确识别，请选择材质类型'))
    if evidence.get('face_defaults') in ('no_face_surface', 'ambiguous_face_parameters'):
        reasons.append('嘴唇的面部参数来源待确认')
    reasons.extend(pbr.get('issues', []))
    return list(dict.fromkeys(reasons))


def analysis_record(row):
    """Reconstruct a planner input from both current and older GUI snapshots."""
    pbr = copy.deepcopy(row['pbr'])
    sources = pbr.get('source_textures')
    if sources is None:
        sources = {key: copy.deepcopy(spec) for key, spec in pbr.get('textures', {}).items()
                   if spec.get('kind', 'pmx') == 'pmx' and key in SEMANTICS}
    record = copy.deepcopy(row.get('source_record', {}))
    record.update(name=row['name'], pmx_index=row['pmx_index'])
    record.setdefault('textures', copy.deepcopy(sources))
    mmd = record.setdefault('mmd', {})
    mmd.setdefault('name_j', row['name']); mmd.setdefault('name_e', row.get('name_e', ''))
    diffuse = row.get('diffuse', [1, 1, 1, pbr.get('params', {}).get('Opacity', 1)])
    mmd.setdefault('diffuse_color', diffuse[:3]); mmd.setdefault('diffuse_alpha', diffuse[3])
    mmd.setdefault('sphere_texture_type', row.get('sphere_mode', pbr.get('matcap', {}).get('mode', 0)))
    for semantic, key in (('base_color', 'texture_rel_path'), ('sphere', 'sphere_texture_rel_path')):
        reference = pbr.get('source_references', {}).get(key, '')
        if not reference:
            reference = next((item['reference'] for item in pbr.get('missing_effects', []) if item['semantic'] == semantic), '')
        if not reference and semantic == 'base_color':
            reference = next((issue.partition(': ')[2] for issue in pbr.get('issues', []) if issue.startswith('PMX基础贴图引用缺失: ')), '')
        mmd.setdefault(key, reference or Path(sources.get(semantic, {}).get('path', '')).name)
    record.setdefault('character_material', {}).update(pbr=pbr, alpha_mode=pbr.get('alpha_mode', 'opaque'))
    return record


def validate_override(value):
    if not isinstance(value, dict):
        raise ValueError('角色材质覆盖必须为对象')
    if value.get('type', 'auto') not in ('auto', *TYPES):
        raise ValueError('未知PBR材质类型')
    if value.get('alpha_mode', 'auto') not in ('auto', 'opaque', 'masked', 'translucent'):
        raise ValueError('未知透明模式')
    if value.get('matcap_mode', 'source_auto') not in ('source_auto', 'on', 'off'):
        raise ValueError('未知MatCap模式')
    for key, number in value.get('params', {}).items():
        if key not in PARAMETERS or type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError('无效PBR参数: ' + key)
        low, high, _ = PARAMETERS[key]
        if not low <= number <= high:
            raise ValueError('PBR参数超出范围: ' + key)
    for semantic, binding in value.get('textures', {}).items():
        if semantic not in SEMANTICS or not isinstance(binding, dict) or not binding.get('path'):
            raise ValueError('无效贴图绑定: ' + semantic)
        if binding.get('channel', 'rgb' if semantic in ('normal', 'base_color', 'sphere', 'direction', 'emission') else 'r') not in ('rgb', 'r', 'g', 'b', 'a'):
            raise ValueError('无效贴图通道: ' + semantic)
        if semantic in ('normal', 'base_color', 'sphere', 'direction', 'emission') and binding.get('channel', 'rgb') != 'rgb':
            raise ValueError('此贴图用途需要RGB通道: ' + semantic)
        if semantic not in ('normal', 'base_color', 'sphere', 'direction', 'emission') and binding.get('channel', 'r') == 'rgb':
            raise ValueError('标量贴图需要明确R/G/B/A通道: ' + semantic)
        if 'invert' in binding and type(binding['invert']) is not bool:
            raise ValueError('反转贴图必须为布尔值')
    return value


def validate_settings(options, overrides):
    if not isinstance(options, dict) or not isinstance(overrides, dict):
        raise ValueError('角色PBR设置必须为对象')
    validate_override(options)
    for entries in overrides.values():
        if not isinstance(entries, dict):
            raise ValueError('角色材质覆盖缺少模型分组')
        for index, entry in entries.items():
            if not str(index).isdigit():
                raise ValueError('材质槽索引无效')
            validate_override(entry)


def override_for(cfg, model_hash, index):
    result = copy.deepcopy(cfg.get('character_pbr_options', {}))
    local = cfg.get('character_material_overrides', {}).get(model_hash, {}).get(str(index), {})
    for key, value in local.items():
        if key in ('params', 'textures'):
            result.setdefault(key, {}).update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return validate_override(result)


def texture_files(model):
    return sorted(p for p in Path(model).resolve().parent.rglob('*') if p.is_file() and
                  p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tga', '.bmp', '.tif', '.tiff', '.exr', '.dds', '.sph', '.spa'))


def discover(base, files):
    """Only bind a unique map associated with the same base texture stem."""
    stem = Path(str(base).replace('\\', '/')).stem
    stem = re.sub(r'(?i)(?:[_ .-](?:basecolor|albedo|diffuse|d))$', '', stem)
    bound, candidates = {}, []
    for semantic, suffixes in SUFFIXES.items():
        pattern = re.compile(re.escape(stem) + r'[_ .-](?:' + '|'.join(suffixes) + r')$', re.I)
        matches = [p for p in files if pattern.fullmatch(p.stem)] if stem else []
        if len(matches) == 1:
            bound[semantic] = {'path': str(matches[0]), 'channel': 'rgb' if semantic in ('normal', 'emission', 'direction') else 'r', 'kind': 'sidecar'}
        elif matches:
            candidates.append({'semantic': semantic, 'reason': 'ambiguous', 'paths': [str(p) for p in matches]})
    candidates.extend({'reason': 'unknown_packed_channels', 'paths': [str(p)]} for p in files
                      if re.search(r'light.?map|(?:^|[_ -])(?:orm|rma|msre)|mask', p.stem, re.I)
                      and str(p) not in {v['path'] for v in bound.values()})
    return bound, candidates


def plan(record, cfg, model_hash, files=()):
    override = override_for(cfg, model_hash, record['pmx_index'])
    context_record = dict(record, model_name=record.get('model_name') or Path(cfg.get('model', '')).stem)
    evidence = classification_evidence(context_record, use_surface_evidence='base_color' not in override.get('textures', {}))
    kind = override.get('type', 'auto')
    if kind == 'auto':
        kind = evidence['type']
    else:
        evidence = dict(evidence, automatic_type=evidence['type'], type=kind,
                        method='manual_override', field='override.type', matched=kind)
    previous = record.get('character_material', {}).get('pbr', {})
    generated = previous.get('generated_defaults', {}) if previous.get('type') == kind else {}
    scattering = cfg.get('character_skin_scattering', 'legacy_v8')
    params = parameter_defaults(kind, scattering)
    face_source = previous.get('face_parameter_source', {}) if kind == 'face' and evidence.get('surface_role') == 'lips' else {}
    if face_source.get('model_sha256') != model_hash:
        face_source = {}
    scattering = face_source.get('skin_scattering', scattering)
    # Keep the imported face defaults during a later single-slot edit. Explicit
    # lip overrides and source visibility still take precedence below.
    params.update(face_source.get('params', {}))
    params.update({key: value for key, value in generated.items()
                   if key in ('DetailTiling', 'WetTiling', 'WetRotation', 'FabricDirection')})
    params['Opacity'] = float(record.get('mmd', {}).get('diffuse_alpha', 1.))
    params.update(override.get('params', {}))
    bound, candidates = discover(record.get('mmd', {}).get('texture_rel_path', ''), files)
    source_textures = previous.get('source_textures')
    if source_textures is None:
        source_textures = {semantic: spec for semantic, spec in record.get('textures', {}).items()
                           if semantic in SEMANTICS and previous.get('textures', {}).get(semantic, {}).get('kind', 'pmx') == 'pmx'}
    source_textures = copy.deepcopy(source_textures)
    for semantic, spec in source_textures.items():
        if semantic in SEMANTICS:
            bound[semantic] = {'path': spec['path'], 'channel': 'rgb' if semantic in ('base_color', 'normal', 'sphere', 'direction', 'emission') else 'r', 'kind': 'pmx'}
    for semantic, spec in override.get('textures', {}).items():
        bound[semantic] = dict(spec, kind='manual')
    generated_direction = previous.get('textures', {}).get('direction', {})
    if kind == 'hair' and 'direction' not in bound and generated_direction.get('kind') == 'generated':
        bound['direction'] = copy.deepcopy(generated_direction)
    generated_sss = previous.get('textures', {}).get('sss_mask', {})
    shared_face_mask = previous.get('face_scattering_source', {}).get('method') == 'shared_face_atlas_mask'
    if (kind == 'face' and (evidence.get('surface_role') != 'lips' or shared_face_mask)
            and 'sss_mask' not in bound and generated_sss.get('kind') == 'generated'):
        bound['sss_mask'] = copy.deepcopy(generated_sss)
    mode = int(record.get('mmd', {}).get('sphere_texture_type') or 0)
    selection = override.get('matcap_mode', 'source_auto')
    enabled = (mode in (1, 2) and 'sphere' in bound) if selection == 'source_auto' else selection == 'on'
    issues, missing_effects = [], []
    base_ref = record.get('mmd', {}).get('texture_rel_path', '')
    if base_ref and 'base_color' not in bound:
        issues.append('PMX基础贴图引用缺失: ' + base_ref)
    if enabled and 'sphere' not in bound:
        issues.append('MatCap已启用但未指定贴图')
    if selection == 'source_auto' and mode in (1, 2) and record.get('mmd', {}).get('sphere_texture_rel_path') and 'sphere' not in bound:
        reference = record['mmd']['sphere_texture_rel_path']
        missing_effects.append({'semantic': 'sphere', 'reference': reference,
                                'fallback': 'preserve_base_color', 'source_mode': mode})
    if selection == 'source_auto' and mode == 3:
        issues.append('PMX额外UV Sphere SubTexture尚不能转换')
    for semantic, binding in bound.items():
        path = Path(binding['path'])
        if not path.is_file():
            issues.append('贴图文件不存在: ' + semantic + ' / ' + str(path))
        else:
            binding['sha256'] = file_hash(path)
        binding.setdefault('channel', 'rgb' if semantic in ('base_color', 'normal', 'sphere', 'direction', 'emission') else 'r')
    alpha = override.get('alpha_mode', 'auto')
    if alpha == 'auto':
        alpha = record.get('character_material', {}).get('alpha_mode', 'opaque')
        if alpha == 'opaque' and params['Opacity'] == 1 and face_source.get('shared_masked_atlas'):
            alpha = 'masked'
    if params['Opacity'] == 0:
        alpha = 'masked'
    if 'emission' in bound and 'EmissiveStrength' not in override.get('params', {}):
        params['EmissiveStrength'] = 1.
    result = {'version': VERSION, 'model_sha256': model_hash, 'type': kind, 'params': params,
              'classification': evidence, 'shading_version': SHADING_VERSION, 'defaults_version': DEFAULTS_VERSION,
              'alpha_mode': alpha, 'matcap': {'enabled': enabled, 'mode': mode if mode in (1, 2) else 2,
                                             'selection': selection},
              'textures': bound, 'candidates': candidates, 'issues': issues,
              'generated_defaults': copy.deepcopy(generated),
              'source_textures': source_textures,
              'source_references': {key: record.get('mmd', {}).get(key, '') for key in ('texture_rel_path', 'sphere_texture_rel_path')},
              'status': 'white' if issues else 'needs_review' if missing_effects else 'ready'}
    if kind == 'face' and evidence.get('surface_role') == 'lips':
        result['surface_role'] = 'lips'
        if face_source:
            result['face_parameter_source'] = copy.deepcopy(face_source)
    if missing_effects:
        result['missing_effects'] = missing_effects
        result['issues'].extend('PMX效果贴图缺失，保留可用基础色: '+item['reference'] for item in missing_effects)
        result['matcap'].update(source_requested=True, available=False)
    if kind in ('skin', 'face') and scattering != 'legacy_v8':
        result['skin_scattering'] = scattering
    if previous.get('type') == kind:
        for key in ('surface_scale', 'face_scattering_source', 'direction_source'):
            semantic = {'face_scattering_source': 'sss_mask', 'direction_source': 'direction'}.get(key)
            if semantic and bound.get(semantic, {}).get('kind') != 'generated':
                continue
            if key in previous:
                result[key] = copy.deepcopy(previous[key])
    result['signature'] = fingerprint(result)
    return result


def inherit_lip_face_defaults(records, cfg, model_hash):
    """Use the matching face's numeric defaults for separate lip surfaces.

    Texture colours, UVs, alpha and explicit per-slot overrides stay authored.
    This is import-time inheritance, so editing one slot never edits another.
    """
    records = list(records)
    faces = [row for row in records if row.get('character_material', {}).get('pbr', {}).get('type') == 'face'
             and row['character_material']['pbr'].get('surface_role') != 'lips'
             and row['character_material']['pbr'].get('status') != 'white'
             and row['character_material']['pbr']['params'].get('Opacity', 1) > 0]

    def base_key(row):
        pbr = row['character_material']['pbr']
        spec = pbr.get('source_textures', {}).get('base_color', {})
        return spec.get('sha256') or str(spec.get('path', '')).replace('\\', '/').casefold()

    for row in records:
        pbr = row.get('character_material', {}).get('pbr', {})
        if pbr.get('type') != 'face' or pbr.get('surface_role') != 'lips':
            continue
        matching = [face for face in faces if base_key(row) and base_key(face) == base_key(row)]
        candidates = matching or faces
        if not candidates:
            pbr['classification']['requires_review'] = True
            pbr['classification']['face_defaults'] = 'no_face_surface'
        elif len({fingerprint(face['character_material']['pbr']['params']) for face in candidates}) > 1 and len(candidates) > 1:
            pbr['classification']['requires_review'] = True
            pbr['classification']['face_defaults'] = 'ambiguous_face_parameters'
        else:
            face = min(candidates, key=lambda item: item['pmx_index'])
            source = face['character_material']['pbr']
            override = override_for(cfg, model_hash, row['pmx_index'])
            explicit = override.get('params', {})
            opacity = pbr['params']['Opacity']
            pbr['params'] = copy.deepcopy(source['params'])
            pbr['params']['Opacity'] = opacity
            pbr['params'].update(explicit)
            pbr['generated_defaults'] = {key: source['params'][key] for key in source.get('generated_defaults', {})}
            pbr['face_parameter_source'] = {'pmx_index': face['pmx_index'], 'name': face['name'],
                                          'model_sha256': model_hash, 'params': copy.deepcopy(source['params']),
                                          'skin_scattering': source.get('skin_scattering', 'legacy_v8')}
            if 'skin_scattering' in source:
                pbr['skin_scattering'] = source['skin_scattering']
            else:
                pbr.pop('skin_scattering', None)
            # A fully opaque lip region can safely use its shared atlas's
            # masked face parent. Never remove cutouts or source translucency.
            if (matching and source.get('alpha_mode') == 'masked' and pbr.get('alpha_mode') == 'opaque'
                    and pbr['params']['Opacity'] == 1 and override.get('alpha_mode', 'auto') == 'auto'):
                pbr['alpha_mode'] = 'masked'
                pbr['face_parameter_source']['shared_masked_atlas'] = True
            pbr['classification']['face_defaults'] = 'matching_texture' if matching else 'unique_face_parameters'
            if (matching and 'sss_mask' in source.get('textures', {})
                    and 'sss_mask' not in override.get('textures', {})):
                pbr['textures']['sss_mask'] = copy.deepcopy(source['textures']['sss_mask'])
                if 'sss_mask' in face.get('textures', {}):
                    row.setdefault('textures', {})['sss_mask'] = copy.deepcopy(face['textures']['sss_mask'])
                pbr['face_scattering_source'] = {'method': 'shared_face_atlas_mask', 'pmx_index': face['pmx_index']}
        pbr['signature'] = fingerprint({key: value for key, value in pbr.items() if key != 'signature'})


def inspect_model(path, cfg=None):
    from .pmx_reader import read_pmx
    model = read_pmx(path)
    digest = file_hash(path)
    files = texture_files(path)
    rows, records = [], []
    for material in model.materials:
        item = dict(material)
        textures = {}
        for semantic, key in (('base_color', 'texture_index'), ('sphere', 'sphere_index')):
            index = item[key]
            if 0 <= index < len(model.textures):
                relative = model.textures[index]
                candidate = Path(path).parent / relative.replace('\\', '/')
                if candidate.is_file():
                    textures[semantic] = {'path': str(candidate.resolve())}
        record = {'name': item['name'], 'pmx_index': item['pmx_index'], 'textures': textures,
                  'model_name': model.encoded_name+' '+Path(path).stem,
                  'mmd': {'name_j': item['name'], 'name_e': item['name_e'],
                          'diffuse_alpha': item['diffuse'][3], 'diffuse_color': item['diffuse'][:3],
                          'sphere_texture_type': item['sphere_mode'],
                          'texture_rel_path': model.textures[item['texture_index']] if 0 <= item['texture_index'] < len(model.textures) else '',
                          'sphere_texture_rel_path': model.textures[item['sphere_index']] if 0 <= item['sphere_index'] < len(model.textures) else ''}}
        item['pbr'] = plan(record, cfg or {}, digest, files)
        item['source_record'] = copy.deepcopy(record)
        record['character_material'] = {'pbr': item['pbr']}
        records.append(record)
        rows.append(item)
    inherit_lip_face_defaults(records, cfg or {}, digest)
    return {'model': str(Path(path).resolve()), 'sha256': digest, 'materials': rows,
            'texture_count': len(files)}
