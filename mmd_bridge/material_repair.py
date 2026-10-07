"""Scoped repair transactions. No Blender or UE dependency in selection/guards."""
import copy
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from .core import atomic_json, read_json, file_hash
from .material_workflow import white_materials


def prepare(job, selected=None):
    job.verify_inputs()
    manifest = read_json(job.folder / 'manifest.json')
    report = read_json(job.folder / 'ue_report.json')
    if manifest.get('scene', {}).get('route') != 'usd_stage' or not report.get('assets', {}).get('level'):
        raise ValueError('补修需要已经保存的 USD 场景')
    eligible = {r['id']: r for r in white_materials(manifest, report)}
    chosen = set(eligible) if selected is None else set(selected)
    if not chosen or not chosen <= eligible.keys():
        raise ValueError('只能选择已记录的失败／占位材质；当前没有可补修项')
    scene = manifest['scene']
    source_records = {r['id']: r for r in scene['materials']}
    targets = []
    for identity in sorted(chosen):
        placements = []
        for prototype in scene['prototypes']:
            slots = prototype.get('material_slots', prototype.get('materials', []))
            if identity not in slots:
                continue
            instances = [i for i in scene['instances'] if i['prototype_id'] == prototype['id']]
            placements.append({'prototype': prototype, 'instances': instances,
                               'slots': [i for i, r in enumerate(slots) if r == identity]})
        if not placements:
            raise ValueError('失败材质没有可验证的原型／槽位: ' + identity)
        targets.append({'id': identity, 'source_aliases': source_records[identity].get('source_aliases', []), 'source_name': source_records[identity].get('source_name', source_records[identity]['name']),
                        'ue_asset': eligible[identity]['ue_asset'], 'placements': placements})
    batch = 'R_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    folder = job.folder / 'MaterialRepairs' / batch
    folder.mkdir(parents=True)
    for name in ('manifest.json', 'ue_report.json', 'job.json', 'white_materials.json'):
        if (job.folder / name).is_file():
            shutil.copy2(job.folder / name, folder / name)
    request = {'schema': 1, 'experimental': True, 'batch': batch, 'job': str(job.folder),
               'source_sha256': job.data['inputs']['scene'], 'root': report['root'],
               'level': report['assets']['level'], 'root_layer': report['checks']['usd_stage']['root_layer'],
               'root_layer_sha256': file_hash(report['checks']['usd_stage']['root_layer']),
               'manifest_sha256': file_hash(job.folder / 'manifest.json'),
               'report_sha256': file_hash(job.folder / 'ue_report.json'),
               'scene_frame': job.data['settings'].get('scene_frame', 1),
               'targets': targets, 'selected': sorted(chosen),
               'protected_materials': {r['id']: r for r in scene['materials'] if r['id'] not in chosen},
               'protected_assets': {k: v for k, v in report['assets']['materials'].items() if k not in chosen}}
    atomic_json(folder / 'request.json', request)
    return folder


def verify_request(job, folder):
    folder = Path(folder).resolve()
    if folder.parent != (job.folder / 'MaterialRepairs').resolve():
        raise ValueError('补修批次不属于当前任务')
    request = read_json(folder / 'request.json')
    job.verify_inputs()
    if file_hash(job.data['settings']['scene']) != request['source_sha256']:
        raise ValueError('补修源文件不再对应请求，请重新选择白模补修')
    if job.data['settings'].get('scene_frame', 1) != request.get('scene_frame', 1):
        raise ValueError('补修源帧已改变，请重新选择白模补修')
    if request['manifest_sha256'] != file_hash(job.folder / 'manifest.json'):
        raise ValueError('主场景材质记录已改变，请重新选择白模补修')
    if request.get('report_sha256', file_hash(job.folder / 'ue_report.json')) != file_hash(job.folder / 'ue_report.json'):
        raise ValueError('主场景导入报告已改变，请重新选择白模补修')
    return request


def add_sublayers(text, layers):
    match = re.search(r'subLayers\s*=\s*\[', text)
    if not match:
        raise ValueError('USD 主层缺少已登记的 subLayers')
    if any('@' in path or '\n' in path for path in layers):
        raise ValueError('非法补修层路径')
    return text[:match.end()] + ''.join('\n        @' + p + '@,' for p in layers) + text[match.end():]


