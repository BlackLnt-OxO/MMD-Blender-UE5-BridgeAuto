"""Build isolated candidates, then patch the selected scene's USD opinions."""
import os
import shutil
from pathlib import Path
import unreal as u
from .core import Job, read_json, atomic_json, file_hash, check_stop, Cancelled, stable_id
from .material_repair import verify_request, add_sublayers, commit_records, restore_records, rollback_interrupted_records


def _file(asset):
    package = asset.split('.')[0]
    if not package.startswith('/Game/'): raise ValueError('补修资产不是项目资源')
    return Path(u.Paths.project_content_dir()) / (package[6:] + '.uasset')


def _write(path, text):
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def run(job_folder, batch, undo=False):
    job = Job(job_folder)
    folder = job.folder / 'MaterialRepairs' / batch
    if folder.resolve().parent != (job.folder/'MaterialRepairs').resolve(): raise ValueError('非法补修批次')
    job.verify_inputs()
    request = read_json(folder/'request.json')
    journal_path = folder/'journal.json'
    journal = read_json(journal_path) if journal_path.is_file() else {}
    receipt_path = folder/'receipt.json'
    receipt = read_json(receipt_path) if receipt_path.is_file() else {}
    interrupted = journal.get('state') == 'applying' and not (receipt.get('undone') if journal.get('action') == 'undo' else receipt.get('committed'))
    cfg = job.data['settings']
    actual = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
    if actual != Path(cfg['project']).resolve(): raise ValueError('补修工程与任务不一致')
    source_root = Path(request['root_layer']).resolve()
    expected = Path(u.Paths.project_content_dir()) / request['root'].removeprefix('/Game/') / 'Source/Scene'
    if not source_root.is_relative_to(expected.resolve()): raise ValueError('USD 主层不属于选中场景')
    if not undo and not interrupted and file_hash(source_root)!=request['root_layer_sha256'] and not receipt.get('committed'):
        raise ValueError('原场景 USD 主层已改变，拒绝使用过期补修范围')
    dirty = list(u.EditorLoadingAndSavingUtils.get_dirty_map_packages()) + list(u.EditorLoadingAndSavingUtils.get_dirty_content_packages())
    # Runtime USD meshes may live in transient packages which cannot be saved.
    unsaved = [p.get_path_name() for p in dirty if not p.get_path_name().startswith(('/Engine/Transient', '/Memory/'))]
    if unsaved:
        raise ValueError('请保存 UE 当前关卡及资源后再补修／撤销，避免覆盖用户编辑: ' + ', '.join(unsaved))
    from .ue_host import save_generated_level, check_gpu_budget, material_compile_errors, active_import_log
    from .ue_materials import Materials
    editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
    if not editor.load_level(request['level']): raise ValueError('无法打开原场景关卡')
    actors = u.get_editor_subsystem(u.EditorActorSubsystem)
    stages = [a for a in actors.get_all_level_actors() if isinstance(a, u.UsdStageActor)]
    def stage_root(a):
        path = Path(a.get_editor_property('root_layer').file_path)
        return (path if path.is_absolute() else actual.parent/path).resolve()
    matched = [a for a in stages if stage_root(a) == source_root]
    if len(matched) != 1: raise ValueError('原场景没有唯一对应的 USD Stage: ' + str([str(stage_root(a)) for a in stages]))
    actor = matched[0]
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    if u.EditorAssetLibrary.get_metadata_tag(world, 'MMDBridge.Owner') != job.data['asset_id']:
        raise ValueError('原场景关卡归属不对应')
    from .ue_usd import save_stage_assets
    def checkpoint():
        save_generated_level(editor, world, request['root'], job.data['asset_id'])
        save_stage_assets(actor, read_json(job.folder/'manifest.json')['scene'], request['root'], job.data['asset_id'])
    def recover():
        if file_hash(source_root) not in (journal['root_before'], journal['root_after']):
            raise ValueError('补修中断后 USD 被修改，保留还原点并拒绝覆盖')
        rollback_interrupted_records(job, folder, journal)
        _write(source_root, (folder/'root_before.usda').read_text(encoding='utf-8'))
        actor.set_root_layer(''); actor.set_root_layer(str(source_root)); checkpoint()
        journal['state'] = 'recovered'; atomic_json(journal_path, journal)
        if journal.get('action') == 'undo':
            receipt['undone'] = True; atomic_json(receipt_path, receipt)
    if interrupted:
        recover()
        if journal.get('action') == 'undo':
            job.status('needs_review', '已从中断还原点完成撤销'); return
    if undo:
        receipt = read_json(folder/'receipt.json')
        if receipt.get('undone'): raise ValueError('该批次已经撤销')
        if file_hash(source_root) != receipt['root_sha256_after']: raise ValueError('USD 主层已改变，拒绝覆盖后续编辑')
        for name, digest in receipt['committed_hashes'].items():
            if file_hash(job.folder/name) != digest: raise ValueError('补修记录已改变，拒绝覆盖后续编辑')
        journal = {'state':'applying', 'action':'undo', 'root_before':file_hash(source_root),
                   'root_after':file_hash(folder/'root_before.usda'),
                   'metadata_before':{name:file_hash(folder/name) for name in receipt['committed_hashes']},
                   'metadata_after':receipt['committed_hashes']}
        atomic_json(journal_path, journal)
        job.status('ue_material_repair_commit', '正在提交撤销还原点')
        _write(source_root, (folder/'root_before.usda').read_text(encoding='utf-8'))
        actor.set_root_layer(''); actor.set_root_layer(str(source_root))
        checkpoint()
        restore_records(job, folder)
        receipt['undone'] = True; atomic_json(folder/'receipt.json', receipt)
        journal['state'] = 'complete'; atomic_json(journal_path, journal)
        job.status('needs_review', '已撤销本批白模补修，原材质绑定与坐标数据恢复')
        return
    if (folder/'receipt.json').is_file() and read_json(folder/'receipt.json').get('committed'):
        job.status('needs_review', '该补修批次已提交；保留原场景结果'); return
    verify_request(job, folder)
    export = read_json(folder/'export.json')
    if not set(export['records']) <= set(request['selected']): raise ValueError('候选材质超出白模范围')
    report = {'assets':{}, 'warnings':[], 'checks':{}, 'failures':list(export['failures']), 'metrics':{}}
    short_batch = 'B_' + stable_id(batch)[:10]
    repair_root = request['root']+'/MaterialRepairs/'+short_batch
    from .material_contract import asset_name_for_path
    try: asset_name_for_path(repair_root+'/Materials', 'M_'+'x'*120, u.Paths.project_content_dir())
    except ValueError:
        repair_root = '/Game/MMDBridgeRepairs/A_'+stable_id(job.data['asset_id'])[:12]+'/'+short_batch
    materials = Materials(repair_root, job.data['asset_id'], report, cfg,
                          lambda: check_gpu_budget(job,cfg,report))
    protected = {p: file_hash(_file(p)) for p in request['protected_assets'].values()}
    log = active_import_log(actual); start = log.stat().st_size if log.is_file() else 0
    successes = {}; layers = []
    for index,(identity,record) in enumerate(export['records'].items()):
        check_stop(job)
        job.status('ue_material_repair', f'验证白模补修 {index+1}/{len(export["records"])}: {record["name"]}')
        try:
            check_gpu_budget(job,cfg,report)
            before = log.stat().st_size if log.is_file() else start
            materials.build(record, folder/'cache')
            errors = material_compile_errors(log,before,job.data['asset_id'])
            if errors: raise ValueError('; '.join(errors))
            asset = materials.validate()[identity]
            saved = export['layers'][identity]
            if file_hash(saved['path']) != saved['sha256']: raise ValueError('候选 USD 层已改变')
            text = Path(saved['path']).read_text(encoding='utf-8')
            token = '__MATERIAL_'+identity+'__'
            if token not in text: raise ValueError('补修层缺少精确材质标记')
            directory = source_root.parent/'MaterialRepairs'/short_batch
            directory.mkdir(parents=True,exist_ok=True)
            layer = directory/(identity+'.usda')
            _write(layer, text.replace(token,asset))
            layers.append(layer.relative_to(source_root.parent).as_posix())
            successes[identity] = {'record':record,'asset':asset}
        except Cancelled: raise
        except Exception as exc:
            from .ue_host import GpuPause
            if isinstance(exc,GpuPause): raise
            report['failures'].append({'material_id':identity,'error':str(exc)})
        atomic_json(folder/'build.json',report)
    if not successes:
        atomic_json(folder/'receipt.json',{'committed':False,'failures':report['failures']})
        job.status('needs_review','实验性补修未得到可靠候选；原场景保持原绑定'); return
    verify_request(job,folder)
    for path,digest in protected.items():
        if file_hash(_file(path)) != digest: raise ValueError('非目标材质发生变化，拒绝提交')
    original = source_root.read_text(encoding='utf-8')
    (folder/'root_before.usda').write_text(original,encoding='utf-8')
    level_file = Path(u.Paths.project_content_dir())/(request['level'].removeprefix('/Game/')+'.umap')
    shutil.copy2(level_file,folder/'level_before.umap')
    updated_root = add_sublayers(original,layers)
    planned = folder/'root_after.usda'; _write(planned, updated_root)
    journal = {'state':'applying','action':'repair','root_before':file_hash(source_root),'root_after':file_hash(planned),
               'metadata_before':{name:file_hash(job.folder/name) for name in ('manifest.json','ue_report.json')}}
    atomic_json(journal_path, journal)
    job.status('ue_material_repair_commit', '正在验证并提交白模补修，保留事务还原点')
    try:
        _write(source_root, updated_root)
        actor.set_root_layer(''); actor.set_root_layer(str(source_root))
        # Resolve native binding at every affected object, not merely build an asset.
        checked = 0; uv_checks = []
        for target in request['targets']:
            if target['id'] not in successes: continue
            desired = successes[target['id']]['asset']
            for placement in target['placements']:
                for instance in placement['instances']:
                    path = instance['prim_path']
                    component = actor.get_generated_component(path) or actor.get_generated_component(path+'/Mesh')
                    if component is None: raise ValueError('补修后的目标组件未生成: '+path)
                    if not hasattr(component,'get_material'):
                        children=component.get_children_components(True)
                        component=next((c for c in children if isinstance(c,u.StaticMeshComponent)),None)
                    if component is None or not all(component.get_material(s) and component.get_material(s).get_path_name()==desired for s in placement['slots']):
                        raise ValueError('原场景白模槽位绑定验证失败: '+path+'; expected='+desired+'; actual='+str([component.get_material(s).get_path_name() if component and component.get_material(s) else None for s in placement['slots']]))
                    checked += len(placement['slots'])
                    added = export['layers'][target['id']].get('added_uv_channels', [])
                    if added:
                        mesh = component.get_editor_property('static_mesh')
                        count = u.get_editor_subsystem(u.StaticMeshEditorSubsystem).get_num_uv_channels(mesh, 0)
                        if count <= max(added): raise ValueError('新增 UV 通道未进入 UE 目标网格: '+path)
                        uv_checks.append({'prim_path':path,'channels':count,'added':added})
        for path,digest in protected.items():
            if file_hash(_file(path)) != digest: raise ValueError('非目标材质校验失败')
        checkpoint()
        commit_records(job,folder,successes)
    except Exception:
        journal = read_json(journal_path)
        recover()
        raise
    atomic_json(folder/'receipt.json',{'committed':True,'undone':False,'materials':sorted(successes),'asset_root':repair_root,
        'bindings_verified':checked,'failures':report['failures'],'visual_status':'needs_review',
        'uv_channels_verified':uv_checks,
        'protected_material_assets_unchanged':True,'root_sha256_after':file_hash(source_root),
        'committed_hashes':{name:file_hash(job.folder/name) for name in ('manifest.json','ue_report.json')}})
    journal['state'] = 'complete'; atomic_json(journal_path, journal)
    job.status('needs_review',f'实验性补修已更新原场景 {len(successes)} 个材质；画面需复核，失败项已保留')
