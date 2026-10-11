"""Explicit surface overrides and conservative foliage atlas repair."""
import copy
import re
from pathlib import Path
from .core import stable_id
from .material_contract import closure_features
from .material_graph import FunctionLibrary

SURFACE_MODES = (('镂空遮罩', 'masked'), ('不透明', 'opaque'),
                 ('薄片透光＋镂空', 'thin'), ('透射折射', 'transmission'))


def selected_surface_mode(targets):
    modes = set()
    for row in targets:
        if row.get('reason'): continue
        facts = row.get('facts', {})
        blend = facts.get('blend_mode', '')
        if 'BLEND_MASKED' in blend:
            mode = 'thin' if facts.get('thin_scattering') else 'masked'
        elif 'BLEND_OPAQUE' in blend:
            mode = 'opaque'
        elif 'BLEND_TRANSLUCENT' in blend:
            mode = 'transmission'
        else:
            mode = 'opaque'
        modes.add(mode)
    return next(iter(modes)) if len(modes) == 1 else 'opaque'


def textures(value):
    if isinstance(value, dict):
        if value.get('op') == 'texture':
            yield value
        else:
            for child in value.values(): yield from textures(child)
    elif isinstance(value, list):
        for child in value: yield from textures(child)


def texture_family(field, role):
    matches = [re.fullmatch(r'(.+)[_ .-]' + role,
                           Path(t['image'].get('name', '')).stem, re.I) for t in textures(field)]
    families = {m.group(1).casefold() for m in matches if m}
    return next(iter(families)) if len(families) == 1 else None


def foliage_atlas_opacity(record):
    """Require paired atlas maps, a misplaced mask and black emission."""
    c = record.get('channels', {})
    if record.get('water') or record.get('surface') != 'pbr': return None
    if c.get('opacity') != {'op': 'constant', 'value': 1}: return None
    emission = c.get('emission', {})
    if emission.get('op') != 'constant' or any(emission.get('value', [1])[:3]): return None
    opacity = c.get('emission_strength')
    if len({t['image']['id'] for t in textures(opacity) if texture_family(t, 'Opacity')}) != 1: return None
    family = texture_family(opacity, 'Opacity')
    if not family or family != texture_family(c.get('transmission'), 'Translucency'): return None
    if family != texture_family(c.get('base_color'), 'Albedo'): return None
    return opacity


def coverage(record):
    c = record.get('channels', {})
    alpha = c.get('opacity')
    if alpha and (alpha.get('op') != 'constant' or alpha.get('value', 1) < 1):
        return copy.deepcopy(alpha), 'source_alpha'
    # Manual override may use a uniquely named, already exported opacity map.
    candidates = {t['image']['id']: t for t in textures(c)
                  if texture_family(t, 'Opacity') is not None}
    if len(candidates) != 1:
        raise ValueError('没有唯一可靠的 Alpha／Opacity 遮罩；保留原材质，请先补充源遮罩')
    texture = copy.deepcopy(next(iter(candidates.values())))
    if texture.get('output') == 'Alpha': texture['output'] = 'Color'
    return {'op': 'to_scalar', 'value': texture, 'weights': [.2126, .7152, .0722]}, 'named_opacity_texture'


