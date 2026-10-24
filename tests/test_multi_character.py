import os
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from mmd_bridge import mmd_avi
from mmd_bridge.core import Job, Settings, atomic_json, read_json
from mmd_bridge.ue_project import BUILTIN_PLUGINS, ensure_builtin_plugins
from tests.test_vmd_reader import make_vmd, _bone_frame


def test_cbt_fix_changes_only_verified_build_and_preserves_source(tmp_path, monkeypatch):
    import hashlib
    source = tmp_path / 'source'
    source.mkdir()
    original = bytes(mmd_avi.CBT_MODULE_OFFSET) + mmd_avi.CBT_MODULE_LOAD + b'unchanged suffix'
    dll = source / 'd3d9.dll'
    dll.write_bytes(original)
    assert mmd_avi.runtime_hook_dll(source) == original
    monkeypatch.setattr(mmd_avi, 'CBT_DLL_SHA256', hashlib.sha256(original).hexdigest())
    fixed = mmd_avi.runtime_hook_dll(source)
    assert len(fixed) == len(original)
    assert fixed == original[:mmd_avi.CBT_MODULE_OFFSET] + mmd_avi.CBT_LOCAL_MODULE + b'unchanged suffix'
    assert dll.read_bytes() == original
    with pytest.raises(ValueError, match='原安装'):
        mmd_avi.configure_runtime(source, source, 2, tmp_path / 'vmd', tmp_path / 'progress.json')


def test_static_morphs_are_valid_and_animated_morphs_still_required():
    from mmd_bridge.blender_validate import animated_morph_names
    reference = [{'morphs': {'blink': 0}}, {'morphs': {'blink': 0}}]
    assert animated_morph_names({}, reference) == set()
    assert animated_morph_names({'animated_morphs': ['blink']}, reference) == {'blink'}
    reference[1]['morphs']['blink'] = 1
    assert animated_morph_names({}, reference) == {'blink'}


