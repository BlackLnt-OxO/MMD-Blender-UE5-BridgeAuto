import time

import mmd_bridge.controller as controller_module
from mmd_bridge.core import Job,atomic_json,read_json,stable_id
from mmd_bridge.controller import Controller, bridge_status, install_ue_bridge


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


def test_camera_cut_session_is_standalone_and_waits_for_ue(tmp_path):
    project = tmp_path/'Camera.uproject'
    project.write_text('{}', encoding='utf-8')
    c = Controller(tmp_path/'home')
    session = c.start_camera_cut(project)
    assert c.job is None
    assert read_json(session/'status.json')['state'] == 'camera_cut_waiting_ue'

    mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    status = c.poll_camera_cut()
    request = read_json(mailbox/'camera_cut_request.json')
    assert request['mode'] == 'probe'
    assert status['state'] == 'camera_cut_queued'


def test_bridge_status_has_three_states(tmp_path):
    project = tmp_path/'Bridge.uproject'
    project.write_text('{}', encoding='utf-8')
    home = tmp_path/'home'
    assert bridge_status(project, home)['state'] == 'not_installed'

    install_ue_bridge(project, home)
    assert bridge_status(project, home)['state'] == 'installed_offline'

    mailbox = home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    assert bridge_status(project, home)['state'] == 'connected'


def test_launch_ue_does_not_duplicate_an_open_project(tmp_path, monkeypatch):
    project = tmp_path/'AlreadyOpen.uproject'
    project.write_text('{}', encoding='utf-8')
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
    assert status['state'] == 'awaiting_ue'
    assert '不会重复启动' in status['message']


def test_low_vram_wait_auto_requeues_after_memory_recovers(tmp_path, monkeypatch):
    project = tmp_path/'Resume.uproject'
    project.write_text('{}', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    data['settings']['unreal'] = str(tmp_path/'UnrealEditor.exe')
    data['settings']['ue_vram_pause_percent'] = 90
    data['settings']['ue_vram_resume_percent'] = 80
    atomic_json(job.path, data)
    job.status('ue_wait_vram', 'paused', retry_after=0, pause_percent=90, resume_percent=80)
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(c.mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    c.ue_started = c.ue_requested = True
    monkeypatch.setattr(controller_module.process, 'gpu_memory_info', lambda: {
        'index': 0, 'name': 'GPU', 'total_mb': 8192, 'used_mb': 6500, 'free_mb': 1692})

    c.poll()

    assert read_json(c.mailbox/'request.json')['job'] == str(job.folder)
    assert read_json(job.folder/'status.json')['state'] == 'awaiting_ue'


def test_low_vram_wait_does_not_resume_above_low_watermark(tmp_path, monkeypatch):
    project = tmp_path/'StillHot.uproject'
    project.write_text('{}', encoding='utf-8')
    job = fake_job(tmp_path, scene='')
    data = read_json(job.path)
    data['settings']['project'] = str(project)
    data['settings']['unreal'] = str(tmp_path/'UnrealEditor.exe')
    data['settings']['ue_vram_pause_percent'] = 90
    data['settings']['ue_vram_resume_percent'] = 80
    atomic_json(job.path, data)
    job.status('ue_wait_vram', 'paused', retry_after=0, pause_percent=90, resume_percent=80)
    c = Controller(tmp_path/'home')
    c.job = job
    c.mailbox = c.home/'connections'/stable_id(str(project.resolve()).casefold())
    atomic_json(c.mailbox/'heartbeat.json', {
        'project': str(project.resolve()), 'pid': 1, 'time': time.time(), 'engine': '5.7.4'})
    c.ue_started = c.ue_requested = True
    monkeypatch.setattr(controller_module.process, 'gpu_memory_info', lambda: {
        'index': 0, 'name': 'GPU', 'total_mb': 8192, 'used_mb': 6963, 'free_mb': 1229})

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
