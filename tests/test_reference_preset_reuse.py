import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.core import file_hash
from mmd_bridge.material_graph import signature


def test_generated_reference_preset_is_registered_and_reusable(tmp_path):
    source=Path('mmd_bridge/ue_reference.py').read_text(encoding='utf-8')
    definition=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='save_reference_preset')
    assets={'MRQ_Lumen':'unregistered previous preset'}
    ledger={}
    base=tmp_path/'base.uasset';base.write_bytes(b'source look')
    class Preset:
        def __init__(self,name):self.name=name;self.path=tmp_path/(name+'.uasset')
        def copy_from(self,value):self.value=value
    class Manager:
        def asset_file(self,asset):return base
        def create(self,name,*args):
            if name in assets:
                assert file_hash(assets[name].path)==ledger[name]
            else:assets[name]=Preset(name)
            return assets[name]
        def protect_asset(self,preset):ledger[preset.name]=file_hash(preset.path)
    def save(preset):preset.path.write_bytes(preset.value);return True
    native=SimpleNamespace(MoviePipelinePrimaryConfig=object,MoviePipelinePrimaryConfigFactory=lambda:None,
                           EditorAssetLibrary=SimpleNamespace(save_loaded_asset=save))
    namespace={'u':native,'signature':signature,'file_hash':file_hash}
    exec(compile(ast.Module(body=[definition],type_ignores=[]),'preset_fixture','exec'),namespace)
    callback=namespace['save_reference_preset']
    root=SimpleNamespace(get_path_name=lambda:'/Game/Run/MRQ_SourceLook')
    manager=Manager()
    first=callback(manager,root,b'configuration',{'frame':0},'lumen')
    second=callback(manager,root,b'configuration',{'frame':0},'lumen')
    assert first is second and first.name in ledger
    assert assets['MRQ_Lumen']=='unregistered previous preset'
    changed=callback(manager,root,b'new frame',{'frame':1},'lumen')
    assert changed is not first
    first.path.write_bytes(b'user edit')
    with pytest.raises(AssertionError):callback(manager,root,b'configuration',{'frame':0},'lumen')
