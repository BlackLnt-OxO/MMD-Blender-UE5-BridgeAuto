"""Custom-mapping page inside the existing character analysis dialog."""
import copy
from pathlib import Path

from PySide6 import QtWidgets as W

from .core import atomic_json, read_json
from .character_custom_materials import (SOURCES, model_entry, selection, asset_path,
    plan, validate, export_template, import_template)


class CustomMappingPage:
    def __init__(self, dialog, snapshot, settings):
        self.dialog, self.snapshot = dialog, snapshot
        self.cfg = copy.deepcopy(settings)
        self.data = dialog.custom_materials
        self.cfg['character_custom_materials'] = self.data
        self.entry = model_entry(self.data, snapshot['sha256'], self.cfg, create=True)
        self.entry.setdefault('default_parents', {'character': settings.get('character_master_material', ''),
                                                 'eye': settings.get('eye_master_material', '')})
        self.current, self.active_parent, self.scope_value, self.loading = None, '', 'common', False
        dialog.setWindowTitle('角色 · 分析材质与自选母材质映射')
        dialog.resize(1200, 850)
        layout = W.QVBoxLayout(dialog)
        hint = W.QLabel('贴图仅在明确映射后连接；未指定时继承 UE 父材质默认。数值仅取 PMX 原始数据。自选映射与内置 PBR 设置相互独立。')
        hint.setWordWrap(True); layout.addWidget(hint)
        split = W.QSplitter(); layout.addWidget(split, 1)
        left = W.QWidget(); ll = W.QVBoxLayout(left); split.addWidget(left)
        search = W.QLineEdit(); search.setPlaceholderText('筛选原槽序号或名称'); ll.addWidget(search)
        self.slots = W.QListWidget(); self.slots.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        ll.addWidget(self.slots); dialog.slots = self.slots
        for row in snapshot['materials']:
            self.slots.addItem(str(row['pmx_index']) + ' · ' + row['name'])
        search.textChanged.connect(lambda text: [self.slots.item(i).setHidden(text.casefold() not in self.slots.item(i).text().casefold())
                                                 for i in range(self.slots.count())])
        right = W.QWidget(); rl = W.QVBoxLayout(right); split.addWidget(right)
        form = W.QFormLayout(); rl.addLayout(form)
        self.defaults = {}
        for key, title in (('character', '本 PMX 默认父材质'), ('eye', '本 PMX 眼睛父材质')):
            field = W.QLineEdit(self.entry['default_parents'].get(key, ''))
            field.setPlaceholderText('/Game/...；眼睛留空沿用角色默认')
            form.addRow(title, field); self.defaults[key] = field
            field.editingFinished.connect(lambda: self.guard(self.change_defaults))
        self.override = W.QCheckBox('此槽单独选择父材质')
        self.parent_path = W.QLineEdit(); form.addRow(self.override, self.parent_path)
        self.override.toggled.connect(lambda _: self.guard(self.change_parent))
        self.parent_path.editingFinished.connect(lambda: self.guard(self.change_parent))
        row = W.QHBoxLayout(); rl.addLayout(row)
        self.read_button = W.QPushButton('读取全部使用的 UE 母材质参数'); row.addWidget(self.read_button)
        self.read_button.clicked.connect(lambda: self.guard(self.read_parameters))
        same = W.QPushButton('将当前父材质用于选中槽'); row.addWidget(same)
        same.clicked.connect(lambda: self.guard(self.assign_parent))
        tabs = W.QTabWidget(); rl.addWidget(tabs, 1)
        maps = W.QWidget(); ml = W.QVBoxLayout(maps); tabs.addTab(maps, 'Texture Parameter 映射')
        self.scope = W.QComboBox()
        self.scope.addItem('同一母材质的统一规则', 'common'); self.scope.addItem('当前原槽的独立覆盖', 'slot')
        ml.addWidget(self.scope); self.scope.currentIndexChanged.connect(lambda _: self.guard(self.change_scope))
        self.table = W.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['目标参数', 'UE 默认贴图', '来源类型', '通道', '反转', '用途'])
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setStyleSheet('QComboBox { padding:2px 6px; min-height:24px; }')
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.Stretch)
        for column, width in ((2, 170), (3, 76), (4, 52), (5, 142)):
            self.table.horizontalHeader().setSectionResizeMode(column, W.QHeaderView.Fixed)
            self.table.setColumnWidth(column, width)
        ml.addWidget(self.table)
        sources = W.QWidget(); sf = W.QFormLayout(sources); tabs.addTab(sources, 'PMX 来源与补选')
        self.source_info = W.QPlainTextEdit(); self.source_info.setReadOnly(True); self.source_info.setMaximumHeight(190)
        sf.addRow(self.source_info)
        self.source_fields = {}
        for semantic, title in SOURCES.items():
            box = W.QWidget(); line = W.QHBoxLayout(box); line.setContentsMargins(0, 0, 0, 0)
            field = W.QLineEdit(); button = W.QPushButton('…'); button.setMaximumWidth(28)
            line.addWidget(field); line.addWidget(button); sf.addRow(title, box)
            self.source_fields[semantic] = field
            button.clicked.connect(lambda _=False, key=semantic: self.browse_source(key))
            field.editingFinished.connect(lambda: self.guard(self.refresh_preview))
        self.preview = W.QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(175)
        rl.addWidget(self.preview)
        split.setSizes([280, 900])
        actions = W.QHBoxLayout(); layout.addLayout(actions)
        for title, callback in (('预览全部槽', self.preview_all), ('导出映射…', self.export), ('载入映射…', self.load),
                                ('应用选中槽到 UE', self.apply), ('撤销上次自选材质应用', self.undo)):
            button = W.QPushButton(title); actions.addWidget(button)
            button.clicked.connect(lambda _=False, fn=callback: self.guard(fn))
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Save | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Save).setText('保存自选映射')
        buttons.accepted.connect(lambda: self.guard(self.save)); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.slots.currentRowChanged.connect(lambda i: self.guard(lambda: self.select(i)))
        self.slots.setCurrentRow(0)

    def guard(self, fn):
        try:
            return fn()
        except (ValueError, OSError) as exc:
            W.QMessageBox.warning(self.dialog, '自选材质映射', str(exc))

    def source(self):
        return self.snapshot['materials'][self.current]

    def slot(self):
        return self.entry['slots'].setdefault(str(self.source()['pmx_index']), {})

    def store(self):
        if self.loading or self.current is None:
            return
        slot = self.slot()
        sources = {}
        for semantic, field in self.source_fields.items():
            if field.text().strip():
                sources[semantic] = {'path': field.text().strip()}
        slot['sources'] = sources
        values = {}
        for name, source, channel, invert, space in getattr(self, 'fields', []):
            semantic = source.currentData()
            if semantic == 'inherit':
                continue
            if semantic is None:
                if self.scope_value == 'slot':
                    values[name] = None
            else:
                values[name] = {'source': semantic, 'channel': channel.currentText(),
                                'invert': invert.isChecked(), 'color_space': space.currentData()}
        if self.active_parent:
            if self.scope_value == 'common':
                self.entry['parents'].setdefault(self.active_parent, {})['textures'] = values
            else:
                slot.setdefault('parent_textures', {})[self.active_parent] = values

    def select(self, index):
        if index < 0:
            return
        self.store(); self.current = index; self.populate()

    def populate(self):
        self.loading = True
        source = self.source()
        parent, _, slot, _ = selection(self.cfg, source)
        self.active_parent = parent
        self.override.setChecked(bool(slot.get('parent')))
        self.parent_path.setText(parent); self.parent_path.setEnabled(self.override.isChecked())
        for semantic, field in self.source_fields.items():
            field.setText(slot.get('sources', {}).get(semantic, {}).get('path', ''))
            spec = source['sources'].get(semantic, {})
            field.setPlaceholderText(spec.get('path') or 'PMX 未引用；可手选并声明用途')
        lines = [f"原槽 {source['pmx_index']} · {source['name']} · PMX 透明度 {source['diffuse'][3]}",
                 'Sphere 模式：' + str(source['sphere_mode'])]
        for semantic, spec in source['sources'].items():
            lines.append(SOURCES[semantic] + ' → ' + (spec.get('reference') or '未引用') + ' · ' + spec['status'])
        self.source_info.setPlainText('\n'.join(lines))
        self.populate_table(); self.loading = False; self.refresh_preview()

    def populate_table(self):
        common = self.entry['parents'].get(self.active_parent, {})
        inventory = common.get('inventory', {})
        slot = self.slot()
        values = (common.get('textures', {}) if self.scope_value == 'common' else
                  slot.get('parent_textures', {}).get(self.active_parent, {}))
        textures = [p for p in inventory.get('parameters', []) if p['kind'] == 'texture']
        known = {p['name'] for p in textures}
        textures += [{'name': name, 'kind': 'texture', 'supported': False,
                      'reason': '目标不存在或尚未读取，请重新读取参数或选择保留默认'}
                     for name in values if name not in known]
        self.table.setRowCount(len(textures)); self.fields = []
        for index, parameter in enumerate(textures):
            name = parameter['name']; binding = values.get(name)
            self.table.setItem(index, 0, W.QTableWidgetItem(name))
            self.table.item(index, 0).setToolTip(parameter.get('reason') or '采样用途：' + str(parameter.get('color_space') or '由用户指定'))
            self.table.setItem(index, 1, W.QTableWidgetItem(parameter.get('default') or '无默认图'))
            source = W.QComboBox()
            if self.scope_value == 'slot': source.addItem('继承统一映射', 'inherit')
            source.addItem('保留 UE 默认', None)
            for key, label in SOURCES.items(): source.addItem(label, key)
            selected = binding['source'] if binding else 'inherit' if self.scope_value == 'slot' and name not in values else None
            source.setCurrentIndex(source.findData(selected))
            channel = W.QComboBox(); channel.addItems(['rgb', 'r', 'g', 'b', 'a'])
            channel.setCurrentText((binding or {}).get('channel', 'rgb'))
            invert = W.QCheckBox(); invert.setChecked((binding or {}).get('invert', False))
            space = W.QComboBox()
            for key, label in (('srgb', '颜色 sRGB'), ('data', '数据 Linear'), ('normal', '法线')): space.addItem(label, key)
            space.setCurrentIndex(space.findData((binding or {}).get('color_space', 'srgb')))
            for column, widget in ((2, source), (3, channel), (4, invert), (5, space)):
                self.table.setCellWidget(index, column, widget)
                widget.setEnabled(parameter.get('supported', True)); widget.setToolTip(parameter.get('reason', ''))
            if not parameter.get('supported', True):
                source.setEnabled(True)
                for index_item in range(source.count()):
                    if source.itemData(index_item) not in (None, 'inherit'):
                        source.model().item(index_item).setEnabled(False)
            self.fields.append((name, source, channel, invert, space))
            def changed(_=None, s=source, c=channel, color=space):
                if self.loading: return
                semantic = s.currentData()
                if semantic not in (None, 'inherit'):
                    color.setCurrentIndex(color.findData('normal' if semantic == 'normal' else
                        'srgb' if semantic in ('base_color', 'sphere', 'toon', 'emission') else 'data'))
                self.refresh_preview()
            source.currentIndexChanged.connect(changed)
            channel.currentIndexChanged.connect(lambda _: self.refresh_preview())
            invert.toggled.connect(lambda _: self.refresh_preview())
            space.currentIndexChanged.connect(lambda _: self.refresh_preview())

    def refresh_preview(self):
        if self.loading or self.current is None: return
        self.store()
        try:
            note = plan(self.cfg, self.source())
            lines = [name + ' ← ' + SOURCES[spec['source']] + '.' + spec.get('channel', 'rgb') + ' · ' + spec['path']
                     for name, spec in note['textures'].items()]
            lines += [name + ' ← PMX ' + value['source'] + ' = ' + str(value['value']) for name, value in note['numeric'].items()]
            lines += [row['parameter'] + '：该槽无启用的 ' + row['source'] + '，继承默认' for row in note['retained_defaults']]
            lines += ['数值参数有歧义，保持默认：' + str(note['ambiguous_numeric'])] if note['ambiguous_numeric'] else []
            self.preview.setPlainText('\n'.join(lines) or '没有贴图或数值覆盖，全部继承父材质默认。')
        except ValueError as exc:
            self.preview.setPlainText(str(exc))

    def change_scope(self):
        if self.loading: return
        self.store(); self.scope_value = self.scope.currentData(); self.populate()

    def change_defaults(self):
        self.store()
        self.entry['default_parents'] = {k: asset_path(v.text()) if v.text().strip() else '' for k, v in self.defaults.items()}
        self.populate()

    def change_parent(self):
        if self.loading: return
        self.store()
        if self.override.isChecked(): self.slot()['parent'] = asset_path(self.parent_path.text())
        else: self.slot().pop('parent', None)
        self.populate()

    def selected_sources(self):
        return [self.snapshot['materials'][self.slots.row(item)] for item in self.slots.selectedItems()]

    def assign_parent(self):
        self.store(); parent = asset_path(self.parent_path.text())
        for source in self.selected_sources():
            self.entry['slots'].setdefault(str(source['pmx_index']), {})['parent'] = parent
        self.populate()

    def browse_source(self, semantic):
        path, _ = W.QFileDialog.getOpenFileName(self.dialog, '选择来源贴图', str(Path(self.snapshot['model']).parent),
            '图片 (*.png *.jpg *.jpeg *.tga *.bmp *.exr *.hdr *.tif *.tiff *.dds *.sph *.spa)')
        if path:
            self.source_fields[semantic].setText(path); self.guard(self.refresh_preview)

    def host(self):
        host = self.dialog.parent()
        if host is None or not hasattr(host, 'controller'):
            raise ValueError('请从桌面程序打开此窗口，并连接目标 UE 工程')
        return host

    def read_parameters(self):
        self.store(); host = self.host()
        parents = sorted(set(selection(self.cfg, row)[0] for row in self.snapshot['materials']))
        def received(result):
            self.store()
            for value in result['inventories']:
                self.entry['parents'].setdefault(value['parent'], {})['inventory'] = value
            self.populate()
        host.execute(lambda: host.controller.material_surface_command('custom_inventory',
                     project=self.cfg.get('project'), parents=parents), received)

    def preview_all(self):
        self.store(); lines = []
        for row in self.snapshot['materials']:
            try:
                note = plan(self.cfg, row)
                lines.append(str(row['pmx_index']) + ' · ' + row['name'] + ' → ' + note['parent'] +
                             f" · {len(note['textures'])} 个贴图覆盖，{len(note['numeric'])} 个数值")
            except ValueError as exc:
                lines.append(str(row['pmx_index']) + ' · ' + str(exc))
        self.preview.setPlainText('\n'.join(lines))

    def export(self):
        self.store()
        path, _ = W.QFileDialog.getSaveFileName(self.dialog, '导出类型映射规则', '', 'JSON (*.json)')
        if path: atomic_json(path, export_template(self.entry))

    def load(self):
        path, _ = W.QFileDialog.getOpenFileName(self.dialog, '手动载入映射规则', '', 'JSON (*.json)')
        if not path: return
        self.store(); value = import_template(self.entry, read_json(path))
        self.entry.clear(); self.entry.update(value)
        for key, field in self.defaults.items(): field.setText(self.entry['default_parents'].get(key, ''))
        self.populate(); self.preview_all()

    def save(self):
        self.store(); validate(self.data)
        for row in self.snapshot['materials']: plan(self.cfg, row)
        self.dialog.accept()

    def apply(self):
        self.store(); validate(self.data); sources = self.selected_sources()
        if not sources: raise ValueError('请选择需要应用的原材质槽')
        for row in sources: plan(self.cfg, row)
        host = self.host()
        settings, sources = copy.deepcopy(self.cfg), copy.deepcopy(sources)
        def received(result):
            host.log.appendPlainText('自选材质应用：' + str(result['count']) + ' 个槽 · ' + result.get('status', 'complete'))
            for warning in result.get('warnings', []):
                host.log.appendPlainText('需复核：' + warning)
        host.execute(lambda: host.controller.apply_custom_materials(settings, sources),
                     received)

    def undo(self):
        host = self.host()
        host.execute(lambda: host.controller.material_surface_command('custom_undo', project=self.cfg.get('project')),
            lambda result: host.log.appendPlainText('自选材质已撤销：' + str(result['count']) + ' 个槽。'))
