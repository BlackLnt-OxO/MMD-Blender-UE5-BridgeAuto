"""A cut must hold at samples introduced by 30 -> 60 fps, even with a 1x bake."""
from types import SimpleNamespace as N

import pytest

from mmd_bridge import ue_camera_cut_baker as cut


@pytest.mark.parametrize('leaks_half_frame', [False, True])
def test_verify_checks_the_added_sixty_fps_frame(monkeypatch, leaks_half_frame):
    plan = cut.plan_shots([2], 0, 4)

    class CutTrack:
        def get_sections(self):
            return [N(get_start_frame=lambda s=s: s.start,
                      get_end_frame=lambda s=s: s.end) for s in plan]

    monkeypatch.setattr(cut, 'unreal', N(MovieSceneCameraCutTrack=CutTrack))
    position = {'frame': 0, 'sub': 0.}

    def jump_to(frame, sub=0.):
        position.update(frame=frame, sub=sub)

    def world_state(actor):
        # Source frame 1.5 becomes output frame 3 at 60 fps. Quarter-frame
        # probes still see A, so they cannot catch the reported intermediate pose.
        leaked = (actor == 'A' and position['frame'] == 1
                  and position['sub'] >= .5 and leaks_half_frame)
        x = 50. if leaked else (0. if actor == 'A' else 100.)
        return {'loc': (x, 0., 0.), 'rot': (0., 0., 0.)}

    monkeypatch.setattr(cut, '_world_state', world_state)
    context = N(sequence=N(get_tracks=lambda: [CutTrack()]),
                cfg={'BAKE_SUBSAMPLES': 1, 'VERIFY_TOLERANCE_CM': 1e-4})
    records = [dict(shot=s.index, actor=actor, channels={}, lens_tracks={})
               for s, actor in zip(plan, ('A', 'B'))]
    samples = {s.index: cut.sample_times(s.start, s.end, 1) for s in plan}

    result = cut.verify(context, N(jump_to=jump_to), plan, records, samples)

    assert result['pass'] is not leaks_half_frame
    if leaks_half_frame:
        assert any('上一镜子帧 Hold' in failure for failure in result['failures'])


@pytest.mark.parametrize('can_open', [True, False])
def test_bake_preview_uses_cuts_and_releases_the_original_camera(monkeypatch, can_open):
    viewport = N(sequence='source sequence', pilot='original MMD camera', cuts=False)
    warnings = []
    baked = object()

    def open_sequence(sequence):
        if can_open:
            viewport.sequence = sequence
        return can_open

    editor = N(eject_pilot_level_actor=lambda: setattr(viewport, 'pilot', None))
    monkeypatch.setattr(cut, 'unreal', N(LevelEditorSubsystem=object(),
                                       get_editor_subsystem=lambda _: editor))
    monkeypatch.setattr(cut, 'log', lambda message: None)
    evaluator = N(lse=N(open_level_sequence=open_sequence,
                        set_lock_camera_cut_to_viewport=lambda enabled: setattr(viewport, 'cuts', enabled)))

    shown = cut.preview_baked_sequence(N(sequence=baked, warn=warnings.append), evaluator)

    assert shown is can_open
    if can_open:
        assert viewport.sequence is baked
        assert viewport.pilot is None
        assert viewport.cuts is True
        assert not warnings
    else:
        assert viewport.sequence == 'source sequence'
        assert viewport.pilot == 'original MMD camera'
        assert viewport.cuts is False
        assert warnings
