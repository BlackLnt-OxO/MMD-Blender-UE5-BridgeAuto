import os
import ctypes
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6 import QtCore as C, QtGui as G, QtTest as T, QtWidgets as W

from mmd_bridge.app import Window, configure_app


@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    monkeypatch.setattr(W.QMessageBox, 'information', lambda *args: None)
    app = W.QApplication.instance() or W.QApplication([])
    configure_app(app)
    window = Window()
    window.timer.stop()
    window.show()
    app.processEvents()
    yield window, app
    window.controller.job = None
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, C.QEvent.DeferredDelete)
    app.processEvents()


def test_sidebar_and_programmatic_navigation_stay_in_sync(gui):
    window, app = gui
    assert window.navigation.currentItem().data(C.Qt.UserRole) == window.tabs.currentIndex() == 0
    assert window.tabs.tabBar().isHidden()
    items = [window.navigation.item(i) for i in range(window.navigation.count())]
    assert [item.text() for item in items] == [
        '工作区', '素材与软件', '参数', '场景还原', '任务与日志',
        '专用工具', 'CameraCutAuto', 'PhysicsBakeFix', 'MMM 物理烘焙（已弃用）']
    for row,item in enumerate(items):
        if item.data(C.Qt.UserRole) is None:
            assert not item.flags() & C.Qt.ItemIsSelectable
            group = window.navigation.itemWidget(item)
            caption = group.findChild(W.QLabel, 'navigationGroupTitle')
            assert caption.text() == item.text()
            assert caption.alignment() & C.Qt.AlignHCenter
            assert caption.y() >= 0 and caption.y() + caption.height() <= group.height()
            continue
        window.navigation.setCurrentRow(row)
        assert window.tabs.currentIndex() == item.data(C.Qt.UserRole)
    window.tabs.setCurrentIndex(window.camera_cut_tab_index)
    assert window.navigation.currentItem().data(C.Qt.UserRole) == window.camera_cut_tab_index
    assert not window.full_button.isEnabled()
    window.tabs.setCurrentIndex(0)
    assert window.full_button.isEnabled()


def test_mmdbridge_detection_controls_and_results(gui, tmp_path, monkeypatch):
    import json
    window, app = gui
    field = window.inputs['mmdbridge_path']
    button = window.detect_mmdbridge_button
    assert 'rintrint/mmdbridge' in field.toolTip() and 'MikuMikuDance_CHS.exe' in field.toolTip()
    assert '所有导出中转文件' in window.inputs['character_output_dir'].toolTip()
    assert button.parentWidget() is field.parentWidget()
    layout = field.parentWidget().layout().itemAt(1).layout()
    assert layout.indexOf(button) > layout.indexOf(field)
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    notices, errors = [], []
    monkeypatch.setattr(W.QMessageBox, 'information', lambda *args: notices.append(args[-1]))
    monkeypatch.setattr(window, 'error', lambda exc: errors.append(str(exc)))

    def detect(result):
        def run(**kwargs):
            assert kwargs['extra_roots']
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr('mmd_bridge.app.discover_mmdbridge', run)
        button.click()
        assert not button.isEnabled() and button.text() == '检测中…'
        for worker in list(window.workers):
            assert worker.wait(5000)
        app.processEvents()
        assert not window.workers and button.isEnabled() and button.text() == '自动检测'

    field.setText(str(tmp_path))
    detect([str(first)])
    assert field.text() == str(first)
    assert json.loads(window.dialog_paths_file.read_text(encoding='utf-8'))['values']['mmdbridge_path'] == str(first)
    selections = []
    def choose(*args):
        selections.append(args)
        return str(second), True
    monkeypatch.setattr(W.QInputDialog, 'getItem', choose)
    detect([str(first), str(second)])
    assert field.text() == str(second) and selections[-1][-1] is False
    monkeypatch.setattr(W.QInputDialog, 'getItem', lambda *args: ('', False))
    detect([str(first), str(second)])
    detect([])
    detect(OSError('test failure'))
    assert field.text() == str(second)
    assert notices and 'rintrint/mmdbridge' in notices[-1]
    assert errors == ['test failure']
    for size in ((1024, 768), (1240, 900)):
        window.resize(*size)
        app.processEvents()
        assert window.tabs.widget(0).horizontalScrollBar().maximum() == 0


