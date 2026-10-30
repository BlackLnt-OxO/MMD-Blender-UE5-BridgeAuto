import json
import time
import pytest

import mmd_bridge.controller as controller_module
from mmd_bridge.core import Job, Settings, atomic_json,read_json,stable_id
from mmd_bridge.controller import Controller, bridge_status, connection_folder, install_ue_bridge


def fake_job(tmp_path,scene='pool.blend'):
    job=Job(tmp_path/'job')
    atomic_json(job.path,{'settings':{'model':'model.pmx','motion':'dance.vmd','camera':'','scene':scene,'project':'','unreal':''}})
    atomic_json(job.folder/'manifest.json',{'files':{'model':{},'animation':{}}})
    job.status('character_done','done')
    return job


def test_no_early_ue_launch_while_scene_starting(tmp_path):
    c=Controller(tmp_path/'home')
    c.job=fake_job(tmp_path)
    launches=[]
    c.launch_blender=lambda mode:launches.append(mode)
    c.launch_ue=lambda:launches.append('ue')
    c.poll()
    c.poll()
    assert launches==['scene']
    atomic_json(c.job.folder/'manifest.json',{'files':{'model':{},'animation':{}},'scene':{}})
    c.job.status('scene_done','done')
    c.poll()
    assert launches==['scene','ue']


def test_import_compaction_precedes_editor_launch_and_runs_once(tmp_path):
    c=Controller(tmp_path/'home');c.job=fake_job(tmp_path)
    data=c.job.data;data['settings']['optimize_usd_scatter']=True
    atomic_json(c.job.path,data)
    atomic_json(c.job.folder/'manifest.json',{'files':{'model':{},'animation':{}},
        'scene':{'route':'usd_stage'}})
    c.job.status('scene_done','done');c.scene_started=True
    launches=[];c.launch_blender=lambda mode:launches.append(mode)
    c.poll();c.poll()
    assert launches==['scene_import']
    assert read_json(c.job.folder/'status.json')['state']=='scene_import_optimizing'


def test_finished_character_does_not_fail_running_scene(tmp_path):
    c=Controller(tmp_path/'home')
    c.job=fake_job(tmp_path)
    c.job.status('scene_export','working')
    class Process:
        def poll(self): return 0
    c.processes=[('character',Process())]
    c.poll()
    assert read_json(c.job.folder/'status.json')['state']=='scene_export'
    assert c.processes==[]


def test_scene_receipt_arriving_during_process_poll_is_not_failed(tmp_path):
    c=Controller(tmp_path/'home');c.job=fake_job(tmp_path)
    c.job.status('scene_export','last batch')
    class Process:
        def poll(self):
            c.job.status('scene_done','receipt finished')
            return 0
    c.processes=[('scene',Process())]
    assert c.poll()['state']=='scene_done'
    assert read_json(c.job.folder/'status.json')['state']=='scene_done'


@pytest.mark.parametrize('format',['legacy','usd_stage','lighting_only'])
def test_scene_completion_manifest_survives_shared_export_status_handoff(tmp_path,format):
    c=Controller(tmp_path/'home');c.job=fake_job(tmp_path)
    usd=tmp_path/'scene.usda';usd.write_text('#usda 1.0\n')
    scene={'usd':str(usd) if format=='legacy' else {'root_layer':str(usd)},'stats':{'instances':1}}
    if format=='lighting_only':scene.update(stats={'instances':0},metrics={'elapsed_seconds':1.})
    atomic_json(c.job.folder/'manifest.json',{'scene':scene})
    c.job.status('scene_export','another export handoff is starting')
    c.processes=[('scene',type('Process',(),{'poll':lambda self:0})())]
    assert c.poll()['state']=='scene_done'


