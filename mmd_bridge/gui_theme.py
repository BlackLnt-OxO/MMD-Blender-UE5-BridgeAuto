"""Apply the application palette to native window frames as well as Qt dialogs."""
import ctypes

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W


class HoverTip(W.QLabel):
    def __init__(self):
        super().__init__(None, C.Qt.ToolTip | C.Qt.FramelessWindowHint)
        self.setObjectName('appTooltip')
        self.setAttribute(C.Qt.WA_TranslucentBackground)
        self.setAttribute(C.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(C.Qt.WA_ShowWithoutActivating)
        self.setWordWrap(True)
        self.source = None
        self.animation = C.QPropertyAnimation(self, b'windowOpacity', self)
        self.animation.setDuration(140)
        self.animation.setEasingCurve(C.QEasingCurve.OutCubic)
        self.animation.finished.connect(self.finish)
        self.expiry = C.QTimer(self)
        self.expiry.setSingleShot(True)
        self.expiry.timeout.connect(self.dismiss)

    def paintEvent(self, event):
        # A translucent top-level QLabel does not paint the stylesheet's panel background.
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.Antialiasing)
        painter.setBrush(G.QColor('#303136'))
        painter.setPen(G.QPen(G.QColor('#53565f'), 1))
        painter.drawRoundedRect(C.QRectF(self.rect()).adjusted(.5,.5,-.5,-.5), 10, 10)
        painter.end()
        super().paintEvent(event)

    def present(self, text, position, source):
        self.source = source
        self.setText(text)
        self.ensurePolished()
        screen = W.QApplication.screenAt(position) or source.screen()
        bounds = screen.availableGeometry().adjusted(8, 8, -8, -8)
        natural = max(self.fontMetrics().horizontalAdvance(line) for line in text.splitlines()) + 28
        width = min(480, max(100, natural), bounds.width())
        self.setFixedWidth(width)
        self.resize(width, self.heightForWidth(width))
        x = min(max(position.x()+12, bounds.left()), bounds.right()-self.width()+1)
        y = position.y()+18
        if y+self.height() > bounds.bottom():
            y = position.y()-self.height()-12
        self.move(x, max(bounds.top(), y))
        if not self.isVisible():
            self.setWindowOpacity(0)
        self.show()
        self.fade(1.)
        duration = source.toolTipDuration()
        self.expiry.start(duration if duration > 0 else max(10000, len(text)*35))

    def fade(self, target):
        self.animation.stop()
        self.animation.setStartValue(self.windowOpacity())
        self.animation.setEndValue(target)
        if W.QApplication.isEffectEnabled(C.Qt.UI_General):
            self.animation.start()
        else:
            self.setWindowOpacity(target)
            self.finish()

    def dismiss(self):
        self.expiry.stop()
        if self.isVisible() and self.animation.endValue() != 0.:
            self.fade(0.)

    def finish(self):
        if self.animation.endValue() == 0.:
            self.hide()
            self.source = None


class WindowFrames(C.QObject):
    def eventFilter(self, widget, event):
        kind = event.type()
        if kind not in (C.QEvent.Show, C.QEvent.ToolTip, C.QEvent.MouseButtonPress,
                        C.QEvent.KeyPress, C.QEvent.Wheel, C.QEvent.WindowDeactivate,
                        C.QEvent.Leave, C.QEvent.Hide):
            return False
        if not isinstance(widget, W.QWidget):
            return False
        tip = getattr(self, 'tooltip', None)
        if kind == C.QEvent.ToolTip:
            text = widget.toolTip()
            view = widget.parentWidget()
            if not text and isinstance(view, W.QAbstractItemView) and widget is view.viewport():
                text = view.indexAt(event.pos()).data(C.Qt.ToolTipRole) or ''
            if text:
                if tip is None:
                    self.tooltip = tip = HoverTip()
                tip.present(text, event.globalPos(), widget)
                return True
            if tip is not None:
                tip.dismiss()
        if tip is not None and tip.isVisible() and widget is not tip:
            if (kind in (C.QEvent.MouseButtonPress, C.QEvent.KeyPress, C.QEvent.Wheel, C.QEvent.WindowDeactivate)
                    or kind in (C.QEvent.Leave, C.QEvent.Hide) and widget is tip.source):
                tip.dismiss()
        control = widget.parentWidget() if isinstance(widget, W.QLineEdit) else widget
        if kind == C.QEvent.Wheel and (
                isinstance(control, (W.QAbstractSpinBox, W.QComboBox)) or
                isinstance(control, W.QAbstractSlider) and not isinstance(control, W.QScrollBar)):
            # Application-wide: includes late dialogs and transient table editors.
            # Forward explicitly; consuming a filtered wheel does not bubble it.
            parent = control.parentWidget()
            while parent is not None:
                if isinstance(parent, W.QAbstractScrollArea):
                    viewport = parent.viewport()
                    forwarded = G.QWheelEvent(C.QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                        event.globalPosition(), event.pixelDelta(), event.angleDelta(), event.buttons(),
                        event.modifiers(), event.phase(), event.inverted(), event.source(), event.pointingDevice())
                    W.QApplication.sendEvent(viewport, forwarded)
                    if forwarded.isAccepted():
                        break
                parent = parent.parentWidget()
            event.ignore()
            return True
        if kind != C.QEvent.Show or not widget.isWindow():
            return False
        owner = widget
        while owner.parentWidget() is not None:
            owner = owner.parentWidget()
        if hasattr(owner, 'motion') and widget.windowType() in (C.Qt.Window, C.Qt.Dialog):
            owner.motion.prepare_widgets(widget)
        if (widget.windowType() in (C.Qt.Window, C.Qt.Dialog)
                and W.QApplication.platformName() == 'windows'):
            # COLORREF uses BGR; retain the native caption buttons and resize handling.
            apply = ctypes.windll.dwmapi.DwmSetWindowAttribute
            apply.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            apply.restype = ctypes.c_long
            applied = True
            caption = 0x2d2928 if isinstance(widget, W.QMainWindow) else 0x242120
            for attribute, value in ((20, 1), (34, 0x4b4442), (35, caption), (36, 0xf0eeee)):
                color = ctypes.c_uint(value)
                applied &= apply(int(widget.winId()), attribute, ctypes.byref(color), ctypes.sizeof(color)) == 0
            widget.setProperty('nativeThemeApplied', applied)
        return False
