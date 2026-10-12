import os
import json
from types import SimpleNamespace
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6 import QtCore as C,QtGui as G,QtTest as T,QtWidgets as W
from mmd_bridge.app import Window
from mmd_bridge.controller import install_ue_bridge
from mmd_bridge.core import atomic_json,read_json


def test_wheel_never_changes_parameters_even_when_focused(tmp_path,monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window();window.timer.stop();window.show();app.processEvents()
    window.background_preset.addItems(['/Game/One','/Game/Two'])
    try:
        for widget in [window.inputs['width'],window.inputs['fog_density_scale'],
                       window.inputs['bake_resolution'],window.background_preset]:
            read=widget.currentIndex if isinstance(widget,W.QComboBox) else widget.value
            before=read()
            for focused in [False,True]:
                widget.setFocus() if focused else widget.clearFocus()
                for delta in [120,-120]:
                    for target in [widget,widget.lineEdit()]:
                        if target is None:continue
                        point=target.rect().center()
                        event=G.QWheelEvent(C.QPointF(point),C.QPointF(target.mapToGlobal(point)),
                            C.QPoint(),C.QPoint(0,delta),C.Qt.NoButton,C.Qt.NoModifier,C.Qt.NoScrollPhase,False)
                        app.sendEvent(target,event)
                        assert read()==before
        field=window.inputs['fog_density_scale'];before=field.value()
        T.QTest.keyClick(field,C.Qt.Key_Up)
        assert field.value()>before
        window.inputs['bake_resolution'].setCurrentIndex(1)
        assert window.inputs['bake_resolution'].currentIndex()==1
    finally:window.close()


def test_path_memory_is_per_field_and_survives_restart_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    model = tmp_path / 'models' / 'character.pmx'
    scene = tmp_path / 'scenes' / 'stage.blend'
    transfer = tmp_path / 'transfer'
    for path in (model, scene):
        path.parent.mkdir()
        path.touch()
    transfer.mkdir()
    window = Window()
    window.inputs['model'].setText(str(model))
    window.inputs['scene'].setText(str(scene))
    window.inputs['character_output_dir'].setText(str(transfer))
    window.inputs['scene'].clear()
    window.close()

    restarted = Window()
    assert restarted.inputs['model'].text() == str(model)
    assert restarted.inputs['scene'].text() == ''
    assert restarted.inputs['character_output_dir'].text() == str(transfer)
    observed = []
    monkeypatch.setattr(W.QFileDialog, 'getOpenFileName',
                        lambda *args: (observed.append(args[2]) or '', ''))
    for key in ('model', 'scene'):
        button = next(b for b in restarted.findChildren(W.QPushButton)
                      if b.property('input_key') == key)
        button.click()
    monkeypatch.setattr(W.QFileDialog, 'getExistingDirectory',
                        lambda *args: observed.append(args[2]) or '')
    restarted.browse_directory('character_output_dir')
    assert observed == [str(model.parent), str(scene.parent), str(transfer)]
    assert restarted.inputs['scene'].text() == ''
    assert restarted.inputs['model'].text() == str(model)
    # A removed source folder falls back to its nearest surviving parent.
    scene.unlink()
    scene.parent.rmdir()
    assert restarted.path_dialog_folder('scene') == str(tmp_path)
    restarted.close()


def test_top_import_buttons_start_independent_flows(tmp_path, monkeypatch):
    from tests.test_core import vmd
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    blender = tmp_path/'blender.exe'; blender.touch()
    scene = tmp_path/'street.blend'; scene.touch()
    camera = vmd(tmp_path/'camera.vmd', cameras=1)
    window.inputs['blender'].addItem('Blender', str(blender))
    window.inputs['scene'].setText(str(scene))
    window.inputs['camera'].setText(str(camera))
    captured = []
    def start(settings):
        captured.append(settings)
        window.controller.job = SimpleNamespace(folder=tmp_path, data={'settings': {'character_output_dir': str(tmp_path)}})
    monkeypatch.setattr(window.controller, 'start', start)
    window.scene_button.click()
    assert captured[-1].flow == 'scene'
    assert captured[-1].scene == str(scene) and not captured[-1].camera
    window.lock(False)
    window.camera_button.click()
    assert captured[-1].flow == 'camera'
    assert captured[-1].camera == str(camera) and not captured[-1].scene
    window.controller.job = None
    window.close()


def test_desktop_settings_and_lock(tmp_path,monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()
    window.show_software([{'kind':'blender','path':'b.exe','version':'4.3.2','source':'test','compatibility':'test'},
                          {'kind':'unreal','path':'u.exe','version':'5.7.4','source':'test','compatibility':'test'}])
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        '素材与软件', '参数', '场景还原', 'CameraCutAuto', 'PhysicsBakeFix', 'MMM 物理烘焙（已弃用）', '任务与日志']
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
    assert settings.character_backend == 'mmdbridge_fbx'
    assert settings.scene_format == 'usd_stage'
    assert settings.scene_light_scale == 1
    assert settings.restore_scene_appearance
    assert settings.content_root == '/Game/Scenes'
    monkeypatch.setattr(W.QMessageBox,'information',lambda *args:None)
    window.restore_default_settings()
    assert window.inputs['scale'].value()==.08
    assert window.inputs['camera_y'].value()==0
    window.lock(True)
    assert not window.inputs['blender'].isEnabled()
    assert window.inputs['project'].isEnabled()
    window.close()


def test_desktop_shows_each_bake_event(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    events = tmp_path/'events.jsonl'
    events.write_text(''.join(json.dumps({'state': 'scene_baking', 'message': message}, ensure_ascii=False)+'\n'
                              for message in ('开始烘焙贴图 网格 1/2', '完成烘焙贴图 网格 1/2',
                                              '复用已烘焙贴图 网格 2/2')),
                      encoding='utf-8')
    window.controller.job = SimpleNamespace(folder=tmp_path)
    monkeypatch.setattr(window.controller, 'poll', lambda: {'state': 'scene_export', 'message': '网格 2/2'})
    monkeypatch.setattr(window, 'update_bridge_status', lambda: None)
    window.poll()
    assert '开始烘焙贴图' in window.log.toPlainText()
    assert '完成烘焙贴图' in window.log.toPlainText()
    assert '复用已烘焙贴图' in window.log.toPlainText()
    window.controller.job = None
    window.close()


def test_gpu_pause_notice_keeps_resume_poll_available(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    window.timer.stop()
    window.controller.job = SimpleNamespace(folder=tmp_path)
    polls = []
    def paused():
        polls.append(True)
        return {'state': 'ue_wait_vram', 'message': '等待显存释放'}
    monkeypatch.setattr(window.controller, 'poll', paused)
    monkeypatch.setattr(window, 'update_bridge_status', lambda: None)
    monkeypatch.setattr(W.QMessageBox, 'warning',
                        lambda *args: (_ for _ in ()).throw(AssertionError('modal warning blocks resume')))
    window.poll()
    notices = window.findChildren(W.QMessageBox)
    assert len(notices) == 1 and not notices[0].isModal()
    window.poll()
    assert len(polls) == 2
    assert len(window.findChildren(W.QMessageBox)) == 1
    notices[0].close()
    window.controller.job = None
    window.close()


def test_physics_bake_tab_is_standalone_tool(tmp_path,monkeypatch):
    """The PhysicsBakeFix page must not join the main automation flow."""
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()
    assert window.tabs.tabText(window.physics_bake_tab_index)=='PhysicsBakeFix'
    # On the standalone page the main start button is disabled, like CameraCutAuto.
    window.tabs.setCurrentIndex(window.physics_bake_tab_index)
    assert not window.start_button.isEnabled()
    window.tabs.setCurrentIndex(window.camera_cut_tab_index)
    assert not window.start_button.isEnabled()
    window.tabs.setCurrentIndex(0)
    assert window.start_button.isEnabled()
    # Repair stays disabled until an analysis finds something to fix.
    assert not window.physics_bake_repair_button.isEnabled()
    window.close()


def test_physics_bake_tab_detects_and_repairs(tmp_path,monkeypatch):
    """End-to-end through the GUI page using a synthesised PMX file."""
    from mmd_bridge import pmx_physics_displayframe as pbf
    from tests.test_pmx_physics_displayframe import make_pmx
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    monkeypatch.setattr(W.QMessageBox,'information',lambda *args:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()

    source=tmp_path/'model.pmx'
    source.write_bytes(make_pmx(
        bones=[('センター',True),('スカート01',True)],
        rigid_bodies=[('スカート01',1,1)],
        display_frames=[('Root',[0])]))
    window.physics_bake_model.setText(str(source))

    def drain():
        # The page runs work on a Worker thread; wait for it and pump signals.
        for worker in list(window.workers):
            worker.wait(5000)
        app.processEvents()

    window.execute(lambda: pbf.analyze(str(source)), window._physics_bake_analyzed)
    drain()
    assert window.physics_bake_repair_button.isEnabled()

    window.execute(lambda: pbf.repair(str(source)), window._physics_bake_repaired)
    drain()
    assert (tmp_path/'model_PhysicsBakeFixed.pmx').exists()
    assert window.physics_bake_model.text() == str(source)
    window.close()

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


def _mmm_window(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'LocalAppData'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    # 页面里的确认/提示框是模态的，测试中必须全部挡掉，否则会挂住。
    # 单个测试如需断言弹窗内容，可在拿到 window 之后再覆盖一次。
    monkeypatch.setattr(W.QMessageBox, 'information',
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(W.QMessageBox, 'warning',
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(W.QMessageBox, 'question',
                        staticmethod(lambda *a, **k: W.QMessageBox.Yes))
    app = W.QApplication.instance() or W.QApplication([])
    return Window()


def _drain(window):
    """Wait for the page's Worker threads and pump Qt signals."""
    for worker in list(window.workers):
        worker.wait(5000)
    app = W.QApplication.instance()
    if app is not None:
        app.processEvents()


def _fake_mmm(tmp_path, name='MMM'):
    """A synthetic MMM tree: enough for validate_mmm_dir to accept it."""
    root = tmp_path / name
    (root / 'System' / 'x64').mkdir(parents=True)
    (root / 'Plugins').mkdir(parents=True)
    (root / 'MikuMikuMoving64.exe').write_bytes(b'MZ')
    (root / 'System' / 'x64' / 'MikuMikuPlugin.dll').write_bytes(b'MZ')
    return root


def test_mmm_tab_is_standalone_tool(tmp_path, monkeypatch):
    """The MMM page must not join the main automation flow."""
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        assert window.tabs.tabText(window.mmm_tab_index) == 'MMM 物理烘焙（已弃用）'
        window.tabs.setCurrentIndex(window.mmm_tab_index)
        assert not window.start_button.isEnabled()
        window.tabs.setCurrentIndex(0)
        assert window.start_button.isEnabled()
    finally:
        window.close()


def test_mmm_tab_reports_invalid_directory(tmp_path, monkeypatch):
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        window.inputs['mmm_path'].setText(str(tmp_path / 'not-mmm'))
        window.refresh_mmm_status()
        assert '✗' in window.mmm_status.text()
    finally:
        window.close()


def test_mmm_tab_accepts_valid_directory(tmp_path, monkeypatch):
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        window.inputs['mmm_path'].setText(str(root))
        window.refresh_mmm_status()
        assert 'MikuMikuMoving64.exe' in window.mmm_status.text()
        assert '✗' not in window.mmm_status.text()
    finally:
        window.close()


def test_mmm_install_button_actually_installs(tmp_path, monkeypatch):
    """The install button must really put the DLL in place — not just report."""
    from mmd_bridge import mmm_runtime as rt
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        dll = tmp_path / 'build' / rt.PLUGIN_DLL_NAME
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b'MZ' + b'\x00' * 512)
        monkeypatch.setattr(window, '_mmm_build_dll', lambda: dll)

        window.inputs['mmm_path'].setText(str(root))
        window.install_mmm_plugin()

        assert (root / 'Plugins' / rt.PLUGIN_DLL_NAME).is_file()
        assert window.mmm_plugin_status.text().startswith('✓ 已安装')
    finally:
        window.close()


def test_mmm_uninstall_button_actually_removes(tmp_path, monkeypatch):
    from mmd_bridge import mmm_runtime as rt
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        plugins = root / 'Plugins'
        (plugins / rt.PLUGIN_DLL_NAME).write_bytes(b'MZ')
        (plugins / (rt.PLUGIN_DLL_NAME + '.bak-20260101-000000')).write_bytes(b'old')
        other = plugins / 'PoseSelectorPlugin.dll'
        other.write_bytes(b'other')

        window.inputs['mmm_path'].setText(str(root))
        window.refresh_mmm_status()
        assert window.mmm_plugin_status.text().startswith('✓ 已安装')

        monkeypatch.setattr(
            W.QMessageBox, 'question',
            staticmethod(lambda *a, **k: W.QMessageBox.Yes))
        window.uninstall_mmm_plugin()

        assert not (plugins / rt.PLUGIN_DLL_NAME).exists()
        assert list(plugins.glob(rt.PLUGIN_DLL_NAME + '*')) == []
        assert other.is_file(), '不能误删其它插件'
        assert window.mmm_plugin_status.text().startswith('未安装')
    finally:
        window.close()


def test_mmm_install_without_build_warns(tmp_path, monkeypatch):
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        window.inputs['mmm_path'].setText(str(root))
        monkeypatch.setattr(window, '_mmm_build_dll',
                            lambda: tmp_path / 'missing' / 'nope.dll')
        warned = []
        monkeypatch.setattr(
            W.QMessageBox, 'warning',
            staticmethod(lambda *a, **k: warned.append(a)))
        window.install_mmm_plugin()
        assert warned, '未构建时应弹窗提示，而不是静默失败'
    finally:
        window.close()


def test_mmm_prepare_runtime_button_copies(tmp_path, monkeypatch):
    from mmd_bridge import mmm_runtime as rt
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        (root / 'Settings.xml').write_text('original', encoding='utf-8')
        window.inputs['mmm_path'].setText(str(root))
        window.prepare_mmm_runtime()
        _drain(window)

        assert rt.runtime_status()['prepared'] is True
        assert (root / 'Settings.xml').read_text(encoding='utf-8') == 'original'
        # 目标目录优先变成隔离 Runtime
        assert window._mmm_target_dir() == Path(rt.runtime_status()['runtime_dir'])
    finally:
        window.close()


def test_mmm_plugin_install_targets_runtime_once_prepared(tmp_path, monkeypatch):
    """准备好 Runtime 后，装插件应落在副本里，而不是原 MMM 目录。"""
    from mmd_bridge import mmm_runtime as rt
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        root = _fake_mmm(tmp_path)
        window.inputs['mmm_path'].setText(str(root))
        window.prepare_mmm_runtime()
        _drain(window)

        dll = tmp_path / 'build' / rt.PLUGIN_DLL_NAME
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b'MZ' + b'\x00' * 256)
        monkeypatch.setattr(window, '_mmm_build_dll', lambda: dll)

        window.install_mmm_plugin()

        runtime = Path(rt.runtime_status()['runtime_dir'])
        assert (runtime / 'Plugins' / rt.PLUGIN_DLL_NAME).is_file()
        assert not (root / 'Plugins' / rt.PLUGIN_DLL_NAME).exists(), \
            '准备 Runtime 后不应再写原 MMM 目录'
    finally:
        window.close()


def test_backend_selector_defaults_to_mode1(tmp_path, monkeypatch):
    """新建 GUI 任务默认执行物理烘焙，历史配置仍可选旧通路。"""
    from mmd_bridge.core import Settings
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        combo = window.inputs['character_backend']
        assert combo.currentData() == 'mmdbridge_fbx'
        assert Settings().character_backend == 'fbx_legacy'
        labels = [combo.itemText(i) for i in range(combo.count())]
        assert any('默认' in text for text in labels)
        assert any('Legacy' in text for text in labels)
        assert any(combo.itemData(i) == 'fbx_legacy' for i in range(combo.count()))
    finally:
        window.close()


def test_gui_refuses_legacy_when_mode1_inputs_are_filled(tmp_path, monkeypatch):
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        window.inputs['character_backend'].setCurrentIndex(1)
        for key in ('model', 'motion', 'mmdbridge_path', 'character_output_dir'):
            window.inputs[key].setText('selected')
        errors = []
        monkeypatch.setattr(window, 'error', lambda exc: errors.append(str(exc)))
        window.start()
        assert errors and '不会启动 MMD' in errors[0]
        assert window.controller.job is None
    finally:
        window.close()


def test_mmm_path_is_persisted_through_settings(tmp_path, monkeypatch):
    """MMM 目录要能随「保存配置」一起存下来。"""
    from mmd_bridge.core import Settings
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        window.inputs['mmm_path'].setText(r'F:/Some/MikuMikuMoving64_v1292')
        collected = window.settings()
        assert collected.mmm_path == r'F:/Some/MikuMikuMoving64_v1292'
        assert hasattr(Settings(), 'mmm_path')
    finally:
        window.close()


def test_mmm_page_uses_runtime_as_install_target(tmp_path, monkeypatch):
    """没有 Runtime、也没填 MMM 目录时，安装按钮必须明确报错而不是静默失败。"""
    window = _mmm_window(tmp_path, monkeypatch)
    try:
        assert window._mmm_target_dir() is None
        errors = []
        monkeypatch.setattr(window, 'error', lambda exc: errors.append(str(exc)))
        window.install_mmm_plugin()
        assert errors and 'MMM 目录' in errors[0]
    finally:
        window.close()
