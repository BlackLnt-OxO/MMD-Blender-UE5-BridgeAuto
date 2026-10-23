from mmd_bridge.controller import Controller
from mmd_bridge.core import Settings


def test_new_scene_task_preserves_explicit_content_namespace(tmp_path, monkeypatch):
    import mmd_bridge.controller as m
    scene=tmp_path/'stage.blend';scene.write_bytes(b'fixture')
    monkeypatch.setattr(m,'check_upstream_installation',lambda _:None)
    monkeypatch.setattr(Settings,'validate',lambda _:None)
    c=Controller(tmp_path/'home')
    monkeypatch.setattr(c,'launch_blender',lambda _:None)
    job=c.start(Settings(flow='scene',scene=str(scene),character_backend='fbx_legacy',
                         character_output_dir=str(tmp_path/'jobs'),content_root='/Game/Acceptance/Stage'))
    assert job.data['settings']['content_root']=='/Game/Acceptance/Stage'