def test_scene_completion_rejects_missing_usd_and_unfinished_zero_instance_manifest(tmp_path):
    for root,stats in ((str(tmp_path/'missing.usda'),{'instances':1}),
                       (str(tmp_path/'scene.usda'),{'instances':0})):
        (tmp_path/'scene.usda').write_text('#usda 1.0\n')
        c=Controller(tmp_path/'home');c.job=fake_job(tmp_path)
        atomic_json(c.job.folder/'manifest.json',{'scene':{'usd':{'root_layer':root},'stats':stats}})
        c.job.status('scene_export','no final receipt')
        c.processes=[('scene',type('Process',(),{'poll':lambda self:0})())]
        assert c.poll()['state']=='failed'


def test_scene_receipt_waits_for_process_exit_before_launching_ue(tmp_path):
    c=Controller(tmp_path/'home');c.job=fake_job(tmp_path);c.scene_started=True
    c.job.status('scene_done','final manifest written')
    launched=[];c.launch_ue=lambda:launched.append('ue');c.exports_ready=lambda:True
    class Process:
        code=None
        def poll(self):return self.code
    process=Process();c.processes=[('scene',process)]
    c.poll();assert not launched
    process.code=0;c.poll();assert not launched
    c.poll();assert launched==['ue']


def test_mmd_bake_handoff_launches_blender_once_and_reports_launch_failure(tmp_path):
    c = Controller(tmp_path/'home')
    c.job = fake_job(tmp_path, scene='')
    data = read_json(c.job.path)
    data['settings']['character_backend'] = 'mmdbridge_fbx'
    atomic_json(c.job.path, data)
    c.job.status('mmd_bake_done', 'ready')
    launches = []
    c.launch_blender = lambda mode: launches.append(mode)
    c.poll()
    c.poll()
    assert launches == ['character']

    c.character_started = False
    c.launch_blender = lambda mode: (_ for _ in ()).throw(OSError('missing executable'))
    assert c.poll()['state'] == 'failed'
    assert 'missing executable' in read_json(c.job.folder/'status.json')['message']


def test_legacy_mode1_job_and_status_history_live_in_transfer_directory(tmp_path, monkeypatch):
    model = tmp_path/'model.pmx'; model.write_bytes(b'PMX ')
    motion = tmp_path/'motion.vmd'
    from tests.test_core import vmd
    vmd(motion, bones=1)
    camera = vmd(tmp_path/'camera.vmd', cameras=1)
    scene = tmp_path/'scene.blend'; scene.touch()
    blender = tmp_path/'blender.exe'; blender.touch()
    bridge = tmp_path/'bridge'
    (bridge/'mmdbridge_scripts').mkdir(parents=True)
    for name in ('MikuMikuDance_CHS.exe', 'd3d9.dll',
                 'mmdbridge_scripts/mmdbridge_vmd_mode1.py'):
        (bridge/name).touch()
    transfer = tmp_path/'transfer'; transfer.mkdir()
    settings = Settings(character_backend='mmdbridge_fbx', model=str(model),
                        motion=str(motion), camera=str(camera), scene=str(scene), blender=str(blender),
                        mmdbridge_path=str(bridge), character_output_dir=str(transfer))
    monkeypatch.setattr(controller_module, 'run_bake',
                        lambda job: job.status('mmd_bake_done', 'ready'))
    c = Controller(tmp_path/'home')
    job = c._start_single(settings)
    c.bake_thread.join(timeout=5)
    assert job.folder.parent == transfer/'MMDBridge_Jobs'
    assert read_json(job.path)['settings']['camera'] == str(camera)
    assert [entry['state'] for entry in map(json.loads,
           (job.folder/'events.jsonl').read_text(encoding='utf-8').splitlines())] == [
               'created', 'mmd_bake_done']
    monkeypatch.setattr('mmd_bridge.core.desktop_folder', lambda: tmp_path / 'Desktop')
    settings.character_output_dir = ''
    automatic = c._start_single(settings)
    c.bake_thread.join(timeout=5)
    assert automatic.folder.parent == tmp_path / 'Desktop/Middle_model.scene_01/MMDBridge_Jobs'


