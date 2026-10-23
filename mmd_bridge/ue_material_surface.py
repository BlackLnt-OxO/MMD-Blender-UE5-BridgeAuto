"""Selection-scoped material candidates, persistent USD bindings and guarded undo."""
import os
import time
import uuid
from pathlib import Path
import unreal as u
from .core import Job, read_json, atomic_json, file_hash
from .material_repair import add_sublayers
from .material_surface import surface_override
from .material_policy import pane_point_evidence


def context(job, require_stage=True):
    report = read_json(job.folder / 'ue_report.json')
    scene = read_json(job.folder / 'manifest.json')['scene']
    actual = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
    if actual != Path(job.data['settings']['project']).resolve(): raise ValueError('当前 UE 工程与场景任务不一致')
    root = Path(report['checks']['usd_stage']['root_layer']).resolve()
    expected = Path(u.Paths.project_content_dir()).resolve() / report['root'].removeprefix('/Game/') / 'Source/Scene'
    if root.parent != expected: raise ValueError('USD 根层不属于当前场景')
    def stage_root(actor):
        path = Path(actor.get_editor_property('root_layer').file_path)
        return (path if path.is_absolute() else actual.parent/path).resolve()
    stages = [a for a in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors()
              if isinstance(a, u.UsdStageActor) and stage_root(a) == root]
    if require_stage and len(stages) != 1: raise ValueError('没有唯一对应的 USD Stage')
    return report, scene, stages[0] if len(stages) == 1 else None, root


def current_level():
    return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world().get_path_name().split('.')[0]


