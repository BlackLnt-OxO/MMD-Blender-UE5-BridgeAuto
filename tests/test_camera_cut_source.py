from types import SimpleNamespace as N
import pytest
from mmd_bridge import ue_camera_cut_baker as cut


class Camera:
    tags=[]
    def __init__(self,name):self.name=name
    def get_actor_label(self):return self.name
    def get_name(self):return self.name
    def get_path_name(self):return '/World/'+self.name


def context(monkeypatch,names):
    class Transform:pass
    native=N(MovieScene3DTransformTrack=Transform,CameraActor=Camera)
    monkeypatch.setattr(cut,'unreal',native)
    actors=[Camera(name) for name in names]
    bindings=[N(get_name=lambda a=a:a.name,get_tracks=lambda:[Transform()]) for a in actors]
    sequence=N(get_bindings=lambda:bindings,get_binding_id=lambda b:bindings.index(b))
    lse=N(get_bound_objects=lambda i:[actors[i]])
    ctx=N(sequence=sequence,notes={},cfg={'GENERATED_PREFIX':'AUTO_Cam_Shot_', 'GENERATED_TAG':'Generated'},
        evaluator=N(lse=lse),warn=lambda message:None)
    monkeypatch.setattr(cut,'all_level_actors',lambda:[*actors,Camera('Unbound editor camera')])
    monkeypatch.setattr(cut,'_camera_component',lambda actor:object() if isinstance(actor,Camera) else None)
    return ctx,bindings,actors


def test_camera_cut_uses_unique_bound_animated_camera_without_name_assumptions(monkeypatch):
    ctx,bindings,actors=context(monkeypatch,['Any imported camera'])
    assert cut.resolve_source_camera(ctx,'MMD_Camera')==(bindings[0],actors[0])
    assert ctx.notes['source_resolved_by']=='unique_animated_camera'


def test_camera_cut_does_not_guess_between_multiple_animated_sources(monkeypatch):
    ctx,_,_=context(monkeypatch,['A','B'])
    with pytest.raises(cut.ConfigError,match='多个动画相机'):
        cut.resolve_source_camera(ctx,'MMD_Camera')


def test_generated_shot_cameras_do_not_confuse_source_on_verify(monkeypatch):
    ctx,bindings,actors=context(monkeypatch,['Original','AUTO_Cam_Shot_001','AUTO_Cam_Shot_002'])
    assert cut.resolve_source_camera(ctx,'MMD_Camera')==(bindings[0],actors[0])
