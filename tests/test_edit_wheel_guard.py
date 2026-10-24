import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W, QtTest as T
from mmd_bridge.app import configure_app


def wheel(app, widget, delta=-120, *, pixels=False):
    point = widget.rect().center()
    event = G.QWheelEvent(C.QPointF(point), C.QPointF(widget.mapToGlobal(point)),
        C.QPoint(0, delta) if pixels else C.QPoint(),
        C.QPoint() if pixels else C.QPoint(0, delta),
        C.Qt.NoButton, C.Qt.NoModifier, C.Qt.ScrollUpdate if pixels else C.Qt.NoScrollPhase, False)
    app.sendEvent(widget, event)


@pytest.fixture(scope='module')
def application():
    app = W.QApplication.instance() or W.QApplication([])
    configure_app(app)
    return app


@pytest.fixture
def surface(application):
    app = application
    dialog = W.QDialog()
    outer = W.QVBoxLayout(dialog)
    area = W.QScrollArea(); area.setWidgetResizable(True); outer.addWidget(area)
    content = W.QWidget(); layout = W.QVBoxLayout(content)
    area.setWidget(content)
    content.setMinimumHeight(1800)
    dialog.resize(500, 350)
    yield app, dialog, area, layout
    dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(None, C.QEvent.DeferredDelete)


@pytest.mark.parametrize('kind', ['integer', 'decimal', 'date', 'combo', 'editable_combo', 'slider', 'dial'])
@pytest.mark.parametrize('focused', [False, True])
def test_late_dialog_fields_ignore_wheel_and_page_still_scrolls(surface, kind, focused):
    app, dialog, area, layout = surface
    if kind in ('combo', 'editable_combo'):
        field = W.QComboBox(); field.addItems(['first', 'second', 'third']); field.setCurrentIndex(1)
        field.setEditable(kind == 'editable_combo')
        value = field.currentText
    elif kind == 'date':
        field = W.QDateEdit(C.QDate(2026, 10, 23)); value = field.date
    else:
        field = {'integer': W.QSpinBox, 'decimal': W.QDoubleSpinBox,
                 'slider': W.QSlider, 'dial': W.QDial}[kind]()
        field.setRange(0, 100); field.setValue(50); value = field.value
    layout.addWidget(field); layout.addStretch()
    dialog.show(); app.processEvents()
    if focused:
        field.setFocus(); app.processEvents()
        assert field.hasFocus()
    else:
        field.clearFocus()
    before = value()
    area.verticalScrollBar().setValue(100)
    wheel(app, field)
    assert value() == before
    assert area.verticalScrollBar().value() > 100
    area.verticalScrollBar().setValue(100)
    wheel(app, area.viewport(), delta=-40, pixels=True)
    native_pixel_position = area.verticalScrollBar().value()
    area.verticalScrollBar().setValue(100)
    wheel(app, field, delta=-40, pixels=True)
    assert value() == before
    assert area.verticalScrollBar().value() == native_pixel_position
    if isinstance(field, (W.QAbstractSpinBox, W.QComboBox)) and field.lineEdit():
        area.verticalScrollBar().setValue(100)
        wheel(app, field.lineEdit())
        assert value() == before
        assert area.verticalScrollBar().value() > 100


def test_keyboard_steps_and_combo_popup_scrolling_still_work(surface):
    app, dialog, area, layout = surface
    spin = W.QSpinBox(); spin.setValue(5); layout.addWidget(spin)
    combo = W.QComboBox(); combo.addItems([str(i) for i in range(100)]); layout.addWidget(combo)
    dialog.show(); app.processEvents()
    T.QTest.keyClick(spin, C.Qt.Key_Up); assert spin.value() == 6
    spin.stepDown(); assert spin.value() == 5
    T.QTest.keyClick(combo, C.Qt.Key_Down); assert combo.currentIndex() == 1
    combo.showPopup(); app.processEvents()
    view = combo.view(); bar = view.verticalScrollBar(); bar.setValue(0)
    wheel(app, view.viewport())
    assert bar.value() > 0
    assert combo.currentIndex() == 1
    combo.hidePopup()


def test_table_cell_controls_scroll_table_without_editing_values(surface):
    app, dialog, area, layout = surface
    table = W.QTableWidget(100, 2)
    combo = W.QComboBox(); combo.addItems(['inherit', 'base_color', 'normal']); combo.setCurrentIndex(1)
    table.setCellWidget(0, 0, combo); layout.addWidget(table)
    dialog.show(); app.processEvents()
    outer_before = area.verticalScrollBar().value()
    wheel(app, combo)
    assert combo.currentIndex() == 1
    assert table.verticalScrollBar().value() > 0
    assert area.verticalScrollBar().value() == outer_before


def test_scrollbar_wheel_is_not_blocked(surface):
    app, dialog, area, layout = surface
    layout.addWidget(W.QLabel('scroll'))
    dialog.show(); app.processEvents()
    bar = area.verticalScrollBar(); bar.setValue(100)
    wheel(app, bar)
    assert bar.value() > 100
