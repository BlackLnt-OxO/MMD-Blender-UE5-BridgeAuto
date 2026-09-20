import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6 import QtWidgets as W
from mmd_bridge.app import Window
from mmd_bridge.controller import install_ue_bridge
from mmd_bridge.core import atomic_json,read_json


def test_desktop_settings_and_lock(tmp_path,monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()
    window.show_software([{'kind':'blender','path':'b.exe','version':'4.3.2','source':'test','compatibility':'test'},
                          {'kind':'unreal','path':'u.exe','version':'5.7.4','source':'test','compatibility':'test'}])
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        '素材与软件', '参数', 'CameraCutAuto', '任务与日志']
    window._set_camera_cut_frame_list([700, 1260], update_detect=True)
    assert window._camera_cut_frames_from_list() == [700, 1260]
    window.camera_cut_frame_edit.setText('900')
    window.add_camera_cut_frames()
    assert window._camera_cut_frames_from_list() == [700, 900, 1260]
    assert window.camera_cut_frame_list.item(1).foreground().color().name() == '#ffd166'
    window.camera_cut_delete_frame_edit.setText('999')
    window.remove_camera_cut_frames_by_input()
    assert window._camera_cut_frames_from_list() == [700, 900, 1260]
    window.camera_cut_delete_frame_edit.setText('900')
    window.remove_camera_cut_frames_by_input()
    assert window._camera_cut_frames_from_list() == [700, 1260]
    window.camera_cut_frame_edit.setText('1100')
    window.add_camera_cut_frames()
    window.restore_camera_cut_detect()
    assert window._camera_cut_frames_from_list() == [700, 1260]
    assert window.camera_cut_bake_button.objectName() == 'primary'
    window.tabs.setCurrentIndex(window.camera_cut_tab_index)
    assert not window.start_button.isEnabled()
    window.tabs.setCurrentIndex(0)
    assert window.start_button.isEnabled()
    window.inputs['scale'].setValue(.1)
    window.inputs['camera_y'].setValue(2.5)
    settings=window.settings()
    assert settings.scale==.1 and settings.camera_offset_xyz==[0,2.5,0]
    window.inputs['ue_vram_pause_percent'].setValue(95)
    window.inputs['ue_vram_resume_percent'].setValue(50)
    monkeypatch.setattr(W.QMessageBox,'information',lambda *args:None)
    window.restore_default_settings()
    assert window.inputs['scale'].value()==.08
    assert window.inputs['camera_y'].value()==0
    assert window.inputs['ue_vram_pause_percent'].value()==90
    assert window.inputs['ue_vram_resume_percent'].value()==80
    window.lock(True)
    assert not window.inputs['blender'].isEnabled()
    assert window.inputs['project'].isEnabled()
    window.close()


def test_install_bridge_preserves_project_fields_and_backup(tmp_path):
    project=tmp_path/'Demo.uproject'
    original={'FileVersion':3,'Description':'preserve me','Plugins':[{'Name':'Other','Enabled':True}]}
    atomic_json(project,original)
    mailbox=install_ue_bridge(project,tmp_path/'home')
    current=read_json(project)
    assert current['Description']=='preserve me'
    assert any(p['Name']=='Other' for p in current['Plugins'])
    assert any(p['Name']=='USDImporter' and p['Enabled'] for p in current['Plugins'])
    assert (project.parent/'Plugins/MMDBridge/Content/Python/mmd_bridge/ue_camera_cut_baker.py').is_file()
    assert (project.parent/'Plugins/MMDBridge/Content/Python/mmd_bridge/process.py').is_file()
    assert (project.parent/'Plugins/MMDBridge/Content/Python/mmd_bridge/ue_usd.py').is_file()
    assert read_json(project.with_suffix('.uproject.mmdb-backup'))==original
    install_ue_bridge(project,tmp_path/'home')
    assert len([p for p in read_json(project)['Plugins'] if p['Name']=='MMDBridge'])==1
    assert mailbox.is_dir()
