"""Independent, conflict-checked transactions for custom character materials."""
import copy
import json
import uuid
from pathlib import Path

import unreal as u

from .core import atomic_json, read_json, file_hash
from .character_custom_materials import plan, asset_path
from .ue_character_pbr import verify_skin_profile
from .ue_custom_materials import build, inventory
from .ue_materials import Materials
from .material_contract import character_revision_root


def ledger_path():
    return Path(u.Paths.project_saved_dir()) / 'MMDBridge/character_custom_undo.json'


def level():
    return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world().get_path_name()


def _record(material):
    for tag in ('MMDBridge.CustomRecord', 'MMDBridge.PBRRecord'):
        text = u.EditorAssetLibrary.get_metadata_tag(material, tag)
        if text:
            record = json.loads(text)
            note = record.get('character_material', {})
            digest = (note['custom']['source']['model_sha256'] if note.get('custom') else note.get('pbr', {}).get('model_sha256'))
            return record, digest
    return None, None


def _manager(material, settings):
    tag = u.EditorAssetLibrary.get_metadata_tag
    root = tag(material, 'MMDBridge.CustomRoot') or tag(material, 'MMDBridge.PBRRoot')
    owner = tag(material, 'MMDBridge.Owner')
    if not root or not owner:
        raise ValueError('目标缺少可靠的任务来源；不按名称猜测角色槽')
    return Materials(root, owner, {'warnings': [], 'failures': [], 'checks': {}}, settings)


def targets(command):
    if file_hash(command['model']) != command['model_sha256']:
        raise ValueError('原 PMX 在分析后发生变化')
    indices = set(command['pmx_indices'])
    if not indices:
        raise ValueError('未选择原材质槽')
    result, found = [], set()
    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_selected_level_actors():
        for component in actor.get_components_by_class(u.SkeletalMeshComponent):
            for slot, material in enumerate(component.get_materials()):
                if not material:
                    continue
                record, digest = _record(material)
                if digest != command['model_sha256'] or record['pmx_index'] not in indices:
                    continue
                manager = _manager(material, command.get('settings', {}))
                manager.verify_asset(material)
                parent = material.get_editor_property('parent')
                if u.EditorAssetLibrary.get_metadata_tag(parent, 'MMDBridge.Owner') == manager.owner:
                    manager.verify_asset(parent)
                verify_skin_profile(manager, material)
                result.append((component, slot, material, record))
                found.add(record['pmx_index'])
    if found != indices:
        raise ValueError('请在 UE 选中有来源记录的对应角色；未找到原槽: ' + str(sorted(indices - found)))
    return result


def apply(command):
    items = targets(command)
    prepared = {r['pmx_index']: r for r in command['records']}
    if len(prepared) != len(command['records']) or set(prepared) != set(command['pmx_indices']):
        raise ValueError('准备记录与选中的原材质槽不一致')
    for index, record in prepared.items():
        source = record.get('character_material', {}).get('custom', {}).get('source', {})
        if source.get('model_sha256') != command['model_sha256'] or source.get('pmx_index') != index:
            raise ValueError('准备记录不属于当前原 PMX 或材质槽')
    revision, changes, cache = uuid.uuid4().hex[:12], [], {}
    report = {'warnings': [], 'failures': [], 'checks': {}}
    for component, slot, original, source in items:
        original_path = original.get_path_name()
        if original_path not in cache:
            manager = _manager(original, command['settings'])
            root = character_revision_root(manager.root, manager.owner, revision, u.Paths.project_content_dir())
            manager = Materials(root, manager.owner, report, command['settings'])
            record = copy.deepcopy(source)
            record.update(copy.deepcopy(prepared[source['pmx_index']]))
            cache[original_path] = build(manager, record)
        candidate = cache[original_path]
        changes.append({'component': component.get_path_name(), 'slot': slot, 'original': original_path,
                        'candidate': candidate.get_path_name()})
    result = commit(changes, revision)
    result.update(status='needs_review' if report['warnings'] else 'complete', warnings=report['warnings'])
    return result


def commit(changes, revision):
    path = ledger_path()
    previous = read_json(path) if path.is_file() else None
    ledger = {'level': level(), 'state': 'applying', 'revision': revision, 'changes': changes}
    atomic_json(path, ledger)
    editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
    changed = []
    try:
        for row in changes:
            component = u.find_object(None, row['component'])
            original = component.get_material(row['slot'])
            if not original or original.get_path_name() != row['original']:
                raise ValueError('预检后绑定已发生变化，保留后续编辑')
            changed.append((component, row, original))
            component.modify(); component.set_material(row['slot'], u.load_asset(row['candidate']))
            if component.get_material(row['slot']).get_path_name() != row['candidate']:
                raise RuntimeError('自选材质绑定验证失败')
        if not editor.save_current_level():
            raise RuntimeError('自选材质应用后保存关卡失败')
    except Exception:
        for component, row, original in changed:
            component.set_material(row['slot'], original)
        restored = editor.save_current_level()
        if restored:
            atomic_json(path, previous or dict(ledger, state='rolled_back'))
        raise
    atomic_json(path, dict(ledger, state='complete'))
    return {'count': len(changes), 'revision': revision}


def undo():
    path = ledger_path()
    if not path.is_file():
        raise ValueError('没有自选材质应用记录')
    ledger = read_json(path)
    if ledger['level'] != level() or ledger['state'] not in ('complete', 'applying'):
        raise ValueError('自选材质记录不属于当前关卡或已经撤销')
    interrupted = ledger['state'] == 'applying'
    items = []
    for row in ledger['changes']:
        component = u.find_object(None, row['component'])
        current = component.get_material(row['slot']) if component else None
        allowed = {row['candidate'], row['original']} if interrupted else {row['candidate']}
        if not current or current.get_path_name() not in allowed:
            raise ValueError('目标已有后续绑定编辑，保留当前结果')
        original = u.load_asset(row['original'])
        if not original:
            raise ValueError('原材质资产不存在')
        original_manager = _manager(original, {})
        original_manager.verify_asset(original)
        verify_skin_profile(original_manager, original)
        manager = _manager(current, {})
        manager.verify_asset(current)
        record, _ = _record(current)
        note = record.get('character_material', {}).get('custom')
        if note and inventory(note['parent'])['signature'] != note['inventory_signature']:
            raise ValueError('父材质在应用后已有修改，保留人工编辑')
        items.append((component, row, original, current))
    editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
    try:
        for component, row, original, _ in items:
            component.modify(); component.set_material(row['slot'], original)
        if not editor.save_current_level():
            raise RuntimeError('撤销后保存关卡失败')
    except Exception:
        for component, row, _, current in items:
            component.set_material(row['slot'], current)
        editor.save_current_level()
        raise
    atomic_json(path, dict(ledger, state='undone'))
    return {'count': len(items)}


def run(command):
    action = command['action']
    if action == 'custom_inventory':
        return {'inventories': [inventory(path) for path in command['parents']]}
    if action == 'custom_targets':
        return {'count': len(targets(command))}
    if action == 'custom_apply':
        return apply(command)
    if action == 'custom_undo':
        return undo()
    raise ValueError('未知自选材质操作')