def commit_records(job, folder, successes):
    request = verify_request(job, folder)
    manifest = read_json(job.folder / 'manifest.json')
    report = read_json(job.folder / 'ue_report.json')
    if not set(successes) <= set(request['selected']):
        raise ValueError('补修结果超出已选择材质')
    replacements = {identity: item['record'] for identity, item in successes.items()}
    manifest['scene']['materials'] = [copy.deepcopy(replacements.get(r['id'], r)) for r in manifest['scene']['materials']]
    for identity, before in request['protected_materials'].items():
        assert next(r for r in manifest['scene']['materials'] if r['id'] == identity) == before
    report['assets']['materials'].update({identity: item['asset'] for identity, item in successes.items()})
    report['assets']['source_packages'] = sorted(set(report['assets'].get('source_packages', [])) |
                                                {item['asset'].split('.')[0] for item in successes.values()})
    report['material_source_records'] = [r for r in report.get('material_source_records', []) if r['id'] not in successes]
    report['material_source_records'].extend(copy.deepcopy(list(replacements.values())))
    report['failures'] = [f for f in report.get('failures', []) if f.get('material_id') not in successes]
    report['complete'] = False  # Native correction still requires visual review.
    from .material_contract import finalize_restoration
    states=[finalize_restoration(copy.deepcopy(record)) for record in manifest['scene']['materials']]
    checks=report.setdefault('checks',{})
    checks['material_restoration']={state:sum(r.get('conversion_status')==state for r in states)
                                   for state in ('converted','approximated','placeholder','failed')}
    checks['failed_items']=len(report['failures'])
    checks['material_visual_review'] = list(dict.fromkeys(checks.get('material_visual_review', []) +
                                                         [r['name'] for r in replacements.values()]))
    checks['material_binding_failures']=[r for r in checks.get('material_binding_failures',[]) if r.get('material_id') not in successes]
    old_assets={t['id']:t['ue_asset'] for t in request['targets']}
    checks['material_compile_errors']=[line for line in checks.get('material_compile_errors',[])
        if not any(old_assets[i] and old_assets[i].split('.')[0] in line for i in successes)]
    report.setdefault('material_repairs', []).append({'batch': request['batch'], 'materials': sorted(successes), 'visual_status': 'needs_review'})
    payloads = {'manifest.json': manifest, 'ue_report.json': report}
    for name, payload in payloads.items():
        atomic_json(Path(folder) / ('commit_' + name), payload)
    journal_path = Path(folder) / 'journal.json'
    if journal_path.is_file():
        journal = read_json(journal_path)
        journal['metadata_after'] = {name: file_hash(Path(folder) / ('commit_' + name)) for name in payloads}
        atomic_json(journal_path, journal)
    for name, payload in payloads.items():
        atomic_json(job.folder / name, payload)
    from .material_workflow import white_ledger
    white_ledger(job, manifest, report)


def rollback_interrupted_records(job, folder, journal):
    """Only restore versions from this transaction; reject later user changes."""
    folder = Path(folder)
    for name, before in journal['metadata_before'].items():
        allowed = {before, journal.get('metadata_after', {}).get(name)}
        if file_hash(job.folder / name) not in allowed:
            raise ValueError('补修中断后记录被修改，保留还原点并拒绝覆盖: ' + name)
    for name in journal['metadata_before']:
        atomic_json(job.folder / name, read_json(folder / name))
    from .material_workflow import white_ledger
    white_ledger(job, read_json(job.folder / 'manifest.json'), read_json(job.folder / 'ue_report.json'))


def restore_records(job, folder):
    receipt = read_json(Path(folder) / 'receipt.json')
    for name in ('manifest.json', 'ue_report.json'):
        if file_hash(job.folder / name) != receipt['committed_hashes'][name]:
            raise ValueError('补修后记录已发生变化，拒绝覆盖后续修改')
    for name in ('manifest.json', 'ue_report.json'):
        shutil.copy2(Path(folder) / name, job.folder / name)
    from .material_workflow import white_ledger
    white_ledger(job, read_json(job.folder / 'manifest.json'), read_json(job.folder / 'ue_report.json'))
