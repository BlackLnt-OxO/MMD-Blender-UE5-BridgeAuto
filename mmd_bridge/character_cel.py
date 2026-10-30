"""Deterministic inputs for the three-surface built-in Cel character preset."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .character_pbr import classification_evidence, override_for
from .character_profiles import fingerprint
from .core import file_hash, resource_root

VERSION = 2
PRESET = 'builtin_cel'
PREFIX = '/Game/MMDBridge/BuiltinCel/v2/'
COLOR_MODES = {'role': 'B：按部位调色', 'neutral': 'A：原色保真'}


def reference(root=None):
    folder = Path(root or resource_root())/'resources/character_materials/BuiltinCel'
    data = json.loads((folder/'reference_parameters.json').read_text(encoding='utf-8'))
    if data.get('version') != VERSION:
        raise ValueError('内置卡通母版版本不匹配')
    return data


def settings_signature(cfg):
    """The shader version, bundled defaults and selected algorithm invalidate exports."""
    if cfg.get('character_material_profile') != PRESET:
        return None
    folder = resource_root()/'resources/character_materials/BuiltinCel'
    return fingerprint([VERSION, cfg.get('character_cel_color_mode', 'role'),
                        file_hash(folder/'reference_parameters.json'), file_hash(folder/'provenance.json')])


def plan(record, cfg, model_hash, ref=None):
    mode = cfg.get('character_cel_color_mode', 'role')
    if mode not in COLOR_MODES:
        raise ValueError('未知内置卡通明暗配色方案: '+str(mode))
    override = override_for(cfg, model_hash, record['pmx_index'])
    evidence = classification_evidence(record)
    kind = override.get('type', 'auto')
    if kind == 'auto':
        kind = evidence['type']
    else:
        evidence = dict(evidence, type=kind, method='manual', requires_review=False)
    role = {'iris': 'eye', 'sclera': 'eye', 'eye_highlight': 'eye', 'eye_shadow': 'eye',
            'lash': 'brow', 'teeth': 'mouth', 'satin': 'cloth', 'hosiery': 'cloth',
            'leather': 'cloth', 'metal': 'cloth', 'plastic': 'cloth', 'rubber': 'cloth',
            'glass': 'cloth'}.get(kind, kind)
    ref = ref or reference()
    source = ref['roles'].get(role)
    # A reference body instance is never imposed on an unrecognized slot.
    values = copy.deepcopy(source or ref['roles'].get('default', {}))
    vectors = values.setdefault('vectors', {})
    vectors['BaseColor'] = [1., 1., 1., 0.]
    if mode == 'neutral' or source is None:
        vectors['ShadowColor'] = [.6, .6, .6, 0.]
    matcap_alpha = vectors.get('MatCapColor', [1., 1., 1., 0.])[3]
    vectors['MatCapColor'] = [.9, .9, .9, matcap_alpha]
    # Defaults are authored on our parent copy; no inherited source MI switches.
    values.setdefault('switches', {}).update({'Brush?': False, '启用边缘光': False})
    mmd = record.get('mmd', {})
    sphere_mode = int(mmd.get('sphere_texture_type') or 0)
    sphere = record.get('textures', {}).get('sphere')
    available = bool(sphere and sphere.get('path') and Path(sphere['path']).is_file())
    enabled = sphere_mode in (1, 2) and available
    issues = []
    if sphere_mode and not available:
        issues.append('PMX 启用 Sphere 但引用贴图不可读，保留缺图诊断')
    if mmd.get('texture_rel_path') and not record.get('textures', {}).get('base_color'):
        issues.append('PMX 基础色引用缺失；无法恢复原图案')
    if record.get('material_error'):
        issues.append(record['material_error'])
    if sphere_mode == 3:
        issues.append('PMX 附加 UV Sphere 尚不能按普通 MatCap 还原')
    fallback = role == 'hair' and sphere_mode == 0
    matcap = {'source': 'pmx' if enabled else 'cel_hair' if fallback else 'disabled',
              'mode': sphere_mode if enabled else 2 if fallback else 0,
              'reference_mode': sphere_mode, 'enabled': enabled or fallback}
    return {'version': VERSION, 'model_sha256': model_hash, 'color_mode': mode, 'role': role,
            'classification': evidence, 'parameters': values, 'matcap': matcap,
            'issues': issues, 'status': 'needs_review' if issues or evidence.get('requires_review') else 'prepared',
            'reference_signature': fingerprint(ref), 'settings_signature': settings_signature(cfg)}


def prepare(worker):
    """Reuse PMX bindings and UV alpha analysis, without generating PBR effects."""
    from .blender_character_materials import prepare_alpha
    original = worker.data['settings'].get('model') or worker.cfg['model']
    digest = file_hash(original)
    ref = reference()
    for record in worker.materials.records.values():
        if record.get('pmx_index') is None:
            continue
        note = record.setdefault('character_material', {})
        note.update(profile=PRESET, sdf={'format': 'none', 'status': 'disabled'})
        prepare_alpha(worker, record)
        note['cel'] = plan(record, worker.cfg, digest, ref)
        note['role'] = note['cel']['role']
        if note['cel']['status'] == 'needs_review':
            record['requires_visual_review'] = True
    worker.manifest['character_cel_signature'] = settings_signature(worker.cfg)
