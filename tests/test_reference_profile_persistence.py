from mmd_bridge.core import Job,atomic_json
from mmd_bridge.controller import Controller


def test_reference_renderer_choice_is_saved_before_native_launch(tmp_path,monkeypatch):
    scene=tmp_path/'stage.blend';scene.touch()
    blender=tmp_path/'blender.exe';blender.touch()
    job=Job(tmp_path/'job')
    atomic_json(job.path,{'settings':{'scene':str(scene),'blender':str(blender),
        'ue_render_memory_profile':'balanced'}})
    atomic_json(job.folder/'manifest.json',{'scene':{'route':'usd_stage',
        'reference_camera':{'name':'SourceCamera'}}})
    c=Controller(tmp_path/'home');c.job=job
    launched=[]
    monkeypatch.setattr(c,'launch_blender',lambda mode:launched.append((mode,job.data['settings']['ue_render_memory_profile'])))
    c.render_reference('blender',memory_profile='low')
    assert launched==[('reference','low')]
    assert job.data['settings']['ue_render_memory_profile']=='low'
