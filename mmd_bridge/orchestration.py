"""Persisted, serial orchestration of the existing independent conversion routes."""
from __future__ import annotations

import copy
import re
import time
from dataclasses import asdict, replace
from pathlib import Path

from .core import Job, SCHEMA, Settings, atomic_json, file_hash, inspect_vmd, read_json, task_transfer_folder

VERSION = 1
TERMINAL = {'complete', 'needs_review', 'failed', 'stopped', 'cancelled', 'upstream_blocked'}
SUCCESS = {'complete', 'needs_review'}


def enabled(settings):
    return settings.character_backend == 'mmdbridge_fbx' and (
        settings.flow == 'auto' or settings.flow == 'character' and bool(settings.additional_characters))


def module_settings(settings):
    """No branch carries another branch's inputs or software prerequisites."""
    common = dict(additional_characters=[], working_blend='')
    if settings.flow == 'auto' and settings.scene:
        yield 'scene', replace(settings, **common, flow='scene', character_backend='fbx_legacy',
                               model='', motion='', lipsync='', supplementary_motions=[], camera='', character_blender='')
    characters = [dict(model=settings.model, motion=settings.motion, lipsync=settings.lipsync,
                       supplementary_motions=settings.supplementary_motions)]
    characters += settings.additional_characters
    for index, character in enumerate(characters, 1):
        if any(character.get(key) for key in ('model', 'motion', 'lipsync', 'supplementary_motions')):
            yield f'character_{index:04d}', replace(settings, **common, flow='character', scene='', camera='',
                blender=settings.character_blender or settings.blender,
                model=character.get('model', ''), motion=character.get('motion', ''),
                lipsync=character.get('lipsync', ''), supplementary_motions=character.get('supplementary_motions', []))
    if settings.flow == 'auto' and settings.camera:
        yield 'camera', replace(settings, **common, flow='camera', character_backend='fbx_legacy',
                               model='', motion='', lipsync='', supplementary_motions=[], scene='', character_blender='')


def create_parent(settings):
    from .character_custom_materials import uses_custom_editor
    transfer = task_transfer_folder(settings)
    source = Path(settings.model or settings.scene or settings.camera).stem
    stem = re.sub(r'[^A-Za-z0-9]+', '_', source).strip('_')[:28] or 'Conversion'
    for number in range(1, 10000):
        identifier = f'Composite_{stem}_{number:02d}'
        folder = transfer / 'MMDBridge_Jobs' / identifier
        try:
            folder.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise ValueError('组合任务目录编号已用尽')
    modules = [{'id': identity, 'settings': asdict(cfg), 'state': 'pending', 'job': None}
               for identity, cfg in module_settings(settings)]
    if not modules:
        raise ValueError('至少选择一个模块的输入')
    cfg = replace(settings, character_output_dir=str(transfer))
    shared_end = 0
    for item in modules:
        values = [item['settings'].get(key) for key in ('motion', 'lipsync', 'camera')]
        supplements = item['settings'].get('supplementary_motions', [])
        if isinstance(supplements, list):
            values += supplements
        for value in values:
            if isinstance(value, str) and value:
                try:
                    shared_end = max(shared_end, inspect_vmd(value)['max_frame'])
                except (OSError, ValueError):
                    pass  # The owning module reports its invalid input later.
    job = Job(folder)
    atomic_json(job.path, {'schema': SCHEMA, 'id': identifier, 'asset_id': identifier,
        'settings': asdict(cfg), 'inputs': {}, 'vmds': {}, 'created': time.time(),
        'orchestration_version': VERSION, 'orchestration_phase': 'exports', 'module_jobs': modules,
        'geometry_input_identity_version': 1,
        **({'custom_material_binding_version': 1} if uses_custom_editor(asdict(cfg)) else {}),
        'shared_bake_end': shared_end})
    job.status('created', '已创建独立模块组合任务')
    return job


def child_folder(parent, value):
    folder = Path(value).resolve()
    if not folder.is_relative_to((parent.folder / 'modules').resolve()):
        raise ValueError('模块任务目录超出组合任务')
    return folder


