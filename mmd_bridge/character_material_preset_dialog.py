"""Review every source-to-target slot before applying a disk material snapshot."""
from PySide6 import QtWidgets as W

from .character_material_preset import match_slots


class MaterialPresetMappingDialog(W.QDialog):
    def __init__(self, preset, target, parent=None):
        super().__init__(parent)
        self.setWindowTitle('角色材质配置 · 核对槽位对应')
        self.resize(920, 580)
        self.preset, self.target, self.mapping = preset, target, {}
        layout = W.QVBoxLayout(self)
        label = W.QLabel(target['label'] + '\n' + target['origin'] + '\n只覆盖下表指定的材质槽，保留网格、骨架和动画。')
        label.setWordWrap(True)
        layout.addWidget(label)
        table = W.QTableWidget(len(preset['slots']), 3)
        table.setHorizontalHeaderLabels(['配置中的原槽', '母材质', '覆盖到目标槽'])
        table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, W.QHeaderView.Stretch)
        table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.choices = []
        for index, row in enumerate(preset['slots']):
            table.setItem(index, 0, W.QTableWidgetItem(f"{row['index']} · {row.get('source_name') or row.get('slot_name', '')}"))
            material = preset['materials'].get(row['material'], {})
            table.setItem(index, 1, W.QTableWidgetItem(material.get('root', '空材质槽')))
            combo = W.QComboBox()
            combo.addItem('请选择对应目标槽', None)
            for entry in target['slots']:
                combo.addItem(f"{entry['index']} · {entry.get('source_name') or entry['slot_name']}", entry['index'])
            try:
                suggested = match_slots([row], target['slots'])[0]['target']
                combo.setCurrentIndex(combo.findData(suggested))
            except ValueError:
                pass
            table.setCellWidget(index, 2, combo)
            self.choices.append(combo)
        layout.addWidget(table)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Ok).setText('应用配置')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        mapping = {str(row['index']): combo.currentData()
                   for row, combo in zip(self.preset['slots'], self.choices)}
        if any(value is None for value in mapping.values()):
            W.QMessageBox.warning(self, '槽位对应', '还有未指定的目标槽，尚未应用任何材质。')
            return
        try:
            match_slots(self.preset['slots'], self.target['slots'], mapping=mapping)
        except ValueError as exc:
            W.QMessageBox.warning(self, '槽位对应', str(exc))
            return
        self.mapping = mapping
        super().accept()
