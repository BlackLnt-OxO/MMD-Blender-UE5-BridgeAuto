import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtWidgets as W
from mmd_bridge.app import Window, configure_app
from mmd_bridge.controller import Controller
from mmd_bridge.core import Job, Settings, atomic_json, read_json, scene_collision_enabled


def test_workflow_buttons_at_bottom_and_full_flow_keeps_all_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    configure_app(app)
    window = Window()
    window.timer.stop()
    try:
        window.apply_settings({'model': 'model.pmx', 'motion': 'motion.vmd', 'scene': 'scene.blend', 'camera': 'camera.vmd'})
        settings = window.flow_settings('auto')
        assert (settings.flow, settings.model, settings.motion, settings.scene, settings.camera) == (
            'auto', 'model.pmx', 'motion.vmd', 'scene.blend', 'camera.vmd')
        assert settings.unattended
        character_blender = tmp_path / 'Blender43/blender.exe'
        scene_blender = tmp_path / 'Blender52/blender.exe'
        for path in (character_blender, scene_blender):
            path.parent.mkdir()
            path.touch()
        window.show_software([
            {'kind': 'blender', 'path': str(character_blender), 'version': '4.3.2', 'source': 'test', 'compatibility': 'test'},
            {'kind': 'blender', 'path': str(scene_blender), 'version': '5.2.2', 'source': 'test', 'compatibility': 'test'}])
        window.inputs['blender'].setCurrentIndex(window.inputs['blender'].findData(str(character_blender)))
        settings = window.flow_settings('auto')
        assert settings.character_blender == str(scene_blender)
        assert settings.blender == str(scene_blender) and settings.unattended
        window.show_software([
            {'kind': 'blender', 'path': str(scene_blender), 'version': '5.2.2', 'source': 'test', 'compatibility': 'test'}])
        settings = window.flow_settings('auto')
        assert settings.character_blender == settings.blender == str(scene_blender)
        window.show()
        app.processEvents()
        buttons = [window.full_button, window.start_button, window.scene_button, window.camera_button]
        assert all(button.objectName() == 'workflow' for button in buttons)
        assert [button.x() for button in buttons] == sorted(button.x() for button in buttons)
        assert all(button.y() >= window.tabs.y() + window.tabs.height() for button in buttons)
        clicked = []
        monkeypatch.setattr(window, 'start', clicked.append)
        for button in buttons:
            button.click()
        assert clicked == ['auto', 'character', 'scene', 'camera']
        window.lock(True)
        assert not any(button.isEnabled() for button in buttons)
    finally:
        window.controller.job = None
        window.close()


def test_relative_collision_and_explicit_overrides():
    assert not scene_collision_enabled(True, [.1, .1, .02], [100, 100, 5])
    assert scene_collision_enabled(True, [100, 100, 1], [100, 100, 5])
    assert scene_collision_enabled(True, [.1, .1, .02], [100, 100, 5], override='exact')
    assert not scene_collision_enabled(True, [100, 100, 1], [100, 100, 5], override='none')
    assert not scene_collision_enabled(False, [100, 100, 1], [100, 100, 5], override='exact')
    assert scene_collision_enabled(True, [.1, .1, .02], [100, 100, 5], min_ratio=0)
    assert scene_collision_enabled(True, [.5, .1, .02], [100, 100, 5])


def test_transfer_directory_defaults_to_desktop_with_unicode_task_names(tmp_path, monkeypatch):
    from mmd_bridge import core
    monkeypatch.setattr(core, 'desktop_folder', lambda: tmp_path / 'Desktop')
    settings = Settings(model='角色.pmx', scene='场景.blend')
    first = core.task_transfer_folder(settings)
    second = core.task_transfer_folder(settings)
    assert first.name == 'Middle_角色.场景_01'
    assert second.name == 'Middle_角色.场景_02'
    selected = tmp_path / 'selected'
    selected.mkdir()
    settings.character_output_dir = str(selected)
    assert core.task_transfer_folder(settings) == selected


def test_scene_jobs_and_outputs_use_transfer_directory(tmp_path, monkeypatch):
    from mmd_bridge import core
    import mmd_bridge.controller as module
    monkeypatch.setattr(core, 'desktop_folder', lambda: tmp_path / 'Desktop')
    monkeypatch.setattr(module, 'check_upstream_installation', lambda settings: None)
    blender = tmp_path / 'blender.exe'
    blender.touch()
    scene = tmp_path / '街道.blend'
    scene.touch()
    controller = Controller(tmp_path / 'home')
    monkeypatch.setattr(controller, 'launch_blender', lambda mode: None)
    job = controller.start(Settings(flow='scene', blender=str(blender), scene=str(scene)))
    transfer = tmp_path / 'Desktop/Middle_街道_01'
    assert job.folder.parent == transfer / 'MMDBridge_Jobs'
    assert job.data['settings']['character_output_dir'] == str(transfer)
    assert not (controller.home / 'jobs').exists()
    next_job = controller.start(Settings(flow='scene', blender=str(blender), scene=str(scene)))
    assert next_job.folder.parent.parent.name == 'Middle_街道_02'
    assert job.data['asset_id'] != next_job.data['asset_id']