def verified_receipt(parent, item):
    folder = child_folder(parent, item['job'])
    child = Job(folder)
    child.verify_inputs()
    manifest = read_json(folder / 'manifest.json')
    checkpoint = folder/'character_checkpoint.json'
    if checkpoint.is_file():
        from .character_texture_review import check_checkpoint as check_texture_checkpoint
        check_texture_checkpoint(child.data['settings'], manifest)
        from .character_geometry import check_geometry_checkpoint
        check_geometry_checkpoint(read_json(checkpoint), child.data['settings'], manifest.get('character_geometry'), folder)
    report = read_json(folder / 'ue_report.json')
    if manifest.get('job_id') != child.data['id'] or report.get('job_id') != child.data['id']:
        raise ValueError('模块清单或 UE 回执身份不匹配')
    if report.get('error') or not report.get('assets') or not report.get('assets', {}).get('level'):
        raise ValueError('模块缺少可装配的终端 UE 结果')
    if item['id'].startswith('character_') and not all(report['assets'].get(k) for k in ('mesh','skeleton','animation')):
        raise ValueError('角色模块未生成可装配的网格、骨架和动画；诊断关卡不代表角色导入完成')
    if item['id'] == 'scene' and manifest.get('scene',{}).get('route') == 'usd_stage' and not report['assets'].get('usd_stage'):
        raise ValueError('场景模块缺少实际 USD Stage 回执')
    project = str(Path(report.get('project', '')).resolve()).casefold()
    if project != str(Path(parent.data['settings']['project']).resolve()).casefold():
        raise ValueError('模块 UE 回执工程不匹配')
    return child, manifest, report


def assembly_manifest(parent):
    data = parent.data
    cfg = data['settings']
    result = {'schema': SCHEMA, 'job_id': data['id'], 'asset_id': data['asset_id'],
        'fps': cfg['fps'], 'frame_range': [cfg['start'], cfg['start']], 'files': {},
        'materials': [], 'characters': [], 'warnings': [], 'composition': [],
        'module_results': []}
    for item in data['module_jobs']:
        summary = {key: item.get(key) for key in ('id', 'state', 'job', 'message')}
        result['module_results'].append(summary)
        if item['state'] not in SUCCESS:
            continue
        child, manifest, report = verified_receipt(parent, item)
        result['composition'].append({'job': str(child.folder),
            'manifest_sha256': file_hash(child.folder / 'manifest.json'),
            'report_sha256': file_hash(child.folder / 'ue_report.json')})
        if manifest['fps'] != result['fps']:
            raise ValueError('模块帧率不一致')
        result['frame_range'][0] = min(result['frame_range'][0], manifest['frame_range'][0])
        result['frame_range'][1] = max(result['frame_range'][1], manifest['frame_range'][1])
        result['warnings'] += [f"{item['id']}: {text}" for text in manifest.get('warnings', [])]
        if item['id'].startswith('character_'):
            record = copy.deepcopy(manifest)
            record['label'] = Path(child.data['settings']['model']).stem + '_' + item['id']
            record['reuse_assets'] = report['assets']
            record['reuse_owner'] = child.data['asset_id']
            result['characters'].append(record)
        elif item['id'] == 'scene':
            result['scene'] = copy.deepcopy(manifest['scene'])
            result['reuse_materials'] = report['assets'].get('materials', {})
            result['scene']['reuse_asset_cache'] = report.get('checks', {}).get('usd_stage', {}).get('asset_cache')
            result['scene']['reuse_root_layer'] = report.get('checks', {}).get('usd_stage', {}).get('root_layer')
            result['scene']['reuse_owner'] = child.data['asset_id']
        elif item['id'] == 'camera':
            result['camera_samples'] = manifest['camera_samples']
    return result


