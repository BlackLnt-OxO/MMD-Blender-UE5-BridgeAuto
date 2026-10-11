import time
import pytest
from mmd_bridge.core import Job, atomic_json, read_json, resource_root, file_hash
from mmd_bridge.controller import Controller, UE_CODE_MODULES, connection_folder
import mmd_bridge.controller as module


def connected(tmp_path, monkeypatch):
    controller = Controller(tmp_path/'home'); controller.job = Job(tmp_path/'job')
    project = tmp_path/'Project.uproject'; project.write_text('{}')
    atomic_json(controller.job.path, {'settings': {'project': str(project)}})
    box = connection_folder(controller.home, project)
    atomic_json(project.parent/'Plugins/MMDBridge/Content/Python/bridge_config.json',
                {'project': str(project), 'mailbox': str(box.resolve())})
    heartbeat = {'pid': 111, 'project': str(project), 'time': time.time(),
                 'code_hashes': {n: file_hash(resource_root()/'mmd_bridge'/n) for n in UE_CODE_MODULES}}
    atomic_json(box/'heartbeat.json', heartbeat)
    monkeypatch.setattr(module.process, 'pid_exists', lambda pid: pid == 111)
    def respond(path, value):
        atomic_json(path, value)
        if path.name == 'material_surface_request.json':
            atomic_json(box/'material_surface_response.json', {'id': value['id'], 'state': 'complete', 'result': {'targets': []}})
    monkeypatch.setattr(module, 'atomic_json', respond)
    return controller, box, heartbeat


def test_current_process_request_waits_and_does_not_replace_pending_command(tmp_path, monkeypatch):
    controller, box, _ = connected(tmp_path, monkeypatch)
    previous = {'id': 'pending', 'ue_pid': 111}
    atomic_json(box/'material_surface_request.json', previous)
    atomic_json(box/'material_surface_response.json', {'id': 'pending', 'state': 'running'})
    with pytest.raises(ValueError, match='仍在处理'): controller.material_surface_command('selection')
    assert read_json(box/'material_surface_request.json') == previous


def test_restarted_editor_can_read_selection_and_recover_a_pending_batch(tmp_path, monkeypatch):
    controller, box, _ = connected(tmp_path, monkeypatch)
    atomic_json(box/'material_surface_request.json', {'id': 'old', 'ue_pid': 222})
    atomic_json(box/'material_surface_response.json', {'id': 'old', 'state': 'running'})
    assert controller.material_surface_command('selection') == {'targets': []}
    assert read_json(box/'material_surface_request.json')['ue_pid'] == 111


def test_live_selection_without_current_job_uses_the_connected_project(tmp_path, monkeypatch):
    controller, box, _ = connected(tmp_path, monkeypatch)
    controller.job = None
    assert controller.inspect_selected_materials('unrelated.uproject') == {'targets': []}
    sent = read_json(box/'material_surface_request.json')
    assert sent['action'] == 'inspect' and sent['job'] == ''


def test_switch_uses_matched_job_without_changing_main_task(tmp_path, monkeypatch):
    controller, box, _ = connected(tmp_path, monkeypatch)
    selected = controller.job
    matched = Job(tmp_path/'matched')
    atomic_json(matched.path, selected.data)
    batch = matched.folder/'MaterialSurfaces/S_Matched'; batch.mkdir(parents=True)
    commands = []
    def command(action, **kwargs):
        commands.append((action, kwargs))
        return {'folder': str(batch)}
    monkeypatch.setattr(controller, 'material_surface_command', command)
    controller.switch_selected_materials([{'job': str(matched.folder), 'binding': 'native'}], 'masked')
    assert controller.job is selected
    assert [action for action, _ in commands] == ['build', 'commit']
    assert all(args['job_folder'] == matched.folder for _, args in commands)
    assert read_json(controller.home/'material_surface_last.json')['job'] == str(matched.folder)


@pytest.mark.parametrize('changed, reason', [('project', '不属于'), ('code_hashes', '旧桥接')])
def test_wrong_project_or_old_code_never_sends_a_request(tmp_path, monkeypatch, changed, reason):
    controller, box, heartbeat = connected(tmp_path, monkeypatch)
    heartbeat[changed] = 'other.uproject' if changed == 'project' else {}
    atomic_json(box/'heartbeat.json', heartbeat)
    with pytest.raises(ValueError, match=reason): controller.material_surface_command('selection')
    assert not (box/'material_surface_request.json').exists()