def test_camera_cut_session_is_standalone_and_waits_for_ue(tmp_path):
    project = tmp_path/'Camera.uproject'
    project.write_text('{}', encoding='utf-8')
    c = Controller(tmp_path/'home')
    session = c.start_camera_cut(project)
    assert c.job is None
    assert read_json(session/'status.json')['state'] == 'camera_cut_waiting_ue'

    mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4',
        'code_hashes':{name:controller_module.file_hash(controller_module.resource_root()/'mmd_bridge'/name)
                       for name in controller_module.UE_CODE_MODULES}})
    status = c.poll_camera_cut()
    request = read_json(mailbox/'camera_cut_request.json')
    assert request['mode'] == 'probe'
    assert status['state'] == 'camera_cut_queued'


def test_frame_upgrade_keeps_detected_sequence_and_rate_in_its_request(tmp_path):
    project = tmp_path/'Camera.uproject'
    project.write_text('{}', encoding='utf-8')
    c = Controller(tmp_path/'home')
    session = c.start_camera_cut(project, frame_rate_upgrade=True, original_fps=30, target_fps=60)
    atomic_json(session/'detect_01.json', {'sequence': '/Game/Current.Current', 'display_rate': [60, 1], 'candidates': [7176]})
    c.camera_cut_command('upgrade', [7176])
    requests = list((c.home/'connections').glob('*/camera_cut_request.json'))
    assert len(requests) == 1
    request = read_json(requests[0])
    assert request['mode'] == 'upgrade' and request['frame_rate_upgrade'] is True
    assert request['original_fps'] == 30 and request['target_fps'] == 60
    assert request['expected_sequence'] == '/Game/Current.Current'
    assert request['expected_display_rate'] == [60, 1]
    assert c.job is None


def test_bridge_status_has_three_states(tmp_path):
    project = tmp_path/'Bridge.uproject'
    project.write_text('{}', encoding='utf-8')
    home = tmp_path/'home'
    assert bridge_status(project, home)['state'] == 'not_installed'

    install_ue_bridge(project, home)
    assert (project.parent/'Plugins/MMDBridge/Content/Python/mmd_bridge/material_policy.py').is_file()
    assert bridge_status(project, home)['state'] == 'installed_offline'

    mailbox = connection_folder(home, project)
    assert mailbox.name == 'Connection_Bridge_01'
    atomic_json(mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4',
        'code_hashes':{name:controller_module.file_hash(controller_module.resource_root()/'mmd_bridge'/name)
                       for name in controller_module.UE_CODE_MODULES}})
    assert bridge_status(project, home)['state'] == 'connected'
    atomic_json(mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(),
        'code_hashes': {'ue_host.py':'stale'}})
    assert bridge_status(project, home)['state'] == 'needs_restart'


def test_launch_ue_does_not_duplicate_an_open_project(tmp_path, monkeypatch):
    project = tmp_path/'AlreadyOpen.uproject'
    from mmd_bridge.ue_project import BUILTIN_PLUGINS
    atomic_json(project, {'Plugins': [{'Name': name, 'Enabled': True} for name in BUILTIN_PLUGINS]})
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    data['settings']['unreal'] = str(tmp_path/'UnrealEditor.exe')
    atomic_json(job.path, data)
    c = Controller(tmp_path/'home')
    c.job = job
    monkeypatch.setattr(controller_module, 'running_projects',
                        lambda: [{'path': str(project.resolve()), 'pid': 123}])
    launches = []
    monkeypatch.setattr(controller_module.process, 'popen', lambda *args, **kwargs: launches.append(args))

    c.launch_ue()

    assert launches == []
    assert c.ue_started
    status = read_json(job.folder/'status.json')
    assert status['state'] == 'ue_wait_restart'
    assert '旧版桥接代码' in status['message']
    assert c.ue_requested


