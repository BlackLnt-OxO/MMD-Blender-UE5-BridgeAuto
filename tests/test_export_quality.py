from types import SimpleNamespace as N

import pytest

from mmd_bridge.core import Settings, atomic_json
from mmd_bridge.export_quality import QualityMismatch
from mmd_bridge.mmd_avi import validate_mode1
from mmd_bridge.blender_validate import roundtrip_ready
from tests.test_vmd_reader import _bone_frame, _morph_frame, make_vmd


def test_readable_incomplete_vmd_continues_with_explicit_review(tmp_path, monkeypatch):
    from mmd_bridge import mmd_avi
    motion, lip, baked = [tmp_path/name for name in ('motion.vmd','face.vmd','baked.vmd')]
    motion.write_bytes(make_vmd(bone_frames=[_bone_frame('腕',0),_bone_frame('腕',5)]))
    lip.write_bytes(make_vmd(morph_frames=[_morph_frame('あ',5,1)]))
    baked.write_bytes(make_vmd(bone_frames=[_bone_frame('腕',0),_bone_frame('腕',4)]))
    monkeypatch.setattr(mmd_avi,'read_pmx',lambda _:N(bones=[N(name_jp='腕')],morph_names=['あ']))
    prepared=N(expected_bones=('髪',),motion=motion,lipsync=lip,model=tmp_path/'model.pmx')
    report=validate_mode1(baked,prepared,5,full=True,policy='review')
    assert report['quality_status']=='needs_review'
    assert len(report['quality_issues'])==3
    assert report['physics_tracks_found']==0
    with pytest.raises(QualityMismatch):
        validate_mode1(baked,prepared,5,full=True,policy='strict')


def test_review_policy_still_rejects_unreadable_vmd(tmp_path):
    path=tmp_path/'broken.vmd';path.write_bytes(b'not a VMD')
    with pytest.raises(ValueError,match='无效 VMD'):
        validate_mode1(path,N(),10,full=False,policy='review')


def test_review_receipt_can_advance_without_claiming_validation_passed(tmp_path):
    manifest={'files':{'model':{'sha256':'original'},'animation':{'sha256':'motion'}}}
    proof={'success':False,'usable':True,'validation_policy':'review',
           'quality_issues':['one morph missing'],'file_hashes':{'model':'original','animation':'motion'}}
    atomic_json(tmp_path/'blender_roundtrip.json',proof)
    assert roundtrip_ready(tmp_path,manifest)
    proof['validation_policy']='strict'
    atomic_json(tmp_path/'blender_roundtrip.json',proof)
    assert not roundtrip_ready(tmp_path,manifest)
    proof.update(validation_policy='review',file_hashes={'model':'changed','animation':'motion'})
    atomic_json(tmp_path/'blender_roundtrip.json',proof)
    assert not roundtrip_ready(tmp_path,manifest)


def test_new_settings_review_policy_is_explicit_and_serialized():
    from dataclasses import asdict
    assert asdict(Settings())['export_validation_policy']=='review'


@pytest.mark.parametrize('saved_policy,expected', [('review','review'),('strict','strict'),(None,'strict')])
def test_cached_mode1_revalidation_keeps_saved_policy(tmp_path,monkeypatch,saved_policy,expected):
    from mmd_bridge import mmd_avi
    from mmd_bridge.core import Job,file_hash
    job=Job(tmp_path/'job');cfg={}
    if saved_policy is not None:cfg['export_validation_policy']=saved_policy
    atomic_json(job.path,{'settings':cfg})
    baked=job.folder/'mmd_full/vmd/result.vmd';baked.parent.mkdir(parents=True)
    baked.write_bytes(b'identified cached native output')
    atomic_json(baked.parent.parent/'attempt.json',{'validated':True,'end_frame':10,
        'vmd':{'path':str(baked),'sha256':file_hash(baked)}})
    seen=[]
    def validate(path,prepared,end,*,full,policy):
        seen.append((path,end,full,policy))
        return {'quality_status':'needs_review','quality_issues':['content mismatch']}
    monkeypatch.setattr(mmd_avi,'validate_mode1',validate)
    monkeypatch.setattr(mmd_avi,'reusable_runtime',lambda *a: pytest.fail('must not rebake verified output'))
    path,report=mmd_avi._attempt(job,tmp_path/'runtime',N(),10,'full')
    assert path==baked and seen==[(baked,10,True,expected)]
    assert report['quality_issues']==['content mismatch']


def test_ui_roundtrips_policy_and_keeps_legacy_job_strict(tmp_path,monkeypatch):
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    app=W.QApplication.instance() or W.QApplication([])
    monkeypatch.setattr(Window,'scan',lambda _:None)
    window=Window()
    policies=[box for box in window.findChildren(W.QComboBox)
              if box.findData('review')>=0 and box.findData('strict')>=0]
    assert policies==[window.inputs['export_validation_policy']]
    assert window.settings().export_validation_policy=='review'
    window.apply_settings({'export_validation_policy':'strict'})
    assert window.settings().export_validation_policy=='strict'
    window.apply_settings({},existing_job=True)
    assert window.settings().export_validation_policy=='strict'
    window.apply_settings({'export_validation_policy':'review'})
    assert window.settings().export_validation_policy=='review'
    window.close()
