from mmd_bridge.vmd_reader import parse_vmd, read_vmd
from mmd_bridge.vmd_writer import merge_missing_morphs, serialize_vmd, write_vmd
from tests.test_vmd_reader import (
    _bone_frame, _camera_frame, _ik_frame, _light_frame,
    _morph_frame, _shadow_frame, make_vmd,
)


def test_complete_vmd_round_trip_preserves_every_section(tmp_path):
    original = make_vmd(bone_frames=[_bone_frame("センター", 2)],
                        morph_frames=[_morph_frame("笑い", 3, 0.5)],
                        camera_frames=[_camera_frame(4)],
                        light_frames=[_light_frame(5)],
                        shadow_frames=[_shadow_frame(6)],
                        ik_frames=[_ik_frame(7, True, [("足ＩＫ", 2)])])
    document = parse_vmd(original)
    assert serialize_vmd(document) == original
    path = write_vmd(document, tmp_path / "out.vmd")
    assert path.read_bytes() == original
    assert read_vmd(path).summary() == document.summary() | {"path": str(path)}


def test_merge_morphs_lipsync_overrides_main_same_key(tmp_path):
    baked = tmp_path / "baked.vmd"
    main = tmp_path / "main.vmd"
    lip = tmp_path / "lip.vmd"
    baked.write_bytes(make_vmd(bone_frames=[_bone_frame("センター", 0)]))
    main.write_bytes(make_vmd(morph_frames=[_morph_frame("笑い", 3, 0.2)]))
    lip.write_bytes(make_vmd(morph_frames=[_morph_frame("笑い", 3, 0.8)]))
    output = merge_missing_morphs(baked, main, lip, tmp_path / "final.vmd")
    frames = read_vmd(output).morph_frames
    assert len(frames) == 1
    assert abs(frames[0].weight - 0.8) < 1e-6
