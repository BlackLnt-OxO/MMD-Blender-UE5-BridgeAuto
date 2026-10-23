from types import SimpleNamespace as N

import pytest

from mmd_bridge.mmd_avi import validate_mode1
from tests.test_vmd_reader import _bone_frame, make_vmd


@pytest.mark.parametrize('frames, expected', [
    ([(0, 0), (1, 2), (2, 2), (5, 2)], {'髪': [[2, 5]]}),
    ([(0, 0), (1, 2), (5, 3)], '姿态变化'),
    ([(0, 0), (1, 2), (1, 2), (5, 2)], '重复帧'),
    ([(0, 0), (1, 2), (4, 2)], '范围'),
])
def test_native_compression_allows_only_constant_covered_intervals(tmp_path, frames, expected):
    path=tmp_path/'baked.vmd'
    path.write_bytes(make_vmd(bone_frames=[_bone_frame('髪',f,(x,0,0)) for f,x in frames]))
    prepared=N(expected_bones=('髪',),motion=path,lipsync=None)
    if isinstance(expected,str):
        with pytest.raises(ValueError,match=expected):
            validate_mode1(path,prepared,5,full=False)
    else:
        before=path.read_bytes()
        report=validate_mode1(path,prepared,5,full=False)
        assert report['constant_physics_intervals']==expected
        assert path.read_bytes()==before


def test_track_endpoint_required_even_when_other_track_has_final_frame(tmp_path):
    path=tmp_path/'baked.vmd'
    path.write_bytes(make_vmd(bone_frames=[_bone_frame('髪',0),_bone_frame('腕',5)]))
    with pytest.raises(ValueError,match='范围端点'):
        validate_mode1(path,N(expected_bones=('髪',),motion=path,lipsync=None),5,full=False)