def test_mmdbridge_detection_runs_in_background_and_preserves_manual_edits(gui, tmp_path, monkeypatch):
    import threading
    window, app = gui
    field = window.inputs['mmdbridge_path']
    button = window.detect_mmdbridge_button
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    def scan(**kwargs):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(5)
        return [str(tmp_path)]
    monkeypatch.setattr('mmd_bridge.app.discover_mmdbridge', scan)
    try:
        button.click()
        assert entered.wait(2)
        field.setText('manually changed')
    finally:
        release.set()
        for worker in list(window.workers):
            assert worker.wait(5000)
        app.processEvents()
    assert field.text() == 'manually changed' and button.isEnabled()
    window.lock(True)
    window.detect_mmdbridge_directory()
    assert not window.workers and not button.isEnabled()
    window.lock(False)


@pytest.mark.parametrize('size', [(1240, 900), (1024, 768)])
def test_sidebar_and_header_form_a_continuous_shell(gui, size):
    window, app = gui
    window.resize(*size)
    app.processEvents()
    root = window.centralWidget()
    header = root.findChild(W.QWidget, 'appHeader')
    operation = root.findChild(W.QWidget, 'operationArea')
    actions = root.findChild(W.QWidget, 'operationActions')
    nav_top = window.navigation.mapTo(root, C.QPoint())
    operation_top = operation.mapTo(root, C.QPoint())
    assert header.geometry().topLeft() == C.QPoint()
    assert header.width() == root.width()
    assert nav_top == C.QPoint(0, header.height())
    assert operation_top == C.QPoint(window.navigation.width(), header.height())
    assert window.navigation.height() == operation.height() == root.height() - header.height()
    assert window.full_button.mapTo(root, C.QPoint()).x() >= operation_top.x()
    assert actions.parentWidget() is operation
    pixels = root.grab().toImage()
    assert pixels.pixelColor(header.width()-20, 10).name() == '#28292d'
    assert pixels.pixelColor(2, root.height()-2).name() == '#28292d'
    assert pixels.pixelColor(operation_top.x()+2, operation_top.y()+2).name() == '#28292d'
    assert pixels.pixelColor(operation_top.x()+24, operation_top.y()+4).name() == '#202124'


def test_numeric_stepper_keeps_its_mouse_controls(gui):
    window, app = gui
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    field = window.inputs['fps']
    option = W.QStyleOptionSpinBox()
    field.initStyleOption(option)
    up = field.style().subControlRect(W.QStyle.CC_SpinBox, option, W.QStyle.SC_SpinBoxUp, field)
    T.QTest.mouseClick(field, C.Qt.LeftButton, pos=up.center())
    assert field.value() == 31


def test_collapsed_settings_preserve_values_and_task_lock(gui):
    window, app = gui
    parameter_page = window.tabs.widget(1)
    scene_page = window.tabs.widget(window.scene_appearance_tab_index)
    sections = window.findChildren(W.QToolButton, 'sectionToggle')
    assert {button.text() for button in sections} == {
        '时间、缩放与镜头', '材质转换与适配', '场景导入与碰撞', '角色通路与兼容',
        '渲染与显存', '光照与色彩', '雾与植被'}
    assert all(not button.isChecked() for button in sections)
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    for key in ('fps', 'start', 'end', 'width', 'height', 'scene_frame',
                'character_master_material', 'eye_master_material', 'character_material_profile'):
        assert window.inputs[key].isVisibleTo(parameter_page)
    for key in ('motion_offset', 'camera_x', 'scale', 'scene_scale', 'scene_master_material',
                'scene_format', 'character_backend', 'material_strategy', 'content_root'):
        assert not window.inputs[key].isVisibleTo(parameter_page)
    for key in ('scene_render_pipeline', 'ue_render_memory_profile', 'scene_light_scale',
                'scene_exposure_offset', 'fog_density_scale', 'thin_surface_scattering'):
        assert not window.inputs[key].isVisibleTo(scene_page)
    window.apply_settings({'camera_offset_xyz': [1, 2, 3], 'motion_offset': 7,
                           'scene_exposure_offset': -1.5, 'scene_scale': 2,
                           'scene_import_profile': 'render_optimized'})
    expected = window.settings()
    for button in sections:
        button.click()
        assert button.arrowType() == C.Qt.DownArrow
    window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
    window.tabs.setCurrentIndex(1)
    assert all(button.isChecked() for button in sections)
    window.lock(True)
    transform_section = next(button for button in sections if button.text() == '时间、缩放与镜头')
    transform_section.click()
    transform_section.click()
    assert not window.inputs['camera_x'].isEnabled()
    window.inputs['scene_import_profile'].setCurrentIndex(0)
    assert not window.inputs['exact_collision'].isEnabled()
    window.inputs['scene_import_profile'].setCurrentIndex(1)
    assert not window.restore_defaults_button.isEnabled()
    assert window.settings() == expected
    window.lock(False)
    for button in sections:
        button.click()
        assert button.arrowType() == C.Qt.RightArrow
    window.apply_settings(vars(expected))
    assert window.settings() == expected
    assert not window.inputs['exact_collision'].isEnabled()
    window.restore_default_settings()
    assert window.inputs['scene_scale'].value() == 1
    assert window.inputs['camera_x'].value() == 0
    assert all(not button.isChecked() for button in sections)