def source_record(material, cache=None):
    actual = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
    asset = material.get_path_name()
    if not asset.startswith('/Game/'): raise ValueError('材质不属于当前项目 Content')
    content = Path(u.Paths.project_content_dir()).resolve()
    directory = asset_file(asset).parent.resolve()
    while directory != content and directory.is_relative_to(content):
        marker = directory/'.mmd_bridge_namespace.json'
        if marker.is_file(): break
        directory = directory.parent
    else: raise ValueError('没有找到材质来源任务标记')
    namespace = read_json(marker)
    job = Job(namespace['job'])
    cache = {} if cache is None else cache
    key = str(job.folder.resolve())
    if key not in cache:
        cache[key] = (job.data, read_json(job.folder/'ue_report.json'), read_json(job.folder/'manifest.json')['scene'])
    data, report, scene = cache[key]
    if Path(data['settings']['project']).resolve() != actual or data['asset_id'] != namespace['asset_id']:
        raise ValueError('任务标记与当前工程或场景不一致')
    expected = content/report['root'].removeprefix('/Game/')
    if expected.resolve() != directory: raise ValueError('任务资源目录与来源标记不一致')
    identity = (u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.SurfaceSource') or
                u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.SourceMaterial'))
    original = report['assets']['materials'].get(identity)
    if not original:
        identity = next((key for key, value in report['assets']['materials'].items() if value == asset), None)
        original = report['assets']['materials'].get(identity)
    if not original or (asset != original and not u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.SurfaceSource')):
        raise ValueError('材质来源 ID 与转换资源绑定不一致')
    if u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.Owner') != data['asset_id']:
        raise ValueError('材质所属场景与任务不一致')
    record = next((r for r in scene['materials'] if r['id'] == identity), None)
    if record is None: raise ValueError('任务没有对应的 Blender 材质记录')
    return job, report, record


def material_facts(material):
    parent = material
    seen = set()
    while isinstance(parent, u.MaterialInstanceConstant) and parent.get_path_name() not in seen:
        seen.add(parent.get_path_name())
        parent = parent.get_editor_property('parent')
    facts = {'asset': material.get_path_name(), 'parent': parent.get_path_name() if parent else ''}
    if parent:
        facts['thin_scattering'] = u.EditorAssetLibrary.get_metadata_tag(parent, 'MMDBridge.ThinSurfaceScattering')
        facts['refraction_profile'] = u.EditorAssetLibrary.get_metadata_tag(parent, 'MMDBridge.RefractionMode')
        for name in ('blend_mode', 'two_sided', 'refraction_method'):
            try: facts[name] = str(parent.get_editor_property(name))
            except Exception: facts[name] = '不可读取'
        for name in ('MP_OPACITY_MASK', 'MP_REFRACTION'):
            try:
                node = u.MaterialEditingLibrary.get_material_property_input_node(parent, getattr(u.MaterialProperty, name))
                facts[name] = node.get_class().get_name() if node else '未连接'
            except Exception: facts[name] = '不可读取'
    if isinstance(material, u.MaterialInstanceConstant):
        overrides = material.get_editor_property('base_property_overrides')
        for name in ('blend_mode', 'two_sided'):
            if overrides.get_editor_property('override_'+name): facts[name] = str(overrides.get_editor_property(name))
    return facts


def selected_glass_geometry(cmp, slot):
    helper = getattr(u, 'MMDBridgeViewportLibrary', None)
    if helper is None or not hasattr(helper, 'get_material_slot_vertices'):
        raise ValueError('当前编辑器缺少材质槽几何接口，请安装当前 UE 桥接并重启后再切换玻璃')
    points = helper.get_material_slot_vertices(cmp, slot)
    if len(points) < 3:
        raise ValueError('无法读取当前材质槽的源网格顶点，保留原材质；请检查网格 LOD0 和材质槽名称')
    return pane_point_evidence([(p.x/100, p.y/100, p.z/100) for p in points])


def inspect_selection():
    actors = u.get_editor_subsystem(u.EditorActorSubsystem)
    stages = [a for a in actors.get_all_level_actors() if isinstance(a, u.UsdStageActor)]
    rows = []
    source_cache, facts_cache = {}, {}
    for actor in actors.get_selected_level_actors():
        if isinstance(actor, u.UsdStageActor): continue
        root_component = actor.get_editor_property('root_component')
        components = list(actor.get_components_by_class(u.StaticMeshComponent))
        if root_component: components += [root_component]+list(root_component.get_children_components(True))
        for cmp in dict.fromkeys(c for c in components if isinstance(c, u.StaticMeshComponent)):
            for slot in range(cmp.get_num_materials()):
                material = cmp.get_material(slot)
                if not material: continue
                asset = material.get_path_name()
                if asset not in facts_cache: facts_cache[asset] = material_facts(material)
                row = {'component_path': cmp.get_path_name(), 'slot': slot, 'original_asset': material.get_path_name(),
                       'label': actor.get_actor_label()+' · 槽 '+str(slot), 'facts': facts_cache[asset],
                       'level': current_level(), 'binding': 'native', 'reason': ''}
                try:
                    job, report, record = source_record(material, source_cache)
                    row.update(job=str(job.folder), source_id=record['id'], source_name=record['name'])
                    row['label'] += ' · '+record['name']
                    for stage in stages:
                        try: path = stage.get_source_prim_path(cmp)
                        except Exception: continue
                        if not path: continue
                        _, _, matching, root = context(job)
                        if stage != matching: raise ValueError('物体 USD 来源与材质任务不一致')
                        row.update(binding='usd', prim_path=path, root_layer=str(root))
                        break
                except (OSError, ValueError, KeyError, TypeError) as exc: row['reason'] = str(exc)
                if row not in rows: rows.append(row)
    if not rows: raise ValueError('请在 UE 中选中网格物体；Stage 根节点没有可分析的材质槽')
    return {'targets': rows, 'level': current_level(),
            'project': u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())}


def component(stage, row):
    if row.get('binding') == 'native':
        value = u.find_object(None, row['component_path'])
        if not isinstance(value, u.StaticMeshComponent) or row['level'] != current_level():
            raise ValueError('选中组件或当前关卡已改变，请重新读取')
        return value
    value = stage.get_generated_component(row['prim_path'])
    items = [value] + list(value.get_children_components(True)) if value else []
    meshes = [c for c in items if isinstance(c, u.StaticMeshComponent)]
    if len(meshes) != 1: raise ValueError('无法唯一对应选中网格组件：' + row['label'])
    return meshes[0]


def saved_guard(job, report):
    cache_path = report['root'] + '/Scene/USD_AssetCache'
    cache = u.load_asset(cache_path)
    owned_cache = bool(cache and u.EditorAssetLibrary.get_metadata_tag(cache, 'MMDBridge.Owner') == job.data['asset_id'])
    dirty = [p.get_path_name() for p in list(u.EditorLoadingAndSavingUtils.get_dirty_map_packages()) +
             list(u.EditorLoadingAndSavingUtils.get_dirty_content_packages())
             if not p.get_path_name().startswith(('/Engine/Transient', '/Memory/')) and
             not (owned_cache and p.get_path_name() == cache_path)]
    if dirty: raise ValueError('请先保存 UE 关卡及资源，再切换／撤销材质，避免覆盖手工改动：' + ', '.join(dirty))


def asset_file(asset):
    if not asset.startswith('/Game/'): raise ValueError('目标材质不属于项目 Content')
    return Path(u.Paths.project_content_dir()) / (asset.split('.')[0][6:] + '.uasset')


def write_root(path, data):
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def save_stage_level(job, report):
    editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
    world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    if (world.get_path_name().startswith(report['root']+'/Maps/') and
            u.EditorAssetLibrary.get_metadata_tag(world,'MMDBridge.Owner')==job.data['asset_id']):
        from .ue_host import save_generated_level
        return save_generated_level(editor,world,report['root'],job.data['asset_id'])
    return editor.save_current_level()


def selection(job):
    if (Path(u.Paths.project_content_dir())/read_json(job.folder/'ue_report.json')['root'].removeprefix('/Game/')/'.mmd_bridge_namespace.json').is_file():
        snapshot = inspect_selection()
        snapshot['targets'] = [r for r in snapshot['targets'] if not r['reason'] and Path(r['job']).resolve() == job.folder.resolve()]
        if not snapshot['targets']: raise ValueError('当前选择没有对应转换任务的可切换材质槽')
        _, _, _, root = context(job, require_stage=any(r['binding'] == 'usd' for r in snapshot['targets']))
        snapshot.update(root_layer=str(root), root_hash=file_hash(root))
        return snapshot
    report, scene, stage, root = context(job)
    inverse = {asset: identity for identity, asset in report['assets']['materials'].items()}
    records = {r['id']: r for r in scene['materials']}
    rows = []
    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_selected_level_actors():
        if isinstance(actor, u.UsdStageActor): continue
        root_component = actor.get_editor_property('root_component')
        components = list(actor.get_components_by_class(u.StaticMeshComponent))
        if root_component:
            components += [root_component] + list(root_component.get_children_components(True))
        for cmp in dict.fromkeys(c for c in components if isinstance(c, u.StaticMeshComponent)):
            path = stage.get_source_prim_path(cmp)
            if not path: continue
            for slot in range(cmp.get_num_materials()):
                material = cmp.get_material(slot)
                if not material: continue
                asset = material.get_path_name()
                identity = inverse.get(asset) or u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.SurfaceSource')
                if identity not in records: continue
                placement = next((i for i in scene.get('instances', []) if
                                  path == i['prim_path'] or path.startswith(i['prim_path']+'/')), {})
                name = placement.get('source_name') or actor.get_actor_label()
                row = {'prim_path': path, 'slot': slot, 'source_id': identity, 'original_asset': asset,
                       'label': name + ' · 槽 ' + str(slot) + ' · ' + records[identity]['name']}
                if row not in rows: rows.append(row)
    if not rows: raise ValueError('请选择本工具导入场景中的网格物体；Stage 根节点或未知材质没有可切换槽位')
    return {'targets': rows, 'root_layer': str(root), 'root_hash': file_hash(root), 'level': report['assets']['level']}


def build(job, targets, mode):
    job.verify_inputs()
    native = all(row.get('binding') == 'native' for row in targets)
    if any(row.get('binding') == 'native' for row in targets) and not native:
        raise ValueError('请分别切换 USD 物体和关卡中的独立物体')
    report, scene, stage, root = context(job, require_stage=not native)
    saved_guard(job, report)
    allowed = selection(job)['targets']
    if not targets or any(row not in allowed for row in targets): raise ValueError('UE 当前选择已改变，请重新读取')
    batch = 'S_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    folder = job.folder / 'MaterialSurfaces' / batch
    folder.mkdir(parents=True)
    snapshot = selection(job)
    if native and not snapshot['level'].startswith('/Game/'): raise ValueError('请先将当前关卡保存到项目，再切换材质')
    snapshot.update(targets=targets, mode=mode)
    atomic_json(folder / 'request.json', snapshot)
    from .ue_materials import Materials
    from .ue_host import check_gpu_budget, active_import_log, material_compile_errors
    cfg = dict(job.data['settings'], use_scene_master_material=False)
    candidate_report = {'assets': {}, 'warnings': [], 'checks': {}, 'failures': [], 'metrics': {}}
    candidate_job = Job(folder)
    atomic_json(candidate_job.path, job.data)
    from .material_contract import surface_revision_root
    output_root=surface_revision_root(report['root'],job.data['asset_id'],batch,u.Paths.project_content_dir())
    snapshot['candidate_root']=output_root
    materials = Materials(output_root, job.data['asset_id'], candidate_report,
                          cfg, lambda: check_gpu_budget(candidate_job, cfg, candidate_report))
    records = {r['id']: r for r in scene['materials']}
    protected = {asset: file_hash(asset_file(asset)) for asset in report['assets']['materials'].values()}
    for row in targets:
        parent = u.load_asset(row['original_asset'])
        while isinstance(parent, u.MaterialInstanceConstant):
            parent = parent.get_editor_property('parent')
            if not parent: break
            asset = parent.get_path_name()
            if not asset.startswith('/Game/'): break
            if asset in protected: break
            protected[asset] = file_hash(asset_file(asset))
    log = active_import_log(Path(cfg['project']))
    start = log.stat().st_size if log.is_file() else 0
    candidates = {}
    for row in targets:
        identity = row['source_id']
        geometry = selected_glass_geometry(component(stage, row), row['slot']) if mode == 'transmission' else None
        record = surface_override(records[identity], mode, scene.get('function_definitions', []), geometry)
        row['candidate_id'] = record['id']
        if record['id'] in candidates: continue
        materials.build(record, folder / 'cache')
        errors = material_compile_errors(log, start, job.data['asset_id'])
        if errors: raise ValueError('; '.join(errors))
        asset = materials.validate()[record['id']]
        loaded = u.load_asset(asset)
        u.EditorAssetLibrary.set_metadata_tag(loaded, 'MMDBridge.SurfaceSource', identity)
        u.EditorAssetLibrary.save_loaded_asset(loaded)
        candidates[record['id']] = asset
    for row in snapshot['targets']:
        row['candidate_asset'] = candidates[row['candidate_id']]
    snapshot['protected_assets'] = protected
    atomic_json(folder / 'build.json', snapshot)
    return {'folder': str(folder), 'mode': mode}


def commit(job, folder, undo=False):
    folder = Path(folder).resolve()
    if folder.parent != (job.folder / 'MaterialSurfaces').resolve(): raise ValueError('切换批次不属于当前任务')
    data = read_json(folder / 'build.json')
    native = all(row.get('binding') == 'native' for row in data['targets'])
    report, scene, stage, root = context(job, require_stage=not native)
    saved_guard(job, report)
    if data.get('level') and current_level() != data['level']: raise ValueError('当前关卡已改变，请返回材质切换所在关卡')
    if root != Path(data['root_layer']).resolve(): raise ValueError('切换根层不对应当前关卡')
    if native: return commit_native(job, folder, data, undo)
    before = root.read_bytes()
    journal_path = folder/'journal.json'
    journal = read_json(journal_path) if journal_path.is_file() else {}
    if undo:
        receipt_path = folder/'receipt.json'
        receipt = read_json(receipt_path) if receipt_path.is_file() else {}
        if journal.get('state') == 'applying':
            if file_hash(root) not in (journal['before'], journal['after']):
                raise ValueError('中断后 USD 根层有后续编辑，拒绝覆盖；还原点已保留')
        else:
            if receipt.get('undone') or not receipt.get('committed'): raise ValueError('该切换批次不能撤销')
            if file_hash(root) != receipt['root_hash_after']: raise ValueError('USD 根层有后续编辑，拒绝覆盖；还原点已保留')
        if file_hash(folder/'root_before.usda') != data['root_hash']:
            raise ValueError('材质切换还原点已改变，拒绝覆盖')
        after = (folder / 'root_before.usda').read_bytes()
    else:
        if file_hash(root) != data['root_hash']: raise ValueError('USD 根层已改变，请重新读取选择')
        for row in data['targets']:
            cmp = component(stage, row)
            if cmp.get_material(row['slot']).get_path_name() != row['original_asset']:
                raise ValueError('目标材质已被手工更改，请重新读取选择')
        layer = read_json(folder / 'layer.json')
        expected_layer = root.parent/'MaterialSurfaces'/(folder.name+'.usda')
        if Path(layer['path']).resolve() != expected_layer.resolve() or layer['relative'] != expected_layer.relative_to(root.parent).as_posix():
            raise ValueError('候选绑定层不属于当前场景')
        if file_hash(layer['path']) != layer['sha256']: raise ValueError('候选绑定层已改变')
        if layer.get('root_candidate_hash'):
            planned = folder/'root_candidate.usda'
            if file_hash(planned) != layer['root_candidate_hash']: raise ValueError('候选根层已改变，保留原绑定')
            after = planned.read_bytes()
        else:
            after = add_sublayers(before.decode('utf-8'), [layer['relative']]).encode('utf-8')
        (folder / 'root_before.usda').write_bytes(before)
    planned = folder/'root_after.usda'
    planned.write_bytes(after)
    atomic_json(journal_path, {'state': 'applying', 'action': 'undo' if undo else 'apply',
                              'before': file_hash(root), 'after': file_hash(planned)})
    try:
        write_root(root, after)
        stage.set_root_layer(''); stage.set_root_layer(str(root))
        for row in data['targets']:
            expected = row['original_asset'] if undo else row['candidate_asset']
            actual = component(stage, row).get_material(row['slot'])
            if not actual or actual.get_path_name() != expected:
                raise ValueError('切换后的材质槽绑定未通过验证：'+row['label']+' · '+row['prim_path']+
                                 '\n预期：'+expected+'\n实际：'+(actual.get_path_name() if actual else '无材质'))
        for asset, digest in data['protected_assets'].items():
            if file_hash(asset_file(asset)) != digest: raise ValueError('原有共享材质发生变化')
        if not save_stage_level(job,report):raise ValueError('保存 USD 材质切换后的当前关卡失败')
    except Exception:
        write_root(root, before)
        stage.set_root_layer(''); stage.set_root_layer(str(root))
        restored=save_stage_level(job,report)
        atomic_json(journal_path, {'state': 'rolled_back' if restored else 'applying'})
        raise
    receipt = {'committed': True, 'undone': undo, 'root_hash_after': file_hash(root),
               'targets': data['targets'], 'mode': data['mode'], 'visual_status': 'needs_review'}
    atomic_json(folder / 'receipt.json', receipt)
    atomic_json(journal_path, {'state': 'complete'})
    return {'folder': str(folder), 'count': len(data['targets']), 'undone': undo}


def commit_native(job, folder, data, undo):
    receipt_path = folder/'receipt.json'
    receipt = read_json(receipt_path) if receipt_path.is_file() else {}
    journal_path = folder/'journal.json'
    journal = read_json(journal_path) if journal_path.is_file() else {}
    interrupted = journal.get('state') == 'applying'
    if undo and not interrupted and (not receipt.get('committed') or receipt.get('undone')):
        raise ValueError('该切换批次不能撤销')
    items = []
    for row in data['targets']:
        cmp = component(None, row)
        current = cmp.get_material(row['slot'])
        expected = row['candidate_asset'] if undo else row['original_asset']
        allowed = {row['original_asset'], row['candidate_asset']} if undo and interrupted else {expected}
        if not current or current.get_path_name() not in allowed: raise ValueError('目标材质已有后续编辑，拒绝覆盖')
        candidate = u.load_asset(row['original_asset'] if undo else row['candidate_asset'])
        if candidate is None: raise ValueError('目标材质资源不存在')
        items.append((cmp, row, current, candidate))
    for asset, digest in data['protected_assets'].items():
        if file_hash(asset_file(asset)) != digest: raise ValueError('原有材质已有后续编辑，拒绝覆盖')
    root = Path(data['root_layer'])
    root_hash = file_hash(root)
    atomic_json(journal_path, {'state': 'applying', 'action': 'undo' if undo else 'apply'})
    editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
    try:
        for cmp, row, original, candidate in items:
            cmp.modify(); cmp.set_material(row['slot'], candidate)
        for cmp, row, original, candidate in items:
            if cmp.get_material(row['slot']) != candidate: raise ValueError('关卡材质槽绑定未通过验证')
        if file_hash(root) != root_hash: raise ValueError('关卡局部切换意外修改了源 USD')
        if not editor.save_current_level(): raise ValueError('保存当前关卡失败')
    except Exception:
        for cmp, row, original, candidate in items:
            cmp.set_material(row['slot'], original)
        restored = editor.save_current_level()
        atomic_json(journal_path, {'state': 'rolled_back' if restored else 'applying'})
        raise
    atomic_json(receipt_path, {'committed': True, 'undone': undo, 'targets': data['targets'],
                             'mode': data['mode'], 'level': data['level'], 'visual_status': 'needs_review'})
    atomic_json(journal_path, {'state': 'complete'})
    return {'folder': str(folder), 'count': len(items), 'undone': undo}


def run(command):
    if command['action'].startswith('character_pbr_'):
        from .ue_character_pbr_edit import run as run_character
        return run_character(command)
    if command['action'] == 'inspect': return inspect_selection()
    job = Job(command['job'])
    action = command['action']
    if action == 'selection': return selection(job)
    if action == 'build': return build(job, command['targets'], command['mode'])
    if action in ('commit', 'undo'): return commit(job, command['folder'], undo=action == 'undo')
    raise ValueError('未知材质切换操作')
