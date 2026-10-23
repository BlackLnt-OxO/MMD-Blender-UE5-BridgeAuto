"""Per-source PMX material overrides in the existing desktop application."""
import copy
from pathlib import Path

from PySide6 import QtWidgets as W, QtGui as G

from .character_pbr import (TYPES, PARAMETERS, SEMANTICS, DEFAULTS, validate_override,
                            parameter_defaults, plan, texture_files, analysis_record,
                            review_reasons, detail_map_names)


class CharacterPBRDialog(W.QDialog):
    def __init__(self, snapshot, settings, parent=None, *, missing_textures=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.settings_data = copy.deepcopy(settings)
        self.files = texture_files(snapshot['model']) if Path(snapshot['model']).is_file() else []
        self.scattering = settings.get('character_skin_scattering', 'legacy_v8')
        self.overrides = copy.deepcopy(settings.get('character_material_overrides', {}))
        self.entries = self.overrides.setdefault(snapshot['sha256'], {})
        self.geometry_overrides = copy.deepcopy(settings.get('character_geometry_overrides', {}))
        self.geometry_entries = self.geometry_overrides.setdefault(snapshot['sha256'], {})
        self.current = None
        self.loading = False
        self.texture_only = settings.get('character_material_profile','character_pbr') != 'character_pbr'
        self.missing_textures = missing_textures or []
        self.continue_with_missing = False
        self.setWindowTitle('角色 · 分析材质与补选贴图' if self.texture_only or self.missing_textures else '内置PBR角色 · 分析与配置材质')
        self.resize(1120, 680 if self.texture_only else 830)
        layout = W.QVBoxLayout(self)
        summary = W.QLabel(Path(snapshot['model']).name + ' · ' + str(len(snapshot['materials'])) +
                           ' 个材质槽 · ' + str(snapshot['texture_count']) + ' 张附带贴图')
        layout.addWidget(summary)
        if self.missing_textures:
            missing = W.QLabel('原PMX存在缺图，请选中对应槽位补选文件。\n'+'\n'.join(
                '原槽 '+str(r['pmx_index'])+' · '+r['name']+' · '+{'base_color':'基础色','sphere':'Sphere／MatCap'}.get(r['semantic'],r['semantic'])+' · '+r['reference']+' · 透明度 '+str(r['alpha'])
                for r in self.missing_textures))
            missing.setWordWrap(True);layout.addWidget(missing)
        hint = W.QLabel('黄色文字表示物理材质、资源或几何用途待确认；选中后可更改材质类型，自动沿用原贴图并匹配同名辅助图。自动透明分类在 Blender 准备时按实际 UV 计算；几何设置需新建任务重新导出。')
        hint.setWordWrap(True); layout.addWidget(hint)
        if self.texture_only:hint.setText('补选原PMX基础色与Sphere／MatCap；保持当前着色预设和原始透明度。映射仅用于转换副本。修改后请新建任务重新导出。')
        split = W.QSplitter(); layout.addWidget(split, 1)
        self.slots = W.QListWidget(); split.addWidget(self.slots)
        for index, row in enumerate(snapshot['materials']):
            self.slots.addItem('')
            self.update_slot(index, row['pbr'])
        scroll = W.QScrollArea(); scroll.setWidgetResizable(True); split.addWidget(scroll)
        content = W.QWidget(); form = W.QFormLayout(content); scroll.setWidget(content)
        self.form = form
        self.kind = W.QComboBox(); self.kind.addItem('自动识别', 'auto')
        for key, title in TYPES.items(): self.kind.addItem(title, key)
        form.addRow('物理材质类型', self.kind)
        self.geometry_local = W.QComboBox()
        for label, value in (('自动判断', 'auto'), ('纳入局部一级', 'include'), ('排除局部一级', 'exclude')):
            self.geometry_local.addItem(label, value)
        form.addRow('局部细分用途', self.geometry_local)
        self.geometry_host = W.QComboBox(); self.geometry_host.addItem('自动判断宿主', None)
        for row in snapshot['materials']:
            self.geometry_host.addItem(str(row['pmx_index'])+' · '+row['name'], row['pmx_index'])
        form.addRow('贴片宿主槽', self.geometry_host)
        geometry_hint = W.QLabel('几何设置用于局部一级细分，需新建任务重新导出；“应用材质到UE”不会修改网格。')
        geometry_hint.setWordWrap(True); form.addRow(geometry_hint)
        self.alpha = W.QComboBox()
        for label, key in (('自动分析', 'auto'), ('不透明', 'opaque'), ('镂空遮罩', 'masked'), ('半透明', 'translucent')):
            self.alpha.addItem(label, key)
        form.addRow('透明模式', self.alpha)
        self.matcap = W.QComboBox()
        for label, key in (('跟随原PMX', 'source_auto'), ('强制开启', 'on'), ('关闭', 'off')):
            self.matcap.addItem(label, key)
        form.addRow('MatCap', self.matcap)
        labels = {'Roughness':'粗糙度','Metallic':'金属度','Specular':'反射强度','DetailStrength':'微法线强度',
                  'DetailTiling':'细节重复','SSSStrength':'皮肤散射','Anisotropy':'各向异性',
                  'HairDirection':'头发方向（度）','MatCapStrength':'MatCap强度','ClearCoat':'清漆强度',
                  'CoatRoughness':'清漆粗糙度','Opacity':'不透明度','EmissiveStrength':'自发光强度',
                  'IrisDepth':'虹膜深度（需遮罩）','Fuzz':'布料纤维光泽',
                  'FabricDirection':'纤维高光方向（度）', 'HosieryCoverage':'丝袜织物覆盖',
                  'HosieryEdgeDensity':'丝袜侧面加密',
                  'HosieryThickness':'丝袜厚度', 'FuzzBrightness':'纤维提亮',
                  'FabricRoughnessVariation':'织物粗糙度变化', 'SkinRosiness':'皮肤粉嫩度',
                  'SSSDetailPreservation':'鼻部等细节保留', 'SSSMaskFloor':'自动散射遮罩下限',
                  'WetAmount':'水渍强度','WetTiling':'水滴重复','WetRotation':'流痕方向（度）',
                  'WetNormalStrength':'水滴凸起','WetRoughness':'水面粗糙度'}
        self.wet = W.QCheckBox('启用水滴与流痕')
        form.addRow('皮肤水渍', self.wet)
        self.params, self.param_rows = {}, {}
        self.thickness_value = None
        self.thickness_presets = {}
        for key, (low, high, default) in PARAMETERS.items():
            if key == 'WetEnabled': continue
            field = W.QDoubleSpinBox(); field.setRange(low, high)
            field.setDecimals(4 if key == 'IrisDepth' else 3); field.setValue(default)
            field.setSingleStep(.001 if key == 'IrisDepth' else 1 if high > 2 else .025)
            self.params[key] = field
            row_widget = field
            if key == 'HosieryThickness':
                row_widget = W.QWidget(); thickness_layout = W.QHBoxLayout(row_widget)
                thickness_layout.setContentsMargins(0, 0, 0, 0)
                # The desktop selects presets. Do not quantize existing stored
                # values to a preset until the user chooses one.
                field.setParent(row_widget); field.setDecimals(6); field.hide()
                label = W.QLabel(); self.thickness_value = label
                def show_thickness(value, target=label):
                    title = next((title for title, number in (('薄', .35), ('中', 1.), ('厚', 2.5))
                                  if abs(value-number)<1e-6), '自定义')
                    target.setText(title+' · '+format(value, '.6f').rstrip('0').rstrip('.'))
                field.valueChanged.connect(show_thickness); show_thickness(default)
                thickness_layout.addWidget(label, 1)
                for title, value in (('薄', .35), ('中', 1.), ('厚', 2.5)):
                    button = W.QPushButton(title); button.setMaximumWidth(42)
                    button.clicked.connect(lambda _=False, value=value, target=field: target.setValue(value))
                    thickness_layout.addWidget(button); self.thickness_presets[title] = button
                row_widget.setToolTip('程序选择薄／中／厚；UE材质实例中用HosieryThickness连续调节。保留已有自定义值，选择预设才会替换。')
            self.param_rows[key] = row_widget
            form.addRow(labels[key], row_widget)
        self.textures = W.QTableWidget(len(SEMANTICS), 4)
        self.textures.setHorizontalHeaderLabels(['贴图用途', '文件（留空使用自动）', '通道', '反转'])
        self.texture_fields = {}
        titles = ('基础色','法线','粗糙度','金属度','AO','透明遮罩','自发光颜色','自发光遮罩','Sphere／MatCap','头发方向','虹膜遮罩','面部散射遮罩')
        for index, (semantic, title) in enumerate(zip(SEMANTICS, titles)):
            self.textures.setItem(index, 0, W.QTableWidgetItem(title))
            box = W.QWidget(); line = W.QHBoxLayout(box); line.setContentsMargins(0, 0, 0, 0)
            path = W.QLineEdit(); browse = W.QPushButton('…'); browse.setMaximumWidth(28)
            line.addWidget(path); line.addWidget(browse)
            browse.clicked.connect(lambda _=False, target=path: self.browse(target))
            self.textures.setCellWidget(index, 1, box)
            vector = semantic in ('normal', 'base_color', 'sphere', 'direction', 'emission')
            channel = W.QComboBox(); channel.addItems(['rgb'] if vector else ['r', 'g', 'b', 'a'])
            self.textures.setCellWidget(index, 2, channel)
            invert = W.QCheckBox(); invert.setEnabled(not vector)
            self.textures.setCellWidget(index, 3, invert)
            self.texture_fields[semantic] = (path, channel, invert)
        self.textures.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.Stretch)
        self.textures.verticalHeader().setDefaultSectionSize(46)
        self.textures.setMinimumHeight(390)
        if self.texture_only:
            self.textures.setMinimumHeight(120);self.textures.setMaximumHeight(165)
            self.textures.verticalHeader().hide()
            self.textures.setColumnHidden(2,True);self.textures.setColumnHidden(3,True)
        form.addRow(self.textures)
        self.auto_maps = W.QLabel(); self.auto_maps.setWordWrap(True)
        if self.texture_only:self.auto_maps.setSizePolicy(W.QSizePolicy.Preferred,W.QSizePolicy.Maximum)
        form.addRow('自动匹配', self.auto_maps)
        self.evidence = W.QPlainTextEdit(); self.evidence.setReadOnly(True); self.evidence.setMaximumHeight(120)
        form.addRow('来源与待确认项', self.evidence)
        split.setSizes([300, 800])
        actions = W.QHBoxLayout(); layout.addLayout(actions)
        reset = W.QPushButton('当前材质恢复自动'); reset.clicked.connect(self.reset_current); actions.addWidget(reset)
        apply = W.QPushButton('应用当前材质到UE'); apply.clicked.connect(self.apply_live); actions.addWidget(apply)
        undo = W.QPushButton('撤销上次UE材质应用'); undo.clicked.connect(self.undo_live); actions.addWidget(undo)
        if self.texture_only:
            apply.setEnabled(False);undo.setEnabled(False)
            apply.setToolTip('此入口仅用于已导入的内置PBR角色；当前预设补图后请新建任务重新导出')
        if self.missing_textures:
            keep=W.QPushButton('保留缺图诊断并继续');keep.clicked.connect(self.accept_missing);actions.addWidget(keep)
        actions.addStretch()
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Save | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Save).setText('保存映射并继续' if self.missing_textures else '保存到角色设置')
        buttons.button(W.QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.save_and_accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.slots.currentRowChanged.connect(self.select)
        self.kind.currentIndexChanged.connect(self.type_changed)
        self.geometry_local.currentIndexChanged.connect(self.geometry_changed)
        self.geometry_host.currentIndexChanged.connect(self.geometry_changed)
        self.alpha.currentIndexChanged.connect(self.refresh_preview)
        self.matcap.currentIndexChanged.connect(self.refresh_preview)
        for path, channel, invert in self.texture_fields.values():
            path.editingFinished.connect(self.refresh_preview)
            channel.currentIndexChanged.connect(self.refresh_preview)
            invert.toggled.connect(self.refresh_preview)
        first = next((i for i,r in enumerate(snapshot['materials']) if self.missing_textures and
                      r['pmx_index']==self.missing_textures[0]['pmx_index']),0)
        self.slots.setCurrentRow(first)
        if self.texture_only:
            for widget in (self.kind,self.geometry_local,self.geometry_host,geometry_hint,self.alpha,self.matcap):
                form.setRowVisible(widget,False)

    def update_slot(self, index, pbr):
        row = self.snapshot['materials'][index]; item = self.slots.item(index)
        if self.texture_only:
            reasons=list(pbr.get('issues',[]))+['缺少效果贴图：'+r['reference'] for r in pbr.get('missing_effects',[])]
            item.setText(str(row['pmx_index'])+' · '+row['name']+(' · 缺图待补选' if reasons else ''))
            item.setForeground(G.QBrush(G.QColor('#d8b45a')) if reasons else G.QBrush())
            item.setToolTip('\n'.join(reasons));return
        reasons = review_reasons(pbr)
        geometry = self.geometry_purpose(row, pbr)
        if row.get('geometry_purpose') and geometry.get('needs_review'):
            reasons += ['几何用途待确认：'+'；'.join(geometry['reasons'])]
        status = {'white': ' · 缺图白模', 'needs_review': ' · 缺效果图，待复核'}.get(pbr.get('status'), '')
        if reasons and not status: status = ' · 待确认'
        manual = ' · 手动' if pbr.get('classification', {}).get('method') == 'manual_override' else ''
        item.setText(str(row['pmx_index'])+' · '+row['name']+'  ['+TYPES[pbr['type']]+']'+manual+status)
        item.setForeground(G.QBrush(G.QColor('#d8b45a')) if reasons else G.QBrush())
        item.setToolTip('\n'.join(reasons) if reasons else '类型已确认；可选中调整')

    def refresh_preview(self):
        if self.loading or self.current is None: return
        row = self.snapshot['materials'][self.current]
        # Blank fields remain automatic, not stored file overrides. Manual files,
        # channels and inversion survive type changes and retain priority.
        value = {'type': self.kind.currentData(), 'alpha_mode': self.alpha.currentData(),
                 'matcap_mode': self.matcap.currentData(), 'textures': {}}
        for semantic, (path, channel, invert) in self.texture_fields.items():
            if path.text().strip():
                value['textures'][semantic] = {'path': path.text().strip(), 'channel': channel.currentText(),
                                               'invert': invert.isChecked()}
        cfg = copy.deepcopy(self.settings_data)
        cfg['model'] = self.snapshot['model']
        cfg.setdefault('character_material_overrides', {}).setdefault(self.snapshot['sha256'], {})[str(row['pmx_index'])] = value
        pbr = plan(analysis_record(row), cfg, self.snapshot['sha256'], self.files)
        self.preview = pbr
        self.loading = True
        for semantic, (path, channel, invert) in self.texture_fields.items():
            automatic = pbr['textures'].get(semantic, {})
            path.setPlaceholderText(automatic.get('path', '未找到明确来源'))
            path.setToolTip(path.text().strip() or automatic.get('path', '未找到明确来源'))
            if not path.text().strip():
                channel.setCurrentText(automatic.get('channel', 'rgb' if channel.count() == 1 else 'r'))
                invert.setChecked(automatic.get('invert', False))
        self.loading = False
        details = '、'.join(detail_map_names(pbr['type']).values())
        matched = '、'.join(semantic for semantic, spec in pbr['textures'].items() if spec.get('kind') == 'sidecar') or '无明确同名辅助图'
        self.auto_maps.setText('原基础色／MatCap按来源保留；同名辅助图：'+matched+'；内置细节：'+details)
        if self.texture_only:self.auto_maps.setText('保留原PMX的基础图、Sphere模式与透明度；明确手选的文件优先。')
        self.update_slot(self.current, pbr)
        self.show_evidence(row, pbr)

    def browse(self, target):
        path, _ = W.QFileDialog.getOpenFileName(self, '选择材质贴图', str(Path(self.snapshot['model']).parent),
                                               '贴图 (*.png *.jpg *.jpeg *.tga *.bmp *.exr *.tif *.tiff *.dds *.sph *.spa)')
        if path:
            target.setText(path);self.refresh_preview()

    def store(self):
        if self.current is None: return
        row = self.snapshot['materials'][self.current]
        geometry = {'local': self.geometry_local.currentData()}
        host = self.geometry_host.currentData()
        if host is not None:
            if host == row['pmx_index']:
                raise ValueError('贴片宿主不能是当前槽')
            geometry['host'] = host
        if geometry != {'local':'auto'}: self.geometry_entries[str(row['pmx_index'])] = geometry
        else: self.geometry_entries.pop(str(row['pmx_index']), None)
        value = {'type': self.kind.currentData(), 'alpha_mode': self.alpha.currentData(),
                 'matcap_mode': self.matcap.currentData(), 'params': {}, 'textures': {}}
        defaults = {key: row['pbr']['params'].get(key, DEFAULTS[row['pbr']['type']].get(key, default))
                    for key, (_, _, default) in PARAMETERS.items()}
        for key, field in self.params.items():
            if abs(field.value()-defaults[key]) > 1e-6:
                value['params'][key] = field.value()
        if self.wet.isChecked() != bool(defaults.get('WetEnabled', 0)):
            value['params']['WetEnabled'] = float(self.wet.isChecked())
        previous = self.entries.get(str(row['pmx_index']), {})
        if self.texture_only:
            value=copy.deepcopy(previous)
            textures=value.setdefault('textures',{})
            for semantic in ('base_color','sphere'):
                path,channel,invert=self.texture_fields[semantic]
                if path.text().strip():textures[semantic]={'path':path.text().strip(),'channel':'rgb','invert':False}
                else:textures.pop(semantic,None)
            validate_override(value)
            self.entries[str(row['pmx_index'])]=value
            return
        # Explicit existing parameters must survive reopening and saving.
        for key in previous.get('params', {}):
            value['params'][key] = float(self.wet.isChecked()) if key == 'WetEnabled' else self.params[key].value()
        for semantic, (path, channel, invert) in self.texture_fields.items():
            if path.text().strip():
                value['textures'][semantic] = {'path': path.text().strip(), 'channel': channel.currentText(),
                                               'invert': invert.isChecked()}
        validate_override(value)
        self.entries[str(row['pmx_index'])] = value

    def select(self, index):
        if index < 0: return
        self.store()
        self.current = index; self.loading = True
        row = self.snapshot['materials'][index]; pbr = row['pbr']
        geometry = self.geometry_entries.get(str(row['pmx_index']), {})
        self.geometry_local.setCurrentIndex(self.geometry_local.findData(geometry.get('local','auto')))
        self.geometry_host.setCurrentIndex(self.geometry_host.findData(geometry.get('host')))
        for i in range(1, self.geometry_host.count()):
            self.geometry_host.model().item(i).setEnabled(self.geometry_host.itemData(i) != row['pmx_index'])
        saved = self.entries.get(str(row['pmx_index']), {})
        for field, key, default in ((self.kind, 'type', 'auto'), (self.alpha, 'alpha_mode', 'auto'),
                                    (self.matcap, 'matcap_mode', 'source_auto')):
            field.setCurrentIndex(field.findData(saved.get(key, default)))
        for key, field in self.params.items():
            default = pbr['params'].get(key, DEFAULTS[pbr['type']].get(key, PARAMETERS[key][2]))
            field.setValue(saved.get('params', {}).get(key, default))
        self.wet.setChecked(bool(saved.get('params', {}).get('WetEnabled', pbr['params'].get('WetEnabled', 0))))
        for semantic, (path, channel, invert) in self.texture_fields.items():
            binding = saved.get('textures', {}).get(semantic, {})
            automatic = pbr['textures'].get(semantic, {})
            path.setText(binding.get('path', '')); path.setPlaceholderText(automatic.get('path', '未找到明确来源'))
            channel.setCurrentText(binding.get('channel', automatic.get('channel', 'r')))
            invert.setChecked(binding.get('invert', False))
        self.loading = False
        self.refresh_preview()
        self.update_parameter_visibility()

    def show_evidence(self, row, pbr):
        if self.texture_only:
            refs=pbr.get('source_references',{})
            lines=['原PMX透明度：'+str(row.get('diffuse',[1,1,1,1])[3]),
                   '原基础图引用：'+refs.get('texture_rel_path',''),
                   '原Sphere引用：'+refs.get('sphere_texture_rel_path','')]
            lines += ['手动 '+key+'：'+value['path'] for key,value in pbr.get('textures',{}).items() if value.get('kind')=='manual']
            lines += list(pbr.get('issues',[]))
            self.evidence.setPlainText('\n'.join(lines));return
        lines = ['当前类型：'+TYPES[pbr['type']], '原PMX Sphere模式：'+str(row.get('sphere_mode', 0)),
                 '原PMX透明度：'+str(row.get('diffuse', [1, 1, 1, 1])[3])]
        evidence = pbr.get('classification', {})
        fields = {'source_name':'原材质名', 'name':'材质名称', 'mmd.name_j':'PMX原名',
                  'mmd.name_e':'PMX英文名', 'override.type':'手动指定'}
        if evidence.get('method') == 'manual_override':
            lines.append('已使用手动类型；自动初判：'+TYPES[evidence['automatic_type']])
        elif evidence.get('method') == 'uv_eye_color':
            lines.append('分类依据：实际UV区域的眼白颜色复核')
        elif evidence.get('field'):
            lines.append('分类依据：'+fields.get(evidence['field'], evidence['field'])+'「'+evidence.get('matched', '')+'」')
            lines.append('命中规则：名称关键词→'+TYPES[evidence['type']])
            if evidence['type']=='generic':lines.append('具体物理材质不足以确定，保留通用PBR并待复核')
        elif evidence:
            lines.append('名称不足以确定材质，使用通用PBR并待复核')
        if evidence.get('conflicts'):
            lines.append('其他类型命中：'+'；'.join(fields.get(item['field'], item['field'])+'→'+TYPES[item['type']]
                                               +'「'+item.get('matched','')+'」'
                                               for item in evidence['conflicts'][:4]))
        face_source = pbr.get('face_parameter_source')
        if face_source:
            lines.append('嘴唇：导入时继承面部槽 '+str(face_source['pmx_index'])+'「'+face_source['name']+'」参数；保留唇色与手动覆盖')
        elif pbr.get('surface_role') == 'lips':
            lines.append('嘴唇使用面部材质；面部参数来源待复核')
        lines += review_reasons(pbr)
        geometry=self.geometry_purpose(row,pbr)
        role_names={'body':'身体','face':'面部／嘴唇','neck':'颈部','close_fitting':'贴身软层','legwear':'贴腿软层','attached_layer':'附属贴层','excluded':'排除','unknown':'待确认'}
        lines += ['几何用途：'+role_names[geometry['role']]+' · '+('纳入局部组' if geometry['local_selected'] else '不自动纳入'),
                  '几何依据：'+'；'.join(geometry['reasons'])]
        if geometry.get('host_override') is not None: lines.append('用户指定贴片宿主：'+str(geometry['host_override']))
        lines += [str(x['reason'])+' · '+'; '.join(x.get('paths', [])) for x in pbr['candidates']]
        self.evidence.setPlainText('\n'.join(lines))

    def geometry_purpose(self, row, pbr):
        from .character_geometry_roles import purpose
        record=analysis_record(row);record['character_material']={'pbr':pbr}
        return purpose(record,row.get('geometry_purpose',{}).get('evidence',{}),
                       self.geometry_entries.get(str(row['pmx_index']),{}),
                       extended=bool(self.settings_data.get('character_geometry_mode')))

    def geometry_changed(self):
        if self.loading or self.current is None:return
        self.store();self.refresh_preview()

    def type_changed(self):
        if self.loading or self.current is None: return
        kind = self.kind.currentData()
        if kind == 'auto': kind = self.snapshot['materials'][self.current]['pbr']['type']
        defaults = parameter_defaults(kind, self.scattering)
        for key, (_, _, default) in PARAMETERS.items():
            if key not in ('Opacity', 'WetEnabled'): self.params[key].setValue(defaults[key])
        self.wet.setChecked(False)
        self.refresh_preview()
        self.update_parameter_visibility()

    def update_parameter_visibility(self):
        if self.texture_only:
            for field in self.param_rows.values():self.form.setRowVisible(field,False)
            self.form.setRowVisible(self.wet,False)
            for i,semantic in enumerate(SEMANTICS):self.textures.setRowHidden(i,semantic not in ('base_color','sphere'))
            return
        kind = self.kind.currentData()
        if kind == 'auto': kind = self.snapshot['materials'][self.current]['pbr']['type']
        specialized = {'SSSStrength': ('skin','face'), 'SSSDetailPreservation': ('face',), 'SSSMaskFloor': ('face',), 'Anisotropy': ('hair','satin','hosiery'),
                       'HairDirection': ('hair',), 'IrisDepth': ('iris',), 'Fuzz': ('cloth',),
                       'FabricDirection': ('satin','hosiery'), 'HosieryCoverage': ('hosiery',),
                       'HosieryEdgeDensity': ('hosiery',),
                       'HosieryThickness': ('hosiery',), 'FuzzBrightness': ('cloth',),
                       'FabricRoughnessVariation': ('cloth','satin'), 'SkinRosiness': ('skin','face'),
                       'ClearCoat': ('iris','sclera','eye_highlight','plastic','glass','teeth'),
                       'CoatRoughness': ('iris','sclera','eye_highlight','plastic','glass','teeth')}
        for key, kinds in specialized.items():
            self.form.setRowVisible(self.param_rows[key], kind in kinds)
        self.form.setRowVisible(self.wet, kind in ('skin', 'face'))
        for key, field in self.params.items():
            if key.startswith('Wet'): self.form.setRowVisible(field, kind in ('skin', 'face'))
        for semantic, kinds in (('direction', ('hair',)), ('iris_mask', ('iris',)), ('sss_mask', ('face',))):
            self.textures.setRowHidden(SEMANTICS.index(semantic), kind not in kinds)

    def reset_current(self):
        index = self.current
        if index is None: return
        self.entries.pop(str(self.snapshot['materials'][index]['pmx_index']), None)
        self.current = None; self.select(index)

    def save_and_accept(self):
        self.store()
        if self.missing_textures:
            from .character_texture_review import missing_sources
            cfg=dict(self.settings_data,character_material_overrides=self.overrides)
            if missing_sources(self.snapshot['model'],cfg):
                W.QMessageBox.warning(self,'贴图仍缺失','请补选有效贴图，或明确选择“保留缺图诊断并继续”。')
                return
        self.accept()

    def accept_missing(self):
        self.store();self.continue_with_missing=True;self.accept()

    def apply_live(self):
        self.store()
        parent = self.parent()
        settings = parent.settings()
        settings.character_material_overrides = self.overrides
        row = self.snapshot['materials'][self.current]
        payload = {'model': self.snapshot['model'], 'model_sha256': self.snapshot['sha256'],
                   'pmx_index': row['pmx_index'], 'settings': {
                       'character_material_overrides': self.overrides,
                       'character_pbr_options': settings.character_pbr_options}}
        parent.execute(lambda: parent.controller.material_surface_command('character_pbr_apply',
                       project=settings.project, **payload),
                       lambda result: parent.log.appendPlainText('角色材质已应用：'+str(result['count'])+' 个槽；可撤销。'))

    def undo_live(self):
        parent = self.parent()
        parent.execute(lambda: parent.controller.material_surface_command('character_pbr_undo',
                       project=parent.settings().project),
                       lambda result: parent.log.appendPlainText('角色材质已撤销：'+str(result['count'])+' 个槽。'))
