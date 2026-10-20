"""Local subdivision review before any native application is started."""
import copy
import hashlib
import json
from pathlib import Path

from .character_geometry import supported
VERSION = 2


def requires_preflight(cfg):
    return supported(cfg) and cfg.get('character_geometry_mode') == 'local_subdivision' and bool(cfg.get('model'))


def configuration_signature(cfg):
    from .character_geometry import geometry_policy
    models = list(dict.fromkeys([cfg['model']] + [r['model'] for r in cfg.get('additional_characters', [])]))
    policies = [geometry_policy(dict(cfg, model=model)) for model in models]
    return hashlib.sha256(json.dumps({'review_version':VERSION,'policies':policies},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def build_review(cfg):
    from .character_pbr import inspect_model
    models = list(dict.fromkeys([cfg['model']] + [r['model'] for r in cfg.get('additional_characters', [])]))
    entries = []; warnings = []
    for model in models:
        snapshot = inspect_model(model, dict(cfg, model=model))
        # Same source model may appear on several character tracks.
        if any(r['sha256'] == snapshot['sha256'] for r in entries): continue
        slots = []
        for row in snapshot['materials']:
            purpose = row['geometry_purpose']
            used = row.get('surface_count', 1) > 0
            region = purpose.get('anatomical_region') or purpose.get('optional_region')
            if not region and purpose['role']=='face': region='face'
            # Only missing/unconfirmed face, lips or neck may interrupt a run.
            pending = used and purpose['needs_review'] and region in ('face','neck','lips')
            slots.append({'pmx_index': row['pmx_index'], 'name': row['name'],
                          'type': row['pbr']['type'], 'selected': used and purpose['local_selected'],
                          'pending': bool(pending), 'optional_region': purpose.get('optional_region'),
                          'warning': bool(used and purpose['needs_review']), 'anatomical_region':region,
                          'reasons': purpose['reasons'], 'role': purpose['role'], 'used': used})
            if used and purpose['needs_review']:
                warnings.append(f"{Path(model).name} · 原槽 {row['pmx_index']}「{row['name']}」：" + '；'.join(purpose['reasons']))
        missing = []
        for region, title in (('face','面部'), ('neck', '脖子'), ('lips', '嘴唇')):
            if not any(r['anatomical_region']==region and r['role']!='unknown' and r['used'] for r in slots):
                missing.append(title)
                warnings.append(f'{Path(model).name}：未单独识别{title}，可能与其它槽合并；请在补选窗口确认')
        entries.append({'model': model, 'sha256': snapshot['sha256'], 'slots': slots,'missing_regions':missing})
    return {'version': VERSION, 'models': entries, 'warnings': warnings, 'signature': configuration_signature(cfg),
            'pending': any(row['missing_regions'] for row in entries)}


def apply_selection(cfg, report, selections):
    """A prompted full-table confirmation saves all explicit unknown choices."""
    from .core import file_hash
    result = copy.deepcopy(cfg)
    overrides = result.setdefault('character_geometry_overrides', {})
    expected = {m['sha256'] for m in report['models']}
    if set(selections) != expected:
        raise ValueError('几何补选必须包含报告中的全部角色')
    for model in report['models']:
        digest = model['sha256']
        if file_hash(model['model']) != digest:
            raise ValueError('模型已改变，请取消后新建任务重新分析')
        selected = set(selections[digest]); slots = {s['pmx_index']: s for s in model['slots']}
        if not selected <= slots.keys() or any(type(i) is not int for i in selected):
            raise ValueError('几何补选包含未知原材质槽')
        entries = overrides.setdefault(digest, {})
        for index, slot in slots.items():
            value = index in selected
            if slot.get('warning',slot['pending']) or value != slot['selected']:
                entries.setdefault(str(index), {})['local'] = 'include' if value else 'exclude'
    return result
