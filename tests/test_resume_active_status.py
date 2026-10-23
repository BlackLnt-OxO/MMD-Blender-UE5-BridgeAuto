from dataclasses import asdict
import pytest
from mmd_bridge.core import Settings,Job,atomic_json,read_json
from mmd_bridge.controller import Controller


@pytest.mark.parametrize('flow,expected',[('character','preparing'),('camera','camera_export')])
def test_resume_publishes_active_state_before_async_blender_start(tmp_path,monkeypatch,flow,expected):
    from mmd_bridge import controller as module
    monkeypatch.setattr(module,'ensure_blender_job_stopped',lambda _:None)
    cfg=Settings(flow=flow,character_backend='mmdbridge_fbx' if flow=='character' else 'fbx_legacy',
        model='source.pmx' if flow=='character' else '',motion='body.vmd' if flow=='character' else '',
        camera='camera.vmd' if flow=='camera' else '')
    job=Job(tmp_path/'job');atomic_json(job.path,{'settings':asdict(cfg),'inputs':{}})
    if flow=='character':atomic_json(job.folder/'effective.json',{'effective_model':'prepared.pmx','effective_motion':'baked.vmd'})
    job.status('stopped','old terminal state')
    c=Controller(tmp_path/'home');states=[]
    monkeypatch.setattr(c,'launch_blender',lambda mode:states.append((mode,read_json(job.folder/'status.json')['state'])))
    c.resume(job.folder)
    assert states==[(flow,expected)]
    assert c.poll()['state']==expected