class CompositeRun:
    def __init__(self, owner, *, resume=False, factory=None):
        self.owner = owner
        self.parent = owner.job
        if self.parent.data.get('orchestration_version') != VERSION:
            raise ValueError('未知组合任务版本')
        self.factory = factory or (lambda: type(owner)(owner.home))
        self.child = None
        self.active = None
        self.assembling = False
        self.done = False
        self.last_event = None
        if resume:
            data = self.parent.data
            for item in data['module_jobs']:
                if item.get('job'):
                    child_folder(self.parent, item['job'])
                if item['state'] not in SUCCESS:
                    item['state'] = 'pending'
                    item.pop('message', None)
                else:
                    try:
                        verified_receipt(self.parent, item)
                    except Exception as exc:
                        item.update(state='failed', message=str(exc))
            data['orchestration_phase'] = 'exports'
            atomic_json(self.parent.path, data)
            (self.parent.folder / 'stop').unlink(missing_ok=True)
            self.parent.status('modules_running', '恢复组合任务：逐模块校验来源与检查点')

    def save_item(self, **updates):
        data = self.parent.data
        item = next(item for item in data['module_jobs'] if item['id'] == self.active)
        item.update(updates)
        atomic_json(self.parent.path, data)
        return item

    def notify(self, status):
        signature = (self.active, status.get('state'), status.get('message'), status.get('updated'))
        if signature != self.last_event:
            self.last_event = signature
            state = status.get('state', '')
            visible = state if state in {'awaiting_geometry_selection', 'ue_wait_restart',
                'ue_wait_save', 'ue_wait_vram', 'ue_paused_gpu_crash', 'ue_paused_resource_limit'} else 'modules_running'
            self.parent.status(visible, f"[{self.active}] {status.get('message', state)}",
                               module=self.active, module_state=state, module_job=str(self.child.job.folder))

    def busy(self):
        return any(proc.poll() is None for _, proc in self.child.processes) or bool(
            self.child.bake_thread and self.child.bake_thread.is_alive()) or bool(
            self.child.geometry_thread and self.child.geometry_thread.is_alive())

    def poll(self):
        if self.assembling:
            return self.owner._poll_single()
        if self.owner.stopped or self.done:
            return read_json(self.parent.folder / 'status.json')
        if self.child:
            try:
                status = self.child.poll()
                if self.child.mailbox:
                    self.owner.mailbox = self.child.mailbox
                self.notify(status)
                state = status.get('state')
                finished = state == 'exports_done' or state in TERMINAL
                if not finished or self.busy():
                    return read_json(self.parent.folder / 'status.json')
                if state in SUCCESS:
                    item = self.save_item(state=state, message=status.get('message', ''))
                    verified_receipt(self.parent, item)
                else:
                    self.save_item(state='exported' if state == 'exports_done' else 'failed',
                                   message=status.get('message', ''))
            except Exception as exc:
                if self.busy():
                    raise
                self.save_item(state='failed', message=str(exc))
                self.parent.status('module_failed', f'[{self.active}] {exc}')
            self.child = None
            self.active = None
        data = self.parent.data
        phase = data['orchestration_phase']
        next_item = next((item for item in data['module_jobs'] if item['state'] ==
                          ('pending' if phase == 'exports' else 'exported')), None)
        if next_item:
            self.active = next_item['id']
            self.child = self.factory()
            self.child.defer_ue = phase == 'exports'
            self.child.geometry_input_identity_version = data.get('geometry_input_identity_version', 0)
            self.child.custom_material_binding_version = data.get('custom_material_binding_version', 0)
            self.child.shared_bake_end = data.get('shared_bake_end', 0)
            try:
                if next_item.get('job'):
                    self.child.resume(child_folder(self.parent, next_item['job']))
                else:
                    transfer = self.parent.folder / 'modules' / self.active
                    transfer.mkdir(parents=True, exist_ok=True)
                    saved_settings = dict(next_item['settings'])
                    saved_settings.setdefault('export_validation_policy', 'strict')
                    cfg = replace(Settings(**saved_settings), character_output_dir=str(transfer))
                    self.child.start(cfg)
                self.save_item(job=str(self.child.job.folder), state='running', phase=phase)
            except Exception as exc:
                self.save_item(state='failed', message=str(exc))
                self.parent.status('module_failed', f'[{self.active}] {exc}')
                self.child = None
                self.active = None
            return read_json(self.parent.folder / 'status.json')
        if phase == 'exports':
            data['orchestration_phase'] = 'imports'
            atomic_json(self.parent.path, data)
            self.parent.status('modules_running', '各模块本地处理结束，逐模块导入 UE')
            return read_json(self.parent.folder / 'status.json')
        if not any(item['state'] in SUCCESS for item in data['module_jobs']):
            report = {'complete': False, 'state': 'failed', 'modules': data['module_jobs'],
                      'error': '没有可装配的成功模块'}
            atomic_json(self.parent.folder / 'ue_report.json', report)
            self.parent.status('failed', '所有模块均未完成；请查看各模块的错误与检查点')
            self.done = True
            return read_json(self.parent.folder / 'status.json')
        manifest = assembly_manifest(self.parent)
        atomic_json(self.parent.folder / 'manifest.json', manifest)
        self.assembling = True
        self.owner.scene_started = True
        self.parent.status('scene_done', '复用成功模块结果，开始最终关卡与序列装配')
        return read_json(self.parent.folder / 'status.json')

    def stop(self):
        if self.child:
            self.child.stop()
        self.owner.stopped = True
        (self.parent.folder / 'stop').touch()
        self.parent.status('stopped', '组合任务已停止；独立模块检查点保留')

    def connect(self, project):
        project = Path(project).resolve()
        if not project.is_file() or project.suffix.lower() != '.uproject':
            raise ValueError('请选择有效的 .uproject')
        data = self.parent.data
        old = data['settings'].get('project')
        if old and Path(old).resolve() != project:
            raise ValueError('组合任务已有目标工程；更换目标请新建任务')
        data['settings']['project'] = str(project)
        for item in data['module_jobs']:
            item['settings']['project'] = str(project)
            if item.get('job'):
                child = Job(child_folder(self.parent, item['job']))
                saved = child.data
                saved['settings']['project'] = str(project)
                atomic_json(child.path, saved)
        atomic_json(self.parent.path, data)
        if self.child:
            self.child.connect(project)
