"""One aggregated, editable selection for every character's original slots."""
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from pathlib import Path


class GeometrySelectionDialog(W.QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle('局部一级细分 · 补选部件')
        self.resize(1100, 680)
        self.report = report; self.items = []
        layout = W.QVBoxLayout(self)
        missing = '；'.join(Path(m['model']).name+'：'+'、'.join(m.get('missing_regions',[]))
                           for m in report['models'] if m.get('missing_regions'))
        hint = W.QLabel(('未单独识别：'+missing+'。这些区域可能与其它部件合并。' if missing else '')+
                        '请确认需要一级细分的部件；确认时未勾选的疑问项将明确排除。其它未识别部件不会单独触发弹窗。')
        hint.setWordWrap(True); layout.addWidget(hint)
        table = W.QTableWidget(sum(len(m['slots']) for m in report['models']), 6)
        table.setHorizontalHeaderLabels(['一级细分', '角色', '原槽', '部件', '自动判断', '依据'])
        table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(5, W.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table); self.table = table
        roles = {'body':'身体','face':'面部／嘴唇','neck':'颈部','legwear':'贴腿软层',
                 'close_fitting':'贴身软层','attached_layer':'附属贴层','excluded':'排除','unknown':'待确认'}
        number = 0
        for model in report['models']:
            for slot in model['slots']:
                check = W.QTableWidgetItem()
                check.setFlags(C.Qt.ItemFlag.ItemIsEnabled | C.Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(C.Qt.CheckState.Checked if slot['selected'] else C.Qt.CheckState.Unchecked)
                if not slot['used']: check.setFlags(C.Qt.ItemFlag.NoItemFlags)
                table.setItem(number, 0, check)
                values = [Path(model['model']).name,str(slot['pmx_index']),slot['name'],
                          roles[slot['role']], '；'.join(slot['reasons'])]
                for column, value in enumerate(values,1):
                    item = W.QTableWidgetItem(value); item.setToolTip(value)
                    if slot.get('warning',slot['pending']): item.setForeground(G.QColor('#d4a72c'))
                    table.setItem(number,column,item)
                self.items.append((model['sha256'],slot['pmx_index'],check)); number += 1
        table.resizeColumnsToContents()
        buttons = W.QDialogButtonBox()
        buttons.addButton('按当前选择继续', W.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton('取消任务', W.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selections(self):
        result = {m['sha256']: [] for m in self.report['models']}
        for digest, index, item in self.items:
            if item.checkState() == C.Qt.CheckState.Checked: result[digest].append(index)
        return result
