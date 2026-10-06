"""Visible Qt-button acceptance driver. Conversion stays in the existing GUI."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
parser = argparse.ArgumentParser()
parser.add_argument('--run', type=Path, default=Path(r'D:\Codex_Projects_RebuildAutoMMD\SceneConversion_20261006'))
parser.add_argument('--resume-job', type=Path)
parser.add_argument('--reconvert-materials', action='store_true')
parser.add_argument('--stage', type=int, choices=(1, 2, 3), default=1)
parser.add_argument('--revision', default='baseline')
# Every invocation stops after the selected scene; later stages require user review.
parser.add_argument('--reference-from', type=Path)
args = parser.parse_args()
RUN = args.run.resolve()
TRANSFER = Path(r'D:\Codex_Projects_RebuildAutoMMD')
PROJECT = Path(r'F:\Ue5_Project\TESTON1_1005_02\TESTON1_1005_02.uproject')
BLENDER = r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
UE = r'C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe'
SCENES = [
    {'key': '01_graffiti', 'name': '涂鸦墙_可食用风茄', 'source': r'F:\MMD_Scene\Blender\涂鸦墙_可食用风茄\涂鸦墙_可食用风茄.blend', 'frame': 0, 'camera': 'Camera', 'width': 2560},
    {'key': '02_cherry', 'name': 'cherry', 'source': r'F:\MMD_Scene\Blender\SableFiSh\cherry\cherry.blend', 'frame': 180, 'camera': '摄像机', 'width': 2580},
    {'key': '03_mygo', 'name': "It'sMyGO!!!!!-ParkingLots", 'source': r"F:\MMD_Scene\Blender\SableFiSh\MyGO-ParkingLots\It'sMyGO!!!!!-ParkingLots.blend", 'frame': 1, 'camera': 'Camera', 'width': 2560},
]
RUN.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / 'scripts/ue_scene_stage_audit.py', RUN / 'ue_scene_stage_audit.py')
os.environ['MMDBRIDGE_HOME'] = str(RUN / 'gui_home')
os.environ['MMDBRIDGE_ACCEPTANCE_RUN'] = str(RUN)
os.environ['MMDBRIDGE_ACCEPTANCE_PROJECT'] = str(PROJECT)
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from mmd_bridge.app import Window, configure_app
from mmd_bridge.core import Settings, atomic_json, read_json, file_hash
from mmd_bridge import process
from mmd_bridge.controller import running_projects, UE_CODE_MODULES
def event(kind, **fields):
    entry = {'time': time.time(), 'kind': kind, **fields}
    with (RUN / 'driver_events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
    print(json.dumps(entry, ensure_ascii=False), flush=True)
def archive_start():
    dest = RUN / 'initial_state'
    if dest.exists():
        return
    dest.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL='NUL')
    for name, command in [('git_status.txt', ['git', 'status', '--short']), ('git_diff.patch', ['git', 'diff', '--binary'])]:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
        (dest / name).write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace'))
    hashes = {}
    with zipfile.ZipFile(dest / 'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for directory in ('mmd_bridge', 'scripts', 'tests', 'docs', 'plugins/unreal/MMDBridge'):
            for path in (ROOT / directory).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:
                    relative = path.relative_to(ROOT).as_posix()
                    archive.write(path, relative)
                    hashes[relative] = file_hash(path)
        for name in ('README.md', 'pyproject.toml'):
            archive.write(ROOT / name, name)
            hashes[name] = file_hash(ROOT / name)
    atomic_json(dest / 'source_hashes.json', hashes)
    for path in (PROJECT, PROJECT.parent / 'Config/DefaultEngine.ini'):
        shutil.copy2(path, dest / path.name)
    plugin = PROJECT.parent / 'Plugins/MMDBridge'
    with zipfile.ZipFile(dest / 'project_plugin.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in plugin.rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(plugin))
    atomic_json(dest / 'sources.json', {s['key']: {'path': s['source'], 'sha256': file_hash(s['source'])} for s in SCENES})
archive_start()
if running_projects():
    raise RuntimeError('Close existing UE editors before starting isolated acceptance')
state_path = RUN / 'driver_state.json'
state = read_json(state_path) if state_path.exists() else {'scenes': {}, 'started': time.time()}
state.update(index=args.stage - 1, phase='boot', revision=args.revision, pid=os.getpid())
for transient in ('last_gui_error','error','attention_phase','audit_id','job_status'):
    state.pop(transient,None)
ue_children = []
original_popen = process.popen
def instrumented_popen(command, **kwargs):
    command = list(command)
    if Path(str(command[0])).name.lower() == 'unrealeditor.exe' and any(str(PROJECT).casefold() == str(x).casefold() for x in command):
        command.append('-ExecCmds=py ' + str(ROOT / 'scripts/ue_scene_stage_audit.py').replace('\\', '/'))
        child = original_popen(command, **kwargs)
        ue_children.append(child)
        event('ue_launch', pid=child.pid, command=command)
        return child
    return original_popen(command, **kwargs)
process.popen = instrumented_popen
app = W.QApplication([])
configure_app(app)
window = Window()
window.resize(1280, 960)
window.setWindowTitle('MMDBridge — 分阶段场景验收')
window.show()
times = {'sample': 0.0, 'status': None}
def save():
    state['updated'] = time.time()
    atomic_json(state_path, {key: value for key, value in state.items() if key != '_tick_active'})
def current():
    return SCENES[state['index']]
def output():
    path = RUN / args.revision / current()['key']
    path.mkdir(parents=True, exist_ok=True)
    return path
def phase(name):
    previous = state['phase']
    event('phase', scene=current()['key'] if state['index'] < len(SCENES) else None, previous=previous, phase=name)
    state['phase'] = name
    state['phase_started'] = time.time()
    save()
def click(button):
    if not button.isEnabled():
        raise RuntimeError('GUI button disabled: ' + button.text())
    window.grab().save(str(output() / (state['phase'] + '_gui.png')))
    event('gui_button', scene=current()['key'], text=button.text())
    button.click()
def named_button(text):
    matches = [b for b in window.findChildren(W.QPushButton) if b.text() == text]
    if len(matches) != 1:
        raise RuntimeError('Ambiguous GUI button: ' + text)
    return matches[0]
def acknowledge():
    for dialog in W.QApplication.topLevelWidgets():
        if isinstance(dialog, W.QMessageBox) and dialog.isVisible():
            button = dialog.button(W.QMessageBox.StandardButton.Ok)
            if button:
                event('gui_message', title=dialog.windowTitle(), message=dialog.text())
                if dialog.icon() in (W.QMessageBox.Icon.Critical, W.QMessageBox.Icon.Warning) and dialog.windowTitle() != 'UE 导入已暂停':
                    state['last_gui_error'] = dialog.text()
                button.click()
dialog_timer = C.QTimer()
dialog_timer.timeout.connect(acknowledge)
dialog_timer.start(200)
def audit(kind, filename):
    request = {'id': uuid.uuid4().hex, 'kind': kind,
               'job': str(window.controller.job.folder) if window.controller.job else None,
               'output': str(output() / filename)}
    atomic_json(RUN / 'ue_audit_request.json', request)
    state['audit_id'] = request['id']
    save()
def audit_response():
    path = RUN / 'ue_audit_response.json'
    result = read_json(path) if path.is_file() else {}
    return result if result.get('id') == state.get('audit_id') else None
def live_ue():
    return [p for p in ue_children if p.poll() is None]
def copy_evidence():
    folder = window.controller.job.folder
    for name in ('job.json', 'manifest.json', 'ue_report.json', 'status.json', 'ue_checkpoint.json', 'events.jsonl'):
        source = folder / name
        if source.is_file():
            shutil.copy2(source, output() / name)
    atomic_json(output() / 'code_hashes.json', {name: file_hash(ROOT / 'mmd_bridge' / name) for name in UE_CODE_MODULES})
def preserve_image(kind):
    folder = window.controller.job.folder / 'visual'
    receipt_name = 'reference.json' if kind == 'blender' else 'ue_render.json'
    receipt = read_json(folder / receipt_name)
    path = folder / (kind + '.png')
    scene = current()
    if not path.is_file() or receipt.get('frame') != scene['frame']:
        raise RuntimeError('Missing or incorrect reference frame: ' + kind)
    picture = G.QImage(str(path))
    if picture.size() != C.QSize(scene['width'], 1080):
        raise RuntimeError('Incorrect reference dimensions: ' + kind)
    if receipt.get('image_sha256') != file_hash(path):
        raise RuntimeError('Reference receipt hash mismatch: ' + kind)
    if kind == 'ue':
        renderer = receipt['renderer']
        if not receipt.get('complete') or renderer.get('mode') != 'lumen' or renderer.get('ShowFlag.PathTracing') != 0 or not renderer.get('path_tracing_force_off_observed'):
            raise RuntimeError('Lumen success receipt not verified')
    elif receipt.get('camera') != scene['camera']:
        raise RuntimeError('Wrong Blender source camera')
    shutil.copy2(path, output() / (kind + '.png'))
    shutil.copy2(folder / receipt_name, output() / receipt_name)
    state['scenes'][scene['key']][kind + '_image'] = str(output() / (kind + '.png'))
    event('image_preserved', scene=scene['key'], renderer=kind, sha256=receipt['image_sha256'])
    save()
def sample():
    if time.monotonic() < times['sample']:
        return
    times['sample'] = time.monotonic() + 10
    info = {'time': time.time(), 'scene': current()['key'], 'phase': state['phase'],
            'physical': process.physical_memory_info(), 'gpu': process.gpu_memory_info(),
            'ue_pids': [p.pid for p in live_ue()],
            'children': [{'mode': mode, 'pid': child.pid, 'exit': child.poll()} for mode, child in window.controller.processes]}
    with (RUN / 'memory_trace.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(info, ensure_ascii=False) + '\n')
def resume_button(folder):
    previous = W.QFileDialog.getOpenFileName
    W.QFileDialog.getOpenFileName = lambda *a, **k: (str(folder / 'job.json'), '')
    try:
        click(window.resume_button)
    finally:
        W.QFileDialog.getOpenFileName = previous
def tick():
    if window.workers:
        return
    sample()
    controller = window.controller
    status_path = controller.job.folder / 'status.json' if controller.job else None
    status = read_json(status_path) if status_path and status_path.is_file() else {}
    signature = (status.get('state'), status.get('message'))
    if signature != times['status']:
        times['status'] = signature
        event('job_status', **status)
        state['job_status'] = status
        save()
    if state['phase'] == 'attention':
        command_path = RUN / 'driver_command.json'
        if not command_path.is_file():
            return
        command = read_json(command_path)
        command_path.rename(RUN / ('driver_command_' + uuid.uuid4().hex + '.json'))
        if command['action'] == 'resume':
            state.pop('last_gui_error', None)
            resume_button(controller.job.folder)
            phase('convert')
        elif command['action'] == 'retry_reference':
            controller.reference_request_id = None
            state.pop('last_gui_error', None)
            phase(command['phase'])
        elif command['action'] == 'skip':
            state.pop('last_gui_error',None)
            state['scenes'][current()['key']]['outcome'] = 'failed'
            if live_ue():
                audit('close', 'skip_close.json')
                phase('close')
            else:
                phase('next')
        return
    if state.get('last_gui_error'):
        raise RuntimeError(state['last_gui_error'])
    p = state['phase']
    terminal = status.get('state') in ('complete', 'needs_review') and not controller.processes and not controller.reference_busy()
    if p not in ('boot', 'start', 'next', 'close', 'pre_reload', 'reload') and status.get('state') in ('failed', 'stopped', 'cancelled', 'upstream_blocked', 'ue_wait_restart', 'ue_paused_gpu_crash', 'ue_paused_resource_limit'):
        raise RuntimeError('Conversion/render needs diagnosis: ' + status.get('message', ''))
    if p == 'boot':
        window.apply_settings({'project': str(PROJECT), 'blender': BLENDER, 'unreal': UE, 'character_output_dir': str(TRANSFER)})
        click(named_button('安装 UE 桥接插件'))
        deployed = PROJECT.parent / 'Plugins/MMDBridge/Content/Python/mmd_bridge'
        if not all((deployed / name).is_file() and file_hash(deployed / name) == file_hash(ROOT / 'mmd_bridge' / name) for name in UE_CODE_MODULES):
            raise RuntimeError('GUI bridge installation incomplete')
        phase('start')
    elif p == 'start':
        scene = current()
        if args.resume_job:
            if args.reconvert_materials:
                previous = W.QFileDialog.getOpenFileName
                W.QFileDialog.getOpenFileName = lambda *a, **k: (str(args.resume_job/'job.json'), '')
                try: click(window.scene_result_button)
                finally: W.QFileDialog.getOpenFileName = previous
                window.apply_settings({'material_bake_fallback': True, 'material_complex_strategy': 'pbr_bake'})
                window.update_scene_actions()
                click(window.scene_reconvert_button)
            else:
                resume_button(args.resume_job)
            args.resume_job = None
        else:
            config = Settings(flow='scene', blender=BLENDER, unreal=UE, project=str(PROJECT), material_bake_fallback=True, material_complex_strategy='pbr_bake',
                character_output_dir=str(TRANSFER), scene=scene['source'], scene_frame=scene['frame'],
                width=scene['width'], height=1080, content_root='/Game/MMDBridgeValidation/SceneConversion_20261006')
            window.apply_settings(asdict(config))
            window.tabs.setCurrentIndex(0)
            click(window.scene_button)
        if not controller.job:
            raise RuntimeError('GUI did not create a scene job')
        state['scenes'][scene['key']] = dict(scene, job=str(controller.job.folder), started=time.time(), revision=args.revision)
        save()
        phase('convert')
    elif p == 'convert' and terminal:
        mailbox=controller.mailbox
        response=read_json(mailbox/'response.json') if mailbox and (mailbox/'response.json').is_file() else {}
        request=read_json(mailbox/'request.json') if mailbox and (mailbox/'request.json').is_file() else {}
        if response.get('id')!=request.get('id') or response.get('state') not in ('complete','needs_review'):return
        pending=set(controller._material_retry_sources())-set(controller.job.data.get('material_bake_retry',{}))
        if pending and controller.job.data['settings'].get('material_complex_strategy')=='pbr_bake':return
        copy_evidence()
        state['scenes'][current()['key']]['import_seconds'] = time.time() - state['phase_started']
        audit('inspect_and_exit', 'before_restart.json')
        phase('pre_reload')
    elif p == 'pre_reload':
        receipt = audit_response()
        if receipt and receipt.get('error'):
            raise RuntimeError(receipt['error'])
        if receipt and not live_ue():
            state['scenes'][current()['key']]['pre_reload_complete'] = receipt['complete']
            audit('reload', 'after_restart.json')
            process.popen([UE, str(PROJECT), '/Engine/Maps/Entry'], creationflags=0x08000000)
            phase('reload')
    elif p == 'reload':
        receipt = audit_response()
        if receipt and not live_ue():
            if receipt.get('error'):
                raise RuntimeError(receipt['error'])
            state['scenes'][current()['key']]['reload_complete'] = receipt['complete']
            before = read_json(output() / 'before_restart.json')
            stable = {name: before.get(name) == receipt.get(name) for name in ('mesh_components', 'instanced_count', 'actor_classes')}
            atomic_json(output() / 'restart_comparison.json', {'complete': receipt['complete'] and before['complete'] and all(stable.values()), 'checks': stable})
            if not receipt['complete'] or not before['complete'] or not all(stable.values()):
                raise RuntimeError('Saved scene changed after restart; inspect reload evidence')
            phase('blender_start')
    elif p == 'blender_start':
        if args.reference_from:
            reference = args.reference_from.resolve()
            receipt = read_json(reference.parent / 'reference.json')
            initial = read_json(Path(r'D:\Codex_Projects_RebuildAutoMMD\SceneConversion_20261006\initial_state\sources.json'))[current()['key']]
            if (receipt['image_sha256'] != file_hash(reference) or
                    controller.job.data['inputs']['scene'] != initial['sha256'] or
                    receipt['frame'] != current()['frame'] or receipt['camera'] != current()['camera']):
                raise RuntimeError('Cannot reuse a reference with mismatched source/camera/frame')
            visual = controller.job.folder / 'visual'
            visual.mkdir(exist_ok=True)
            shutil.copy2(reference, visual / 'blender.png')
            shutil.copy2(reference.parent / 'reference.json', visual / 'reference.json')
            preserve_image('blender')
            event('source_reference_reused', path=str(reference), source_sha256=initial['sha256'])
            phase('ue_start')
            return
        window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
        window.update_scene_actions()
        click(window.blender_reference_button)
        phase('blender_render')
    elif p == 'blender_render' and terminal:
        preserve_image('blender')
        state['scenes'][current()['key']]['blender_render_seconds'] = time.time() - state['phase_started']
        phase('ue_start')
    elif p == 'ue_start':
        window.update_scene_actions()
        click(window.ue_reference_button)
        phase('ue_render')
    elif p == 'ue_render' and terminal:
        preserve_image('ue')
        state['scenes'][current()['key']]['ue_render_seconds'] = time.time() - state['phase_started']
        phase('compare_start')
    elif p == 'compare_start':
        window.update_scene_actions()
        click(window.comparison_button)
        phase('compare_wait')
    elif p == 'compare_wait':
        visual = controller.job.folder / 'visual'
        if (visual / 'difference.json').is_file():
            for name in ('comparison.png', 'difference.png', 'difference.json'):
                if (visual / name).is_file():
                    shutil.copy2(visual / name, output() / name)
            copy_evidence()
            window.grab().save(str(output() / 'completed_gui.png'))
            state['scenes'][current()['key']].update(outcome='needs_review', finished=time.time())
            audit('close', 'editor_close.json')
            phase('close')
    elif p == 'close':
        receipt = audit_response()
        if receipt and receipt.get('error'):
            raise RuntimeError(receipt['error'])
        if receipt and not live_ue():
            phase('next')
    elif p == 'next':
        if (process.gpu_budget_percent(process.gpu_memory_info()) or 0) >= 80:
            return
        controller.job = None
        controller.reference_request_id = None
        for handle in controller.log_handles:
            if not handle.closed:
                handle.close()
        controller.log_handles = []
        window.lock(False)
        phase('baseline_done')
        event('baseline_done', scenes=state['scenes'])
        window.close()
        app.quit()
def checked_tick():
    if state['phase'] == 'baseline_done' or state.get('_tick_active'):
        return
    state['_tick_active'] = True
    try:
        tick()
    except Exception:
        error = traceback.format_exc()
        event('driver_attention', error=error, previous_phase=state['phase'])
        state['attention_phase'] = state['phase']
        state['error'] = error
        phase('attention')
    finally:
        state['_tick_active'] = False
timer = C.QTimer()
timer.timeout.connect(checked_tick)
timer.start(2000)
event('driver_started', revision=args.revision, run=str(RUN))
save()
raise SystemExit(app.exec())
