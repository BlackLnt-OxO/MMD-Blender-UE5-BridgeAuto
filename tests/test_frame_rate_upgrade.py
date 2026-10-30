from types import SimpleNamespace as N

import pytest

from mmd_bridge import ue_frame_rate_upgrade as upgrade


def test_thirty_to_sixty_and_already_sixty_use_identical_tick_times():
    a = upgrade.jump_plan([3588], 30, 24000)
    b = upgrade.jump_plan([7176], 60, 24000)
    for plan in (a, b):
        row = plan[0]
        assert row['prior_tick'] == 7174 * 400
        assert row['delayed_tick'] == 7175 * 400
        assert row['cut_tick'] == 7176 * 400
        assert row['target_prior_frame'] == 7174
        assert row['target_delayed_frame'] == 7175
        assert row['target_cut_frame'] == 7176


def test_every_confirmed_cut_moves_only_existing_preceding_keys():
    jumps = upgrade.jump_plan([120, 244, 3588], 30, 24000)
    existing = [jumps[0]['prior_tick'], jumps[0]['cut_tick'], jumps[2]['prior_tick'], jumps[2]['cut_tick']]
    assert upgrade.plan_key_moves(existing, jumps) == [
        (jumps[0]['prior_tick'], jumps[0]['delayed_tick']),
        (jumps[2]['prior_tick'], jumps[2]['delayed_tick'])]
    assert upgrade.plan_key_moves([], jumps) == []
    assert existing == [jumps[0]['prior_tick'], jumps[0]['cut_tick'], jumps[2]['prior_tick'], jumps[2]['cut_tick']]


def test_key_at_new_camera_frame_is_never_delayed():
    jumps = upgrade.jump_plan([7176], 60, 24000)
    assert upgrade.plan_key_moves([7176 * 400], jumps) == []


def test_destination_collision_stops_instead_of_overwriting():
    jumps = upgrade.jump_plan([7176], 60, 24000)
    with pytest.raises(ValueError, match='已有关键帧'):
        upgrade.plan_key_moves([7174 * 400, 7175 * 400], jumps)


def test_unrepresentable_tick_time_is_rejected():
    with pytest.raises(ValueError, match='精确表示'):
        upgrade.jump_plan([120], 30, 100, target_fps=60)


class Key:
    def __init__(self, tick): self.tick = tick
    def get_time(self, **kwargs): return N(frame_number=N(value=self.tick), sub_frame=0)


class Channel:
    def __init__(self, ticks): self.keys = [Key(t) for t in ticks]
    def get_keys(self): return self.keys
    def get_name(self): return 'channel'


class Section:
    def __init__(self, channel, blend='Absolute'): self.channel, self.blend = channel, blend
    def get_all_channels(self): return [self.channel]
    def get_blend_type(self): return N(export_text=lambda: self.blend)


class Track:
    def __init__(self, section): self.section = section
    def get_sections(self): return [self.section]
    def get_display_name(self): return 'track'


class TransformTrack(Track): pass
class FloatTrack(Track): pass


class Binding:
    def __init__(self, name, tracks, parent=None): self.name, self.tracks, self.parent = name, tracks, parent
    def get_tracks(self): return self.tracks
    def get_name(self): return self.name
    def get_id(self): return N(export_text=lambda: self.name)
    def get_parent(self): return self.parent
    def is_valid(self): return True


@pytest.mark.parametrize('selected,expected', [('camera_b', 'camera_b'), ('lens_b', 'camera_b'),
                                              ('missing_camera', None)])
def test_camera_cut_binding_selects_actor_or_component_and_never_guesses(monkeypatch, selected, expected):
    class Camera: pass
    class CutTrack:
        def get_sections(self):
            return [N(get_camera_binding_id=lambda: N(get_editor_property=lambda name: N(export_text=lambda: selected)))]
    first = Binding('camera_a', [TransformTrack(Section(Channel([])))])
    second = Binding('camera_b', [TransformTrack(Section(Channel([])))])
    lens = Binding('lens_b', [], second)
    # A broken cut must fail even if only one other animated camera exists.
    bindings = [first] if expected is None else [first, second, lens]
    seq = N(get_tracks=lambda: [CutTrack()], get_bindings=lambda: bindings,
            locate_bound_objects=lambda b, world: [] if b is lens else [Camera()])
    monkeypatch.setattr(upgrade, 'u', N(MovieSceneCameraCutTrack=CutTrack,
        MovieScene3DTransformTrack=TransformTrack, CameraActor=Camera))
    if expected is None:
        with pytest.raises(ValueError, match='不会猜选'):
            upgrade.camera_bindings(seq, None)
    else:
        root, group = upgrade.camera_bindings(seq, None)
        assert root is second and group == [second, lens]


def test_native_edit_plan_preserves_add_and_does_not_create_missing_object_keys(monkeypatch):
    native = N(MovieScene3DTransformTrack=TransformTrack, MovieSceneFloatTrack=FloatTrack,
               MovieSceneTimeUnit=N(TICK_RESOLUTION=object()))
    monkeypatch.setattr(upgrade, 'u', native)
    base = Channel([0, 238*400, 240*400, 486*400, 488*400])
    additive = Channel([238*400, 240*400])
    other = Channel([238*400, 240*400, 488*400])
    camera = Binding('camera', [TransformTrack(Section(base)), TransformTrack(Section(additive, 'Additive'))])
    object_binding = Binding('light', [Track(Section(other))])
    seq = N(get_bindings=lambda: [camera, object_binding], get_display_rate=lambda: 60, get_tick_resolution=lambda: 24000)
    monkeypatch.setattr(upgrade, 'camera_bindings', lambda *args: (camera, [camera]))
    jumps, holds, moves, skipped = upgrade._actions(seq, None, [240, 488], 30, 60)
    assert [key.tick for key, label, row in holds] == [238*400, 486*400]
    assert [(old, new) for key, label, old, new in moves] == [(238*400, 239*400)]
    assert [k.tick for k in base.keys] == [0, 238*400, 240*400, 486*400, 488*400]
    assert [k.tick for k in additive.keys] == [238*400, 240*400]
    assert not skipped


def test_frame_open_configuration_retains_sampling_settings(monkeypatch):
    camera, output, aa = object(), object(), object()
    values = {camera: {}, output: {}, aa: {'temporal': 4, 'spatial': 4}}
    config = N(find_or_add_setting_by_class=lambda cls: N(set_editor_property=lambda k, v: values[cls].__setitem__(k, v)))
    monkeypatch.setattr(upgrade, 'u', N(MoviePipelineCameraSetting=camera, MoviePipelineOutputSetting=output,
        MoviePipelineShutterTiming=N(FRAME_OPEN='open'), FrameRate=lambda n, d: (n, d)))
    upgrade.set_frame_open(config, 60)
    assert values[camera] == {'shutter_timing': 'open'}
    assert values[output] == {'use_custom_frame_rate': True, 'output_frame_rate': (60, 1)}
    assert values[aa] == {'temporal': 4, 'spatial': 4}
