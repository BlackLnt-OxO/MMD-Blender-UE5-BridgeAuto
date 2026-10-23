import copy
import pytest
from mmd_bridge.core import atomic_json,file_hash
from mmd_bridge.effective_inputs import write_effective_inputs
from mmd_bridge.character_geometry import geometry_input_settings,geometry_policy,check_geometry_checkpoint


def fixture(tmp_path,version):
    source=tmp_path/'source.pmx';source.write_bytes(b'original-model')
    job=tmp_path/'job';prepared=job/'input/model.pmx';prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b'display-frame-repaired-model')
    digest=file_hash(source)
    cfg={'model':str(source),'character_backend':'mmdbridge_fbx','character_material_profile':'character_pbr',
         'character_geometry_mode':'local_subdivision',
         'character_geometry_overrides':{digest:{'0':{'local':'include'}}},
         'character_material_overrides':{digest:{'0':{'type':'face'}}}}
    atomic_json(job/'job.json',{'geometry_input_identity_version':version,'settings':cfg})
    write_effective_inputs(job,source_model=str(source),effective_model=str(prepared))
    return job,source,prepared,cfg


def test_original_model_choices_reach_prepared_geometry_without_mutating_settings(tmp_path):
    job,source,prepared,cfg=fixture(tmp_path,1);before=copy.deepcopy(cfg)
    resolved=geometry_input_settings(cfg,job);policy=geometry_policy(resolved)
    assert resolved['model']==str(prepared)
    assert policy['version']==5
    assert policy['source_model_sha256']==file_hash(prepared)
    assert policy['selection_model_sha256']==file_hash(source)
    assert policy['candidate']['overrides']=={'0':{'local':'include'}}
    assert resolved['character_material_overrides'][file_hash(prepared)]=={'0':{'type':'face'}}
    assert cfg==before
    check_geometry_checkpoint({'geometry_policy':policy},cfg,job_folder=job)
    prepared.write_bytes(b'changed-prepared-model')
    with pytest.raises(ValueError,match='处理版本'):
        check_geometry_checkpoint({'geometry_policy':policy},cfg,job_folder=job)


def test_historical_jobs_keep_their_original_policy_and_require_new_task_for_migration(tmp_path):
    job,source,prepared,cfg=fixture(tmp_path,0)
    old=geometry_policy(dict(cfg,model=str(prepared)))
    assert old['version']==4 and not old['candidate']['overrides']
    check_geometry_checkpoint({'geometry_policy':old},cfg,job_folder=job)
    atomic_json(job/'job.json',{'geometry_input_identity_version':1,'settings':cfg})
    with pytest.raises(ValueError,match='处理版本'):
        check_geometry_checkpoint({'geometry_policy':old},cfg,job_folder=job)


def test_unrelated_source_override_cannot_leak_through_a_prepared_hash(tmp_path):
    job,source,prepared,cfg=fixture(tmp_path,1)
    cfg['character_geometry_overrides']={file_hash(prepared):{'8':{'local':'include'}}}
    resolved=geometry_input_settings(cfg,job)
    assert geometry_policy(resolved)['candidate']['overrides']=={}
