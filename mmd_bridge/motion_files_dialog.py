"""Ordered supplementary motion selection for a single character."""
from pathlib import Path

from PySide6 import QtCore as C, QtWidgets as W

from .core import inspect_vmd


class MotionFilesField(W.QLineEdit):
    pathsChanged = C.Signal()

    def __init__(self):
        super().__init__()
        self._paths = []
        self._setting_paths = False
        self.textChanged.connect(self._edited)

    def _edited(self, text):
        if not self._setting_paths:
            self._paths = [text.strip()] if text.strip() else []
            self.setToolTip('\n'.join(self._paths))
            self.pathsChanged.emit()

    def paths(self):
        return list(self._paths)

    def set_paths(self, paths):
        paths = list(paths)
        if paths == self._paths:
            return
        self._paths = paths
        self._setting_paths = True
        try:
            self.setText(paths[0] if paths else '')
        finally:
            self._setting_paths = False
        self.setToolTip('\n'.join(f'{index}. {path}' for index, path in enumerate(paths, 1)))
        self.pathsChanged.emit()


class MotionFilesDialog(W.QDialog):
    def __init__(self, paths, folder='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('补充动作 VMD · 顺序与移除')
        self.resize(720, 420)
        self.folder = folder
        layout = W.QVBoxLayout(self)
        rule = W.QLabel('主动作先加载，再从上到下应用补充动作。\n'
                        '同名骨骼／表情整条轨道以后面的文件为准；未包含的轨道保留。')
        rule.setWordWrap(True)
        layout.addWidget(rule)
        self.files = W.QListWidget()
        self.files.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.files)
        actions = W.QHBoxLayout()
        for label, handler in (('添加…', self.add_files), ('移除选中', self.remove_selected),
                               ('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1))):
            button = W.QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        layout.addLayout(actions)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.append_paths(paths)

    def append_paths(self, paths):
        for path in paths:
            details = ''
            try:
                info = inspect_vmd(path)
                details = ' · ' + '／'.join(f'{label} {info["tracks"][key]}'
                                           for key, label in (('bone', '骨骼'), ('morph', '表情'), ('ik', 'IK'))
                                           if info['tracks'].get(key))
            except (OSError, ValueError):
                details = ' · 无法读取，请检查文件'
            item = W.QListWidgetItem(Path(path).name + details)
            item.setData(C.Qt.UserRole, path)
            item.setToolTip(path)
            self.files.addItem(item)

    def add_files(self):
        current = self.paths()
        paths, _ = W.QFileDialog.getOpenFileNames(self, '添加补充动作 VMD',
            str(Path(current[-1]).parent) if current else self.folder, 'VMD (*.vmd)')
        self.append_paths(paths)

    def remove_selected(self):
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))

    def move(self, direction):
        rows = sorted(self.files.row(item) for item in self.files.selectedItems())
        if not rows or rows[0] + direction < 0 or rows[-1] + direction >= self.files.count():
            return
        for row in rows if direction < 0 else reversed(rows):
            item = self.files.takeItem(row)
            self.files.insertItem(row + direction, item)
            item.setSelected(True)

    def paths(self):
        return [self.files.item(row).data(C.Qt.UserRole) for row in range(self.files.count())]