def test_selected_project_uses_readable_unique_scene_names_across_transfer_roots(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    monkeypatch.setattr(module, 'check_upstream_installation', lambda settings: None)
    blender = tmp_path / 'blender.exe'; blender.touch()
    scene = tmp_path / 'railway.blend'; scene.touch()
    project = tmp_path / 'UE' / 'Test.uproject'; project.parent.mkdir(); project.write_text('{}')
    controller = Controller(tmp_path / 'home')
    monkeypatch.setattr(controller, 'launch_blender', lambda mode: None)
    jobs = []
    for name in ('transfer_a', 'transfer_b'):
        (tmp_path / name).mkdir()
        jobs.append(controller.start(Settings(flow='scene', blender=str(blender), scene=str(scene),
            project=str(project), character_output_dir=str(tmp_path / name))))
    assert [job.data['asset_id'] for job in jobs] == ['railway_01', 'railway_02']
    for job in jobs:
        folder = project.parent / 'Content' / 'Scenes' / job.data['asset_folder']
        assert read_json(folder / '.mmd_bridge_namespace.json')['job'] == str(job.folder)
    old_transfer = tmp_path / 'old_config'; old_transfer.mkdir()
    old_config_job = controller.start(Settings(flow='scene', blender=str(blender), scene=str(scene),
        project=str(project), character_output_dir=str(old_transfer),
        content_root='/Game/On_9_24_开发_PlanV2/On_9_25/On_9_26'))
    assert old_config_job.data['settings']['content_root'] == '/Game/Scenes'
    assert old_config_job.data['asset_id'] == 'railway_03'
    assert not (project.parent / 'Content' / 'On_9_24_开发_PlanV2').exists()


def test_scene_content_names_preserve_source_unicode_and_skip_existing_folder(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    monkeypatch.setattr(module, 'check_upstream_installation', lambda settings: None)
    blender = tmp_path / 'blender.exe'; blender.touch()
    scene = tmp_path / '港口_夜景.blend'; scene.touch()
    project = tmp_path / 'UE' / 'Test.uproject'; project.parent.mkdir(); project.write_text('{}')
    existing = project.parent / 'Content' / 'Scenes' / '港口_夜景_01'
    existing.mkdir(parents=True)
    protected = existing / 'user.txt'; protected.write_text('keep')
    transfer = tmp_path / 'transfer'; transfer.mkdir()
    controller = Controller(tmp_path / 'home')
    monkeypatch.setattr(controller, 'launch_blender', lambda mode: None)
    job = controller.start(Settings(flow='scene', blender=str(blender), scene=str(scene),
        project=str(project), character_output_dir=str(transfer), content_root='/Game/Custom'))
    assert job.data['settings']['content_root'] == '/Game/Scenes'
    assert job.data['asset_folder'] == '港口_夜景_02'
    assert protected.read_text() == 'keep'
    assert not (project.parent / 'Content' / 'Custom').exists()


def test_full_flow_launches_each_blender_version(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    controller = Controller(tmp_path / 'home')
    job = Job(tmp_path / 'job')
    atomic_json(job.path, {'settings': asdict(Settings(blender='scene52.exe', character_blender='character43.exe', unattended=True))})
    controller.job = job
    launched = []
    monkeypatch.setattr(module.process, 'popen', lambda args, **kw: launched.append(args))
    controller.launch_blender('character')
    controller.launch_blender('scene')
    assert [args[0] for args in launched] == ['character43.exe', 'scene52.exe']
    assert all('--background' in args for args in launched)
    for handle in controller.log_handles:
        handle.close()


def test_stop_kills_task_children_and_only_active_target_ue(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    controller = Controller(tmp_path / 'home')
    job = Job(tmp_path / 'job')
    project = tmp_path / 'Target.uproject'
    atomic_json(job.path, {'settings': asdict(Settings(project=str(project)))})
    job.status('ue_scene', 'native import')
    atomic_json(job.folder / 'ue_checkpoint.json', {'completed': {'materials': ['kept']}})
    original = (job.folder / 'ue_checkpoint.json').read_bytes()
    controller.job = job
    controller.mailbox = tmp_path / 'mailbox'
    atomic_json(controller.mailbox / 'request.json', {'id': 'current', 'job': str(job.folder)})
    controller.ue_requested = True
    killed = []
    monkeypatch.setattr(module.process, 'terminate_pid', killed.append)
    monkeypatch.setattr(module, 'running_projects', lambda: [
        {'path': str(project), 'pid': 22}, {'path': str(tmp_path / 'Other.uproject'), 'pid': 33}])
    controller.processes = [('scene', SimpleNamespace(pid=11, poll=lambda: None, wait=lambda timeout: None))]
    controller.stop()
    assert killed == [11, 22]
    assert not controller.processes and controller.stopped
    assert (job.folder / 'ue_checkpoint.json').read_bytes() == original
    assert read_json(controller.mailbox / 'response.json') == {'id': 'current', 'state': 'stopped'}
    assert controller.poll()['state'] == 'stopped'
    job.status('complete', 'finished')
    controller.stop()
    assert killed == [11, 22]  # An idle editor must survive stop.


def test_scene_resume_retains_checkpoint_and_skips_completed_export(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', lambda folder: None)
    job = Job(tmp_path / 'job')
    atomic_json(job.path, {'settings': asdict(Settings(flow='scene', scene='source.blend')), 'inputs': {}})
    atomic_json(job.folder / 'scene_checkpoint.json', {'completed': [1, 2]})
    (job.folder / 'stop').touch()
    controller = Controller(tmp_path / 'home')
    launches = []
    monkeypatch.setattr(controller, 'launch_blender', launches.append)
    controller.resume(job.folder)
    assert '还原点' in controller.poll()['message']
    assert launches == ['scene']
    assert read_json(job.folder / 'scene_checkpoint.json')['completed'] == [1, 2]
    atomic_json(job.folder / 'manifest.json', {'scene': {'route': 'usd_stage'}})
    controller.resume(job.folder)
    monkeypatch.setattr(controller, 'launch_ue', lambda: None)
    controller.poll()
    assert launches == ['scene']


def test_partial_freeze_resume_preserves_batch_and_reports_continuation(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', lambda folder: None)
    job = Job(tmp_path / 'job')
    atomic_json(job.path, {'settings': asdict(Settings(flow='scene', scene='source.blend')), 'inputs': {}})
    batch = job.folder / 'scene_checkpoint/Freeze_000001.json.gz'
    batch.parent.mkdir()
    batch.write_bytes(b'committed-batch')
    controller = Controller(tmp_path / 'home')
    launches = []
    monkeypatch.setattr(controller, 'launch_blender', launches.append)
    controller.resume(job.folder)
    assert '冻结或尺寸统计批次' in controller.poll()['message']
    assert launches == ['scene']
    assert batch.read_bytes() == b'committed-batch'


def test_mode1_resume_uses_effective_inputs_without_rebaking(tmp_path, monkeypatch):
    import mmd_bridge.controller as module
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', lambda folder: None)
    monkeypatch.setattr(module, 'run_bake', lambda job: (_ for _ in ()).throw(AssertionError('must not rebake')))
    job = Job(tmp_path / 'job')
    atomic_json(job.path, {'settings': asdict(Settings(model='source.pmx', motion='motion.vmd',
                                                      character_backend='mmdbridge_fbx')), 'inputs': {}})
    atomic_json(job.folder / 'effective.json', {'effective_model': 'fixed.pmx', 'effective_motion': 'baked.vmd'})
    controller = Controller(tmp_path / 'home')
    launches = []
    monkeypatch.setattr(controller, 'launch_blender', launches.append)
    controller.resume(job.folder)
    assert launches == ['character'] and controller.bake_thread is None


def test_mmd_resume_reuses_prepared_input_and_validated_attempt(tmp_path, monkeypatch):
    from mmd_bridge import mmd_avi
    from mmd_bridge.core import file_hash
    from mmd_bridge.plan_v2_prep import PreparedCharacterV2
    job = Job(tmp_path / 'job')
    atomic_json(job.path, {'settings': asdict(Settings(model='source.pmx', motion='source.vmd'))})
    paths = []
    for name in ('model.pmx', 'motion.vmd', 'bone_name_map.json'):
        path = job.folder / 'input' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture', encoding='utf-8')
        paths.append(path)
    prepared = PreparedCharacterV2(tmp_path/'source.pmx', tmp_path/'source.vmd', *paths[:2], None, paths[2],
                                   0, 0, 0, (0, 2), (), {})
    values = {k: str(v) if isinstance(v, Path) else v for k, v in asdict(prepared).items()}
    atomic_json(job.folder / 'mmd_prepared.json', {'prepared': values, 'hashes': {str(p): file_hash(p) for p in paths}})
    monkeypatch.setattr(mmd_avi, 'prepare_character_v2', lambda *a: (_ for _ in ()).throw(AssertionError('must not reprepare')))
    mmd_avi.run_bake(job)
    assert read_json(job.folder / 'status.json')['state'] == 'mmd_bake_done'
    assert read_json(job.folder / 'effective.json')['mmm_skipped_reason'] == 'zero_physics_bones'
    baked = job.folder / 'mmd_short/vmd/baked.vmd'
    baked.parent.mkdir(parents=True)
    baked.write_bytes(b'validated-bake')
    atomic_json(job.folder / 'mmd_short/attempt.json', {'validated': True, 'end_frame': 2,
                 'vmd': {'path': str(baked), 'sha256': file_hash(baked)}})
    monkeypatch.setattr(mmd_avi, 'validate_mode1', lambda *a, **kw: {'valid': True})
    monkeypatch.setattr(mmd_avi, '_runtime', lambda *a: (_ for _ in ()).throw(AssertionError('must not launch MMD')))
    result_path, result_receipt = mmd_avi._attempt(job, tmp_path/'runtime', prepared, 2, 'short')
    assert result_path == baked and result_receipt['valid'] is True
    assert result_receipt['avi_cleanup']['status'] == 'already_absent'