def test_runtime_reuse_across_tasks_retargets_outputs_and_rebuilds_corrupt_copy(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    waiting, sleep = Event(), mmd_avi.time.sleep
    def wait_for_lock(seconds):
        waiting.set()
        sleep(seconds)
    monkeypatch.setattr(mmd_avi.time, 'sleep', wait_for_lock)
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    source = tmp_path / 'source'
    (source / 'mmdbridge_scripts').mkdir(parents=True)
    (source / 'mmdbridge_settings').mkdir()
    for name in (mmd_avi.MMD_EXE, 'd3d9.dll', 'python312.dll'):
        (source / name).write_bytes(b'original')
    import hashlib
    original_dll = bytes(mmd_avi.CBT_MODULE_OFFSET) + mmd_avi.CBT_MODULE_LOAD
    (source / 'd3d9.dll').write_bytes(original_dll)
    monkeypatch.setattr(mmd_avi, 'CBT_DLL_SHA256', hashlib.sha256(original_dll).hexdigest())
    ini = source / 'mmdbridge_settings' / (mmd_avi.MMD_EXE + '.ini')
    ini.write_text('ScriptName=old.py\nScriptCallSetting=0\nStartFrame=0\nEndFrame=9\nExportFPS=30\n', encoding='utf-16')
    (source / 'mmdbridge_scripts' / mmd_avi.SCRIPT).write_text('from mmdbridge_vmd import *\ndst_dir = "out"\n')
    first, second = Job(tmp_path / 'first'), Job(tmp_path / 'second')
    first.folder.mkdir(); second.folder.mkdir()
    with mmd_avi.reusable_runtime(first, source, 2, first.folder / 'vmd', first.folder / 'progress.json') as executable:
        original = executable
        first_stamp = executable.stat().st_mtime_ns
        (executable.parent / 'reuse.marker').touch()
        marker = read_json(executable.parent.parent / 'source.json')
        assert marker['hashes']['d3d9.dll'] != marker['runtime_hashes']['d3d9.dll']
        atomic_json(executable.parent.parent / 'active.json', {'job': str(first.folder), 'pid': 123})
        def competing_task():
            with mmd_avi.reusable_runtime(second, source, 4, second.folder / 'vmd', second.folder / 'progress.json'):
                pytest.fail('A running task must retain exclusive access')
        with ThreadPoolExecutor(max_workers=1) as pool:
            competing = pool.submit(competing_task)
            assert waiting.wait(3)
            (second.folder / 'stop').touch()
            with pytest.raises(RuntimeError, match='停止'):
                competing.result(timeout=3)
        assert read_json(executable.parent.parent / 'active.json')['job'] == str(first.folder)
        (second.folder / 'stop').unlink()
    assert not (executable.parent.parent / 'active.json').exists()
    ini.write_text(ini.read_text(encoding='utf-16').replace('EndFrame=9', 'EndFrame=99'), encoding='utf-16')
    with mmd_avi.reusable_runtime(second, source, 4, second.folder / 'vmd', second.folder / 'progress.json') as executable:
        assert executable == original and executable.stat().st_mtime_ns == first_stamp
        assert (executable.parent / 'reuse.marker').exists()
        script = (executable.parent / 'mmdbridge_scripts' / mmd_avi.SCRIPT).read_text()
        assert str(second.folder).replace('\\', '\\\\') in script
        assert str(first.folder).replace('\\', '\\\\') not in script
        assert 'EndFrame=4' in (executable.parent / 'mmdbridge_settings' / ini.name).read_text(encoding='utf-16')
    executable.write_bytes(b'corrupt')
    with mmd_avi.reusable_runtime(second, source, 4, second.folder / 'vmd', second.folder / 'progress.json') as rebuilt:
        assert rebuilt == original and rebuilt.read_bytes() == b'original'


def test_stopping_job_does_not_kill_other_task_using_shared_runtime(tmp_path, monkeypatch):
    from mmd_bridge.controller import Controller
    from mmd_bridge import process
    job = Job(tmp_path / 'job'); job.folder.mkdir()
    atomic_json(job.path, {'settings': {}, 'character_jobs': []})
    cache = tmp_path / 'cache'
    cache.mkdir()
    atomic_json(cache / 'active.json', {'job': str(tmp_path / 'other'), 'pid': 123})
    attempt = job.folder / 'mmd_full'
    attempt.mkdir()
    atomic_json(attempt / 'attempt.json', {'validated': False, 'mmd_pid': 123,
        'runtime_reusable': True, 'runtime': str(cache / 'runtime' / mmd_avi.MMD_EXE)})
    controller = Controller(tmp_path / 'home')
    controller.job = job
    monkeypatch.setattr(process, 'pid_exists', lambda pid: pytest.fail('Must not inspect another task PID'))
    controller.stop()
    assert read_json(job.folder / 'status.json')['state'] == 'stopped'


def test_independent_bakes_keep_role_inputs_and_shared_duration(tmp_path, monkeypatch):
    from tests.test_pmx_physics_displayframe import make_pmx
    model = tmp_path / 'actor.pmx'
    model.write_bytes(make_pmx())
    motions = []
    for index, end in enumerate((2, 4, 3)):
        path = tmp_path / f'role{index}.vmd'
        path.write_bytes(make_vmd(bone_frames=[_bone_frame('頭', 0), _bone_frame('頭', end)]))
        motions.append(str(path))
    monkeypatch.setattr(Settings, 'validate', lambda self: {
        'motion': {'max_frame': mmd_avi.scan_vmd(self.motion)['bone_max']}})
    parent = Job.create(tmp_path / 'jobs', Settings(model=str(model), motion=motions[0],
        additional_characters=[{'model': str(model), 'motion': motion} for motion in motions[1:]]))
    runs = []
    def bake(child):
        cfg = child.data['settings']
        runs.append((cfg['model'], cfg['motion'], child.data['shared_bake_end']))
        assert not cfg['additional_characters']
        assert Path(cfg['character_output_dir']) == child.folder
        child.status('mmd_bake_done', 'ok')
    monkeypatch.setattr(mmd_avi, 'run_bake', bake)
    mmd_avi.run_multi_bake(parent)
    assert runs == [(str(model), motion, 4) for motion in motions]
    assert len(set(parent.data['character_jobs'])) == 3
    assert read_json(parent.folder / 'status.json')['state'] == 'mmd_bake_done'
    monkeypatch.setattr(mmd_avi.shutil, 'disk_usage', lambda path: N(free=0))
    with pytest.raises(ValueError, match='AVI 临时空间不足'):
        mmd_avi.run_multi_bake(parent)
    assert len(runs) == 3


def test_runtime_cleanup_removes_only_validated_task_copy(tmp_path):
    folder = tmp_path / 'attempt'
    runtime = folder / 'runtime'
    runtime.mkdir(parents=True)
    executable = runtime / mmd_avi.MMD_EXE
    executable.write_bytes(b'task copy')
    source = tmp_path / 'source' / mmd_avi.MMD_EXE
    source.parent.mkdir()
    source.write_bytes(b'original')
    capture = folder / 'capture.avi'
    capture.write_bytes(b'generated')
    attempt = {'validated': False, 'runtime': str(executable)}
    mmd_avi.cleanup_export_avi(folder, attempt)
    assert runtime.exists() and capture.exists()
    attempt.update(validated=True, runtime=str(source))
    mmd_avi.cleanup_export_avi(folder, attempt)
    assert attempt['runtime_cleanup']['status'] == 'failed'
    assert runtime.exists() and source.is_file()
    attempt['runtime'] = str(executable)
    mmd_avi.cleanup_export_avi(folder, attempt)
    assert attempt['runtime_cleanup']['status'] == 'deleted'
    assert not runtime.exists() and source.read_bytes() == b'original'


@pytest.mark.parametrize('control_id,action', [(1148, '打开'), (1152, '打开'), (1001, '保存')])
def test_file_dialog_waits_for_filename_control_not_address_bar(monkeypatch, control_id, action):
    ui = object.__new__(mmd_avi.MmdUi)
    ui.process = N(poll=lambda: None)
    filled, clicks = [], []
    other = N(class_name=lambda: 'Edit', control_id=lambda: 1001 if action == '打开' else 2000,
              set_edit_text=lambda text: pytest.fail('Wrong edit control'))
    filename = N(class_name=lambda: 'Edit', control_id=lambda: control_id, set_edit_text=filled.append)
    frames = iter([[other], [other, filename]])
    dialog = N(children=lambda: next(frames))
    ui.dialog = lambda title: dialog
    ui._button = lambda *args: clicks.append(args)
    monkeypatch.setattr(mmd_avi.time, 'sleep', lambda *args: None)
    ui._file('Open', Path('role.vmd'), action)
    assert filled == ['role.vmd'] and clicks == [(dialog, action)]


def test_builtin_plugins_enable_disabled_entries_preserve_other_plugins_and_backup(tmp_path):
    project = tmp_path / 'Demo.uproject'
    original = {'Description': 'keep', 'Plugins': [
        {'Name': 'USDImporter', 'Enabled': False, 'Optional': True},
        {'Name': 'Custom', 'Enabled': False}]}
    atomic_json(project, original)
    assert set(ensure_builtin_plugins(project)) == set(BUILTIN_PLUGINS)
    data = read_json(project)
    assert data['Description'] == 'keep'
    assert next(p for p in data['Plugins'] if p['Name'] == 'Custom')['Enabled'] is False
    assert next(p for p in data['Plugins'] if p['Name'] == 'USDImporter')['Optional']
    assert read_json(project.with_suffix('.uproject.mmdb-backup')) == original
    assert ensure_builtin_plugins(project) == []


@pytest.mark.parametrize('dialog_kind', ['none', 'confirm', 'late', 'unknown', 'command_error'])
def test_motion_waits_for_own_command_even_when_main_is_responsive(monkeypatch, dialog_kind):
    from threading import Event
    ui = object.__new__(mmd_avi.MmdUi)
    ui.process = N(poll=lambda: None)
    ui.main = N(handle=1, is_enabled=lambda: True)
    entered, release = Event(), Event()
    def command(command, *, wait=False):
        assert command == mmd_avi.MOTION_LOAD_ID and wait
        entered.set()
        assert release.wait(2)
        if dialog_kind == 'command_error':
            raise TimeoutError('native command timed out')
    ui._command = command
    def file(*args):
        assert entered.wait(2)
    ui._file = file
    confirm = N(window_text=lambda: 'Error' if dialog_kind == 'unknown' else '動作数据',
                children=lambda: [N(window_text=lambda: '同骨骼名')])
    clicked, sleeps = [], []
    def dialogs():
        visible = dialog_kind in ('confirm', 'unknown') or (dialog_kind == 'late' and len(sleeps) >= 2)
        return [confirm] if visible and not clicked else []
    ui._visible_dialogs = dialogs
    ui._button = lambda *args: clicked.append(args)
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            release.set()
        Event().wait(.001)
    monkeypatch.setattr(mmd_avi.time, 'sleep', sleep)
    try:
        if dialog_kind in ('unknown', 'command_error'):
            with pytest.raises((RuntimeError, TimeoutError), match='确认内容|native command'):
                ui.load_motion(Path('motion.vmd'))
        else:
            ui.load_motion(Path('motion.vmd'))
            assert release.is_set() and len(sleeps) >= 4
            assert bool(clicked) == (dialog_kind != 'none')
    finally:
        release.set()


def test_native_motion_command_waits_for_actual_handler(monkeypatch):
    import sys
    replies = []
    monkeypatch.setitem(sys.modules, 'win32gui', N(SendMessageTimeout=lambda *args: replies.append(args)))
    ui = object.__new__(mmd_avi.MmdUi)
    ui.main = N(handle=1, menu_items=lambda: [{'item_id': mmd_avi.MOTION_LOAD_ID}])
    ui._command(mmd_avi.MOTION_LOAD_ID, wait=True)
    assert replies == [(1, mmd_avi.WM_COMMAND, mmd_avi.MOTION_LOAD_ID, 0, 33, 90000)]


@pytest.mark.parametrize('stage', ['导出AVI', 'AVI导出设定'])
def test_confirmation_arriving_after_load_returns_aborts_avi_instead_of_clicking(stage):
    ui = object.__new__(mmd_avi.MmdUi)
    ui.process = N(poll=lambda: None)
    confirm = N(window_text=lambda: '動作数据', children=lambda: [N(window_text=lambda: '同骨骼名')])
    ui._visible_dialogs = lambda: [confirm, N(window_text=lambda: stage)]
    with pytest.raises(mmd_avi.MotionLoadRace, match='拒绝继续'):
        ui.dialog(stage)


@pytest.mark.parametrize('failure,count,success', [
    (mmd_avi.MotionLoadRace, 1, True), (mmd_avi.MotionLoadRace, 2, False), (RuntimeError, 1, False)])
def test_only_known_load_race_retries_once_without_skipping_validation(tmp_path, monkeypatch, failure, count, success):
    job = Job(tmp_path / 'job')
    job.folder.mkdir()
    atomic_json(job.path, {'settings': {'mmdbridge_path': 'runtime'}})
    prepared = N(frame_range=(0, 2), lipsync=None, requires_mmd_bake=True,
                 source_model=Path('source.pmx'), source_motion=Path('source.vmd'),
                 model=Path('fixed.pmx'), repaired_bone_count=0, renamed_bone_count=0, expected_bones=['髪'])
    monkeypatch.setattr(mmd_avi, 'prepare_job', lambda job: prepared)
    calls = []
    def attempt(*args):
        calls.append(args)
        if len(calls) <= count:
            raise failure('test load error')
        return tmp_path / 'baked.vmd', {'validated': True}
    monkeypatch.setattr(mmd_avi, '_attempt', attempt)
    mmd_avi.run_bake(job)
    status = read_json(job.folder / 'status.json')
    assert status['state'] == ('mmd_bake_done' if success else 'failed')
    assert len(calls) == (2 if failure is mmd_avi.MotionLoadRace else 1)
    if success:
        events = (job.folder / 'events.jsonl').read_text(encoding='utf-8')
        assert '"severity": "warning"' in events


@pytest.mark.parametrize('motion_end,lip_end,shared_end,expected', [
    (240, 0, 0, 240), (2, 120, 0, 120), (2, 3, 400, 400), (0, 0, 0, None)])
def test_bake_runs_only_full_duration_without_sample(tmp_path, monkeypatch, motion_end, lip_end, shared_end, expected):
    from tests.test_vmd_reader import _morph_frame
    job = Job(tmp_path / 'job')
    job.folder.mkdir()
    atomic_json(job.path, {'settings': {'mmdbridge_path': str(tmp_path / 'runtime')},
                          'shared_bake_end': shared_end})
    lipsync = tmp_path / 'facial.vmd'
    lipsync.write_bytes(make_vmd(morph_frames=[_morph_frame('笑い', lip_end, 1)]))
    prepared = N(frame_range=(0, motion_end), requires_mmd_bake=True, lipsync=lipsync,
                 source_model=tmp_path / 'model.pmx', source_motion=tmp_path / 'motion.vmd',
                 model=tmp_path / 'fixed.pmx', repaired_bone_count=0, renamed_bone_count=0,
                 expected_bones=['髪'])
    monkeypatch.setattr(mmd_avi, 'prepare_job', lambda job: prepared)
    calls = []
    baked = tmp_path / 'full.vmd'
    def attempt(job, source, inputs, end, label):
        calls.append((end, label))
        return baked, {'success': True}
    monkeypatch.setattr(mmd_avi, '_attempt', attempt)
    mmd_avi.run_bake(job)
    status = read_json(job.folder / 'status.json')
    if expected is None:
        assert status['state'] == 'failed' and '至少需要 2 帧' in status['message']
        assert not calls
    else:
        assert status['state'] == 'mmd_bake_done'
        assert calls == [(expected, 'full')]
        assert read_json(job.folder / 'effective.json')['effective_motion'] == str(baked)


@pytest.mark.parametrize('missing_export', [False, True])
def test_merge_preserves_per_character_contract_and_scene(tmp_path, missing_export):
    from dataclasses import asdict
    from mmd_bridge.controller import Controller
    from mmd_bridge.core import file_hash
    parent = Job(tmp_path / 'parent')
    parent.folder.mkdir()
    children = []
    for index in range(2):
        child = Job(parent.folder / 'characters' / str(index))
        child.folder.mkdir(parents=True)
        atomic_json(child.path, {'settings': {'model': f'role{index}.pmx'}})
        files = {}
        for kind in ('model', 'animation'):
            path = child.folder / (kind + '.fbx')
            path.write_bytes(str(index).encode())
            files[kind] = {'path': str(path), 'sha256': file_hash(path)}
        atomic_json(child.folder / 'blender_roundtrip.json', {'success': True})
        atomic_json(child.folder / 'manifest.json', {
            'fps': 30, 'files': files, 'materials': [{'id': str(index), 'pmx_index': 0}],
            'bones': [{'original': f'head{index}'}], 'warnings': ['FBX 精度警告'] if index else [],
            'roundtrip_warnings': ['FBX 精度警告'] if index else [], 'frame_range': [0, 2]})
        children.append(str(child.folder))
    if missing_export:
        (Job(children[1]).folder / 'animation.fbx').unlink()
    atomic_json(parent.path, {'id': 'parent', 'asset_id': 'parent',
        'settings': asdict(Settings(scene='scene.blend', additional_characters=[{}])),
        'character_jobs': children})
    atomic_json(parent.folder / 'manifest.json', {'scene': {'route': 'usd_stage'}})
    controller = Controller(tmp_path / 'home')
    controller.job = parent
    launched = []
    controller.launch_blender = lambda mode, job=None: launched.append((mode, job.folder))
    status = controller.poll_character_exports()
    if missing_export:
        assert status['state'] == 'characters_export'
        assert launched == [('character', Job(children[1]).folder)]
    else:
        assert status['state'] == 'character_done' and not launched
        manifest = read_json(parent.folder / 'manifest.json')
        assert [c['bones'][0]['original'] for c in manifest['characters']] == ['head0', 'head1']
        assert [c['materials'][0]['id'] for c in manifest['characters']] == ['0', '1']
        assert manifest['scene']['route'] == 'usd_stage' and controller.scene_started
        events = [__import__('json').loads(line) for line in (parent.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert any(event['state'] == 'warning' and event['severity'] == 'warning' for event in events)
        assert manifest['warnings'] == ['role1: FBX 精度警告']


def test_gui_keeps_per_character_inputs_and_independent_scene_flow(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    window.timer.stop()
    characters = [{'model': 'rin.pmx', 'motion': 'rin.vmd', 'lipsync': 'rin_face.vmd'},
                  {'model': 'len.pmx', 'motion': 'len.vmd', 'lipsync': ''}]
    expected = [characters[0] | {'supplementary_motions': ['rin_face.vmd']}, characters[1]]
    try:
        window.apply_settings({'additional_characters': characters})
        assert window.settings().additional_characters == expected
        assert window.flow_settings('scene').additional_characters == []
        window.lock(True)
        assert not window.character_table.isEnabled()
        window.lock(False)
        assert window.character_table.isEnabled()
        window.close()
        restarted = Window()
        assert restarted.multi_character_button.isChecked()
        assert restarted.settings().additional_characters == expected
        assert restarted.additional_characters() == expected
        restarted.multi_character_button.click()
        assert restarted.settings().additional_characters == []
        # An unrelated path update must preserve the selected single-character mode.
        scene = tmp_path / 'scene.blend'
        scene.touch()
        restarted.inputs['scene'].setText(str(scene))
        assert read_json(restarted.dialog_paths_file)['multi_character_mode'] is False
        restarted.timer.stop()
        restarted.close()
        single = Window()
        single.timer.stop()
        assert not single.multi_character_button.isChecked()
        assert single.additional_characters() == expected
        single.close()
    finally:
        window.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_gui_preset_remembers_mode_even_without_extra_characters(tmp_path, monkeypatch, enabled):
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    window.timer.stop()
    preset = tmp_path / 'preset.json'
    monkeypatch.setattr(W.QFileDialog, 'getSaveFileName', lambda *args: (str(preset), ''))
    try:
        assert not window.multi_character_button.isChecked()
        window.multi_character_button.setChecked(enabled)
        window.save_preset()
        saved = read_json(preset)
        assert saved['multi_character_mode'] is enabled
        assert saved['additional_characters'] == []
        window.multi_character_button.setChecked(not enabled)
        window.apply_settings(saved)
        assert window.multi_character_button.isChecked() is enabled
    finally:
        window.close()


def test_gui_character_mode_switches_preserve_full_paths_and_fit_cells(tmp_path, monkeypatch):
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window, CharacterFileField, configure_app
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    configure_app(app)
    window = Window()
    window.timer.stop()
    window.show()
    app.processEvents()
    primary = {}
    for key, filename in (('model', '角色模型.pmx'), ('motion', '角色动作.vmd'), ('lipsync', '角色表情.vmd')):
        path = tmp_path / filename
        path.touch()
        primary[key] = str(path)
    extra = {'model': str(tmp_path / 'Rin.pmx'), 'motion': str(tmp_path / 'Rin.vmd'), 'lipsync': ''}
    try:
        assert not window.multi_character_button.isChecked()
        assert window.single_character_panel.isVisible() and not window.multi_character_panel.isVisible()
        for key, value in primary.items():
            window.inputs[key].setText(value)
        window.multi_character_button.click()
        app.processEvents()
        assert not window.single_character_panel.isVisible() and window.multi_character_panel.isVisible()
        assert window.character_table.rowCount() == 2
        assert window.character_values(0) == primary | {'supplementary_motions': [primary['lipsync']]}
        for column, key in enumerate(primary):
            field = window.character_table.cellWidget(0, column).findChild(CharacterFileField, key)
            assert field.text() == Path(primary[key]).name
            assert field.toolTip() == primary[key]
        expression = window.character_table.cellWidget(0, 2).findChild(CharacterFileField)
        expression.clear_action.trigger()
        assert window.settings().lipsync == '' and expression.text() == ''
        for column, key in enumerate(extra):
            window.character_table.cellWidget(1, column).findChild(CharacterFileField, key).set_path(extra[key])
        assert window.settings().additional_characters == [extra]
        window.add_character_row({'model': 'Len.pmx', 'motion': 'Len.vmd'})
        expected = window.settings().additional_characters
        window.multi_character_button.click()
        assert window.single_character_panel.isVisible() and not window.multi_character_panel.isVisible()
        assert window.settings().additional_characters == []
        assert window.settings().model == primary['model']
        window.multi_character_button.click()
        assert window.settings().additional_characters == expected
        same_name = tmp_path / 'other' / Path(primary['model']).name
        same_name.parent.mkdir(); same_name.touch()
        observed = []
        monkeypatch.setattr(W.QFileDialog, 'getOpenFileName',
                            lambda *args: (observed.append(args[2]) or str(same_name), ''))
        window.character_table.cellWidget(0, 0).findChild(W.QPushButton).click()
        assert observed == [str(tmp_path)]
        assert window.settings().model == str(same_name)
        assert read_json(window.dialog_paths_file)['values']['model'] == str(same_name)
        for width, height in ((1240, 900), (1000, 760)):
            window.resize(width, height)
            app.processEvents()
            assert window.character_table.columnWidth(2) < window.character_table.columnWidth(0)
            for row in range(window.character_table.rowCount()):
                for column in range(3):
                    cell = window.character_table.cellWidget(row, column)
                    field = cell.findChild(CharacterFileField)
                    button = cell.findChild(W.QPushButton)
                    assert field.height() >= field.minimumSizeHint().height()
                    assert button.height() >= button.minimumSizeHint().height()
                    assert cell.rect().contains(field.geometry()) and cell.rect().contains(button.geometry())
        window.character_table.setCurrentCell(0, 0)
        window.remove_character_row()
        assert window.settings().model == extra['model']
        promoted = window.character_table.cellWidget(0, 0).findChild(CharacterFileField)
        promoted.set_path(primary['model'])
        assert window.settings().model == primary['model']
        window.lock(True)
        assert not window.multi_character_button.isEnabled()
        window.lock(False)
        assert window.multi_character_button.isEnabled()
        window.apply_settings({'model': primary['model'], 'additional_characters': []}, existing_job=True)
        assert not window.multi_character_button.isChecked()
        assert window.single_character_panel.isVisible() and window.settings().additional_characters == []
    finally:
        window.close()


def test_character_row_selects_from_children_and_removes_clicked_role(tmp_path, monkeypatch):
    from PySide6 import QtCore as C, QtTest as T, QtWidgets as W
    from mmd_bridge.app import Window, CharacterFileField, configure_app
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    configure_app(app)
    window = Window()
    window.timer.stop()
    window.apply_settings({'model': 'Miku.pmx', 'motion': 'Miku.vmd', 'additional_characters': [
        {'model': 'Rin.pmx', 'motion': 'Rin.vmd', 'lipsync': ''},
        {'model': 'Len.pmx', 'motion': 'Len.vmd', 'lipsync': ''}]})
    window.show()
    app.processEvents()
    window.tabs.widget(window.files_tab_index).ensureWidgetVisible(window.multi_character_panel)
    app.processEvents()
    table = window.character_table
    dialogs = []
    monkeypatch.setattr(W.QFileDialog, 'getOpenFileName',
                        lambda *args: (dialogs.append(table.currentRow()) or '', ''))
    monkeypatch.setattr(W.QFileDialog, 'getOpenFileNames',
                        lambda *args: (dialogs.append(table.currentRow()) or [], ''))
    def selected(row):
        assert table.currentRow() == row
        assert [index.row() for index in table.selectionModel().selectedRows()] == [row]
        for r in range(table.rowCount()):
            assert all(table.cellWidget(r, c).property('selected') == (r == row) for c in range(3))
    try:
        for row in (0, 2, 1):
            for column in range(3):
                cell = table.cellWidget(row, column)
                field = cell.findChild(CharacterFileField)
                button = cell.findChild(W.QPushButton)
                for widget, point in ((cell, C.QPoint(3, 3)), (field, C.QPoint(8, field.height() // 2)),
                                      (button, button.rect().center())):
                    table.setCurrentCell((row + 1) % 3, 0)
                    T.QTest.mouseClick(widget, C.Qt.LeftButton, pos=point)
                    app.processEvents()
                    selected(row)
                assert dialogs[-1] == row
        field = table.cellWidget(2, 0).findChild(CharacterFileField)
        clear = next(b for b in field.findChildren(W.QToolButton) if b.isVisible())
        T.QTest.mouseClick(clear, C.Qt.LeftButton)
        selected(2)
        assert window.character_values(2)['model'] == ''
        field.set_path('Len.pmx')
        header = table.verticalHeader()
        T.QTest.mouseClick(header.viewport(), C.Qt.LeftButton,
                          pos=C.QPoint(header.width() // 2, header.sectionViewportPosition(1) + 10))
        selected(1)
        window.remove_character_row()
        assert window.character_values(0)['model'] == 'Miku.pmx'
        assert window.character_values(1)['model'] == 'Len.pmx'
        T.QTest.mouseClick(table.cellWidget(1, 0).findChild(CharacterFileField), C.Qt.LeftButton)
        selected(1)
        window.remove_character_row()
        assert table.rowCount() == 1 and window.settings().model == 'Miku.pmx'
    finally:
        window.close()
