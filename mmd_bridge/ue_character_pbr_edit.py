"""Apply character-only material revisions through the existing live bridge."""
import copy
import json
import uuid
from pathlib import Path

import unreal as u

from .core import atomic_json, read_json, file_hash, stable_id
from .character_pbr import plan, texture_files, override_for, detail_bindings, inherit_lip_face_defaults
from .ue_character_pbr import build
from .ue_materials import Materials


def _ledger():
    return Path(u.Paths.project_saved_dir())/'MMDBridge/character_pbr_undo.json'


def _level():
    return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world().get_path_name()


def apply(command):
    if file_hash(command['model']) != command['model_sha256']:
        raise ValueError('PMX在分析后发生变化，请重新分析材质')
    actors = u.get_editor_subsystem(u.EditorActorSubsystem).get_selected_level_actors()
    targets = []
    for actor in actors:
        for component in actor.get_components_by_class(u.SkeletalMeshComponent):
            for index, original in enumerate(component.get_materials()):
                if not original: continue
                text = u.EditorAssetLibrary.get_metadata_tag(original, 'MMDBridge.PBRRecord')
                if not text: continue
                record = json.loads(text)
                source = record['character_material']['pbr']
                if source.get('model_sha256') != command['model_sha256'] or record['pmx_index'] != command['pmx_index']:
                    continue
                targets.append((component, index, original, record))
    if not targets:
        raise ValueError('请在UE中选中已使用内置PBR角色预设导入的对应角色')
    settings = dict(command['settings'], character_material_profile='character_pbr', character_backend='mmdbridge_fbx')
    report = {'warnings': [], 'failures': [], 'checks': {}}
    revision = uuid.uuid4().hex[:12]
    changes, cache = [], {}
    for component, slot, original, record in targets:
        original_path = original.get_path_name()
        if original_path not in cache:
            root = u.EditorAssetLibrary.get_metadata_tag(original, 'MMDBridge.PBRRoot')
            owner = u.EditorAssetLibrary.get_metadata_tag(original, 'MMDBridge.Owner')
            verifier = Materials(root, owner, report, settings)
            verifier.verify_asset(original)
            verifier.verify_asset(original.get_editor_property('parent'))
            record = copy.deepcopy(record)
            previous = record['character_material']['pbr']
            # A single-slot numeric edit keeps the imported scattering style,
            # including pre-option records, independent of the GUI's new default.
            style_settings = dict(settings, character_skin_scattering=previous.get('skin_scattering', 'legacy_v8'))
            updated = plan(record, style_settings, command['model_sha256'], texture_files(command['model']))
            if updated.get('surface_role') == 'lips' and not updated.get('face_parameter_source'):
                peers = []
                for material in component.get_materials():
                    text = u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.PBRRecord') if material else ''
                    peer = json.loads(text) if text else {}
                    if peer.get('character_material', {}).get('pbr', {}).get('model_sha256') == command['model_sha256']:
                        peers.append(peer)
                record['character_material']['pbr'] = updated
                inherit_lip_face_defaults([record, *peers], style_settings, command['model_sha256'])
            if 'HairDirection' not in override_for(settings, command['model_sha256'], record['pmx_index']).get('params', {}):
                updated['params']['HairDirection'] = previous['params'].get('HairDirection', 90)
            if 'opacity' in updated['textures'] and 'opacity' not in previous.get('textures', {}) and override_for(
                    settings, command['model_sha256'], record['pmx_index']).get('alpha_mode', 'auto') == 'auto':
                raise ValueError('新增透明遮罩时请明确选择镂空或半透明；重新生成角色可按UV自动分类')
            record['character_material']['pbr'] = updated
            record['textures'] = copy.deepcopy(updated['source_textures'])
            record['textures'].update(detail_bindings(updated['type']))
            for semantic, binding in updated['textures'].items():
                record['textures'][semantic] = {'path': binding['path'], 'id': stable_id(binding['path'], binding.get('sha256')),
                    'name': Path(binding['path']).name, 'color_space': 'sRGB' if semantic in ('base_color','sphere','emission') else 'Non-Color'}
            base_root = root.split('/PBRRevisions/', 1)[0]
            manager = Materials(base_root+'/PBRRevisions/R_'+revision, owner, report, settings)
            cache[original_path] = build(manager, record)
        candidate = cache[original_path]
        changes.append({'component': component.get_path_name(), 'slot': slot,
                        'original': original_path, 'candidate': candidate.get_path_name()})
    ledger = {'level': _level(), 'changes': changes, 'state': 'applying'}
    # Persist the rollback information before changing any component.
    previous_ledger = read_json(_ledger()) if _ledger().is_file() else None
    atomic_json(_ledger(), ledger)
    try:
        for row in changes:
            component = u.find_object(None, row['component'])
            component.modify(); component.set_material(row['slot'], u.load_asset(row['candidate']))
        if not u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level():
            raise RuntimeError('角色材质应用后保存关卡失败')
    except Exception:
        for row in changes:
            component = u.find_object(None, row['component'])
            component.set_material(row['slot'], u.load_asset(row['original']))
        u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level()
        ledger['state'] = 'rolled_back'
        atomic_json(_ledger(), previous_ledger or ledger)
        raise
    ledger['state'] = 'complete'; atomic_json(_ledger(), ledger)
    return {'count': len(changes), 'report': report, 'revision': revision}


def undo():
    if not _ledger().is_file():
        raise ValueError('没有可撤销的角色PBR材质应用')
    ledger = read_json(_ledger())
    if ledger['level'] != _level() or ledger['state'] not in ('complete', 'applying'):
        raise ValueError('当前关卡没有可撤销的角色PBR材质应用')
    items = []
    for row in ledger['changes']:
        component = u.find_object(None, row['component'])
        current = component.get_material(row['slot']) if component else None
        if not current or current.get_path_name() not in ({row['candidate'], row['original']} if ledger['state'] == 'applying' else {row['candidate']}):
            raise ValueError('材质槽已有后续编辑，保留当前绑定')
        original = u.load_asset(row['original'])
        if not original: raise ValueError('原材质资产不存在')
        items.append((component, row, original, current))
    try:
        for component, row, original, current in items:
            component.modify(); component.set_material(row['slot'], original)
        if not u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level():
            raise RuntimeError('撤销后保存关卡失败')
    except Exception:
        for component, row, original, current in items:
            component.set_material(row['slot'], current)
        u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level()
        raise
    ledger['state'] = 'undone'; atomic_json(_ledger(), ledger)
    return {'count': len(items)}


def run(command):
    return undo() if command['action'] == 'character_pbr_undo' else apply(command)
