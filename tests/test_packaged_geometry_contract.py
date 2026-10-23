import subprocess,sys
from mmd_bridge.controller import install_ue_bridge


def test_deployed_bridge_can_validate_geometry_without_source_checkout(tmp_path):
    project=tmp_path/'project/Test.uproject';project.parent.mkdir();project.write_text('{}')
    install_ue_bridge(project,tmp_path/'home')
    python=project.parent/'Plugins/MMDBridge/Content/Python'
    model=tmp_path/'fixture.pmx';model.write_bytes(b'identity fixture')
    code=(f'import sys;sys.path.insert(0,{str(python)!r});'
          'from mmd_bridge.character_geometry import geometry_policy;'
          f'cfg=dict(model={str(model)!r},character_backend="mmdbridge_fbx",'
          'character_material_profile="character_pbr",character_geometry_mode="weld_weighted");'
          'assert geometry_policy(cfg)["mode"]=="weld_weighted";'
          'from mmd_bridge.effective_inputs import resolve_character_inputs;'
          'from mmd_bridge.character_texture_review import check_checkpoint;'
          'check_checkpoint(cfg,{})')
    result=subprocess.run([sys.executable,'-I','-c',code],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
