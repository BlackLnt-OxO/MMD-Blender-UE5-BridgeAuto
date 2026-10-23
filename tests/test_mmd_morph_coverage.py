from types import SimpleNamespace as N

import pytest

from mmd_bridge import mmd_avi as m
from tests.test_mmd_avi import _bone_frame, _morph_frame, make_vmd


@pytest.mark.parametrize('model_names, missing_is_error', [(['あ'],False), (['あ','ω'],True), ([],False)])
def test_missing_active_morph_is_fatal_only_if_model_supports_it(tmp_path, monkeypatch, model_names, missing_is_error):
    motion, lip, baked = [tmp_path/name for name in ('motion.vmd','face.vmd','baked.vmd')]
    bones=[_bone_frame('髪', frame) for frame in range(3)]
    motion.write_bytes(make_vmd(bone_frames=bones))
    lip.write_bytes(make_vmd(morph_frames=[_morph_frame('あ',1,.5),_morph_frame('ω',2,1)]))
    baked.write_bytes(make_vmd(bone_frames=bones,morph_frames=[_morph_frame('あ',1,.5)]))
    monkeypatch.setattr(m,'read_pmx',lambda _:N(bones=[N(name_jp='髪')],morph_names=model_names))
    prepared=N(expected_bones=('髪',),model=tmp_path/'model.pmx',motion=motion,lipsync=lip)
    if missing_is_error:
        with pytest.raises(ValueError,match='烘焙丢失口型轨道'):
            m.validate_mode1(baked,prepared,2,full=True)
    else:
        report=m.validate_mode1(baked,prepared,2,full=True)
        assert 'ω' in report['unmatched_lipsync_morphs']
        assert not report['lipsync_zero_tracks_omitted']
