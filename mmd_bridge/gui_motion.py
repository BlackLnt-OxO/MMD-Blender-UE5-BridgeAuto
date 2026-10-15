from PySide6 import QtCore as C, QtGui as G, QtWidgets as W


class SectionClip(W.QWidget):
    def __init__(self, content):
        super().__init__()
        self.content = content
        content.setParent(self)

    def sizeHint(self):
        hint = self.content.sizeHint()
        hint.setHeight(max(hint.height(), self.content.heightForWidth(self.width())))
        return hint

    def minimumSizeHint(self):
        hint = self.content.minimumSizeHint()
        hint.setHeight(0)
        return hint

    def resizeEvent(self, event):
        self.content.setGeometry(0, 0, self.width(), max(self.height(), self.sizeHint().height()))
        super().resizeEvent(event)


class MotionEffects(C.QObject):
    """Short, interruptible transitions; Qt's desktop animation setting is respected."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.enabled = W.QApplication.isEffectEnabled(C.Qt.UI_General)
        self.animations = {}
        self.button_colors = {}
        self.effects = {}
        self.popups = {}
        self.scrollbars = {}
        self.prepare_widgets(window)
        window.tabs.currentChanged.connect(self.fade_page)

    def prepare_widgets(self, root):
        for button in root.findChildren(W.QPushButton) + root.findChildren(W.QToolButton):
            button.installEventFilter(self)
        for combo in root.findChildren(W.QComboBox):
            self.prepare_combo(combo)
        for area in root.findChildren(W.QScrollArea):
            self.prepare_scrollbar(area.verticalScrollBar())

    def prepare_scrollbar(self, bar):
        if bar.property('fadingScrollbar'):
            return
        bar.setProperty('fadingScrollbar', True)
        effect = W.QGraphicsOpacityEffect(bar)
        effect.setOpacity(.28)
        bar.setGraphicsEffect(effect)
        idle = C.QTimer(bar)
        idle.setObjectName('scrollbarIdle')
        idle.setSingleShot(True)
        idle.setInterval(700)

        def reveal():
            self.animate(bar, 'scrollbar', effect.opacity(), 1., 120, effect.setOpacity)
            idle.start()

        def fade():
            if not bar.isSliderDown() and not bar.property('scrollbarHovered'):
                self.animate(bar, 'scrollbar', effect.opacity(), .28, 180, effect.setOpacity)

        bar.valueChanged.connect(reveal)
        bar.actionTriggered.connect(reveal)
        bar.sliderPressed.connect(reveal)
        bar.sliderReleased.connect(reveal)
        idle.timeout.connect(fade)
        self.scrollbars[bar] = reveal
        bar.installEventFilter(self)
        bar.destroyed.connect(lambda: self.scrollbars.pop(bar, None))

    def prepare_combo(self, combo):
        if combo.property('styledPopup'):
            return
        combo.setProperty('styledPopup', True)
        # Use the styled item view instead of Fusion's native menu delegate.
        combo.setView(W.QListView(combo))
        popup = combo.view().window()
        self.popups[popup] = combo
        popup.installEventFilter(self)
        combo.destroyed.connect(lambda: self.popups.pop(popup, None))

    def animate(self, target, kind, start, end, duration, update, finished=None):
        if not target.property('motionTracked'):
            target.setProperty('motionTracked', True)
            target.destroyed.connect(lambda: self.forget(target))
        key = (target, kind)
        previous = self.animations.pop(key, None)
        if previous:
            previous.stop()
            previous.deleteLater()
        if not self.enabled or start == end:
            update(end)
            if finished:
                finished()
            return
        animation = C.QVariantAnimation(self)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(duration)
        animation.setEasingCurve(C.QEasingCurve.OutCubic)
        animation.valueChanged.connect(update)

        def complete():
            self.animations.pop(key, None)
            if finished:
                finished()
            animation.deleteLater()

        animation.finished.connect(complete)
        self.animations[key] = animation
        update(start)
        animation.start()

    def forget(self, target):
        for key in [key for key in self.animations if key[0] is target]:
            animation = self.animations.pop(key)
            animation.stop()
            animation.deleteLater()
        self.button_colors.pop(target, None)
        self.effects.pop(target, None)

    def fade_page(self, index):
        page = self.window.tabs.widget(index).widget()
        if not self.enabled:
            return
        effect = self.effects.get(page)
        if effect is None:
            effect = W.QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(effect)
            self.effects[page] = effect
        self.animate(page, 'page', .72, 1., 140, effect.setOpacity)

    def fold(self, clip, content, expanded):
        start = clip.height() if clip.isVisible() else 0
        clip.setFixedHeight(start)
        if expanded:
            clip.show()
            content.ensurePolished()
            for field in content.findChildren(W.QWidget):
                field.ensurePolished()
                field.updateGeometry()
            content.layout().invalidate()
            content.layout().activate()
        end = clip.sizeHint().height() if expanded else 0
        group = clip.parentWidget()
        page = group.parentWidget()
        scroll = page.parentWidget().parentWidget()
        bar = scroll.verticalScrollBar()
        anchor = group.mapTo(scroll.viewport(), C.QPoint()).y()

        def update(height):
            clip.setFixedHeight(height)
            group.layout().invalidate()
            group.layout().activate()
            page.layout().invalidate()
            page.layout().activate()
            # Update the scroll range each frame, so a shorter page cannot jump at the end.
            page.resize(page.width(), max(scroll.viewport().height(), page.minimumSizeHint().height()))
            top = group.mapTo(scroll.viewport(), C.QPoint()).y()
            if expanded:
                overflow = top + group.height() - scroll.viewport().height() + 12
                # Reveal new fields, keeping the header visible for groups taller than the viewport.
                bar.setValue(bar.value() + min(max(0, overflow), max(0, top - 12)))
            else:
                bar.setValue(bar.value() + top - anchor)

        def complete():
            clip.setVisible(expanded)
            clip.setMinimumHeight(0)
            clip.setMaximumHeight(16777215)
            clip.updateGeometry()
            form = clip.parentWidget().layout()
            form.invalidate()
            form.activate()
            group = clip.parentWidget()
            group.updateGeometry()
            group.parentWidget().layout().invalidate()
            group.parentWidget().layout().activate()

        self.animate(clip, 'fold', start, end, 160, update, complete)

    def eventFilter(self, widget, event):
        kind = event.type()
        if widget in self.scrollbars:
            if kind in (C.QEvent.Enter, C.QEvent.Leave):
                widget.setProperty('scrollbarHovered', kind == C.QEvent.Enter)
                self.scrollbars[widget]()
            return False
        if kind not in (C.QEvent.Polish, C.QEvent.Show, C.QEvent.Hide, C.QEvent.EnabledChange,
                        C.QEvent.Enter, C.QEvent.Leave, C.QEvent.MouseButtonPress, C.QEvent.MouseButtonRelease):
            return False
        if widget in self.popups:
            if kind == C.QEvent.Show:
                combo = self.popups[widget]
                index = combo.model().index(combo.currentIndex(), combo.modelColumn(), combo.rootModelIndex())
                combo.view().selectionModel().select(index, C.QItemSelectionModel.ClearAndSelect)
                if self.enabled:
                    self.animate(widget, 'popup', .2, 1., 140, widget.setWindowOpacity)
            elif kind == C.QEvent.Hide:
                self.animate(widget, 'popup', 1., 1., 0, widget.setWindowOpacity)
            return False
        if not isinstance(widget, (W.QPushButton, W.QToolButton)):
            return False
        if kind == C.QEvent.EnabledChange and not widget.isEnabled():
            previous = self.animations.pop((widget, 'button'), None)
            if previous:
                previous.stop()
                previous.deleteLater()
            self.button_colors.pop(widget, None)
            widget.setStyleSheet('')
            return False
        if not self.enabled or not widget.isEnabled():
            return False
        if kind not in (C.QEvent.Enter, C.QEvent.Leave, C.QEvent.MouseButtonPress, C.QEvent.MouseButtonRelease):
            return False
        if kind in (C.QEvent.MouseButtonPress, C.QEvent.MouseButtonRelease) and event.button() != C.Qt.LeftButton:
            return False
        primary = (widget.objectName() in ('primary', 'workflow')
                   or isinstance(widget, W.QPushButton) and widget.isDefault())
        normal, hover, pressed = ('#e0e1e6', '#f1f1f4', '#c4c7cf') if primary else (
            ('#28292d', '#33353c', '#3e4047') if isinstance(widget, W.QToolButton)
            else ('#303136', '#3b3d44', '#494b53'))
        color = pressed if kind == C.QEvent.MouseButtonPress else (
            normal if kind == C.QEvent.Leave or (kind == C.QEvent.MouseButtonRelease and not widget.underMouse()) else hover)

        def apply(value):
            self.button_colors[widget] = value
            widget.setStyleSheet('background-color:' + value.name() + ';')

        def complete():
            if color == normal:
                widget.setStyleSheet('')

        self.animate(widget, 'button', self.button_colors.get(widget, G.QColor(normal)),
                     G.QColor(color), 70 if kind == C.QEvent.MouseButtonPress else 140, apply, complete)
        return False
