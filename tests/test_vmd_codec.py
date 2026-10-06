from mmd_bridge.vmd_codec import rewrite_bone_names
from mmd_bridge.vmd_reader import read_vmd
from tests.test_vmd_reader import _bone_frame, _camera_frame, _morph_frame, make_vmd


def test_rewrite_bone_names_preserves_all_other_vmd_bytes(tmp_path):
    source, target = tmp_path / "source.vmd", tmp_path / "fixed.vmd"
    original = make_vmd(bone_frames=[_bone_frame("旧骨", 2)],
                        morph_frames=[_morph_frame("笑い", 3, 0.5)],
                        camera_frames=[_camera_frame(4)])
    source.write_bytes(original)
    rewrite_bone_names(source, target, {"旧骨": "新骨"})
    changed = target.read_bytes()
    assert changed[:54] == original[:54]
    assert changed[69:] == original[69:]
    assert read_vmd(target).bone_frames[0].name == "新骨"
    assert read_vmd(target).camera_frames == read_vmd(source).camera_frames
