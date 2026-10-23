import pytest
from mmd_bridge.character_geometry import geometry_policy,check_geometry_checkpoint
from mmd_bridge.effective_inputs import write_effective_inputs


def test_prepared_pmx_checkpoint_uses_effective_identity_and_rejects_changes(tmp_path):
    source=tmp_path/'original.pmx';source.write_bytes(b'original')
    prepared=tmp_path/'job/input/model.pmx';prepared.parent.mkdir(parents=True);prepared.write_bytes(b'prepared-display-frames')
    cfg={'model':str(source),'character_backend':'mmdbridge_fbx','character_material_profile':'character_pbr',
         'character_geometry_mode':'weld_weighted'}
    write_effective_inputs(tmp_path/'job',source_model=str(source),effective_model=str(prepared))
    saved={'geometry_policy':geometry_policy(dict(cfg,model=str(prepared)))}
    check_geometry_checkpoint(saved,cfg,job_folder=tmp_path/'job')
    prepared.write_bytes(b'changed')
    with pytest.raises(ValueError,match='处理版本'):
        check_geometry_checkpoint(saved,cfg,job_folder=tmp_path/'job')