def surface_override(record, mode, definitions=(), geometry=None):
    """Convert an isolated copy; never edit a shared UE material in place."""
    if mode not in {value for _, value in SURFACE_MODES}: raise ValueError('无效的材质表面逻辑')
    if record.get('pmx_index') is not None or record.get('material_error'):
        raise ValueError('角色或失败占位材质不能直接切换场景表面逻辑')
    result = copy.deepcopy(record)
    alpha, source = ({'op': 'constant', 'value': 1}, 'opaque') if mode in ('opaque', 'transmission') else coverage(record)
    library = FunctionLibrary()
    library.definitions = {d['id']: d for d in [*definitions, *record.get('function_definitions', [])]}
    graph = library.expand(record['surface_graph']) if record.get('surface_graph') else {
        'kind': 'bsdf_glass' if record.get('surface') == 'glass' else 'bsdf_principled',
        'channels': copy.deepcopy(record.get('channels', {}))}

    def convert(node):
        if node['kind'] in ('mix', 'add'):
            return {**node, 'a': convert(node['a']), 'b': convert(node['b'])}
        kind = node['kind']
        if kind not in ('bsdf_principled', 'bsdf_glass', 'bsdf_refraction', 'bsdf_transparent', 'bsdf_glossy', 'bsdf_metallic',
                        'bsdf_diffuse', 'bsdf_translucent', 'emission'):
            raise ValueError('该表面节点无法安全切换：' + kind)
        c = copy.deepcopy(node['channels'])
        transmission = c.pop('transmission', {'op': 'constant', 'value': .35})
        if transmission.get('op') == 'constant' and transmission.get('value', 0) == 0:
            transmission = {'op': 'constant', 'value': .35}
        if kind in ('bsdf_glass','bsdf_refraction') and mode == 'transmission':
            c['opacity'] = {'op': 'constant', 'value': 1}
            changed = {'kind': kind, 'channels': c}
            changed['features'] = sorted(closure_features(changed))
            return changed
        if kind in ('bsdf_glass','bsdf_refraction'): kind = 'bsdf_principled'
        if kind == 'bsdf_transparent': kind = 'bsdf_diffuse'
        if kind == 'bsdf_translucent': kind = 'bsdf_diffuse'
        c['opacity'] = {'op': 'constant', 'value': 1}
        if mode == 'transmission':
            if kind == 'emission': raise ValueError('纯发光表面没有可转换的透射基底')
            kind = 'bsdf_principled'
            c['transmission'] = node['channels'].get('transmission', {'op': 'constant', 'value': 1})
            if c['transmission'].get('op') == 'constant' and c['transmission'].get('value', 0) == 0:
                c['transmission'] = {'op': 'constant', 'value': 1}
            c.setdefault('ior', {'op': 'constant', 'value': 1.45})
        changed = {'kind': kind, 'channels': c}
        changed['features'] = sorted(closure_features(changed))
        if mode == 'thin' and kind != 'emission':
            return {'kind': 'mix', 'factor': transmission, 'a': changed,
                    'b': {'kind': 'bsdf_translucent', 'channels': {
                        k: copy.deepcopy(v) for k, v in c.items() if k in ('base_color', 'normal')}, 'features': []}}
        return changed

    result['surface_graph'] = convert(graph)
    result['closure_features'] = sorted(closure_features(result['surface_graph']))
    result['channels']['opacity'] = alpha
    if mode in ('opaque', 'masked'): result['channels']['transmission'] = {'op': 'constant', 'value': 0}
    result.update(surface='pbr', water=False, master_material_candidate=False,
                  requires_visual_review=True, thin_surface_transmission=mode == 'thin',
                  legacy_thin_sheet=mode == 'thin',
                  surface_override={'mode': mode, 'coverage_source': source, 'source_id': record['id']})
    for key in ('refraction_ior', 'continuous_opacity', 'master_material', '_render_state',
                '_parent_key', '_bindings', 'basic_pbr_bake'):
        result.pop(key, None)
    if mode == 'transmission' and geometry is not None:
        result['surface_policy'] = dict(result.get('surface_policy') or {}, glass_geometry=copy.deepcopy(geometry))
    from .material_policy import glass_refraction_mode
    profile = glass_refraction_mode(result) if mode == 'transmission' else ''
    result['id'] = stable_id(record['id'], 'surface_override_v2', mode, profile)
    result['asset_name'] = 'M_Surface_' + result['id']
    return result


def repair_foliage_atlas(record):
    evidence = (record.get('surface_policy') or {}).get('mesh_surface', {})
    if not evidence.get('thin_open_surface') or not foliage_atlas_opacity(record): return record
    graph = record.get('surface_graph')
    # A material with additional layers needs explicit review, not atlas guessing.
    if graph and (graph.get('kind') != 'bsdf_principled' or
                  set(closure_features(graph)) - {'transmission'}): return record
    result = surface_override(record, 'thin')
    result['channels']['opacity'] = copy.deepcopy(foliage_atlas_opacity(record))
    result['id'] = record['id']
    result['asset_name'] = record['asset_name']
    result['foliage_atlas_repair'] = {'version': 1, 'reason': 'paired_atlas_maps_and_open_sheet',
                                    'coverage_source': 'misplaced_opacity_texture'}
    result['strategy_reasons'].append('同系列 Albedo／Opacity／Translucency 贴图与开放薄片一致；修复误接的裁切，保留薄叶透光')
    return result