@pytest.mark.parametrize('size', [(1240, 900), (1024, 768)])
def test_paths_use_full_width_without_horizontal_page_scrolling(gui, size):
    window, app = gui
    window.resize(*size)
    long_path = 'D:/' + '/'.join(['很长的素材目录名称'] * 24) + '/角色模型.pmx'
    for key in ('model', 'scene', 'character_output_dir'):
        window.inputs[key].setText(long_path)
    window.show_software([{'kind': 'blender', 'path': long_path, 'version': '5.2.2',
                          'source': 'test', 'compatibility': 'test'}])
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        for button in window.tabs.currentWidget().findChildren(W.QToolButton, 'sectionToggle'):
            button.setChecked(True)
        app.processEvents()
        page = window.tabs.currentWidget()
        assert page.horizontalScrollBar().maximum() == 0, window.tabs.tabText(index)
        assert window.width() == size[0]
        for field in page.findChildren(W.QAbstractSpinBox) + page.findChildren(W.QComboBox):
            if field.isVisibleTo(page):
                assert field.height() >= field.minimumSizeHint().height(), window.tabs.tabText(index)
        for content in page.findChildren(W.QWidget, 'sectionContent'):
            if content.isVisibleTo(page):
                top = content.mapTo(page.widget(), C.QPoint()).y()
                for toggle in page.findChildren(W.QToolButton, 'sectionToggle'):
                    next_top = toggle.mapTo(page.widget(), C.QPoint()).y()
                    if next_top > top:
                        assert next_top >= top + content.height()

    window.tabs.setCurrentIndex(0)
    app.processEvents()
    fields = [window.inputs[key] for key in ('model', 'motion', 'lipsync', 'camera', 'scene', 'project')]
    assert len({field.parentWidget() for field in fields}) == len(fields)
    for field in fields:
        container = field.parentWidget()
        assert container.layout().itemAt(0).widget().y() < field.y()
        assert container.width() >= window.tabs.currentWidget().viewport().width() - 45
        assert field.width() > 400
    assert window.inputs['model'].text() == long_path
    assert window.inputs['model'].maxLength() > len(long_path)


def test_motion_is_interruptible_and_lock_clears_button_feedback(gui):
    window, app = gui
    window.motion.enabled = True
    window.tabs.setCurrentIndex(1)
    page = window.tabs.currentWidget()
    content = window.inputs['camera_x'].parentWidget()
    clip = content.parentWidget()
    toggle = next(button for button in page.findChildren(W.QToolButton, 'sectionToggle')
                  if button.text() == '时间、缩放与镜头')
    before = window.settings()
    toggle.click()
    T.QTest.qWait(60)
    assert 0 < clip.height() < content.sizeHint().height()
    toggle.click()
    T.QTest.qWait(30)
    toggle.click()
    T.QTest.qWait(220)
    assert clip.isVisibleTo(page) and clip.maximumHeight() == 16777215
    assert clip.height() >= content.sizeHint().height()
    assert window.settings() == before
    app.sendEvent(window.full_button, C.QEvent(C.QEvent.Enter))
    T.QTest.qWait(50)
    assert window.full_button.styleSheet()
    window.lock(True)
    assert not window.full_button.styleSheet()
    assert not window.full_button.isEnabled()
    toggle.click()
    T.QTest.qWait(200)
    assert not clip.isVisibleTo(page)
    assert not window.inputs['camera_x'].isEnabled()
    window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
    T.QTest.qWait(200)
    effect = window.tabs.currentWidget().widget().graphicsEffect()
    assert effect.opacity() == 1
    assert not window.motion.animations
    window.motion.enabled = False
    window.tabs.setCurrentIndex(1)
    toggle.click()
    assert clip.isVisibleTo(page) and not window.motion.animations


