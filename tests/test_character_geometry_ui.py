import copy
from dataclasses import asdict
from PySide6 import QtWidgets as W
from mmd_bridge.character_pbr import plan
from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
from mmd_bridge.character_geometry_roles import purpose
from mmd_bridge.core import Settings


def test_geometry_choices_are_separate_and_material_apply_does_not_rebuild_mesh():
    app=W.QApplication.instance() or W.QApplication([])
    record={'name':'Anonymous','pmx_index':3,'textures':{}}
    pbr=plan(record,{},'model',[])
    row={'name':'Anonymous','pmx_index':3,'pbr':pbr,'source_record':record,
         'geometry_purpose':purpose(record,{})}
    snapshot={'model':'not_present.pmx','sha256':'model','texture_count':0,'materials':[row]}
    other={'other-model':{'3':{'local':'include','host':2}}}
    calls=[]
    class Parent(W.QWidget):
        def __init__(self):
            super().__init__();self.log=W.QPlainTextEdit()
            self.controller=self
        def settings(self):return Settings()
        def execute(self,action,done):done(action())
        def material_surface_command(self,command,**payload):
            calls.append((command,payload));return {'count':1}
    parent=Parent()
    dialog=CharacterPBRDialog(snapshot,dict(asdict(Settings()),character_geometry_overrides=copy.deepcopy(other)),parent)
    assert '待确认' in dialog.slots.item(0).text()
    dialog.geometry_local.setCurrentIndex(dialog.geometry_local.findData('include'))
    dialog.store()
    assert dialog.geometry_overrides['other-model']==other['other-model']
    assert dialog.geometry_overrides['model']['3']['local']=='include'
    dialog.apply_live()
    assert calls[0][0]=='character_pbr_apply'
    assert set(calls[0][1]['settings'])=={'character_material_overrides','character_pbr_options'}
    restored=CharacterPBRDialog(snapshot,{'character_geometry_overrides':dialog.geometry_overrides})
    assert restored.geometry_local.currentData()=='include'
    restored.close();dialog.close();parent.close()


def test_candidate_control_roundtrip_does_not_reinterpret_legacy_tasks(monkeypatch,tmp_path):
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    from mmd_bridge.app import Window
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([]);window=Window()
    try:
        combo=window.inputs['character_material_profile'];combo.setCurrentIndex(combo.findData('character_pbr'))
        scope=window.inputs['character_geometry_mode']
        assert scope.count()==4
        assert scope.currentData()=='weld_weighted'
        assert 'character_subdivision_enabled' not in window.inputs
        assert 'character_geometry_candidate_scope' not in window.inputs
        scope.setCurrentIndex(scope.findData('local_subdivision'))
        assert window.settings().character_geometry_mode=='local_subdivision'
        assert scope.itemText(2).endswith('（约2倍顶点）')
        assert scope.itemText(3).endswith('（约5倍顶点）')
        saved=asdict(window.settings())
        scope.setCurrentIndex(0);window.apply_settings(saved,existing_job=True)
        assert scope.currentData()=='local_subdivision'
        legacy={'character_subdivision_enabled':True,'character_material_profile':'character_pbr'}
        window.apply_settings(legacy,existing_job=True)
        assert scope.currentIndex()==-1
        assert window.settings().character_geometry_mode is None
        assert window.settings().character_subdivision_enabled is True
        window.apply_settings(legacy,existing_job=False)
        import pytest
        with pytest.raises(ValueError,match='范围不同'):window.flow_settings('character')
        assert window.flow_settings('camera').model==''
        scope.setCurrentIndex(scope.findData('weld_weighted'))
        assert window.settings().character_geometry_mode=='weld_weighted'
    finally:window.timer.stop();window.close()


def test_geometry_review_dialog_shows_original_slots_and_preserves_choices():
    from mmd_bridge.character_geometry_dialog import GeometrySelectionDialog
    from PySide6 import QtCore as C
    app=W.QApplication.instance() or W.QApplication([])
    report={'models':[{'model':'one.pmx','sha256':'one','slots':[
        {'pmx_index':2,'name':'Body','type':'skin','selected':True,'pending':False,'role':'body','reasons':['verified'],'used':True},
        {'pmx_index':7,'name':'Unknown','type':'cloth','selected':False,'pending':True,'role':'unknown','reasons':['review'],'used':True}]}]}
    dialog=GeometrySelectionDialog(report)
    try:
        assert dialog.selections()=={'one':[2]}
        assert dialog.table.item(1,2).text()=='7'
        assert dialog.table.item(1,3).foreground().color().name()=='#d4a72c'
        dialog.table.item(1,0).setCheckState(C.Qt.CheckState.Checked)
        dialog.table.item(0,0).setCheckState(C.Qt.CheckState.Unchecked)
        assert dialog.selections()=={'one':[7]}
        dialog.reject()
        assert dialog.result()==W.QDialog.DialogCode.Rejected
    finally:dialog.close()
