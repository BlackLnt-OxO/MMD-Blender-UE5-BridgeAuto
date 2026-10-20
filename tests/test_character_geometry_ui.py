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
        scope=window.inputs['character_geometry_candidate_scope']
        assert scope.currentData()==''
        hair=window.inputs['character_geometry_hair_continuity']
        assert not hair.isChecked() and not hair.isEnabled()
        scope.setCurrentIndex(scope.findData('local'))
        assert window.settings().character_geometry_candidate_scope=='local'
        assert not window.inputs['character_subdivision_enabled'].isEnabled()
        assert hair.isEnabled();hair.setChecked(True)
        normal=window.inputs['character_geometry_hair_normal_mode'];normal.setCurrentIndex(normal.findData('cage'))
        window.inputs['character_geometry_hair_seams'].setChecked(True)
        assert normal.isEnabled() and window.settings().character_geometry_hair_normal_mode=='cage'
        assert window.settings().character_geometry_hair_continuity is True
        saved=asdict(window.settings())
        scope.setCurrentIndex(0);window.apply_settings(saved,existing_job=True)
        assert scope.currentData()=='local'
        assert hair.isChecked()
        assert window.settings().character_geometry_hair_normal_mode=='cage'
        assert window.settings().character_geometry_hair_seams is True
        window.apply_settings({'character_geometry_hair_continuity':True,
                               'character_geometry_hair_normal_mode':'cage','character_geometry_hair_seams':True,
                               'character_geometry_hair_protection':'authored',
                               'character_geometry_candidate_scope':'all','character_material_profile':'character_pbr'},existing_job=True)
        assert hair.isChecked() and hair.isEnabled()
        assert window.settings().character_geometry_hair_protection=='authored'
        assert window.settings().character_geometry_hair_normal_mode=='cage'
        legacy={'character_subdivision_enabled':True,'character_material_profile':'character_pbr'}
        window.apply_settings(legacy,existing_job=True)
        assert scope.currentData()==''
        assert window.settings().character_subdivision_enabled is True
        assert not hair.isChecked() and not hair.isEnabled()
        assert window.settings().character_geometry_hair_protection=='legacy'
        assert window.settings().character_geometry_hair_normal_mode=='surface'
        assert window.settings().character_geometry_hair_seams is False
        assert window.inputs['character_subdivision_enabled'].isEnabled()
    finally:window.timer.stop();window.close()