def test_low_vram_wait_auto_requeues_after_memory_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module.process,'physical_memory_info',lambda: {'total_mb':16000,'available_mb':6000})
    project = tmp_path/'Resume.uproject'
    project.write_text('{}', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    data['settings']['unreal'] = str(tmp_path/'UnrealEditor.exe')
    data['settings']['ue_vram_pause_percent'] = 85
    data['settings']['ue_vram_resume_percent'] = 80
    atomic_json(job.path, data)
    job.status('ue_wait_vram', 'paused', retry_after=0, pause_percent=85, resume_percent=80)
    monkeypatch.setattr(controller_module.process, 'pid_exists', lambda pid: True)
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(c.mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    c.ue_started = c.ue_requested = True
    monkeypatch.setattr(controller_module.process, 'gpu_memory_info', lambda: {
        'index': 0, 'name': 'GPU', 'shared_total_mb': 8192, 'shared_used_mb': 6500,
        'dedicated_total_mb': 8192, 'dedicated_used_mb': 8192,
        'local_budget_mb': 6500, 'local_used_mb': 8000})

    c.poll()

    assert read_json(c.mailbox/'request.json')['job'] == str(job.folder)
    assert read_json(job.folder/'status.json')['state'] == 'awaiting_ue'
    assert '共享显存占用已降至' in read_json(job.folder/'status.json')['message']


@pytest.mark.parametrize('memory', [
    {'index': 0, 'name': 'GPU', 'shared_total_mb': 8192, 'shared_used_mb': 6963},
    {'total_mb': 8192, 'used_mb': 10},
])
def test_low_vram_wait_does_not_resume_above_low_watermark(tmp_path, monkeypatch, memory):
    project = tmp_path/'StillHot.uproject'
    project.write_text('{}', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    data['settings']['unreal'] = str(tmp_path/'UnrealEditor.exe')
    data['settings']['ue_vram_pause_percent'] = 85
    data['settings']['ue_vram_resume_percent'] = 80
    atomic_json(job.path, data)
    job.status('ue_wait_vram', 'paused', retry_after=0, pause_percent=85, resume_percent=80)
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(c.mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    c.ue_started = c.ue_requested = True
    monkeypatch.setattr(controller_module.process, 'gpu_memory_info', lambda: memory)

    c.poll()

    assert not (c.mailbox/'request.json').exists()
    assert read_json(job.folder/'status.json')['state'] == 'ue_wait_vram'


def test_stale_heartbeat_pid_falls_back_to_current_project_process(tmp_path, monkeypatch):
    project = tmp_path/'Restarted.uproject'
    project.write_text('{}', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    atomic_json(job.path, data)
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(c.mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 111, 'time': time.time()-60, 'engine': '5.7.4'})
    monkeypatch.setattr(controller_module.process, 'pid_exists', lambda pid: False)
    monkeypatch.setattr(controller_module, 'running_projects',
                        lambda: [{'path': str(project.resolve()), 'pid': 222}])

    assert c._bridge_process_running(data['settings']) is True


def test_ue_restart_reuses_unfinished_request(tmp_path):
    job = fake_job(tmp_path, scene='')
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = tmp_path/'mailbox'
    request = c.mailbox/'request.json'
    atomic_json(request, {'id': 'interrupted', 'job': str(job.folder)})
    atomic_json(c.mailbox/'response.json', {'id': 'older', 'state': 'complete'})
    c._send_ue_request()
    assert read_json(request)['id'] == 'interrupted'
    atomic_json(c.mailbox/'response.json', {'id': 'interrupted', 'state': 'failed'})
    c._send_ue_request()
    assert read_json(request)['id'] != 'interrupted'


def test_gpu_page_fault_restarts_from_checkpoint(tmp_path, monkeypatch):
    project = tmp_path/'Crash.uproject'
    project.write_text('{}', encoding='utf-8')
    logs = tmp_path/'Saved'/'Logs'
    logs.mkdir(parents=True)
    (logs/'Crash.log').write_text('PageFault: PageFault at VA GPUAddress', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    atomic_json(job.path, data)
    job.status('ue_collision', 'saving')
    status = read_json(job.folder/'status.json')
    status['updated'] -= 20
    c = Controller(tmp_path/'home')
    c.job = job
    monkeypatch.setattr(c, '_bridge_process_running', lambda cfg: False)
    c._poll_ue_crash_recovery(status, data['settings'])
    assert read_json(job.folder/'status.json')['state'] == 'ue_restarting'
    assert c.ue_restart_attempts == 1