def test_hidden_master_instance_option_keeps_saved_value(gui):
    window, app = gui
    window.tabs.setCurrentIndex(1)
    toggle = next(button for button in window.findChildren(W.QToolButton, 'sectionToggle')
                  if button.text() == '材质转换与适配')
    toggle.click()
    window.apply_settings({'use_scene_master_material': True})
    field = window.inputs['use_scene_master_material']
    assert field.isHidden() and field.isChecked()
    assert window.settings().use_scene_master_material is True
    assert field.parentWidget().layout().labelForField(field).isHidden()


def test_character_profile_is_a_common_choice_and_scene_master_stays_hidden(gui):
    window, app = gui
    window.tabs.setCurrentIndex(1)
    page = window.tabs.currentWidget()
    profile = window.inputs['character_material_profile']
    assert profile.isVisibleTo(page) and not profile.isEditable()
    profile.setCurrentIndex(profile.findData('ZZZ_Render_PMX'))
    T.QTest.keyClicks(profile, 'SSS')
    assert profile.currentText() == 'ZZZ_Render_PMX'
    assert window.settings().character_material_profile == 'ZZZ_Render_PMX'
    toggle = next(button for button in page.findChildren(W.QToolButton, 'sectionToggle')
                  if button.text() == '材质转换与适配')
    toggle.click()
    app.processEvents()
    window.apply_settings({'scene_master_material': '/Game/User/SavedMaster'})
    scene = window.inputs['scene_master_material']
    assert scene.parentWidget().isHidden()
    assert not scene.isVisibleTo(page)
    assert window.settings().scene_master_material == '/Game/User/SavedMaster'


def test_official_toon_is_default_and_zzz_is_second_with_template_paths(gui):
    window, app = gui
    combo = window.inputs['character_material_profile']
    assert [combo.itemData(i) for i in range(4)] == ['ue_official_toon', 'ZZZ_Render_PMX', 'auto', 'generic']
    assert window.settings().character_material_profile == 'ue_official_toon'
    assert not window.inputs['character_master_material'].isEnabled()
    assert not window.inputs['eye_master_material'].isEnabled()
    combo.setCurrentIndex(1)
    assert window.inputs['character_master_material'].text() == '/Game/Character/MasterMaterial/M_RenderMaster'
    assert window.inputs['eye_master_material'].text() == ''
    assert window.inputs['character_master_material'].isEnabled()
    window.apply_settings({'character_master_material':'/Game/User/Existing'}, existing_job=True)
    assert combo.currentData() == 'auto'
    assert window.settings().character_master_material == '/Game/User/Existing'
    window.restore_default_settings()
    assert combo.currentData() == 'ue_official_toon'


def test_material_profile_appears_after_both_master_paths(gui):
    window, app = gui
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    page = window.tabs.currentWidget().widget()
    tops = [window.inputs[key].mapTo(page, C.QPoint()).y() for key in (
        'character_master_material', 'eye_master_material', 'character_material_profile')]
    assert tops == sorted(tops) and len(set(tops)) == 3


def test_scrollbar_brightens_on_scroll_hover_and_drag_then_returns_dim(gui):
    window, app = gui
    window.motion.enabled = True
    page = window.tabs.currentWidget()
    bar = page.verticalScrollBar()
    effect = bar.graphicsEffect()
    assert bar.maximum() > 0 and bar.width() == 6
    assert effect.opacity() == .28
    viewport_width = page.viewport().width()
    bar.triggerAction(W.QAbstractSlider.SliderPageStepAdd)
    T.QTest.qWait(50)
    assert .28 < effect.opacity() < 1
    T.QTest.qWait(140)
    assert effect.opacity() == 1
    bar.setSliderDown(True)
    T.QTest.qWait(760)
    assert effect.opacity() == 1
    bar.setSliderDown(False)
    T.QTest.qWait(780)
    assert 0 < effect.opacity() < 1
    bar.triggerAction(W.QAbstractSlider.SliderPageStepSub)
    T.QTest.qWait(160)
    assert effect.opacity() == 1
    T.QTest.qWait(1000)
    assert effect.opacity() == .28
    assert page.viewport().width() == viewport_width
    app.sendEvent(bar, C.QEvent(C.QEvent.Enter))
    T.QTest.qWait(900)
    assert effect.opacity() == 1
    app.sendEvent(bar, C.QEvent(C.QEvent.Leave))
    T.QTest.qWait(1000)
    assert effect.opacity() == .28
    window.motion.enabled = False
    bar.triggerAction(W.QAbstractSlider.SliderPageStepAdd)
    assert effect.opacity() == 1


