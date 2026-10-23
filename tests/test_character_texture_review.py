import copy,sys
from types import SimpleNamespace
import pytest
from mmd_bridge.core import file_hash
from mmd_bridge import character_texture_review as review


def fixture(tmp_path,monkeypatch):
    model=tmp_path/'sample.pmx';model.write_bytes(b'original source')
    mat={'pmx_index':4,'name':'hidden','diffuse':[1,1,1,0],
         'texture_index':0,'sphere_index':1,'sphere_mode':0}
    monkeypatch.setattr('mmd_bridge.pmx_reader.read_pmx',lambda p:SimpleNamespace(materials=[mat],textures=['missing.png','unused.spa']))
    return model,mat,{'model':str(model),'character_material_profile':'generic'}


def test_missing_reference_keeps_hidden_slot_and_ignores_disabled_sphere(tmp_path,monkeypatch):
    model,mat,cfg=fixture(tmp_path,monkeypatch)
    missing=review.missing_sources(model,cfg)
    assert len(missing)==1 and missing[0]['pmx_index']==4 and missing[0]['alpha']==0
    assert missing[0]['reference']=='missing.png' and missing[0]['semantic']=='base_color'
    replacement=tmp_path/'chosen.png';replacement.write_bytes(b'explicit selected source')
    cfg['character_material_overrides']={file_hash(model):{'4':{'textures':{'base_color':{'path':str(replacement)}}}}}
    assert review.missing_sources(model,cfg)==[]
    before=copy.deepcopy(cfg);signature=review.binding_signature(cfg)
    review.check_checkpoint(cfg,{'character_texture_overrides_signature':signature})
    replacement.write_bytes(b'changed selected image')
    with pytest.raises(ValueError,match='新建任务'):review.check_checkpoint(cfg,{'character_texture_overrides_signature':signature})
    assert cfg==before


def test_explicit_mapping_changes_only_import_copy_and_preserves_source_identity(tmp_path,monkeypatch):
    model,mat,cfg=fixture(tmp_path,monkeypatch);original=file_hash(model)
    image=tmp_path/'chosen.png';image.write_bytes(b'actual chosen bytes')
    cfg['character_material_overrides']={file_hash(model):{'4':{'textures':{'base_color':{'path':str(image)}}}},
                                         'another-model':{'4':{'textures':{'sphere':{'path':'never-used.spa'}}}}}
    calls=[]
    class Fn:
        def __init__(self,material):self.material=material
        def create_texture(self,path):calls.append(('base_color',path))
        def create_sphere_texture(self,path):calls.append(('sphere',path))
    monkeypatch.setitem(sys.modules,'mmd_tools',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'mmd_tools.core',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'mmd_tools.core.material',SimpleNamespace(FnMaterial=Fn))
    actual=review.apply_source_bindings(object(),cfg,model,4)
    assert calls==[('base_color',str(image.resolve()))]
    assert actual['base_color']['sha256']==file_hash(image) and file_hash(model)==original
    assert review.source_bindings(cfg,model,5)=={}


def test_non_pbr_missing_dialog_preserves_other_slots_and_saved_parameters(tmp_path,monkeypatch):
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr import plan
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    model,mat,cfg=fixture(tmp_path,monkeypatch)
    row={'name':'hidden','pmx_index':4,'diffuse':[1,1,1,0],'pbr':plan({'name':'hidden','pmx_index':4},cfg,file_hash(model),[])}
    snapshot={'model':str(model),'sha256':file_hash(model),'texture_count':0,'materials':[row]}
    cfg['character_material_overrides']={'foreign':{'4':{'params':{'Roughness':.4}}},
        file_hash(model):{'4':{'params':{'SkinRosiness':.45}}}}
    app=W.QApplication.instance() or W.QApplication([])
    dialog=CharacterPBRDialog(snapshot,cfg,missing_textures=review.missing_sources(model,cfg))
    assert dialog.texture_only and dialog.slots.currentRow()==0
    image=tmp_path/'chosen.png';image.write_bytes(b'chosen')
    dialog.texture_fields['base_color'][0].setText(str(image));dialog.save_and_accept()
    assert dialog.result()==W.QDialog.DialogCode.Accepted and not dialog.continue_with_missing
    saved=dialog.overrides[file_hash(model)]['4']
    assert saved['params']['SkinRosiness']==.45 and saved['textures']['base_color']['path']==str(image)
    assert dialog.overrides['foreign']==cfg['character_material_overrides']['foreign']
    dialog.close()


def test_desktop_cancelled_missing_review_does_not_start_conversion(tmp_path,monkeypatch):
    from dataclasses import asdict
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    from mmd_bridge.core import Settings
    from mmd_bridge.character_pbr import plan
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    model,mat,cfg=fixture(tmp_path,monkeypatch)
    row={'name':'hidden','pmx_index':4,'pbr':plan({'name':'hidden','pmx_index':4},cfg,file_hash(model),[])}
    snapshot={'model':str(model),'sha256':file_hash(model),'texture_count':0,'materials':[row]}
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    monkeypatch.setattr(Window,'error',lambda self,exc:(_ for _ in ()).throw(exc))
    monkeypatch.setattr(Settings,'validate',lambda self:{})
    monkeypatch.setattr('mmd_bridge.character_pbr.inspect_model',lambda *a:snapshot)
    monkeypatch.setattr(CharacterPBRDialog,'exec',lambda self:W.QDialog.DialogCode.Rejected)
    app=W.QApplication.instance() or W.QApplication([]);window=Window();window.timer.stop()
    selected=Settings(model=str(model),character_material_profile='generic',character_backend='mmdbridge_fbx')
    monkeypatch.setattr(window,'flow_settings',lambda mode:selected)
    calls=[];monkeypatch.setattr(window.controller,'start',lambda s:calls.append(asdict(s)))
    window.start('character')
    assert calls==[] and '尚未创建' in window.log.toPlainText()
    window.close()
