from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.controller import Controller
from tests.test_controller import fake_job


def test_unattended_editor_launch_keeps_project_first_and_avoids_modal_startup(tmp_path,monkeypatch):
    import mmd_bridge.controller as m
    project=tmp_path/'Stage.uproject';project.write_text('{}')
    job=fake_job(tmp_path,scene='')
    data=read_json(job.path)
    data['settings'].update(unreal=str(tmp_path/'UnrealEditor.exe'),project=str(project),unattended=True)
    atomic_json(job.path,data)
    c=Controller(tmp_path/'home');c.job=job;c.mailbox=tmp_path/'box'
    monkeypatch.setattr(c,'connect',lambda _:None)
    monkeypatch.setattr(m,'running_projects',lambda:[])
    monkeypatch.setattr(m,'install_ue_bridge',lambda *a,**k:None)
    commands=[]
    monkeypatch.setattr(m.process,'popen',lambda command,**k:commands.append(command))
    c.launch_ue()
    assert commands[0][1]==str(project)
    assert '-unattended' in commands[0][2:]