def test_hover_tip_has_transparent_corners_and_interruptible_fades(gui):
    window, app = gui
    previous = app.isEffectEnabled(C.Qt.UI_General)
    app.setEffectEnabled(C.Qt.UI_General, True)
    button = window.full_button
    button.setToolTip('按钮说明：圆角提示框，保持原有说明文字。')
    tip = None
    try:
        position = button.mapToGlobal(button.rect().center())
        app.sendEvent(button, G.QHelpEvent(C.QEvent.ToolTip, button.rect().center(), position))
        tip = app._window_frames.tooltip
        T.QTest.qWait(50)
        assert tip.isVisible() and 0 < tip.windowOpacity() < 1
        T.QTest.qWait(150)
        assert tip.text() == button.toolTip() and tip.windowOpacity() == 1
        pixels = tip.grab().toImage()
        assert pixels.pixelColor(0, 0).alpha() == 0
        assert pixels.pixelColor(tip.width()//2, 5).name() == '#303136'
        assert pixels.pixelColor(tip.width()//2, 5).alpha() == 255
        app.sendEvent(button, C.QEvent(C.QEvent.Leave))
        T.QTest.qWait(50)
        assert tip.isVisible() and 0 < tip.windowOpacity() < 1
        edge = button.screen().availableGeometry().bottomRight() - C.QPoint(2, 2)
        app.sendEvent(button, G.QHelpEvent(C.QEvent.ToolTip, C.QPoint(), edge))
        T.QTest.qWait(180)
        assert tip.windowOpacity() == 1
        assert button.screen().availableGeometry().contains(tip.geometry())
        app.sendEvent(button, G.QKeyEvent(C.QEvent.KeyPress, C.Qt.Key_Escape, C.Qt.NoModifier))
        T.QTest.qWait(180)
        assert not tip.isVisible()
    finally:
        if tip is not None:
            tip.hide()
            tip.expiry.stop()
        app.setEffectEnabled(C.Qt.UI_General, previous)


def test_combo_popup_has_styled_rows_and_keyboard_selection(gui):
    window, app = gui
    window.tabs.setCurrentIndex(1)
    toggle = next(button for button in window.findChildren(W.QToolButton, 'sectionToggle')
                  if button.text() == '材质转换与适配')
    toggle.click()
    combo = window.inputs['material_workflow']
    window.tabs.currentWidget().ensureWidgetVisible(combo)
    combo.showPopup()
    app.processEvents()
    assert isinstance(combo.view(), W.QListView)
    assert isinstance(combo.view().itemDelegate(), W.QStyledItemDelegate)
    assert combo.view().visualRect(combo.model().index(0, 0)).height() >= 40
    assert [index.row() for index in combo.view().selectedIndexes()] == [combo.currentIndex()]
    T.QTest.keyClick(combo.view(), C.Qt.Key_Down)
    T.QTest.keyClick(combo.view(), C.Qt.Key_Return)
    assert combo.currentData() == 'semantic_plan_v1'


def test_fold_height_and_bottom_scroll_change_throughout_animation(gui):
    window, app = gui
    window.resize(1024, 768)
    window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
    page = window.tabs.currentWidget()
    toggle = next(button for button in page.findChildren(W.QToolButton, 'sectionToggle')
                  if button.text() == '雾与植被')
    clip = toggle.parentWidget().findChild(W.QWidget, 'sectionClip')
    window.motion.enabled = True
    T.QTest.qWait(180)
    toggle.click()
    assert clip.height() == 0
    opening = []
    for _ in range(14):
        T.QTest.qWait(16)
        opening.append(clip.height())
    assert opening == sorted(opening)
    assert len(set(opening)) >= 3
    bar = page.verticalScrollBar()
    bar.setValue(bar.maximum())
    maximum = bar.maximum()
    assert maximum > 0
    toggle.click()
    heights, ranges = [], []
    for _ in range(14):
        T.QTest.qWait(16)
        heights.append(clip.height())
        ranges.append(bar.maximum())
    assert heights == sorted(heights, reverse=True)
    assert ranges == sorted(ranges, reverse=True)
    assert len(set(ranges)) >= 3
    assert ranges[3] < maximum
    assert not clip.isVisibleTo(page)


@pytest.mark.parametrize('animated', [False, True])
@pytest.mark.parametrize('title', ['角色通路与兼容', '材质转换与适配'])
def test_expanding_near_page_bottom_scrolls_new_fields_into_view(gui, animated, title):
    window, app = gui
    window.resize(1024, 768)
    window.tabs.setCurrentIndex(1)
    page = window.tabs.currentWidget()
    sections = page.findChildren(W.QToolButton, 'sectionToggle')
    toggle = next(button for button in sections if button.text() == title)
    for button in sections:
        if button is not toggle:
            button.setChecked(True)
    app.processEvents()
    title_y = toggle.mapTo(page.widget(), C.QPoint()).y()
    page.verticalScrollBar().setValue(title_y - page.viewport().height() + toggle.height() + 20)
    if title == '角色通路与兼容':
        page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
    app.processEvents()
    bar = page.verticalScrollBar()
    before = bar.value()
    window.motion.enabled = animated
    toggle.click()
    positions = [bar.value()]
    for _ in range(14):
        T.QTest.qWait(16)
        positions.append(bar.value())
    assert positions == sorted(positions)
    assert bar.value() > before
    if animated:
        assert len(set(positions)) >= 3
    group = toggle.parentWidget()
    top = group.mapTo(page.viewport(), C.QPoint()).y()
    if group.height() <= page.viewport().height() - 24:
        assert top >= 0
        assert top + group.height() <= page.viewport().height() - 10
    else:
        assert 10 <= top <= 14


def test_dialogs_and_late_created_combos_share_application_theme(gui, tmp_path):
    window, app = gui
    assert not app.testAttribute(C.Qt.AA_DontUseNativeDialogs)
    dialog = W.QDialog(window)
    layout = W.QVBoxLayout(dialog)
    combo = W.QComboBox()
    combo.addItems(['第一项', '第二项'])
    combo.setEditable(True)
    layout.addWidget(combo)
    button = W.QPushButton('确定')
    layout.addWidget(button)
    dialog.show()
    app.processEvents()
    assert combo.property('styledPopup')
    combo.showPopup()
    app.processEvents()
    assert isinstance(combo.view().itemDelegate(), W.QStyledItemDelegate)
    assert combo.view().window() in window.motion.popups
    combo.hidePopup()
    combo.setEditText('自定义配置')
    assert combo.currentText() == '自定义配置'
    window.motion.enabled = True
    app.sendEvent(button, C.QEvent(C.QEvent.Enter))
    T.QTest.qWait(40)
    assert button.styleSheet()
    dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(None, C.QEvent.DeferredDelete)
    assert button not in window.motion.button_colors
    assert not any(key[0] is button for key in window.motion.animations)
    file_dialog = W.QFileDialog(window, '选择文件', str(tmp_path))
    assert not file_dialog.testOption(W.QFileDialog.DontUseNativeDialog)
    file_dialog.deleteLater()


@pytest.mark.parametrize('result', [0, -1])
def test_native_frame_colors_keep_system_window_controls(gui, monkeypatch, result):
    from mmd_bridge.gui_theme import WindowFrames
    window, app = gui
    colors = []

    def set_attribute(handle, attribute, pointer, size):
        colors.append((attribute, ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint)).contents.value))
        return result

    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(dwmapi=SimpleNamespace(
        DwmSetWindowAttribute=set_attribute)), raising=False)
    monkeypatch.setattr(W.QApplication, 'platformName', lambda: 'windows')
    flags = window.windowFlags()
    WindowFrames().eventFilter(window, C.QEvent(C.QEvent.Show))
    assert colors == [(20, 1), (34, 0x4b4442), (35, 0x2d2928), (36, 0xf0eeee)]
    assert window.property('nativeThemeApplied') is (result == 0)
    assert window.windowFlags() == flags
