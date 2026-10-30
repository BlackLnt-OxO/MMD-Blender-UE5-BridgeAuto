"""Disk snapshots and strict slot matching, independent of Unreal/PySide."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FORMAT = 'MMDBridge.CharacterMaterialPreset'
VERSION = 1


def signature(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def validate(data):
    if not isinstance(data, dict) or data.get('format') != FORMAT or data.get('version') != VERSION:
        raise ValueError('不是支持的 UE 角色材质配置文件')
    slots, materials = data.get('slots'), data.get('materials')
    if not isinstance(slots, list) or not slots or not isinstance(materials, dict):
        raise ValueError('材质配置缺少槽位或材质快照')
    indices = set()
    for row in slots:
        index = row.get('index')
        if type(index) is not int or index < 0 or index in indices:
            raise ValueError('材质配置有重复或无效的源槽号')
        indices.add(index)
        if row.get('material') is not None and row['material'] not in materials:
            raise ValueError('槽位引用的材质快照不存在')
    for key, material in materials.items():
        if not isinstance(material, dict) or not isinstance(material.get('root'), str):
            raise ValueError('材质快照缺少母材质引用: ' + str(key))
        if not material['root'].startswith('/') or material['root'].startswith('/Script/'):
            raise ValueError('母材质必须是 UE 内容资产路径')
        if not isinstance(material.get('instances'), list):
            raise ValueError('材质实例继承记录无效')
    return data


def load(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('角色材质配置文件不存在: ' + str(path))
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('无法读取角色材质配置 JSON: ' + str(path)) from exc
    return validate(data)


def match_slots(source, target, *, allow_index=False, mapping=None):
    """Use original PMX identity or unique slot names; never guess a reordered slot."""
    mapping = mapping or {}
    targets = {row['index']: row for row in target}
    if len(targets) != len(target):
        raise ValueError('目标槽位身份重复')
    matched, used, errors = [], set(), []
    for row in source:
        explicit = mapping.get(str(row['index']), mapping.get(row['index']))
        candidates = []
        if explicit is not None:
            candidates = [targets[explicit]] if explicit in targets else []
        elif row.get('model_sha256') and row.get('pmx_index') is not None:
            candidates = [item for item in target if item.get('model_sha256') == row['model_sha256']
                          and item.get('pmx_index') == row['pmx_index']]
        if not candidates and explicit is None:
            for field in ('imported_slot_name', 'slot_name', 'source_name'):
                if row.get(field):
                    candidates = [item for item in target if item.get(field) == row[field]]
                    if candidates:
                        break
        if not candidates and explicit is None and allow_index and row['index'] in targets:
            candidates = [targets[row['index']]]
        if len(candidates) != 1 or candidates[0]['index'] in used:
            errors.append(f"源槽 {row['index']} {row.get('source_name') or row.get('slot_name', '')}")
            continue
        index = candidates[0]['index']
        used.add(index)
        matched.append({'source': row['index'], 'target': index})
    if errors:
        raise ValueError('槽位未唯一对应，尚未应用材质；请核对名称或明确指定目标槽：' + '、'.join(errors))
    return matched


def snapshot(job):
    """Freeze the chosen JSON with the new job; source hashes still guard resume."""
    path = job.data['settings'].get('character_material_preset', '')
    if not path:
        return None
    from .core import atomic_json, file_hash
    data = load(path)
    destination = job.folder / 'character_material_preset.json'
    atomic_json(destination, data)
    return {'path': str(destination), 'sha256': file_hash(destination),
            'source': str(Path(path).resolve()), 'signature': signature(data)}


def job_preset(job):
    note = job.data.get('character_material_preset_snapshot')
    if not note:
        if job.data['settings'].get('character_material_preset'):
            raise ValueError('任务缺少角色材质配置快照；请新建任务')
        return None
    from .core import file_hash
    path = Path(note['path']).resolve()
    if path != (job.folder / 'character_material_preset.json').resolve() or file_hash(path) != note['sha256']:
        raise ValueError('任务内角色材质配置快照已改变')
    return load(path)
