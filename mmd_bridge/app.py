from __future__ import annotations
import json
import os
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from . import __version__
from .core import Settings, read_json, atomic_json, file_hash, desktop_folder, resource_root
from .controller import Controller, app_home, bridge_status, install_ue_bridge, running_projects
from .controller import MissingUpstreamPlugins, install_upstream_plugins
from .discovery import discover, discover_mmdbridge
from .pmx_physics_displayframe import analyze as physics_bake_analyze
from .pmx_physics_displayframe import repair as physics_bake_repair
from . import mmm_runtime as mmm_rt
from .gui_motion import MotionEffects, SectionClip
from .gui_theme import WindowFrames


class Worker(C.QThread):
    result = C.Signal(object)
    def __init__(self,fn):
        super().__init__()
        self.fn = fn
    def run(self):
        try:
            self.result.emit(self.fn())
        except Exception as exc:
            self.result.emit(exc)


class LogHighlighter(G.QSyntaxHighlighter):
    """Make the actionable error line easy to find in the shared task log."""

    ERROR_PATTERN = re.compile(
        r"(?:configerror|\berror\b|\bexception\b|\btraceback\b|\bfailed\b|失败)",
        re.IGNORECASE,
    )
    WARNING_PATTERN = re.compile(r"(?:ue_wait_vram|显存|已暂停|无法可靠还原|外部文件不存在|已跳过|需复核)", re.IGNORECASE)

    def highlightBlock(self, text):
        if self.ERROR_PATTERN.search(text):
            color = "#ff6b6b"
        elif self.WARNING_PATTERN.search(text):
            color = "#ffd166"
        else:
            return
        fmt = G.QTextCharFormat()
        fmt.setForeground(G.QColor(color))
        fmt.setFontWeight(G.QFont.Weight.Bold)
        self.setFormat(0, len(text), fmt)


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MMDBridgeForUE · Blender → Unreal")
        self.setWindowIcon(G.QIcon(str(resource_root()/'assets/app-icon.png')))
        self.resize(1240,900)
        self.controller = Controller()
        self.workers = []
        self.manual_paths = read_json(self.controller.home/'software.json') if (self.controller.home/'software.json').exists() else []
        self.inputs = {}
        self.dialog_paths_file = self.controller.home / 'dialog_paths.json'
        try:
            saved = read_json(self.dialog_paths_file) if self.dialog_paths_file.exists() else {}
            locations = saved.get('paths', saved) if isinstance(saved, dict) else {}
            values = saved.get('values', saved) if isinstance(saved, dict) else {}
            self.dialog_paths = {key: value for key, value in locations.items()
                                 if isinstance(value, str)} if isinstance(locations, dict) else {}
            self.path_values = {key: value for key, value in values.items()
                                if isinstance(value, str)} if isinstance(values, dict) else {}
        except (OSError, ValueError):
            self.dialog_paths = {}
            self.path_values = {}
        self.software = []
        self.last_state = None
        self.job_event_offset = 0
        self.last_camera_cut_state = None
        self.camera_cut_log_offsets = {}
        self.camera_cut_stage = None
        self.camera_cut_detect_frames = []
        self.main_flow_locked = False
        self.vram_wait_prompted = False
        self.discovering_projects = False
        self.project_prompted = set()
        widget = W.QWidget()
        widget.setObjectName('appShell')
        self.setCentralWidget(widget)
        layout = W.QVBoxLayout(widget)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        header = W.QWidget()
        header.setObjectName('appHeader')
        header.setFixedHeight(56)
        header_layout = W.QHBoxLayout(header)
        header_layout.setContentsMargins(20,0,20,0)
        heading = W.QLabel("MMDBridgeForUE")
        heading.setObjectName('appBrand')
        header_layout.addWidget(heading)
        layout.addWidget(header)
        workflows = W.QHBoxLayout()
        self.full_button = self.button(workflows, "走全流程", lambda: self.start("auto"))
        self.start_button = self.button(workflows, "导入角色", lambda: self.start("character"))
        self.scene_button = self.button(workflows, "导入场景", lambda: self.start("scene"))
        self.camera_button = self.button(workflows, "导入镜头", lambda: self.start("camera"))
        for button in (self.full_button, self.start_button, self.scene_button, self.camera_button):
            button.setObjectName("workflow")
        self.tabs = W.QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        self.navigation = W.QListWidget()
        self.navigation.setObjectName('navigation')
        self.navigation.setFixedWidth(206)
        self.navigation.setWordWrap(True)
        self.navigation.setHorizontalScrollBarPolicy(C.Qt.ScrollBarAlwaysOff)
        self.navigation.currentRowChanged.connect(self.navigate_page)
        self.tabs.currentChanged.connect(self.select_navigation)
        workspace = W.QHBoxLayout()
        workspace.setContentsMargins(0,0,0,0)
        workspace.setSpacing(0)
        operation_area = W.QWidget()
        operation_area.setObjectName('operationArea')
        operation_layout = W.QVBoxLayout(operation_area)
        operation_layout.setContentsMargins(1,14,1,14)
        operation_layout.setSpacing(0)
        operation_layout.addWidget(self.tabs,1)
        workspace.addWidget(self.navigation)
        workspace.addWidget(operation_area,1)
        layout.addLayout(workspace,1)
        files, form = self.page("素材与软件")
        self.files_tab_index = self.tabs.indexOf(files)
        for kind,label in (("blender","Blender"),("unreal","Unreal Engine")):
            combo = W.QComboBox()
            combo.setSizeAdjustPolicy(W.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(20)
            self.inputs[kind] = combo
            self.path_row(form,label,combo)
        tools = W.QHBoxLayout()
        self.button(tools,"重新检测",self.scan)
        self.button(tools,"添加软件路径",self.add_executable)
        form.addRow(tools)
        for key,label,pattern in (("model","PMX 模型","PMX (*.pmx)"),("motion","动作 VMD","VMD (*.vmd)"),
            ("lipsync","口型 VMD（可选）","VMD (*.vmd)"),("camera","镜头 VMD（可选）","VMD (*.vmd)"),
            ("scene","场景（可选）","Blender (*.blend)"),
            ("project","UE 项目（可稍后选择）","Unreal (*.uproject)")):
            self.file_field(form,key,label,pattern)
        self.working_blend_row = self.file_field(form, 'working_blend', '旧 FBX：角色工作文件（可选）', 'Blender (*.blend)')
        self.inputs['working_blend'].setToolTip('仅旧 Blender / FBX 通路使用：从已有角色 .blend 开始处理；不是临时中转目录。默认 MMDBridge 通路忽略此项。')
        for key, label in (("mmdbridge_path", "MMDBridge 安装目录"),
                           ("character_output_dir", "临时中转目录")):
            row = W.QHBoxLayout()
            field = W.QLineEdit()
            field.setClearButtonEnabled(True)
            self.inputs[key] = field
            self.bind_path_memory(key, field)
            row.addWidget(field)
            self.button(row, "选择目录…", lambda _=False, k=key: self.browse_directory(k))
            if key == 'mmdbridge_path':
                field.setToolTip('选择上游 rintrint/mmdbridge 的 Release 配套程序目录（包含 MikuMikuDance_CHS.exe），用于角色动作与物理烘焙导出。需要配套的导出脚本和运行文件；此处不是本工具的 UE 桥接插件目录。')
                self.detect_mmdbridge_button = self.button(row, '自动检测', self.detect_mmdbridge_directory)
                self.detect_mmdbridge_button.setMinimumWidth(self.detect_mmdbridge_button.sizeHint().width())
                self.detect_mmdbridge_button.setToolTip('在已记住的位置和常见安装目录中查找完整的 MMDBridge 安装；找到多个时可选择，不启动 MMD。')
            else:
                field.setToolTip('程序运行时保存任务记录、断点及所有导出中转文件的工作目录。留空时会在桌面自动创建独立任务目录。')
            self.path_row(form,label,row)
        self.bridge_status_label = W.QLabel("未选择 UE 项目")
        self.bridge_status_label.setWordWrap(True)
        form.addRow("UE 桥接状态",self.bridge_status_label)
        connections = W.QHBoxLayout()
        self.install_button = self.button(connections,"安装 UE 桥接插件",self.install)
        self.connect_button = self.button(connections,"连接所选项目",self.connect)
        for button in (self.install_button, self.connect_button):
            button.setProperty('connection_action', True)
        form.addRow(connections)
        label = W.QLabel("中转目录保存任务、断点和导出文件；留空时自动创建桌面 Middle_任务名_01、02 等目录，源文件保持不变。")
        label.setWordWrap(True)
        form.addRow(label)
        presets = W.QHBoxLayout()
        self.button(presets,"保存配置",self.save_preset)
        self.button(presets,"载入配置",self.load_preset)
        self.button(presets,"素材预检",self.preflight)
        form.addRow(presets)
        params,p = self.page("参数")
        for key,label,val,lo,hi in (("fps","帧率（保持动作时长）",30,1,240),
            ("start","导出开始帧",0,0,1000000),("end","导出结束帧（0=自动）",0,0,1000000),
            ("width","画幅宽",1920,1,16384),("height","画幅高",1080,1,16384),
            ("scene_frame","场景参考帧",1,0,1000000)):
            self.number(p,key,label,val,lo,hi)
        for key, label, default in (("character_master_material", "角色母材质", "/Game/SelfMakeMaterial/Material/M_00"),
                                    ("eye_master_material", "眼睛母材质", "/Game/SelfMakeMaterial/Material/M_00_Eye")):
            row = W.QHBoxLayout()
            field = W.QLineEdit(default)
            field.setPlaceholderText('留空时自动创建默认角色母材质')
            field.setToolTip('未选择或母材质不存在时，UE 5.8 Substrate Blendable 模式自动使用官方 Toon；其它配置使用工具 PBR 默认材质。')
            if key=='eye_master_material':
                field.setPlaceholderText('留空时使用角色配置的默认眼睛材质')
                field.setToolTip('ZZZ 配置下留空会自动适配眼睛、瞳孔和眼白，弱光仍保留贴图颜色；明确选择的眼睛母材质保持原样。其它配置未登记默认眼睛时使用通用材质。')
            self.inputs[key] = field
            row.addWidget(field)
            self.button(row, "选择 UE 材质…", lambda _=False, k=key: self.browse_ue_material(k))
            self.path_row(p,label,row)
        profile_choice = W.QComboBox()
        for title, value, tip in (
                ('UE 官方卡通（默认）', 'ue_official_toon', '工具使用 UE 官方 Substrate Toon 节点创建角色母材质；需启用 Substrate Blendable GBuffer。不使用自选母材质路径。'),
                ('ZZZ_Render_PMX', 'ZZZ_Render_PMX', '使用已登记的 ZZZ 专用参数映射、控制贴图和描边适配；默认填入配套模板路径。'),
                ('自动按母材质匹配', 'auto', '按所选 UE 母材质的完整资源路径匹配已登记配置；未匹配时仅使用通用参数映射。'),
                ('仅通用参数匹配', 'generic', '角色主体按 BaseColor、Albedo、Normal 等参数名称匹配贴图和数值，不使用专用适配；眼睛仍按单独的母材质自动匹配。')):
            profile_choice.addItem(title, value)
            profile_choice.setItemData(profile_choice.count()-1, tip, C.Qt.ToolTipRole)
        profile_choice.setToolTip('官方卡通由工具自动生成母材质；ZZZ 和自选方案使用对应的 UE 母材质。详细规则见各选项的悬停说明。')
        self.inputs['character_material_profile'] = profile_choice
        p.addRow('角色母材质适配配置', profile_choice)
        profile_choice.currentIndexChanged.connect(self.update_character_material_profile)
        self.update_character_material_profile()
        transforms = self.parameter_group(p,"时间、缩放与镜头")
        self.number(transforms,"scale","历史 FBX：MMD 导入缩放",.08,.0001,100,4)
        presets = W.QHBoxLayout()
        self.button(presets,"0.08",lambda:self.inputs["scale"].setValue(.08))
        self.button(presets,"0.1",lambda:self.inputs["scale"].setValue(.1))
        transforms.addRow("快捷缩放",presets)
        for key,label in (("motion_offset","动作偏移（输出帧）"),
                          ("lipsync_offset","口型偏移（输出帧）"),("camera_offset","镜头偏移（输出帧）")):
            self.number(transforms,key,label,0,-100000,100000)
        self.number(transforms,"camera_scale","镜头构图比例",1,.001,100,3)
        for axis in "xyz":
            self.number(transforms,"camera_"+axis,"镜头 "+axis.upper()+" 偏移（米）",0,-10000,10000,3)
        self.number(transforms,"scene_scale","场景比例（独立于 MMD）",1,.0001,10000,4)
        materials = self.parameter_group(p,"材质转换与适配")
        material_strategy = W.QComboBox()
        for label, value in (("自动：按材质语义选择节点、混合或烘焙", "auto"), ("优先 UE 节点", "nodes"),
                             ("混合还原", "hybrid"), ("完整烘焙", "bake")):
            material_strategy.addItem(label, value)
        self.inputs["material_strategy"] = material_strategy
        materials.addRow("场景材质策略", material_strategy)
        workflow = W.QComboBox()
        workflow.addItem('默认材质转换（推荐）', 'legacy_main_v1')
        workflow.addItem('新版语义方案（已有任务兼容）', 'semantic_plan_v1')
        workflow.setToolTip('默认转换：沿用当前主流程，节点优先，复杂字段局部烘焙。\n语义方案：按材质依赖和表面特征规划节点、局部或完整烘焙；保留已有任务兼容。')
        self.inputs['material_workflow'] = workflow
        materials.addRow('材质工作流', workflow)
        bake_fallback = W.QCheckBox("材质转换失败或超时时允许烘焙贴图兜底")
        bake_fallback.setChecked(True)
        bake_fallback.setToolTip("已适配图直接转节点；实时混合、分层与平铺表面采用局部烘焙；普通静态 PBR 的多个复杂字段完整烘焙。重复数量决定资源复用。失败或超时先局部兜底，透光降级仍保留独立裁切；强制节点模式不烘焙。")
        self.inputs["material_bake_fallback"] = bake_fallback
        materials.addRow("材质烘焙兜底", bake_fallback)
        complex_strategy = W.QComboBox()
        complex_strategy.addItem('优先还原节点（兼容旧配置）', 'nodes')
        complex_strategy.addItem('语义优先：局部兜底，静态 PBR 完整烘焙', 'pbr_bake')
        complex_strategy.setCurrentIndex(1)
        self.inputs['material_complex_strategy'] = complex_strategy
        materials.addRow('复杂材质处理', complex_strategy)
        self.number(materials, 'material_conversion_timeout', '单材质转换时间预算（秒，0=不限时）', 120, 0, 86400, 1)
        scene_master = W.QLineEdit("/Game/SelfMakeMaterial/Material/M_00")
        self.inputs["scene_master_material"] = scene_master
        scene_master_row = self.path_row(materials,"场景通用母材质（可选）",scene_master)
        materials.setRowVisible(scene_master_row, False)
        profile_directory = W.QLineEdit(str(app_home() / 'material_profiles'))
        profile_directory.setToolTip('用户自定义的角色母材质参数映射配置目录；不是 UE 材质资产目录。程序自带配置会自动读取。')
        self.inputs['character_material_profiles_dir'] = profile_directory
        self.path_row(materials,'母材质配置目录',profile_directory)
        use_scene_master = W.QCheckBox("简单贴图 PBR 匹配时优先创建用户母材质实例")
        use_scene_master.setChecked(False)
        self.inputs["use_scene_master_material"] = use_scene_master
        materials.addRow("母材质实例", use_scene_master)
        materials.setRowVisible(use_scene_master, False)
        resolution = W.QComboBox()
        resolution.addItem("自动（1K/2K/4K）",0)
        for n in (1024,2048,4096,8192):
            resolution.addItem(f"{n//1024}K",n)
        resolution.setCurrentIndex(resolution.findData(2048))
        self.inputs["bake_resolution"] = resolution
        materials.addRow("新增材质烘焙分辨率",resolution)
        imports = self.parameter_group(p,"场景导入与碰撞")
        scene_format = W.QComboBox()
        scene_format.addItem("USD Stage（推荐）", "usd_stage")
        scene_format.addItem("第一阶段 FBX 兼容通路", "fbx_legacy")
        self.inputs["scene_format"] = scene_format
        imports.addRow("场景导入通路", scene_format)
        self.number(imports,"usd_payload_triangle_budget","旧版 USD 分块预算（当前按物体导出）",1000000,10000,100000000)
        exact_collision = W.QCheckBox("静态模型使用显示三角面碰撞，自动排除相对场景很小的物体")
        exact_collision.setChecked(True)
        self.inputs["exact_collision"] = exact_collision
        imports.addRow("精确碰撞", exact_collision)
        import_profile = W.QComboBox()
        import_profile.addItem('完整还原', 'faithful')
        import_profile.addItem('渲染减负：散布实例批次、无精细碰撞、省去网格体积', 'render_optimized')
        import_profile.setToolTip('保持几何、UV、源颜色/透明贴图和光照；重复散件改为 PointInstancer。浮点烘焙数据贴图 GPU 尺寸限制为 1K，程序纹理细节需复核。保留原物体父层级；各散件名称和变换保存在清单。此策略不关闭硬件光追或光追阴影。')
        self.inputs['scene_import_profile'] = import_profile
        import_profile.currentIndexChanged.connect(lambda: exact_collision.setEnabled(
            not self.main_flow_locked and import_profile.currentData() != 'render_optimized'))
        imports.addRow('场景导入策略', import_profile)
        scatter = W.QCheckBox('导入前自动合并兼容散件为实例批次，保留原物体层级')
        scatter.setChecked(True)
        scatter.setToolTip('按共同父物体和相同原型分组；保留材质、UV、光照、体积和实例数量。单独隐藏、动画、镜像、剪切或带自定义属性的散件保持原样。名称与原始变换保存在任务清单，可关闭以保留每个独立 Actor。')
        self.inputs['optimize_usd_scatter'] = scatter
        imports.addRow('USD 散件优化', scatter)
        self.number(imports, "collision_min_scene_ratio", "碰撞最小尺寸 / 场景最长边（0=不筛选）", .005, 0, 1, 4)
        self.inputs['collision_min_scene_ratio'].setToolTip('默认 0.5%；按每个实例自身大小判断，不把整片树叶当作一个大物体。Blender 自定义属性 mmd_bridge_collision=exact 可强制保留碰撞。')
        content = W.QLineEdit(Settings().content_root)
        self.inputs["content_root"] = content
        self.path_row(imports,"UE 内容目录",content)
        compatibility = self.parameter_group(p,"角色通路与兼容")
        backend = W.QComboBox()
        backend.addItem("默认：MMDBridge Mode 1 → 角色双 FBX", "mmdbridge_fbx")
        backend.addItem("旧 Blender / FBX（不烘焙物理）", "fbx_legacy")
        backend.addItem("MMD2Unreal（Legacy）", "mmd2unreal")
        self.inputs["character_backend"] = backend
        backend.currentIndexChanged.connect(lambda: form.setRowVisible(self.working_blend_row, backend.currentData() == 'fbx_legacy'))
        form.setRowVisible(self.working_blend_row, False)
        compatibility.addRow("角色导入通路", backend)
        hint = W.QLabel(
            "默认通路从原始 PMX/VMD 开始，先修复骨名与显示枠，再由 MMD/MMDBridge Mode 1 烘焙。\n"
            "旧 Blender / FBX 不执行物理烘焙；MMD2Unreal 继续保留为 Legacy。")
        hint.setWordWrap(True)
        compatibility.addRow(hint)
        defaults = W.QHBoxLayout()
        defaults.addStretch(1)
        self.restore_defaults_button = self.button(defaults,"恢复默认设置",self.restore_default_settings)
        p.addRow(defaults)
        self.build_scene_appearance_page()
        self.build_camera_cut_page()
        self.build_physics_bake_page()
        self.build_mmm_page()
        progress,progress_layout = self.page("任务与日志")
        self.task_tab_index = self.tabs.indexOf(progress)
        self.status = W.QLabel("就绪")
        self.status.setWordWrap(True)
        progress_layout.addRow(self.status)
        self.progress=W.QProgressBar()
        self.progress.setRange(0,1)
        self.progress.setValue(0)
        progress_layout.addRow('当前步骤',self.progress)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(290)
        self.log_highlighter = LogHighlighter(self.log.document())
        progress_layout.addRow(self.log)
        actions = W.QHBoxLayout()
        self.reopen_button = self.button(actions,"重开导出窗口",self.reopen)
        self.attach_button = self.button(actions,"关联已导出 FBX",self.attach)
        self.open_job_button = self.button(actions,"打开任务文件夹",self.open_job)
        self.report_button = self.button(actions,"读取结果报告",self.report)
        progress_layout.addRow(actions)
        connections = W.QHBoxLayout()
        self.resume_button = self.button(connections,"恢复已有任务",self.resume)
        progress_layout.addRow(connections)
        self.build_navigation()
        footer = W.QHBoxLayout()
        footer.addStretch(1)
        self.button(footer,"停止任务",self.stop)
        self.tabs.currentChanged.connect(self._update_main_start_button_state)
        self._update_main_start_button_state()
        action_bar = W.QWidget()
        action_bar.setObjectName('operationActions')
        action_layout = W.QVBoxLayout(action_bar)
        action_layout.setContentsMargins(20,12,20,16)
        action_layout.setSpacing(12)
        action_layout.addLayout(workflows)
        action_layout.addLayout(footer)
        operation_layout.addWidget(action_bar)
        for widget in self.findChildren(W.QAbstractSpinBox)+self.findChildren(W.QComboBox):
            widget.installEventFilter(self)
        self.motion = MotionEffects(self)
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        self.update_scene_actions()
        self.scan()

    def eventFilter(self,widget,event):
        if event.type()==C.QEvent.Type.Wheel and isinstance(widget,(W.QAbstractSpinBox,W.QComboBox)):
            event.ignore()
            return True
        return super().eventFilter(widget,event)

    def build_scene_appearance_page(self):
        page, form = self.page('场景还原')
        self.scene_appearance_tab_index = self.tabs.indexOf(page)
        rendering = self.parameter_group(form,"渲染与显存")
        pipeline = W.QComboBox()
        pipeline.addItem('Lumen', 'lumen')
        pipeline.addItem('路径追踪（现有还原基线）', 'path_tracing')
        self.inputs['scene_render_pipeline'] = pipeline
        rendering.addRow('场景渲染管线', pipeline)
        pipeline.currentIndexChanged.connect(self.update_scene_actions)
        memory = W.QComboBox()
        memory.addItem('均衡：保留光追，缩小缓存（默认）', 'balanced')
        memory.addItem('低显存：进一步缩小缓存和反射分辨率', 'low')
        memory.addItem('兼容：软件 Lumen＋虚拟阴影，避开硬件光追', 'compatible')
        memory.addItem('原电影级缓存：显存占用较高', 'quality')
        memory.setToolTip('均衡和低显存保留光追，并限制光追几何更新与驻留预算。兼容模式关闭硬件光追，反射、间接光和阴影会有差异。纹理池不是总显存上限；画面与稳定性需复核。')
        self.inputs['ue_render_memory_profile'] = memory
        rendering.addRow('Lumen 渲染显存策略', memory)
        lighting = self.parameter_group(form,"光照与色彩")
        restore_appearance = W.QCheckBox('还原世界光、灯光、体积雾与源色彩（Blender 5.2）')
        restore_appearance.setChecked(True)
        self.inputs['restore_scene_appearance'] = restore_appearance
        lighting.addRow('场景光影还原', restore_appearance)
        viewport_ocio = W.QCheckBox('导入时默认启用对应 OCIO；之后保留用户的视口设置')
        viewport_ocio.setChecked(True)
        self.inputs['scene_viewport_ocio'] = viewport_ocio
        lighting.addRow('编辑器视口色彩',viewport_ocio)
        self.number(lighting,'scene_light_scale','灯光亮度倍率',1,0,5,3)
        self.number(lighting,'scene_exposure_offset','曝光补偿（EV）',0,-20,20,2)
        fog = self.parameter_group(form,"雾与植被")
        self.number(fog,'fog_density_scale','雾密度倍率',1,0,100,3)
        fog_quality = W.QComboBox()
        fog_quality.addItem('原生体积雾（默认，支持雾中遮挡）','native_balanced')
        fog_quality.addItem('SVT 异质体积＋VSM 精细缓存／步进（实验性）','vsm_finer')
        fog_quality.addItem('SVT 异质体积＋VSM 无照明缓存（实验性，开销高）','vsm_nocache')
        fog_quality.addItem('源高度雾／SVT 异质体积（异质体积为实验性）','legacy')
        fog_quality.addItem('异质体积细遮挡候选（实验性，待验证）','detail')
        fog_quality.setToolTip('新导入默认采用原生体积雾：8 像素栅格、128 深度层及光追灯注入。导入时保存源雾的密度和边界，灯光、遮挡及散射实时计算，不烘焙光束。路径追踪恢复原始雾，不使用原生近似；不自动模拟透镜折射聚光。彩色消光、多相函数及细结构仍需复核。不支持的源雾保留原表达并报告限制；旧任务保留已保存配置。')
        self.inputs['fog_occlusion_quality'] = fog_quality
        fog.addRow('世界雾渲染方案',fog_quality)
        self.number(fog,'foliage_distance_scale','叶片显示距离倍率（视口／渲染）',2,1,4,1)
        scattering = W.QComboBox()
        scattering.addItem('原方案（性能优先，默认）','legacy_diffusion')
        scattering.addItem('双面透光修正（GPU 开销较高）','two_sided_wrap')
        scattering.setToolTip('原方案保留当前树叶外观。双面透光修正保留叶片遮罩和阴影，改善薄叶散射映射，但 railway 实测 GPU 开销明显增加。用于下一次场景转换，外观仍需复核。')
        self.inputs['thin_surface_scattering'] = scattering
        fog.addRow('叶片透光方案',scattering)
        self.number(fog,'volume_resolution','体积最长轴体素数',256,8,512)
        self.number(fog,'world_volume_distance','世界体积默认裁剪距离（米）',100,1,10000,1)
        hint = W.QLabel('以上光影参数用于下一次场景转换。已有场景仅切换渲染管线时复用原资产；修改光影参数后需重新导入。')
        hint.setWordWrap(True)
        fog.addRow(hint)
        actions = W.QGridLayout()
        actions.setDefaultPositioning(3,C.Qt.Horizontal)
        self.scene_result_button = self.button(actions, '选择已转换场景任务…', self.load_scene_results)
        self.scene_parameters_button = self.button(actions, '打开材质与其他参数', lambda: self.tabs.setCurrentIndex(1))
        self.scene_reconvert_button = self.button(actions, '按当前策略重做材质', self.reconvert_scene_materials)
        self.white_repair_button = self.button(actions, '补修白模（实验性）', self.repair_white_materials)
        self.white_undo_button = self.button(actions, '撤销上次白模补修', self.undo_material_repair)
        self.material_analysis_button=self.button(actions,'独立分析材质',self.analyze_scene_materials)
        self.material_restoration_button=self.button(actions,'材质还原结果',self.show_material_restoration_report)
        self.material_analysis_stop_button=self.button(actions,'停止材质分析',self.stop_material_analysis)
        self.material_analysis_stop_button.setEnabled(False)
        self.material_calibration_button=self.button(actions,'独立性能校准',self.calibrate_scene_materials)
        form.addRow(actions)
        surfaces = W.QHBoxLayout()
        self.surface_switch_button = self.button(surfaces, '分析／切换 UE 选中物体的材质…', self.switch_selected_materials)
        self.surface_undo_button = self.button(surfaces, '撤销上次材质逻辑切换', self.undo_selected_materials)
        form.addRow('局部材质切换', surfaces)
        self.scene_result_label = W.QLabel('未选择场景任务；完成导入或选择已有任务后可生成对照。')
        self.scene_result_label.setWordWrap(True)
        form.addRow('当前任务', self.scene_result_label)
        self.scene_assets_label = W.QLabel()
        self.scene_assets_label.setWordWrap(True)
        self.scene_assets_label.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
        form.addRow('UE 资产与配置', self.scene_assets_label)
        self.scene_render_label = W.QLabel()
        self.scene_render_label.setWordWrap(True)
        form.addRow('最新 UE 参考帧', self.scene_render_label)
        configs = W.QHBoxLayout()
        self.copy_render_config_button = self.button(configs, '复制 MRQ 配置路径', self.copy_render_config)
        self.scene_folder_button = self.button(configs, '打开任务文件夹', self.open_job)
        form.addRow(configs)
        renders = W.QHBoxLayout()
        self.blender_reference_button = self.button(renders, '渲染 Blender 参考帧', lambda: self.render_scene_reference('blender'))
        self.ue_reference_button = self.button(renders, '渲染 UE 参考帧', lambda: self.render_scene_reference('ue'))
        self.comparison_button = self.button(renders, '生成对照与差分', self.compare_scene_reference)
        form.addRow(renders)
        self.background_preset = W.QComboBox()
        self.background_preset.setEditable(True)
        self.background_preset.setMinimumContentsLength(20)
        self.background_preset.setSizeAdjustPolicy(W.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.background_preset.lineEdit().setPlaceholderText('留空时自动使用生成的预设')
        self.background_preset.setToolTip('留空时按当前渲染管线选用任务已生成的预设；也可填写 UE 资产路径或粘贴预设的复制引用。预设在场景导入或参考帧配置阶段生成。')
        self.background_options_signature = None
        self.path_row(form,'后台渲染预设',self.background_preset)
        range_row = W.QHBoxLayout()
        self.background_range = W.QComboBox()
        self.background_range.addItem('按预设范围（未设则完整序列）', 'preset')
        self.background_range.addItem('只渲染一帧，检查效果', 'frame')
        self.background_frame = W.QSpinBox()
        self.background_frame.setRange(-1000000, 1000000000)
        self.background_frame.setValue(1)
        self.background_frame.setEnabled(False)
        self.background_range.currentIndexChanged.connect(lambda: self.background_frame.setEnabled(
            self.background_range.currentData() == 'frame' and not self.main_flow_locked))
        range_row.addWidget(self.background_range)
        range_row.addWidget(self.background_frame)
        form.addRow('后台渲染范围／帧号', range_row)
        output_row = W.QHBoxLayout()
        self.background_output = W.QLineEdit()
        self.bind_path_memory('background_output', self.background_output)
        self.background_output.setPlaceholderText('留空：任务文件夹/renders；每次渲染创建独立子文件夹')
        output_row.addWidget(self.background_output)
        self.background_output_button = self.button(output_row, '选择输出目录…', self.choose_background_output)
        self.path_row(form,'后台输出位置',output_row)
        background_actions = W.QHBoxLayout()
        self.background_render_button = self.button(background_actions, '后台渲染（不打开编辑器）', self.render_scene_background)
        self.background_stop_button = self.button(background_actions, '停止后台渲染', self.stop_scene_background)
        self.background_resume_button=self.button(background_actions,'恢复未完成帧',self.resume_scene_background)
        self.background_open_button = self.button(background_actions, '打开后台渲染输出', self.open_background_output)
        form.addRow(background_actions)
        self.background_status = W.QLabel('后台渲染使用已保存的关卡、序列和预设。请先保存并关闭目标 UE 工程；工具不会自动关闭编辑器。\n'
            '沿用预设的画质、采样和控制台变量，只补齐缺少的渲染／文件输出设置；源预设保持不变。')
        self.background_status.setWordWrap(True)
        form.addRow(self.background_status)
        reviews = W.QHBoxLayout()
        self.show_comparison_button = self.button(reviews, '查看对照图', lambda: self.show_scene_visual('comparison.png'))
        self.scene_report_button = self.button(reviews, '查看还原报告', self.report)
        form.addRow(reviews)
        images = W.QHBoxLayout()
        self.show_blender_button = self.button(images, '查看 Blender 参考图', lambda: self.show_scene_visual('blender.png'))
        self.show_ue_button = self.button(images, '查看 UE 渲染图', lambda: self.show_scene_visual('ue.png'))
        form.addRow(images)
        note = W.QLabel('首次运行需部署 Blender 5.2、UE 官方插件（USD、OpenColorIO、MovieRenderPipeline）和本工具桥接插件；安装后重启 UE。')
        note.setWordWrap(True)
        form.addRow(note)

    def analyze_scene_materials(self):
        import threading
        from .material_analysis import analyze_source
        settings=self.settings()
        self.material_analysis_cancel=threading.Event()
        self.material_analysis_stop_button.setEnabled(True)
        self.scene_result_label.setText('正在读取有效材质图，分析结果为估算…')
        def finished(result):
            self.material_analysis_stop_button.setEnabled(False)
            self.material_analysis_folder=Path(result['folder'])
            report=result['report']
            text=json.dumps(report,ensure_ascii=False,indent=2)
            self.log.appendPlainText(text)
            self.scene_result_label.setText('材质分析：'+str(self.material_analysis_folder/'report.json'))
        self.execute(lambda:analyze_source(settings.blender,settings.scene,app_home()/'material_analysis',
                                          settings.scene_frame,self.material_analysis_cancel),finished,
                     cleanup=lambda:self.material_analysis_stop_button.setEnabled(False))

    def stop_material_analysis(self):
        cancel=getattr(self,'material_analysis_cancel',None)
        if cancel is not None:cancel.set()

    def show_material_restoration_report(self):
        try:
            job=self.controller.job
            if not job:raise ValueError('请先选择已转换场景任务')
            from .material_results import restoration_rows
            manifest=read_json(job.folder/'manifest.json')
            report_path=job.folder/'ue_report.json'
            report=read_json(report_path) if report_path.is_file() else {}
            rows=restoration_rows(manifest,report)
            dialog=W.QDialog(self);dialog.setWindowTitle('材质还原结果 · 视觉验收独立于转换状态');dialog.resize(1200,650)
            layout=W.QVBoxLayout(dialog)
            failed=sum(row['conversion_status'] in ('placeholder','failed') for row in rows)
            layout.addWidget(W.QLabel(f'{len(rows)} 条材质记录；{failed} 条失败或占位。双击原因可查看兜底过程。'))
            table=W.QTableWidget(len(rows),6,dialog)
            table.setHorizontalHeaderLabels(['物体','材质','策略','转换结果','视觉状态','原因与限制'])
            statuses={'converted':'已转换','approximated':'近似还原','placeholder':'失败占位','failed':'失败'}
            for index,row in enumerate(rows):
                values=[', '.join(row['objects']),row['material'],row['strategy'],statuses[row['conversion_status']],
                    '已发现外观不符' if row.get('visual_result')=='appearance_mismatch' else '需视觉复核' if row['visual_status']=='needs_review' else '未进行视觉验收', '\n'.join(row['reasons'])]
                for column,value in enumerate(values):
                    item=W.QTableWidgetItem(value);item.setToolTip(value);item.setData(C.Qt.UserRole,row['id']);table.setItem(index,column,item)
            table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
            table.setSortingEnabled(True);table.horizontalHeader().setStretchLastSection(True)
            table.resizeColumnsToContents();table.setColumnWidth(0,200);table.setColumnWidth(1,190)
            table.itemDoubleClicked.connect(lambda item: W.QMessageBox.information(dialog,'原因与兜底过程',
                item.toolTip() if item.column()!=5 else next((
                    '\n'.join(row['reasons'])+'\n\n'+json.dumps(row['fallback_attempts'],ensure_ascii=False,indent=2)
                    for row in rows if row['id']==item.data(C.Qt.UserRole)),item.toolTip())))
            layout.addWidget(table);dialog.setAttribute(C.Qt.WA_DeleteOnClose);dialog.show()
        except Exception as exc:self.error(exc)

    def calibrate_scene_materials(self):
        try:
            run=self.controller.render_background(self.background_preset.currentText().strip(),
                pipeline=self.inputs['scene_render_pipeline'].currentData(),calibration=True)
            self.log.appendPlainText('独立进程场景 GPU 校准：'+str(run)+'；逐字段候选仍需隔离校准，场景时间不能当作单材质时间。')
            self.lock(True)
        except Exception as exc:self.error(exc)

    def resume_scene_background(self):
        try:
            session=self.controller.standalone_render
            if not session:
                path,_=W.QFileDialog.getOpenFileName(self,'选择未完成渲染的 request.json',self.task_dialog_folder(),'Render request (request.json)')
                if not path:return
                request=read_json(Path(path))
                session={'request':request,'run':str(Path(path).parent)}
            self.controller.render_background(session['request']['preset'],resume=session['run'])
            self.lock(True)
        except Exception as exc:self.error(exc)

    def load_scene_results(self):
        path, _ = W.QFileDialog.getOpenFileName(self, '选择已转换的 USD 场景任务', self.task_dialog_folder(), 'Job (job.json)')
        if path:
            try:
                job = self.controller.load_scene_results(Path(path).parent)
                self.apply_settings(job.data['settings'], existing_job=True)
                self.last_state = None
                events = job.folder / 'events.jsonl'
                self.job_event_offset = events.stat().st_size if events.exists() else 0
                self.log.appendPlainText('已读取场景任务：' + str(job.folder))
                self.update_scene_actions()
            except Exception as exc:
                self.error(exc)

    def repair_white_materials(self):
        try:
            job = self.controller.job
            if not job: raise ValueError('请先选择已转换场景任务')
            from .material_workflow import white_materials
            rows=white_materials(read_json(job.folder/'manifest.json'),read_json(job.folder/'ue_report.json'))
            if not rows: raise ValueError('没有已记录的失败／占位材质；正常白色和需视觉复核材质不属于补修范围')
            dialog=W.QDialog(self); dialog.setWindowTitle('白模补修（实验性）'); dialog.resize(920,600); layout=W.QVBoxLayout(dialog)
            layout.addWidget(W.QLabel('只更新勾选的失败材质及必要坐标数据；原场景其它材质保留。补修结果仍需视觉复核。'))
            scroll=W.QScrollArea(); scroll.setWidgetResizable(True); content=W.QWidget(); rows_layout=W.QVBoxLayout(content)
            scroll.setWidget(content); layout.addWidget(scroll)
            choices=[]
            for row in rows:
                objects=', '.join(row['objects'][:3]) + (f' 等 {len(row["objects"])} 个物体' if len(row['objects'])>3 else '')
                check=W.QCheckBox(row['material']+' ['+row['id'][-8:]+'] · '+objects); check.setChecked(True)
                check.setToolTip('\n'.join([', '.join(row['objects'])]+row['reasons']+row['failures']));rows_layout.addWidget(check);choices.append((row['id'],check))
            rows_layout.addStretch()
            buttons=W.QDialogButtonBox(W.QDialogButtonBox.Ok|W.QDialogButtonBox.Cancel)
            buttons.button(W.QDialogButtonBox.Ok).setText('开始补修原场景')
            buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
            if dialog.exec()!=W.QDialog.Accepted:return
            folder=self.controller.repair_white_materials([identity for identity,check in choices if check.isChecked()])
            self.log.appendPlainText('实验性白模补修批次：'+str(folder));self.last_state=None;self.lock(True)
        except Exception as exc:self.error(exc)

    def switch_selected_materials(self):
        def selected(snapshot):
            from .material_surface import SURFACE_MODES, selected_surface_mode
            dialog = W.QDialog(self); dialog.setWindowTitle('UE 当前选择 · 材质分析与表面逻辑'); dialog.resize(860, 560)
            layout = W.QVBoxLayout(dialog)
            notice = W.QLabel('当前关卡：'+snapshot.get('level','')+'\n自动匹配转换记录，无需选择任务。只更新勾选槽位；独立网格体的切换会保存当前关卡。')
            notice.setWordWrap(True); layout.addWidget(notice)
            mode = W.QComboBox()
            for label, value in SURFACE_MODES: mode.addItem(label, value)
            mode.setCurrentIndex(mode.findData(selected_surface_mode(snapshot['targets'])))
            mode.installEventFilter(self)
            layout.addWidget(mode)
            scroll = W.QScrollArea(); scroll.setWidgetResizable(True)
            content = W.QWidget(); choices_layout = W.QVBoxLayout(content); choices = []
            for row in snapshot['targets']:
                check = W.QCheckBox(row['label']); check.setChecked(not row.get('reason'))
                check.setEnabled(not row.get('reason'))
                check.setToolTip(row.get('prim_path') or row.get('component_path', ''))
                choices_layout.addWidget(check); choices.append((row, check))
                facts = row.get('facts', {})
                blend = facts.get('blend_mode','未知')
                for name, label in (('BLEND_OPAQUE','不透明'), ('BLEND_MASKED','镂空遮罩'),
                                    ('BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE','透射透明'), ('BLEND_TRANSLUCENT','半透明')):
                    if name in blend: blend = label; break
                sided = {'True':'是', 'False':'否'}.get(facts.get('two_sided'), '未知')
                def connection(name):
                    value = facts.get(name, '不可读取')
                    return value if value in ('未连接','不可读取') else '已连接'
                text = '当前模式：'+blend+' · 双面：'+sided
                text += '\n遮罩输入：'+connection('MP_OPACITY_MASK')+' · 折射输入：'+connection('MP_REFRACTION')
                profile = facts.get('refraction_profile', '')
                if profile:
                    text += '\n折射处理：'+{'thin_raster':'薄板实时优化；光追／PT 保留源 IOR',
                        'index_of_refraction':'源 IOR', 'straight_transmission':'直通透明（不折射）'}.get(profile, profile)
                text += '\n'+('仅分析：'+row['reason'] if row.get('reason') else '来源任务：'+Path(row.get('job','当前任务')).name)
                details = W.QLabel(text); details.setWordWrap(True); details.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
                details.setToolTip('\n'.join([row.get('job',''),facts.get('asset',''),facts.get('parent','')]))
                choices_layout.addWidget(details)
            choices_layout.addStretch(); scroll.setWidget(content); layout.addWidget(scroll)
            buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
            buttons.button(W.QDialogButtonBox.Ok).setText('应用到勾选材质槽')
            buttons.button(W.QDialogButtonBox.Cancel).setText('关闭')
            buttons.button(W.QDialogButtonBox.Ok).setEnabled(any(not row.get('reason') for row in snapshot['targets']))
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
            if dialog.exec() != W.QDialog.Accepted: return
            targets = [row for row, check in choices if check.isChecked()]
            if not targets: self.error('请至少勾选一个材质槽'); return
            chosen = mode.currentData()
            self.execute(lambda: self.controller.switch_selected_materials(targets, chosen),
                         lambda result: self.log.appendPlainText('已切换 '+str(result['count'])+' 个材质槽；请检查画面。还原点：'+result['folder']))
        self.execute(lambda: self.controller.inspect_selected_materials(self.inputs['project'].text().strip()), selected)

    def undo_selected_materials(self):
        self.execute(self.controller.undo_selected_materials,
                     lambda result: self.log.appendPlainText('已撤销材质表面切换：'+result['folder']))

    def undo_material_repair(self):
        try:
            folders=sorted((self.controller.job.folder/'MaterialRepairs').glob('R_*'),reverse=True)
            batch=next((p for p in folders if (p/'receipt.json').is_file() and read_json(p/'receipt.json').get('committed') and not read_json(p/'receipt.json').get('undone')),None)
            if not batch:raise ValueError('没有可撤销的补修批次')
            self.controller.undo_material_repair(batch.name);self.last_state=None;self.lock(True)
        except Exception as exc:self.error(exc)

    def reconvert_scene_materials(self):
        try:
            archive = self.controller.reconvert_scene_materials(self.settings())
            self.log.appendPlainText('旧材质与图片已归档：' + str(archive))
            self.last_state = None
            self.lock(True)
        except Exception as exc:
            self.error(exc)

    def render_scene_reference(self, kind):
        try:
            self.controller.render_reference(kind, pipeline=self.inputs['scene_render_pipeline'].currentData(),
                                             memory_profile=self.inputs['ue_render_memory_profile'].currentData())
            self.lock(True)
            self.tabs.setCurrentIndex(self.task_tab_index)
            self.update_scene_actions()
        except Exception as exc:
            self.error(exc)

    def choose_background_output(self):
        folder = W.QFileDialog.getExistingDirectory(self, '选择后台渲染输出目录',
            self.background_output.text().strip() or self.dialog_paths.get('background_output', self.task_dialog_folder()))
        if folder:
            self.background_output.setText(folder)

    def render_scene_background(self):
        preset = self.background_preset.currentText().strip()
        output = self.background_output.text().strip()
        frame = self.background_frame.value() if self.background_range.currentData() == 'frame' else None
        pipeline = self.inputs['scene_render_pipeline'].currentData()
        self.lock(True)
        def started(result):
            if isinstance(result, tuple):
                self.lock(False)
                self.error(result[1])
                return
            self.log.appendPlainText('后台 UE 渲染已启动：' + str(result))
            self.lock(True)
            self.last_state = None
            self.update_scene_actions()
        def launch():
            try:
                return self.controller.render_background(preset, output, frame, pipeline)
            except Exception as exc:
                return ('error', str(exc))
        self.execute(launch, started)

    def stop_scene_background(self):
        try:
            self.controller.stop_background()
            self.lock(False)
            self.background_status.setText('后台渲染已停止；已写出的文件保留。')
        except Exception as exc:
            self.error(exc)

    def open_background_output(self):
        session = self.controller.standalone_render
        if session:
            G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(session['request']['output']))

    def compare_scene_reference(self):
        if not self.controller.job:
            self.error('请先选择场景任务')
            return
        from .scene_reference import compare
        folder = self.controller.job.folder
        self.comparison_button.setEnabled(False)
        def compared(path):
            self.update_scene_actions()
            self.log.appendPlainText('对照与差分已生成：' + str(path) + '；还原效果需人工复核')
            self.show_scene_visual('comparison.png')
        self.execute(lambda: compare(folder), compared)

    def show_scene_visual(self, name):
        path = self.controller.job.folder / 'visual' / name if self.controller.job else None
        if not path or not path.is_file():
            self.error('对照图尚未生成')
            return
        if name == 'ue.png' and (path.parent/'ue_render.json').is_file():
            receipt = read_json(path.parent/'ue_render.json')
            if receipt.get('complete') is False or (receipt.get('image_sha256') and file_hash(path) != receipt['image_sha256']):
                self.error('UE 图片与成功回执不匹配，请等待渲染完成或重新渲染')
                return
        if name == 'comparison.png' and (path.parent/'difference.json').is_file():
            hashes = read_json(path.parent/'difference.json').get('image_hashes',{})
            if any(not (path.parent/(key+'.png')).is_file() or file_hash(path.parent/(key+'.png')) != digest
                   for key,digest in hashes.items() if key in ('blender','ue')):
                self.error('参考帧已更新，请重新生成对照与差分')
                return
        dialog = W.QDialog(self)
        dialog.setWindowTitle({'blender.png':'Blender 参考图', 'ue.png':'UE 渲染图',
                              'comparison.png':'Blender / UE / 差分 ×4'}.get(name,name) + ' · 需复核')
        dialog.resize(1100, 520)
        layout = W.QVBoxLayout(dialog)
        label = W.QLabel()
        pixmap = G.QPixmap(str(path))
        label.setPixmap(pixmap.scaled(1060, 460, C.Qt.KeepAspectRatio, C.Qt.SmoothTransformation))
        layout.addWidget(label)
        dialog.setAttribute(C.Qt.WA_DeleteOnClose)
        dialog.show()

    def copy_render_config(self):
        if not self.scene_mrq_path:
            self.error('请先渲染所选管线的 UE 参考帧，以生成完整 MRQ 配置')
            return
        W.QApplication.clipboard().setText(self.scene_mrq_path)
        self.log.appendPlainText('已复制完整 MRQ 配置：' + self.scene_mrq_path)

    def update_scene_actions(self):
        job = self.controller.job
        folder = job.folder if job else None
        busy = self.main_flow_locked or bool(self.workers) or bool(self.controller.processes) or self.controller.reference_busy()
        scene = read_json(folder / 'manifest.json').get('scene', {}) if folder and (folder / 'manifest.json').exists() else {}
        report = read_json(folder / 'ue_report.json') if folder and (folder / 'ue_report.json').is_file() else {}
        assets = report.get('assets', {})
        preferred = assets.get('render_presets', {}).get(self.inputs['scene_render_pipeline'].currentData())
        presets = list(dict.fromkeys(path for path in [preferred, assets.get('render_preset'),
            *assets.get('render_presets', {}).values(), *assets.get('memory_presets', {}).values()] if path))
        signature = (str(folder), tuple(presets))
        if signature != self.background_options_signature:
            current = self.background_preset.currentText() if self.background_options_signature and self.background_options_signature[0] == str(folder) else ''
            self.background_preset.clear()
            self.background_preset.addItems(presets)
            if current:
                self.background_preset.setCurrentText(current)
            self.background_options_signature = signature
        session = self.controller.standalone_render
        background_active = bool(session and session.get('active'))
        self.background_render_button.setEnabled(bool(folder and not busy and assets.get('level') and assets.get('sequence')))
        self.background_stop_button.setEnabled(background_active)
        self.background_resume_button.setEnabled(bool(self.controller.job and not busy and
            (not session or not session.get('result',{}).get('complete'))))
        self.material_calibration_button.setEnabled(bool(folder and not busy and assets.get('level') and assets.get('sequence')))
        self.background_open_button.setEnabled(bool(session))
        for widget in (self.background_preset, self.background_range, self.background_output, self.background_output_button):
            widget.setEnabled(not busy)
        self.background_frame.setEnabled(not busy and self.background_range.currentData() == 'frame')
        if session:
            runtime_path = session['run'] / 'runtime.json'
            runtime = read_json(runtime_path) if runtime_path.is_file() else {}
            result_path = session['run'] / 'result.json'
            result = read_json(result_path) if result_path.is_file() else {}
            text = ('后台渲染中：' + format(runtime.get('progress', 0) * 100, '.1f') + '%'
                    if background_active else ('后台渲染完成，画面需复核' if result.get('complete') else
                    '后台渲染已停止' if result.get('state') == 'stopped' else '后台渲染失败，请查看日志'))
            self.background_status.setText(text + '\n输出：' + session['request']['output'] + '\n日志与回执：' + str(session['run']))
        ready = bool(scene.get('route') == 'usd_stage' and scene.get('reference_camera'))
        self.scene_result_button.setEnabled(not busy)
        self.material_analysis_button.setEnabled(not busy)
        surface_busy = busy or bool(self.workers)
        self.surface_switch_button.setEnabled(not surface_busy)
        last_path = self.controller.home/'material_surface_last.json'
        surface_folder = Path(read_json(last_path)['job']) if last_path.is_file() else folder
        surfaces = sorted((surface_folder/'MaterialSurfaces').glob('S_*'), reverse=True) if surface_folder else []
        self.surface_undo_button.setEnabled(bool(not surface_busy and any(
            (p/'journal.json').is_file() and read_json(p/'journal.json').get('state') == 'applying' or
            (p/'receipt.json').is_file() and read_json(p/'receipt.json').get('committed') and
            not read_json(p/'receipt.json').get('undone') for p in surfaces)))
        self.blender_reference_button.setEnabled(ready and not busy)
        self.scene_reconvert_button.setEnabled(bool(folder and not busy and (folder/'scene_checkpoint.json').is_file()))
        from .material_workflow import white_materials
        self.white_repair_button.setEnabled(bool(folder and not busy and assets.get('level') and white_materials({'scene':scene},report)))
        repairs=sorted((folder/'MaterialRepairs').glob('R_*'),reverse=True) if folder else []
        self.white_undo_button.setEnabled(bool(not busy and any((p/'receipt.json').is_file() and read_json(p/'receipt.json').get('committed') and not read_json(p/'receipt.json').get('undone') for p in repairs)))
        self.ue_reference_button.setEnabled(ready and not busy and all(assets.get(key) for key in ('level','sequence','render_preset')))
        self.comparison_button.setEnabled(bool(ready and not busy and all((folder / 'visual' / (name + '.png')).is_file() for name in ('blender', 'ue'))))
        self.show_comparison_button.setEnabled(bool(folder and (folder / 'visual/comparison.png').is_file()))
        self.scene_report_button.setEnabled(bool(folder and (folder / 'ue_report.json').is_file()))
        self.scene_result_label.setText(str(folder) if folder else '未选择场景任务')
        pipeline = self.inputs['scene_render_pipeline'].currentData()
        self.ue_reference_button.setText('渲染 UE 参考帧（' + ('路径追踪' if pipeline == 'path_tracing' else 'Lumen') + '）')
        self.scene_mrq_path = assets.get('render_presets', {}).get(pipeline, '')
        self.copy_render_config_button.setEnabled(bool(self.scene_mrq_path))
        self.scene_folder_button.setEnabled(bool(folder))
        self.scene_assets_label.setText('\n'.join(('关卡：' + assets.get('level','未生成'),
            '序列：' + assets.get('sequence','未生成'),
            '完整 MRQ：' + (self.scene_mrq_path or '所选管线尚未生成，请先渲染 UE 参考帧'))))
        receipt = read_json(folder / 'visual/ue_render.json') if folder and (folder / 'visual/ue_render.json').is_file() else {}
        mode = {'lumen':'Lumen', 'path_tracing':'路径追踪'}.get(receipt.get('renderer',{}).get('mode'),'未记录管线')
        renderer = receipt.get('renderer',{})
        if renderer.get('mode') == 'lumen' and not (renderer.get('path_tracing_force_off_observed') and renderer.get('ShowFlag.PathTracing') == 0):
            mode += '（旧回执未确认关闭路径追踪）'
        camera = receipt.get('source_camera') or {}
        text = mode + (' · 渲染完成，画面需复核' if receipt.get('complete') else ' · 无有效完成回执') if receipt else '尚未生成'
        if camera:
            text += '\n源镜头：' + str(camera.get('focal_length')) + ' mm，f/' + format(camera.get('aperture',0),'.3g')
            focus = camera.get('focus_method','')
            text += '，关闭景深' if 'DISABLE' in focus.upper() else '，对焦距离 ' + format(camera.get('focus_distance_cm',0)/100,'.3g') + ' m'
        self.scene_render_label.setText(text)
        self.show_blender_button.setEnabled(bool(folder and (folder / 'visual/blender.png').is_file()))
        self.show_ue_button.setEnabled(bool(folder and (folder / 'visual/ue.png').is_file()))
        self.open_job_button.setEnabled(bool(folder))
        self.report_button.setEnabled(bool(folder))
        status = read_json(folder / 'status.json').get('state') if folder and (folder / 'status.json').exists() else ''
        manual_export = status in ('awaiting_export', 'manual_export', 'export_paused')
        self.reopen_button.setEnabled(bool(folder and manual_export))
        self.attach_button.setEnabled(bool(folder and manual_export))
        self.resume_button.setEnabled(not busy)
        self._update_main_start_button_state()

    def page(self,title):
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        widget = W.QWidget()
        form = W.QFormLayout(widget)
        form.setSizeConstraint(W.QLayout.SetMinAndMaxSize)
        form.setSpacing(13)
        form.setContentsMargins(20,20,20,20)
        form.setFieldGrowthPolicy(W.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(W.QFormLayout.WrapLongRows)
        heading = W.QLabel(title)
        heading.setObjectName('pageTitle')
        if title in ('参数','场景还原'):
            page = W.QWidget()
            stack = W.QVBoxLayout(page)
            stack.setContentsMargins(20,20,20,20)
            stack.setSpacing(13)
            stack.setSizeConstraint(W.QLayout.SetMinAndMaxSize)
            stack.setAlignment(C.Qt.AlignTop)
            stack.addWidget(heading)
            stack.addWidget(widget)
            form.setContentsMargins(0,0,0,0)
            form.section_stack = stack
            form.sections_before_form = title == '场景还原'
        else:
            page = widget
            form.addRow(heading)
        scroll.setWidget(page)
        self.tabs.addTab(scroll,title)
        # Return the actual tab page so indexOf() and currentChanged handling
        # refer to the QTabWidget page, not its inner form widget.
        return scroll,form

    def build_navigation(self):
        for title,indices in (("工作区",(self.files_tab_index,1,self.scene_appearance_tab_index,self.task_tab_index)),
                              ("专用工具",(self.camera_cut_tab_index,self.physics_bake_tab_index,self.mmm_tab_index))):
            header = W.QListWidgetItem(title)
            header.setFlags(C.Qt.NoItemFlags)
            header.setSizeHint(C.QSize(180,48 if title == '工作区' else 76))
            self.navigation.addItem(header)
            group = W.QWidget()
            group.setObjectName('navigationGroup')
            group_layout = W.QVBoxLayout(group)
            group_layout.setContentsMargins(10,6,10,6)
            if title == '专用工具':
                group_layout.addStretch(1)
                divider = W.QFrame()
                divider.setObjectName('navigationDivider')
                divider.setFrameShape(W.QFrame.HLine)
                group_layout.addWidget(divider)
            caption = W.QLabel(title)
            caption.setObjectName('navigationGroupTitle')
            caption.setAlignment(C.Qt.AlignCenter)
            caption.setMinimumHeight(20)
            group_layout.addWidget(caption)
            self.navigation.setItemWidget(header,group)
            for index in indices:
                item = W.QListWidgetItem(self.tabs.tabText(index))
                item.setData(C.Qt.UserRole,index)
                item.setToolTip(self.tabs.tabText(index))
                item.setSizeHint(C.QSize(180,48))
                self.navigation.addItem(item)
        self.select_navigation(self.tabs.currentIndex())

    def navigate_page(self,row):
        item = self.navigation.item(row)
        if item is not None and item.data(C.Qt.UserRole) is not None:
            self.tabs.setCurrentIndex(item.data(C.Qt.UserRole))

    def select_navigation(self,index):
        for row in range(self.navigation.count()):
            if self.navigation.item(row).data(C.Qt.UserRole) == index:
                self.navigation.setCurrentRow(row)
                break

    def parameter_group(self,form,title):
        toggle = W.QToolButton()
        toggle.setObjectName('sectionToggle')
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setArrowType(C.Qt.RightArrow)
        toggle.setToolButtonStyle(C.Qt.ToolButtonTextBesideIcon)
        toggle.setSizePolicy(W.QSizePolicy.Expanding,W.QSizePolicy.Fixed)
        content = W.QWidget()
        content.setObjectName('sectionContent')
        fields = W.QFormLayout(content)
        fields.setSizeConstraint(W.QLayout.SetMinimumSize)
        fields.setContentsMargins(12,21,12,12)
        fields.setSpacing(13)
        fields.setFieldGrowthPolicy(W.QFormLayout.AllNonFixedFieldsGrow)
        fields.setRowWrapPolicy(W.QFormLayout.WrapLongRows)
        clip = SectionClip(content)
        clip.setObjectName('sectionClip')
        group = W.QWidget()
        group_layout = W.QVBoxLayout(group)
        group_layout.setContentsMargins(0,0,0,0)
        group_layout.setSpacing(0)
        group_layout.setSizeConstraint(W.QLayout.SetMinimumSize)
        group_layout.addWidget(toggle)
        group_layout.addWidget(clip)
        position = form.section_stack.count() - 1 if form.sections_before_form else form.section_stack.count()
        form.section_stack.insertWidget(position,group)
        clip.hide()
        toggle.toggled.connect(lambda expanded: self.motion.fold(clip,content,expanded))
        toggle.toggled.connect(lambda expanded: toggle.setArrowType(C.Qt.DownArrow if expanded else C.Qt.RightArrow))
        return fields

    def build_physics_bake_page(self):
        """独立PhysicsBake预处理工具：只改PMX显示枠，不进入自动化任务流程。"""
        page, form = self.page("PhysicsBakeFix")
        self.physics_bake_tab_index = self.tabs.indexOf(page)
        intro = W.QLabel(
            "独立工具流程：选择 PMX → 检测 → 人工复核 → 修复 → 输出新 PMX。\n"
            "只把「被 Mode 1/2 刚体驱动、但不在任何显示枠中」的骨骼加入 PhysicsBake，"
            "不改动刚体、Joint、骨骼层级或作者的原有显示枠。\n"
            "本工具不会自动加入角色、场景或镜头导入。"
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        row = W.QHBoxLayout()
        self.physics_bake_model = W.QLineEdit()
        self.physics_bake_model.setClearButtonEnabled(True)
        self.physics_bake_model.setPlaceholderText("选择要检查的 .pmx 模型")
        row.addWidget(self.physics_bake_model)
        self.button(row, "选择 PMX…", self.browse_physics_bake_model)
        self.path_row(form,"输入模型",row)

        self.physics_bake_status = W.QLabel("未检测")
        self.physics_bake_status.setWordWrap(True)
        form.addRow("当前状态", self.physics_bake_status)

        controls = W.QHBoxLayout()
        self.physics_bake_analyze_button = self.button(
            controls, "① 检测（不写文件）", self.analyze_physics_bake)
        self.physics_bake_repair_button = self.button(
            controls, "② 修复并输出新 PMX", self.repair_physics_bake)
        self.physics_bake_repair_button.setObjectName("primary")
        self.physics_bake_repair_button.setEnabled(False)
        form.addRow(controls)

        self.physics_bake_summary = W.QPlainTextEdit()
        self.physics_bake_summary.setReadOnly(True)
        self.physics_bake_summary.setMinimumHeight(300)
        form.addRow("检测结果", self.physics_bake_summary)

        note = W.QLabel(
            "检测只读取文件，不产生任何写入。修复始终输出到 <原名>_PhysicsBakeFixed.pmx，"
            "绝不覆盖原始模型。修复后的 PMX 再交给 MMM 做物理焼き込み。"
        )
        note.setWordWrap(True)
        form.addRow(note)

    def browse_physics_bake_model(self):
        path, _ = W.QFileDialog.getOpenFileName(
            self, "选择 PMX 模型", self.physics_bake_model.text(), "PMX (*.pmx)")
        if path:
            self.physics_bake_model.setText(path)
            self.physics_bake_status.setText("已选择模型，请先执行检测")
            self.physics_bake_repair_button.setEnabled(False)
            self.physics_bake_summary.clear()

    def analyze_physics_bake(self):
        path = self.physics_bake_model.text().strip()
        if not path:
            self.error("请先选择要检测的 PMX 模型")
            return
        self.physics_bake_status.setText("正在检测…")
        self.physics_bake_repair_button.setEnabled(False)
        self.execute(lambda: physics_bake_analyze(path), self._physics_bake_analyzed)

    def _physics_bake_analyzed(self, payload):
        result, report = payload
        self.physics_bake_summary.setPlainText(report)
        if result.errors:
            self.physics_bake_status.setText(f"检测失败：{result.errors[0]}")
            return
        self.physics_bake_status.setText(result.summary())
        if result.missing_physics_bone_count:
            self.physics_bake_repair_button.setEnabled(True)
        else:
            self.physics_bake_repair_button.setEnabled(False)
        self.log.appendPlainText(f"[physics_bake] {result.model_name}: "
                                f"{result.missing_physics_bone_count} 根物理骨缺少显示枠")

    def repair_physics_bake(self):
        path = self.physics_bake_model.text().strip()
        if not path:
            self.error("请先选择要修复的 PMX 模型")
            return
        self.physics_bake_status.setText("正在修复…")
        self.physics_bake_repair_button.setEnabled(False)
        self.execute(lambda: physics_bake_repair(path), self._physics_bake_repaired)

    def _physics_bake_repaired(self, payload):
        result, report = payload
        self.physics_bake_summary.setPlainText(report)
        if result.errors:
            self.physics_bake_status.setText(f"修复失败：{result.errors[0]}")
            return
        self.physics_bake_status.setText(
            f"完成：新增 {len(result.added_bone_indices)} 根骨骼到 {result.target_frame}")
        self.log.appendPlainText(f"[physics_bake] 输出：{result.output_path}")
        W.QMessageBox.information(self, "PhysicsBake 修复", result.summary())

    def build_mmm_page(self):
        """独立MMM工具页：检测 MMM、准备隔离 Runtime、安装/卸载 telemetry 插件。

        MMM 是免安装软件、不写注册表，因此以**手动指定目录**为主（方案 §8），
        自动检测只作为首次配置的辅助。所有写操作都限制在
        `%LOCALAPPDATA%/MMDBridge/mmm_runtime/` 或用户明确选择的 MMM 目录里。
        """
        page, form = self.page("MMM 物理烘焙（已弃用）")
        self.mmm_tab_index = self.tabs.indexOf(page)
        intro = W.QLabel(
            "已弃用：暂时停止维护，现有功能仍可使用；推荐默认 MMDBridge 角色流程。\n"
            "MMM 承担「把物理解算固化成骨骼关键帧」这一步。本页负责环境准备：\n"
            "① 指定 MMM 安装目录 → ② 复制出隔离 Runtime → ③ 安装 telemetry 插件。\n"
            "隔离 Runtime 位于 %LOCALAPPDATA%\\MMDBridge\\mmm_runtime\\，"
            "你的原始 MMM 目录不会被修改。"
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        row = W.QHBoxLayout()
        self.inputs["mmm_path"] = W.QLineEdit()
        self.inputs["mmm_path"].setClearButtonEnabled(True)
        self.inputs["mmm_path"].setPlaceholderText(
            "选择 MikuMikuMoving64.exe 所在目录")
        row.addWidget(self.inputs["mmm_path"])
        self.button(row, "选择目录…", self.browse_mmm_dir)
        self.path_row(form,"MMM 目录",row)

        detect_row = W.QHBoxLayout()
        self.button(detect_row, "检测目录", self.apply_mmm_path)
        self.button(detect_row, "自动搜索", self.detect_mmm)
        self.button(detect_row, "打开目录", self.open_mmm_dir)
        form.addRow(detect_row)

        self.mmm_status = W.QLabel("未指定 MMM 目录")
        self.mmm_status.setWordWrap(True)
        form.addRow("安装状态", self.mmm_status)

        form.addRow(W.QLabel("—— 隔离 Runtime ——"))
        self.mmm_runtime_status = W.QLabel("尚未准备")
        self.mmm_runtime_status.setWordWrap(True)
        self.mmm_runtime_status.setSizePolicy(W.QSizePolicy.Ignored,W.QSizePolicy.Preferred)
        form.addRow("Runtime", self.mmm_runtime_status)
        runtime_row = W.QHBoxLayout()
        self.mmm_prepare_button = self.button(
            runtime_row, "复制到隔离 Runtime", self.prepare_mmm_runtime)
        self.button(runtime_row, "打开 Runtime 目录", self.open_mmm_runtime)
        form.addRow(runtime_row)

        form.addRow(W.QLabel("—— Telemetry 插件 ——"))
        self.mmm_plugin_status = W.QLabel("尚未检查")
        self.mmm_plugin_status.setWordWrap(True)
        form.addRow("插件状态", self.mmm_plugin_status)
        plugin_row = W.QHBoxLayout()
        self.mmm_install_button = self.button(
            plugin_row, "安装插件", self.install_mmm_plugin)
        self.mmm_install_button.setObjectName("primary")
        self.mmm_uninstall_button = self.button(
            plugin_row, "卸载插件", self.uninstall_mmm_plugin)
        form.addRow(plugin_row)

        note = W.QLabel(
            "安装 = 复制 MMDBridgeMMMPlugin.dll 到 <目标>\\Plugins\\（同名旧文件会先备份）。\n"
            "卸载 = 删除该 DLL 并清理遗留备份；不影响其它插件。\n"
            "插件只读取 MMM 内部状态，不执行 Physics Bake —— "
            "烘焙触发仍由 UI Automation 负责。"
        )
        note.setWordWrap(True)
        form.addRow(note)

        self.refresh_mmm_status()

    # -- MMM 页辅助 ------------------------------------------------------

    def _mmm_target_dir(self):
        """插件安装目标：优先隔离 Runtime，其次用户指定的 MMM 目录。"""
        status = mmm_rt.runtime_status()
        if status["prepared"]:
            return Path(status["runtime_dir"])
        text = self.inputs["mmm_path"].text().strip()
        return Path(text) if text else None

    @staticmethod
    def reveal(path):
        G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(path)))

    def _mmm_build_dll(self):
        return (resource_root() / "plugins" / "mmm"
                / "MMDBridgeMMMPlugin" / "bin" / "Release"
                / mmm_rt.PLUGIN_DLL_NAME)

    def refresh_mmm_status(self):
        text = self.inputs["mmm_path"].text().strip()
        if not text:
            self.mmm_status.setText("未指定 MMM 目录")
            self.mmm_status.setStyleSheet("color:#c9a227;")
        else:
            try:
                install = mmm_rt.validate_mmm_dir(text)
            except mmm_rt.MmmRuntimeError as exc:
                self.mmm_status.setText(f"✗ {exc.message.splitlines()[0]}")
                self.mmm_status.setStyleSheet("color:#e05561;")
            else:
                mark = "✓" if install.supported else "⚠"
                color = "#6cc26c" if install.supported else "#c9a227"
                self.mmm_status.setText(
                    f"{mark} {install.executable.name} · "
                    f"{install.file_version or '版本未知'} · "
                    f"ProductName={install.product_name or '未知'}")
                self.mmm_status.setStyleSheet(f"color:{color};")

        runtime = mmm_rt.runtime_status()
        if runtime["prepared"]:
            baseline = ("、".join(runtime["baseline_files"])
                        if runtime["baseline_files"] else "无")
            self.mmm_runtime_status.setText(
                f"✓ 已准备\n{runtime['runtime_dir']}\nbaseline: {baseline}")
            self.mmm_runtime_status.setStyleSheet("color:#6cc26c;")
        else:
            self.mmm_runtime_status.setText(
                f"尚未准备\n将复制到：{runtime['runtime_dir']}")
            self.mmm_runtime_status.setStyleSheet("color:#c9a227;")

        target = self._mmm_target_dir()
        if target is None:
            self.mmm_plugin_status.setText("尚未检查（先指定 MMM 目录或准备 Runtime）")
            self.mmm_plugin_status.setStyleSheet("color:#c9a227;")
            return
        status = mmm_rt.plugin_status(target, expected_dll=self._mmm_build_dll())
        if status["installed"]:
            extra = ""
            if status["matches_build"] is False:
                extra = "  ⚠ 与当前构建大小不一致，建议重装"
            self.mmm_plugin_status.setText(
                f"✓ 已安装（{status['size']} 字节，{status['mtime']}）{extra}\n"
                f"{status['plugin_path']}")
            self.mmm_plugin_status.setStyleSheet(
                "color:#c9a227;" if extra else "color:#6cc26c;")
        else:
            self.mmm_plugin_status.setText(f"未安装\n{status['plugin_path']}")
            self.mmm_plugin_status.setStyleSheet("color:#c9a227;")

    def detect_mmdbridge_directory(self):
        if self.main_flow_locked or not self.detect_mmdbridge_button.isEnabled():
            return
        field = self.inputs['mmdbridge_path']
        original = field.text()
        extra = [p for p in (original.strip(), self.dialog_paths.get('mmdbridge_path', '')) if p]
        self.detect_mmdbridge_button.setEnabled(False)
        self.detect_mmdbridge_button.setText('检测中…')

        def found(paths):
            if field.text() != original:
                self.log.appendPlainText('MMDBridge 检测结束；保留检测期间手动修改的安装路径。')
                return
            if not paths:
                W.QMessageBox.information(self, '自动检测 MMDBridge', '未在常见位置找到完整的 rintrint/mmdbridge 配套程序，请使用“选择目录…”手动指定包含 MikuMikuDance_CHS.exe 的安装目录。')
                return
            chosen = paths[0]
            if len(paths) > 1:
                chosen, ok = W.QInputDialog.getItem(self, '选择 MMDBridge 安装', '找到多个完整安装目录', paths, 0, False)
                if not ok:
                    return
            field.setText(chosen)
            self.log.appendPlainText('已检测并填入 MMDBridge 安装目录：' + chosen)

        def cleanup():
            self.detect_mmdbridge_button.setText('自动检测')
            self.detect_mmdbridge_button.setEnabled(not self.main_flow_locked)

        self.execute(lambda: discover_mmdbridge(extra_roots=extra), found, cleanup)

    def browse_directory(self, key):
        path = W.QFileDialog.getExistingDirectory(
            self, "选择目录", self.path_dialog_folder(key))
        if path:
            self.inputs[key].setText(path)

    def browse_mmm_dir(self):
        path = W.QFileDialog.getExistingDirectory(
            self, "选择 MMM 安装目录", self.inputs["mmm_path"].text().strip())
        if path:
            self.inputs["mmm_path"].setText(path)
            self.refresh_mmm_status()

    def apply_mmm_path(self):
        text = self.inputs["mmm_path"].text().strip()
        if not text:
            self.error("请先选择 MMM 安装目录")
            return
        try:
            install = mmm_rt.validate_mmm_dir(text)
        except mmm_rt.MmmRuntimeError as exc:
            self.error(exc.message)
            self.refresh_mmm_status()
            return
        self.log.appendPlainText(f"[mmm] 已识别 {install.summary()}")
        self.refresh_mmm_status()

    def detect_mmm(self):
        self.mmm_status.setText("正在搜索…")
        self.mmm_status.setStyleSheet("color:#c9a227;")
        self.execute(mmm_rt.discover_mmm, self._mmm_detected)

    def _mmm_detected(self, found):
        self.refresh_mmm_status()
        if not found:
            W.QMessageBox.information(
                self, "MMM 自动搜索",
                "没有在常见目录中找到 MMM。\n\n"
                "MMM 是免安装软件、不写注册表，自动搜索只能覆盖常见位置。\n"
                "请用「选择目录…」手动指定 MikuMikuMoving64.exe 所在目录。")
            return
        if len(found) == 1:
            self.inputs["mmm_path"].setText(str(found[0].directory))
            self.refresh_mmm_status()
            self.log.appendPlainText(f"[mmm] 自动找到 {found[0].summary()}")
            return
        labels = [f"{item.directory}  ({item.file_version or '版本未知'})"
                  for item in found]
        choice, ok = W.QInputDialog.getItem(
            self, "MMM 自动搜索", "找到多个 MMM，请选择：", labels, 0, False)
        if ok and choice:
            self.inputs["mmm_path"].setText(
                str(found[labels.index(choice)].directory))
            self.refresh_mmm_status()

    def open_mmm_dir(self):
        text = self.inputs["mmm_path"].text().strip()
        if not text or not Path(text).is_dir():
            self.error("请先指定一个有效的 MMM 目录")
            return
        self.reveal(Path(text))

    def open_mmm_runtime(self):
        target = Path(mmm_rt.runtime_status()["runtime_dir"])
        if not target.is_dir():
            self.error("隔离 Runtime 尚未准备，请先点击「复制到隔离 Runtime」")
            return
        self.reveal(target)

    def prepare_mmm_runtime(self):
        text = self.inputs["mmm_path"].text().strip()
        if not text:
            self.error("请先选择 MMM 安装目录")
            return
        status = mmm_rt.runtime_status()
        if status["prepared"]:
            answer = W.QMessageBox.question(
                self, "重新复制 Runtime",
                f"隔离 Runtime 已存在：\n{status['runtime_dir']}\n\n"
                "重新复制会**清空并重建**该目录，其中的改动将丢失。\n"
                "（你的原始 MMM 目录不受影响）\n\n确定继续？")
            if answer != W.QMessageBox.Yes:
                return
        self.mmm_runtime_status.setText("正在复制…")
        self.execute(
            lambda: mmm_rt.prepare_runtime(text, force=status["prepared"]),
            self._mmm_runtime_prepared)

    def _mmm_runtime_prepared(self, target):
        self.refresh_mmm_status()
        self.log.appendPlainText(f"[mmm] 隔离 Runtime 已就绪：{target}")
        W.QMessageBox.information(
            self, "隔离 Runtime",
            f"已准备好隔离 Runtime：\n{target}\n\n"
            "之后的 MMM 操作都在这个副本上进行，你的原始 MMM 目录不会被修改。")

    def install_mmm_plugin(self):
        target = self._mmm_target_dir()
        if target is None:
            self.error("请先指定 MMM 目录，或先准备隔离 Runtime")
            return
        dll = self._mmm_build_dll()
        if not dll.is_file():
            W.QMessageBox.warning(
                self, "找不到插件 DLL",
                f"未找到：\n{dll}\n\n"
                "请先构建插件：\n"
                "dotnet build plugins\\mmm\\MMDBridgeMMMPlugin\\"
                "MMDBridgeMMMPlugin.csproj -c Release "
                '-p:MmmPath="<你的 MMM 目录>"')
            return
        try:
            installed = mmm_rt.install_plugin(target, dll)
        except mmm_rt.MmmRuntimeError as exc:
            self.error(exc.message)
            self.refresh_mmm_status()
            return
        self.refresh_mmm_status()
        self.log.appendPlainText(f"[mmm] 插件已安装：{installed}")
        W.QMessageBox.information(
            self, "安装插件",
            f"已安装到：\n{installed}\n\n"
            "重启 MMM 后插件才会加载。")

    def uninstall_mmm_plugin(self):
        target = self._mmm_target_dir()
        if target is None:
            self.error("请先指定 MMM 目录，或先准备隔离 Runtime")
            return
        status = mmm_rt.plugin_status(target)
        if not status["installed"]:
            W.QMessageBox.information(self, "卸载插件", "该目录下没有安装本插件。")
            self.refresh_mmm_status()
            return
        answer = W.QMessageBox.question(
            self, "卸载插件",
            f"将从以下位置移除 {mmm_rt.PLUGIN_DLL_NAME}：\n"
            f"{status['plugin_path']}\n\n"
            "并清理遗留的 .bak- 备份。其它插件不受影响。\n\n确定继续？")
        if answer != W.QMessageBox.Yes:
            return
        try:
            removed = mmm_rt.uninstall_plugin(target)
        except mmm_rt.MmmRuntimeError as exc:
            self.error(exc.message)
            return
        self.refresh_mmm_status()
        self.log.appendPlainText(f"[mmm] 插件已卸载（{target}）")
        W.QMessageBox.information(
            self, "卸载插件",
            "已卸载。" if removed else "没有需要移除的文件。")

    def build_camera_cut_page(self):
        """独立CameraCutAuto工具：不读取主自动化素材，也不改变主流程状态。"""
        page, form = self.page("CameraCutAuto")
        self.camera_cut_tab_index = self.tabs.indexOf(page)
        intro = W.QLabel(
            "独立工具流程：Probe → Detect → 人工复核/编辑帧序列 → Bake → Verify。\n"
            "请先在 UE 中打开目标 Level Sequence；本工具不会自动加入角色、场景或镜头导入。"
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        self.camera_cut_status = W.QLabel("未启动")
        self.camera_cut_status.setWordWrap(True)
        form.addRow("当前状态", self.camera_cut_status)
        self.camera_cut_bridge_status = W.QLabel("未选择 UE 项目")
        self.camera_cut_bridge_status.setWordWrap(True)
        form.addRow("UE 桥接状态", self.camera_cut_bridge_status)
        self.camera_cut_steps = W.QLabel("① Probe  ② Detect  ③ 人工复核  ④ Bake  ⑤ Verify")
        form.addRow("执行顺序", self.camera_cut_steps)

        controls = W.QHBoxLayout()
        self.camera_cut_start_button = self.button(controls, "开始 Probe + Detect", self.start_camera_cut)
        self.camera_cut_bake_button = self.button(controls, "确认帧序列并 Bake", self.confirm_camera_cut)
        self.camera_cut_bake_button.setObjectName("primary")
        self.camera_cut_bake_button.setEnabled(False)
        form.addRow(controls)

        self.camera_cut_frame_edit = W.QLineEdit()
        self.camera_cut_frame_edit.setPlaceholderText("添加帧号，例如：700 或 700, 1260, 1800")
        add_row = W.QHBoxLayout()
        add_row.addWidget(self.camera_cut_frame_edit)
        self.button(add_row, "添加帧", self.add_camera_cut_frames)
        form.addRow("添加切点", add_row)

        self.camera_cut_delete_frame_edit = W.QLineEdit()
        self.camera_cut_delete_frame_edit.setPlaceholderText("删除帧号；不存在的帧会自动忽略")
        delete_row = W.QHBoxLayout()
        delete_row.addWidget(self.camera_cut_delete_frame_edit)
        self.button(delete_row, "删除填写帧", self.remove_camera_cut_frames_by_input)
        self.button(delete_row, "删除选中帧", self.remove_camera_cut_frames)
        self.button(delete_row, "恢复 Detect 结果", self.restore_camera_cut_detect)
        form.addRow("删除/恢复", delete_row)

        self.camera_cut_frame_list = W.QListWidget()
        self.camera_cut_frame_list.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        self.camera_cut_frame_list.setMinimumHeight(210)
        form.addRow("当前帧序列", self.camera_cut_frame_list)

        note = W.QLabel(
            "Detect 完成后会暂停在这里。你可以添加或删除帧号；确认后才会执行 Bake，"
            "Bake 完成后脚本会自动执行 Verify。UE 的完整输出会同步到“任务与日志”。"
        )
        note.setWordWrap(True)
        form.addRow(note)

    def _set_camera_cut_frame_list(self, frames, update_detect=False):
        values = sorted({int(frame) for frame in (frames or [])})
        if update_detect:
            self.camera_cut_detect_frames = values.copy()
        self.camera_cut_frame_list.clear()
        for frame in values:
            item = W.QListWidgetItem(str(frame))
            if frame not in self.camera_cut_detect_frames:
                item.setForeground(G.QColor("#ffd166"))
                item.setToolTip("人工添加的切点")
            self.camera_cut_frame_list.addItem(item)

    def _camera_cut_frames_from_list(self):
        return [int(self.camera_cut_frame_list.item(index).text())
                for index in range(self.camera_cut_frame_list.count())]

    def add_camera_cut_frames(self):
        text = self.camera_cut_frame_edit.text().strip()
        if not text:
            return
        try:
            parts = [part for part in re.split(r"[,，;；\s]+", text) if part]
            additions = [int(part) for part in parts]
            frames = self._camera_cut_frames_from_list() + additions
            if any(frame < 0 for frame in frames):
                raise ValueError("帧号不能为负数")
            self._set_camera_cut_frame_list(frames)
            self.camera_cut_frame_edit.clear()
            self.camera_cut_status.setText(f"已添加 {len(set(additions))} 个切点；黄色帧为人工添加")
        except Exception as exc:
            self.error(exc)

    def _parse_camera_cut_edit(self, edit):
        text = edit.text().strip()
        if not text:
            return []
        parts = [part for part in re.split(r"[,，;；\s]+", text) if part]
        frames = [int(part) for part in parts]
        if any(frame < 0 for frame in frames):
            raise ValueError("帧号不能为负数")
        return frames

    def remove_camera_cut_frames_by_input(self):
        try:
            requested = self._parse_camera_cut_edit(self.camera_cut_delete_frame_edit)
            if not requested:
                return
            current = self._camera_cut_frames_from_list()
            requested_set = set(requested)
            remaining = [frame for frame in current if frame not in requested_set]
            removed = len(current) - len(remaining)
            self._set_camera_cut_frame_list(remaining)
            self.camera_cut_delete_frame_edit.clear()
            self.camera_cut_status.setText(
                f"已删除 {removed} 个切点；不存在的帧已忽略")
        except Exception as exc:
            self.error(exc)

    def remove_camera_cut_frames(self):
        selected_items = self.camera_cut_frame_list.selectedItems()
        selected = {int(item.text()) for item in selected_items}
        if not selected:
            self.camera_cut_status.setText("请先在帧序列中选择要删除的帧")
            return
        current = self._camera_cut_frames_from_list()
        self._set_camera_cut_frame_list([frame for frame in current if frame not in selected])
        self.camera_cut_status.setText(f"已删除 {len(selected)} 个选中切点")

    def restore_camera_cut_detect(self):
        self._set_camera_cut_frame_list(self.camera_cut_detect_frames)
        self.camera_cut_status.setText("已恢复 Detect 识别出的帧序列")

    def button(self,layout,text,fn):
        b = W.QPushButton(text)
        b.clicked.connect(fn)
        layout.addWidget(b)
        return b

    def update_bridge_status(self):
        project = self.inputs["project"].text().strip()
        status = bridge_status(project, self.controller.home)
        text = f"{status['label']}"
        color = status["color"]
        style = f"color:{color};font-weight:600;"
        self.bridge_status_label.setText(text)
        self.bridge_status_label.setStyleSheet(style)
        self.camera_cut_bridge_status.setText(text)
        self.camera_cut_bridge_status.setStyleSheet(style)

    def file_field(self,form,key,label,pattern):
        row = W.QHBoxLayout()
        edit = W.QLineEdit()
        edit.setClearButtonEnabled(True)
        self.inputs[key] = edit
        self.bind_path_memory(key, edit)
        row.addWidget(edit)
        def browse():
            path,_ = W.QFileDialog.getOpenFileName(self,label,self.path_dialog_folder(key),pattern)
            if path:
                edit.setText(path)
        self.button(row,"选择…",browse).setProperty('input_key',key)
        return self.path_row(form,label,row)

    def path_row(self,form,label,field):
        container = W.QWidget()
        layout = W.QVBoxLayout(container)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(6)
        caption = W.QLabel(label)
        layout.addWidget(caption)
        if isinstance(field,W.QLayout):
            layout.addLayout(field)
        else:
            caption.setBuddy(field)
            layout.addWidget(field)
        form.addRow(container)
        return container

    def bind_path_memory(self, key, edit):
        edit.setText(self.path_values.get(key, ''))
        edit.textChanged.connect(lambda text, k=key: self.remember_path(k, text))

    def remember_path(self, key, text):
        text = text.strip()
        # Clearing an optional input must not erase its last browsing location.
        if text and not Path(text).exists():
            return
        if self.path_values.get(key) == text and (not text or self.dialog_paths.get(key) == text):
            return
        self.path_values[key] = text
        if text:
            self.dialog_paths[key] = text
        try:
            atomic_json(self.dialog_paths_file, {'paths': self.dialog_paths, 'values': self.path_values})
        except OSError as exc:
            if hasattr(self, 'status'):
                self.status.setText(f'路径记忆保存失败：{exc}')
            else:
                print(f'路径记忆保存失败：{exc}', file=sys.stderr)

    def path_dialog_folder(self, key):
        current = self.inputs[key].text().strip()
        for value in (current, self.dialog_paths.get(key, '')):
            if not value:
                continue
            path = Path(value)
            if path.is_file():
                path = path.parent
            while not path.is_dir() and path != path.parent:
                path = path.parent
            if path.is_dir():
                return str(path)
        return ''

    def browse_ue_material(self, key):
        project = self.inputs["project"].text().strip()
        if not project:
            self.error("请先选择 UE 项目，以便把 .uasset 路径转换为 /Game 资源路径")
            return
        content = (Path(project).parent / "Content").resolve()
        path, _ = W.QFileDialog.getOpenFileName(self, "选择 UE 母材质", str(content), "UE 材质 (*.uasset)")
        if path:
            try:
                relative = Path(path).resolve().relative_to(content)
            except ValueError:
                self.error("母材质必须位于所选 UE 工程的 Content 目录")
                return
            self.inputs[key].setText("/Game/" + relative.with_suffix("").as_posix())

    def number(self,form,key,label,value,low,high,decimals=0):
        widget = W.QDoubleSpinBox() if decimals else W.QSpinBox()
        if decimals:
            widget.setDecimals(decimals)
            widget.setSingleStep(10**-min(decimals,2))
        widget.setRange(low,high)
        widget.setValue(value)
        self.inputs[key] = widget
        form.addRow(label,widget)

    def execute(self,fn,callback,cleanup=None):
        worker = Worker(fn)
        self.workers.append(worker)
        def finished(result):
            if isinstance(result,Exception):
                self.error(result)
            else:
                callback(result)
            self.update_scene_actions()
        worker.result.connect(finished)
        def cleaned_up():
            if cleanup:cleanup()
            self.workers.remove(worker)
            self.update_scene_actions()
        worker.finished.connect(cleaned_up)
        worker.start()
        self.update_scene_actions()

    def error(self,exc):
        self.log.appendPlainText(str(exc))
        W.QMessageBox.warning(self,"MMDBridgeForUE",str(exc))

    def camera_cut_error(self, exc):
        """Keep camera-cut failures visible in the shared task log."""
        self.error(exc)
        self.tabs.setCurrentIndex(self.task_tab_index)

    def scan(self):
        self.execute(lambda:discover(self.manual_paths),self.show_software)

    def show_software(self,items):
        self.software = items
        for kind in ("blender","unreal"):
            combo = self.inputs[kind]
            old = combo.currentData()
            combo.clear()
            for item in items:
                if item["kind"] == kind:
                    combo.addItem(f"{item['version']} · {item['source']} · {item['compatibility']} · {item['path']}",item["path"])
            if old and combo.findData(old)<0:
                combo.addItem('已选择 · '+str(old),old)
            if old:
                combo.setCurrentIndex(combo.findData(old))
            elif kind == 'blender':
                preferred = next((item['path'] for item in items if item['kind'] == kind and item['version'].startswith('5.2.')), None)
                if preferred:
                    combo.setCurrentIndex(combo.findData(preferred))

    def add_executable(self):
        path,_ = W.QFileDialog.getOpenFileName(self,"选择 Blender 或 UnrealEditor","","Executable (*.exe)")
        if path:
            self.manual_paths.append(path)
            atomic_json(self.controller.home/'software.json',self.manual_paths)
            self.scan()

    def settings(self):
        data = {}
        for key,w in self.inputs.items():
            if key.startswith("camera_") and key[-1:] in "xyz" and len(key)==8:
                continue
            data[key] = (w.currentData() if isinstance(w,W.QComboBox) else
                         w.text().strip() if isinstance(w,W.QLineEdit) else
                         w.isChecked() if isinstance(w,W.QCheckBox) else w.value())
        data["camera_offset_xyz"] = [self.inputs["camera_"+axis].value() for axis in "xyz"]
        return Settings(**data)

    def update_character_material_profile(self):
        selected = self.inputs['character_material_profile'].currentData()
        for key in ('character_master_material', 'eye_master_material'):
            self.inputs[key].parentWidget().setEnabled(selected != 'ue_official_toon')
        if selected == 'ZZZ_Render_PMX':
            from .character_profiles import load_profiles
            profile = load_profiles(resource_root())['ZZZ_Render_PMX']
            if self.inputs['character_master_material'].text() in ('', Settings().character_master_material):
                self.inputs['character_master_material'].setText(profile['parents'][0])
            if self.inputs['eye_master_material'].text() == Settings().eye_master_material:
                self.inputs['eye_master_material'].clear()

    def apply_settings(self,data, existing_job=False):
        if existing_job and 'character_material_profile' not in data:
            self.inputs['character_material_profile'].setCurrentIndex(self.inputs['character_material_profile'].findData('auto'))
        old_fog=data.get('fog_occlusion_quality','legacy' if existing_job else None)
        if old_fog in ('native_balanced','native_quality','native_detail','legacy','detail','vsm_finer','vsm_nocache'):
            combo=self.inputs['fog_occlusion_quality']
            if combo.findData(old_fog)<0:
                labels={'legacy':'源高度雾／异质体积','detail':'细遮挡候选',
                        'vsm_finer':'实验性 VSM 精细缓存','vsm_nocache':'实验性 VSM 无缓存',
                        'native_balanced':'原生体积雾','native_quality':'原生雾高精度','native_detail':'原生雾细节档'}
                combo.addItem('旧任务配置：'+labels[old_fog],old_fog)
        if existing_job and 'fog_occlusion_quality' not in data:
            self.inputs['fog_occlusion_quality'].setCurrentIndex(self.inputs['fog_occlusion_quality'].findData('legacy'))
        if 'scene_format' in data and 'material_workflow' not in data:
            self.inputs['material_workflow'].setCurrentIndex(self.inputs['material_workflow'].findData('semantic_plan_v1' if existing_job else 'legacy_main_v1'))
        if 'scene_format' in data and 'material_bake_fallback' not in data:
            self.inputs['material_bake_fallback'].setChecked(True)
        if 'scene_format' in data and 'material_complex_strategy' not in data:
            self.inputs['material_complex_strategy'].setCurrentIndex(1)
        if 'material_conversion_timeout' not in data:
            self.inputs['material_conversion_timeout'].setValue(120)
        if 'scene_format' in data and 'restore_scene_appearance' not in data:
            self.inputs['restore_scene_appearance'].setChecked(False)
        if 'scene_format' in data and 'scene_render_pipeline' not in data:
            self.inputs['scene_render_pipeline'].setCurrentIndex(self.inputs['scene_render_pipeline'].findData('path_tracing'))
        for key,value in data.items():
            if key not in self.inputs:
                continue
            w = self.inputs[key]
            if isinstance(w,W.QLineEdit):
                w.setText(str(value))
            elif isinstance(w,W.QComboBox):
                if key in ('blender','unreal','character_material_profile') and value and w.findData(value)<0:
                    w.addItem('任务配置 · '+str(value),value)
                if w.findData(value)>=0:
                    w.setCurrentIndex(w.findData(value))
            elif isinstance(w,W.QCheckBox):
                w.setChecked(bool(value))
            else:
                w.setValue(value)
        for axis,val in zip("xyz",data.get("camera_offset_xyz",[0,0,0])):
            self.inputs["camera_"+axis].setValue(val)

    def restore_default_settings(self):
        """Restore conversion parameters, keeping source and software paths."""
        defaults = asdict(Settings())
        keys = {
            "scale", "fps", "start", "end", "motion_offset", "lipsync_offset",
            "camera_offset", "camera_scale", "camera_offset_xyz", "width", "height",
            "scene_scale", "scene_light_scale", "scene_frame", "scene_format", "material_strategy", "material_workflow",
            "restore_scene_appearance", "scene_render_pipeline", "ue_render_memory_profile", "foliage_distance_scale", "thin_surface_scattering", "fog_density_scale", "fog_occlusion_quality", "scene_exposure_offset", "volume_resolution", "world_volume_distance",
            "scene_master_material", "character_master_material", "eye_master_material", "character_material_profile", "character_material_profiles_dir",
            "use_scene_master_material", "bake_resolution", "material_bake_fallback", "material_complex_strategy", "material_conversion_timeout",
            "usd_payload_triangle_budget", "scene_import_profile", "optimize_usd_scatter", "exact_collision", "collision_min_scene_ratio", "content_root",
        }
        self.apply_settings({key: defaults[key] for key in keys})
        W.QMessageBox.information(self,"参数","参数已恢复为默认设置。素材和软件路径未更改。")

    def preflight(self):
        try:
            current = self.settings()
            mode = 'character' if current.model else 'scene' if current.scene else 'camera'
            info = self.flow_settings(mode).validate()
            self.log.appendPlainText(json.dumps(info,ensure_ascii=False,indent=2))
            W.QMessageBox.information(self,"素材预检","文件与轨道预检通过。插件与导出能力将在对应软件中继续检查。")
        except Exception as exc:
            self.error(exc)

    def save_preset(self):
        path,_ = W.QFileDialog.getSaveFileName(self,"保存配置","mmd-preset.json","JSON (*.json)")
        if path:
            atomic_json(path,asdict(self.settings()))

    def load_preset(self):
        path,_ = W.QFileDialog.getOpenFileName(self,"载入配置","","JSON (*.json)")
        if path:
            try:
                self.apply_settings({'character_material_profile': 'auto', **read_json(path)})
            except Exception as exc:
                self.error(exc)

    def lock(self,value):
        self.main_flow_locked = value
        for w in self.inputs.values():
            w.setEnabled(not value)
        self.inputs["project"].setEnabled(True)
        self.inputs['exact_collision'].setEnabled(not value and self.inputs['scene_import_profile'].currentData() != 'render_optimized')
        self._update_main_start_button_state()
        self.resume_button.setEnabled(not value)
        # Disable the containing pages' action buttons too: preset loads and scan
        # callbacks must not change the visibly selected software during a task.
        for page_index in (0,1):
            for button in self.tabs.widget(page_index).findChildren(W.QPushButton):
                button.setEnabled(not value or button.property('input_key')=='project' or bool(button.property('connection_action')))
        self.update_scene_actions()

    def _update_main_start_button_state(self, index=None):
        if not hasattr(self, "start_button"):
            return
        # 独立工具页不参与“开始流程”：CameraCutAuto 需要先 Probe，
        # PhysicsBakeFix 只处理 PMX 文件本身，MMM 页只做环境准备。
        standalone = {self.camera_cut_tab_index, self.physics_bake_tab_index,
                      self.mmm_tab_index}
        enabled = (not self.main_flow_locked and not self.workers and not self.controller.processes
                   and not self.controller.reference_busy() and self.tabs.currentIndex() not in standalone)
        for button in (self.full_button, self.start_button, self.scene_button, self.camera_button):
            button.setEnabled(enabled)

    def flow_settings(self, mode):
        settings = self.settings()
        if settings.character_backend != 'fbx_legacy':
            settings = replace(settings, working_blend='')
        if mode == 'auto':
            if not all((settings.model, settings.motion, settings.scene)):
                raise ValueError('全流程需要同时选择角色 PMX、动作 VMD 和场景；镜头 VMD 可选')
            if settings.blender and settings.character_backend != 'mmd2unreal':
                scene_blender = settings.blender
                selected = next((item for item in self.software if item['path'] == scene_blender), {})
                if settings.restore_scene_appearance and not selected.get('version', '').startswith('5.2.'):
                    scene_blender = next((item['path'] for item in self.software
                                          if item['kind'] == 'blender' and item['version'].startswith('5.2.')), '')
                    if not scene_blender:
                        raise ValueError('完整场景光影需要 Blender 5.2，请安装或添加该版本的软件路径')
                character_blender = scene_blender
                selected = next((item for item in self.software if item['path'] == character_blender), {})
                if not selected.get('version', '').startswith(('4.3.', '5.2.')):
                    raise ValueError('角色采样需要 Blender 4.3 或 5.2，请安装或添加该版本的软件路径')
                return replace(settings, flow=mode, blender=scene_blender, character_blender=character_blender, unattended=True)
            return replace(settings, flow=mode, unattended=True)
        if mode == 'character':
            return replace(settings, flow=mode, scene='', camera='')
        if mode in ('scene', 'camera'):
            return replace(settings, flow=mode, character_backend='fbx_legacy', model='', motion='', lipsync='',
                           camera='' if mode == 'scene' else settings.camera,
                           scene=settings.scene if mode == 'scene' else '', working_blend='')
        raise ValueError('未知导入流程')

    def start(self, mode="character"):
        try:
            settings = self.flow_settings(mode)
            if (mode == "character" and settings.character_backend == "fbx_legacy" and settings.model and settings.motion
                    and settings.mmdbridge_path and settings.character_output_dir):
                raise ValueError("当前选择旧 Blender / FBX 通路，不会启动 MMD 或烘焙物理。请在参数页选择 MMDBridge Mode 1。")
            settings.validate()
            self.controller.start(settings)
            self.job_event_offset = 0
            self.vram_wait_prompted = False
            self.lock(True)
            self.tabs.setCurrentIndex(self.task_tab_index)
            self.log.appendPlainText("新任务："+str(self.controller.job.folder))
            self.log.appendPlainText("中转目录：" + self.controller.job.data['settings']['character_output_dir'])
            if mode == 'auto' and settings.character_blender:
                self.log.appendPlainText(f"全流程软件：角色 Blender {settings.character_blender}；场景 Blender {settings.blender}")
        except MissingUpstreamPlugins as exc:
            answer = W.QMessageBox.question(self, '下载并安装插件', str(exc) + '\n\n现在下载安装？',
                                           W.QMessageBox.Yes | W.QMessageBox.No)
            if answer == W.QMessageBox.Yes:
                self.lock(True)
                self.log.appendPlainText('正在从 GitHub 下载并安装 MMD2Unreal / KawaiiPhysics…')
                def install_dependencies():
                    try:
                        return install_upstream_plugins(settings)
                    except Exception as error:
                        return (False, str(error))
                def installed(result):
                    self.lock(False)
                    if isinstance(result, tuple):
                        self.error(result[1])
                        return
                    self.log.appendPlainText(result)
                    W.QMessageBox.information(self, '插件已安装', '请重新打开 UE 工程并连接，然后重新开始导入。')
                self.execute(install_dependencies, installed)
        except Exception as exc:
            self.error(exc)

    def start_camera_cut(self):
        if self.controller.camera_cut_session is not None and self.camera_cut_stage not in (None, "complete", "failed", "stopped"):
            self.error("CameraCutAuto已经在运行中")
            return
        project = self.inputs["project"].text().strip()
        if project and Path(project).is_file():
            self._launch_camera_cut(project)
        else:
            self.execute(running_projects, self._start_camera_cut_from_records)

    def _start_camera_cut_from_records(self, records):
        choices = [record["path"] for record in records]
        if not choices:
            self.error("没有找到正在运行的 UE 项目，请先选择 UE 项目并连接桥接插件")
            return
        if len(choices) == 1:
            chosen = choices[0]
        else:
            chosen, ok = W.QInputDialog.getItem(self, "选择 UE 项目", "CameraCutAuto使用的 UE 项目", choices, 0, False)
            if not ok:
                return
        self.inputs["project"].setText(chosen)
        self._launch_camera_cut(chosen)

    def _launch_camera_cut(self, project):
        try:
            session = self.controller.start_camera_cut(project)
            self.camera_cut_stage = "probe"
            self.camera_cut_log_offsets = {}
            self.camera_cut_detect_frames = []
            self._set_camera_cut_frame_list([], update_detect=True)
            self.camera_cut_bake_button.setEnabled(False)
            self.camera_cut_status.setText("等待 UE 桥接插件，然后执行 Probe…")
            self.tabs.setCurrentIndex(self.task_tab_index)
            self.log.appendPlainText(f"[camera_cut] 独立会话：{session}\n[camera_cut] 流程：Probe → Detect")
        except Exception as exc:
            self.camera_cut_error(exc)

    def confirm_camera_cut(self):
        if self.camera_cut_stage != "review":
            self.error("请等待 Detect 完成后再确认帧序列")
            return
        try:
            frames = self._camera_cut_frames_from_list()
            self.controller.camera_cut_command("bake", frames)
            self.camera_cut_stage = "bake"
            self.camera_cut_bake_button.setEnabled(False)
            self.camera_cut_status.setText(f"已确认 {len(frames)} 个切点，等待 Bake…")
            self.tabs.setCurrentIndex(self.task_tab_index)
        except Exception as exc:
            self.camera_cut_error(exc)

    def _camera_cut_report(self, mode):
        session = self.controller.camera_cut_session
        if session is None:
            return None
        if mode == "probe":
            path = session / "probe.json"
            return path if path.exists() else None
        matches = sorted(session.glob(f"{mode}_*.json"), key=lambda path: path.stat().st_mtime)
        return matches[-1] if matches else None

    def _append_camera_cut_logs(self):
        session = self.controller.camera_cut_session
        if session is None:
            return
        # Keep the visible log in the same order as the GUI pipeline.  Alphabetic
        # sorting would otherwise put Bake/Detect before Probe, which is confusing
        # while the UE-side files are being written asynchronously.
        paths = []
        for mode in ("probe", "detect", "bake", "verify"):
            path = session / f"ue_{mode}.log"
            if path.exists():
                paths.append(path)
        paths.extend(
            path for path in sorted(session.glob("ue_*.log")) if path not in paths
        )
        for path in paths:
            key = str(path)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(self.camera_cut_log_offsets.get(key, 0))
                    text = handle.read()
                    self.camera_cut_log_offsets[key] = handle.tell()
                if text:
                    self.log.moveCursor(G.QTextCursor.End)
                    self.log.insertPlainText(text)
                    if not text.endswith("\n"):
                        self.log.insertPlainText("\n")
                    self.log.ensureCursorVisible()
            except OSError:
                pass

    def poll_camera_cut(self):
        status = self.controller.poll_camera_cut()
        self._append_camera_cut_logs()
        if not status:
            return
        signature = (status.get("state"), status.get("mode"), status.get("message"))
        if signature == self.last_camera_cut_state:
            return
        self.last_camera_cut_state = signature
        self.camera_cut_status.setText(status.get("message", "CameraCutAuto"))
        self.status.setText(status.get("message", "CameraCutAuto"))
        self.log.appendPlainText(f"[camera_cut/{status.get('state','')}] {status.get('message','')}")
        state = status.get("state")
        mode = status.get("mode")
        if state == "camera_cut_complete" and mode == "probe" and self.camera_cut_stage == "probe":
            try:
                self.controller.camera_cut_command("detect")
                self.camera_cut_stage = "detect"
                self.camera_cut_status.setText("Probe 通过，正在执行 Detect…")
            except Exception as exc:
                self.camera_cut_error(exc)
        elif state == "camera_cut_complete" and mode == "detect" and self.camera_cut_stage == "detect":
            report = self._camera_cut_report("detect")
            try:
                payload = read_json(report) if report else {}
                frames = payload.get("candidates", [])
                self._set_camera_cut_frame_list(frames, update_detect=True)
                self.camera_cut_stage = "review"
                self.camera_cut_bake_button.setEnabled(True)
                self.camera_cut_status.setText(
                    f"Detect 完成，找到 {len(frames)} 个候选切点；请人工复核后编辑并确认 Bake。")
                self.tabs.setCurrentIndex(self.camera_cut_tab_index)
                self.log.appendPlainText(f"[camera_cut] Detect 候选帧：{frames}")
            except Exception as exc:
                self.camera_cut_error(exc)
        elif state == "camera_cut_complete" and mode == "bake" and self.camera_cut_stage == "bake":
            try:
                self.controller.camera_cut_command("verify", self._camera_cut_frames_from_list())
                self.camera_cut_stage = "verify"
                self.camera_cut_status.setText("Bake 完成，正在执行 Verify…")
            except Exception as exc:
                self.camera_cut_error(exc)
        elif state == "camera_cut_complete" and mode == "verify" and self.camera_cut_stage == "verify":
            self.camera_cut_stage = "complete"
            self.camera_cut_bake_button.setEnabled(False)
            self.camera_cut_status.setText("CameraCutAuto完成，已通过 Verify（具体结果见任务日志）")
        elif state == "camera_cut_existing":
            self.camera_cut_stage = "complete"
            self.camera_cut_bake_button.setEnabled(False)
            self.camera_cut_status.setText(status.get("message", "已有镜头切换，已保留"))
        elif state == "camera_cut_failed":
            self.camera_cut_stage = "failed"
            self.camera_cut_bake_button.setEnabled(False)
            self.tabs.setCurrentIndex(self.task_tab_index)
        elif state == "stopped":
            self.camera_cut_stage = "stopped"
            self.camera_cut_bake_button.setEnabled(False)

    def stop(self):
        try:
            self.controller.stop()
        except Exception as exc:
            self.error(exc)
            return
        if self.controller.camera_cut_session is not None and self.camera_cut_stage not in (None, "complete", "failed", "stopped"):
            self.controller.stop_camera_cut()
            self.camera_cut_stage = "stopped"
            self.camera_cut_bake_button.setEnabled(False)
        self.lock(False)

    def reopen(self):
        if self.controller.job:
            self.controller.job.command("reopen_export")
        else:
            self.error('尚无任务，请先开始导入或恢复任务')

    def attach(self):
        if self.controller.job:
            path,_ = W.QFileDialog.getOpenFileName(self,"关联当前等待的 FBX","","FBX (*.fbx)")
            if path:
                self.controller.job.command("attach_export",path=path)
        else:
            self.error('尚无等待关联 FBX 的任务')

    def open_job(self):
        if self.controller.job:
            G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(self.controller.job.folder)))
        else:
            self.error('尚无任务目录')

    def report(self):
        if self.controller.job:
            path = self.controller.job.folder/"ue_report.json"
            if path.exists():
                report = read_json(path)
                visual = self.controller.job.folder / 'visual'
                for key, name in (('ue_reference','ue_render.json'),('blender_reference','reference.json'),
                                  ('visual_comparison','difference.json')):
                    if (visual / name).is_file():
                        report[key] = read_json(visual / name)
                self.log.setPlainText(json.dumps(report,ensure_ascii=False,indent=2))
            else:
                self.log.appendPlainText("UE 报告尚未生成；可在任务目录查看 Blender 日志与 manifest.json。")
            self.tabs.setCurrentIndex(self.task_tab_index)
        else:
            self.error('尚无任务报告')

    def install(self):
        try:
            path = self.inputs["project"].text().strip()
            install_ue_bridge(path,self.controller.home,self.settings().unreal,character_settings=asdict(self.settings()))
            self.update_bridge_status()
            message = "已安装到该项目的 Plugins/MMDBridge。请保存当前 UE 内容并重启编辑器，然后点击连接所选项目。"
            if not any(p.get('Name') == 'MMDBridgeViewport' and p.get('Enabled')
                       for p in read_json(path).get('Plugins', [])):
                message += "\n视口接口尚未安装或与 UE 不匹配；自动切换 PT 雾和所选玻璃几何分析需要先按 docs/INSTALL_WINDOWS.md 编译本项目小插件，再安装桥接。"
            W.QMessageBox.information(self,"插件已安装",message)
        except Exception as exc:
            self.error(exc)

    def connect(self):
        try:
            self.controller.connect(self.inputs["project"].text().strip())
            self.update_bridge_status()
        except Exception as exc:
            self.error(exc)

    def task_dialog_folder(self):
        if self.controller.job:
            return str(self.controller.job.folder)
        return self.inputs['character_output_dir'].text().strip() or str(desktop_folder())

    def resume(self):
        path,_ = W.QFileDialog.getOpenFileName(self,"选择任务 job.json",self.task_dialog_folder(),"Job (job.json)")
        if path:
            try:
                self.controller.resume(Path(path).parent)
                self.last_state = None
                events = self.controller.job.folder / "events.jsonl"
                self.job_event_offset = events.stat().st_size if events.exists() else 0
                self.vram_wait_prompted = False
                self.apply_settings(self.controller.job.data["settings"], existing_job=True)
                self.lock(True)
            except Exception as exc:
                self.error(exc)

    def poll(self):
        try:
            self.update_bridge_status()
            status = self.controller.poll()
            if self.controller.job:
                events = self.controller.job.folder / "events.jsonl"
                if events.exists():
                    with events.open("r", encoding="utf-8") as stream:
                        stream.seek(self.job_event_offset)
                        while True:
                            position = stream.tell()
                            line = stream.readline()
                            if not line:
                                break
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                stream.seek(position)
                                break
                            self.log.appendPlainText(f"[{entry['state']}] {entry['message']}")
                        self.job_event_offset = stream.tell()
            signature = (status.get("state"),status.get("message"))
            if signature != self.last_state:
                self.last_state = signature
                self.status.setText(status.get("message","就绪"))
                if status:
                    match=re.search(r'(\d+)/(\d+)',status['message'])
                    if status.get('state') == 'background_render':
                        self.progress.setRange(0, 1000)
                        self.progress.setValue(round(status.get('progress', 0) * 1000))
                    elif match and int(match[2]):
                        self.progress.setRange(0,int(match[2]))
                        self.progress.setValue(int(match[1]))
                    else:
                        self.progress.setRange(0,0)
                if status.get("state") in ("failed","complete","stopped","needs_review","cancelled","upstream_blocked"):
                    self.progress.setRange(0,1)
                    self.progress.setValue(1 if status['state']=='complete' else 0)
                    self.lock(False)
                if status.get("state") == "ue_wait_vram" and not self.vram_wait_prompted:
                    self.vram_wait_prompted = True
                    # Keep the poll timer available to resume from the GPU checkpoint.
                    notice = W.QMessageBox(W.QMessageBox.Icon.Warning,
                        "UE 导入已暂停",
                        status.get("message","剩余显存不足，已保存检查点；释放显存后将自动恢复。"),
                        W.QMessageBox.StandardButton.Ok, self)
                    notice.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose)
                    notice.setModal(False)
                    notice.show()
                elif status.get("state") not in ("ue_wait_vram","awaiting_ue"):
                    self.vram_wait_prompted = False
            if status.get("state") == "awaiting_project" and not self.discovering_projects:
                self.discovering_projects = True
                def found(records):
                    self.discovering_projects = False
                    fresh = [r for r in records if r["path"] not in self.project_prompted]
                    if fresh:
                        choices = [r["path"] for r in fresh]
                        chosen,ok = W.QInputDialog.getItem(self,"发现 UE 项目","连接目标项目",choices,0,False)
                        self.project_prompted.update(choices)
                        if ok:
                            self.inputs["project"].setText(chosen)
                            self.controller.connect(chosen)
                self.execute(running_projects,found)
            self.poll_camera_cut()
            self.update_scene_actions()
        except Exception as exc:
            self.log.appendPlainText(str(exc))

    def closeEvent(self,event):
        self.stop_material_analysis()
        if self.workers:
            event.ignore()
            self.status.setText("正在完成软件探测，请稍后关闭")
        else:
            if self.controller.standalone_render and self.controller.standalone_render.get('active'):
                try:
                    self.controller.stop_background()
                except Exception as exc:
                    event.ignore()
                    self.error(exc)
                    return
            job = self.controller.job
            if job and job.data["settings"].get("character_backend") == "mmdbridge_fbx":
                status = read_json(job.folder / "status.json")
                if status.get("state") not in ("complete", "failed", "stopped"):
                    self.controller.stop()
                if self.controller.bake_thread and self.controller.bake_thread.is_alive():
                    event.ignore()
                    self.status.setText("正在停止本任务 MMD 进程，请稍后")
                    C.QTimer.singleShot(500, self.close)
                    return
            event.accept()


def configure_app(app):
    app.setApplicationVersion(__version__)
    app.setAttribute(C.Qt.AA_DontUseNativeDialogs, False)
    if not hasattr(app, '_qt_translator'):
        app._qt_translator = C.QTranslator(app)
        if app._qt_translator.load('qtbase_zh_CN', C.QLibraryInfo.path(C.QLibraryInfo.TranslationsPath)):
            app.installTranslator(app._qt_translator)
    if not hasattr(app, '_window_frames'):
        app._window_frames = WindowFrames(app)
        app.installEventFilter(app._window_frames)
    app.setWindowIcon(G.QIcon(str(resource_root()/'assets/app-icon.png')))
    for name in ('msyh.ttc','segoeui.ttf'):
        path = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
        G.QFontDatabase.addApplicationFont(str(path))
    app.setStyle("Fusion")
    palette = app.palette()
    for role,color in ((G.QPalette.Window,'#202124'),(G.QPalette.Base,'#292a2f'),
                       (G.QPalette.Button,'#303136'),(G.QPalette.WindowText,'#eeeef0'),
                       (G.QPalette.Text,'#eeeef0'),(G.QPalette.ButtonText,'#eeeef0'),
                       (G.QPalette.Highlight,'#e0e1e6'),(G.QPalette.HighlightedText,'#26272c'),
                       (G.QPalette.AlternateBase,'#25262a'),(G.QPalette.ToolTipBase,'#303136'),
                       (G.QPalette.ToolTipText,'#eeeef0'),(G.QPalette.PlaceholderText,'#a0a3ad'),
                       (G.QPalette.Link,'#c5c8d2'),(G.QPalette.Light,'#53565f'),
                       (G.QPalette.Mid,'#42444b'),(G.QPalette.Dark,'#202124')):
        palette.setColor(role,G.QColor(color))
    for role in (G.QPalette.WindowText,G.QPalette.Text,G.QPalette.ButtonText):
        palette.setColor(G.QPalette.Disabled,role,G.QColor('#858891'))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { background:#202124; color:#eeeef0; font-family:'Microsoft YaHei UI'; font-size:13px; }
        QWidget#appShell,QWidget#appHeader { background:#28292d; }
        QLabel#appBrand { background:transparent; font-size:22px; font-weight:600; }
        QWidget#operationArea { background:#202124; border:1px solid #3d3f46; border-radius:18px; }
        QWidget#operationActions { background:#202124; }
        QWidget:disabled { color:#858891; }
        QDialog,QMessageBox,QFileDialog,QInputDialog { background:#202124; }
        QToolTip,QLabel#appTooltip { background:#303136; color:#eeeef0; border:1px solid #53565f; border-radius:10px; padding:10px 14px; }
        QLabel#pageTitle { font-size:20px; font-weight:600; padding-bottom:10px; }
        QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QPlainTextEdit { background:#292a2f; border:1px solid #42444b; padding:8px; border-radius:6px; selection-background-color:#e0e1e6; selection-color:#26272c; }
        QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus,QPlainTextEdit:focus { border-color:#e0e1e6; }
        QComboBox { padding-right:32px; }
        QComboBox::drop-down { subcontrol-origin:border; subcontrol-position:top right; width:28px; border:0; }
        QComboBox::down-arrow { image:url("__ASSETS__/chevron-down.svg"); width:12px; height:8px; }
        QComboBox QAbstractItemView { background:#292a2f; color:#eeeef0; border:1px solid #53565f; border-radius:8px; padding:6px; outline:0; selection-background-color:#444750; selection-color:#ffffff; }
        QComboBox QAbstractItemView::item { min-height:32px; padding:4px 12px; border-radius:4px; }
        QComboBox QAbstractItemView::item:hover { background:#373940; }
        QComboBox QAbstractItemView::item:selected { background:#444750; color:#ffffff; }
        QSpinBox,QDoubleSpinBox { padding-right:24px; }
        QSpinBox::up-button,QDoubleSpinBox::up-button { subcontrol-origin:border; subcontrol-position:top right; width:22px; border-left:1px solid #42444b; }
        QSpinBox::down-button,QDoubleSpinBox::down-button { subcontrol-origin:border; subcontrol-position:bottom right; width:22px; border-left:1px solid #42444b; }
        QSpinBox::up-arrow,QDoubleSpinBox::up-arrow { image:url("__ASSETS__/chevron-up.svg"); width:12px; height:8px; }
        QSpinBox::down-arrow,QDoubleSpinBox::down-arrow { image:url("__ASSETS__/chevron-down.svg"); width:12px; height:8px; }
        QPushButton { background:#303136; border:1px solid #494b53; border-radius:6px; padding:8px 14px; }
        QPushButton:hover { background:#3b3d44; border-color:#7e818d; }
        QPushButton:disabled { background:#292a2f; border-color:#3d3f46; color:#858891; }
        QPushButton:focus { border-color:#e0e1e6; }
        QPushButton:default { background:#e0e1e6; color:#26272c; border-color:#e0e1e6; }
        QToolButton { background:#303136; border:1px solid #42444b; border-radius:5px; padding:6px; }
        QToolButton:hover { background:#3b3d44; border-color:#7e818d; }
        QCheckBox,QRadioButton { spacing:8px; }
        QCheckBox::indicator { width:16px; height:16px; border:1px solid #686b75; border-radius:4px; background:#292a2f; }
        QCheckBox::indicator:hover { border-color:#e0e1e6; }
        QCheckBox::indicator:checked { background:#e0e1e6; border-color:#e0e1e6; image:url("__ASSETS__/check.svg"); }
        QCheckBox::indicator:indeterminate { background:#92959f; }
        QCheckBox::indicator:disabled { border-color:#42444b; background:#303136; }
        QRadioButton::indicator { width:16px; height:16px; border:1px solid #686b75; border-radius:9px; background:#292a2f; }
        QRadioButton::indicator:checked { background:#e0e1e6; border:4px solid #53565f; width:10px; height:10px; }
        QAbstractItemView { background:#292a2f; alternate-background-color:#25262a; border:1px solid #42444b; border-radius:6px; selection-background-color:#444750; selection-color:#ffffff; outline:0; }
        QAbstractItemView::item { padding:6px; }
        QAbstractItemView::item:selected { background:#444750; color:#ffffff; }
        QAbstractItemView::item:hover { background:#373940; }
        QHeaderView::section { background:#303136; color:#b8bac2; border:0; border-right:1px solid #42444b; border-bottom:1px solid #42444b; padding:8px; }
        QTableCornerButton::section { background:#303136; border:1px solid #42444b; }
        QGroupBox { border:1px solid #42444b; border-radius:6px; margin-top:12px; padding:12px; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#b8bac2; }
        QMenu { background:#292a2f; border:1px solid #53565f; border-radius:8px; padding:6px; }
        QMenu::item { padding:8px 24px; border-radius:4px; }
        QMenu::item:selected { background:#444750; }
        QMenu::separator { height:1px; background:#42444b; margin:5px 8px; }
        QProgressBar { border:1px solid #42444b; border-radius:5px; background:#292a2f; text-align:center; min-height:18px; }
        QProgressBar::chunk { background:#70747f; border-radius:4px; }
        QPushButton#primary { background:#e0e1e6; border-color:#e0e1e6; color:#26272c; font-weight:600; }
        QPushButton#primary:hover { background:#f1f1f4; border-color:#f1f1f4; }
        QPushButton#primary:disabled { background:#4c4e57; border-color:#5b5e68; color:#b8bac2; }
        QPushButton#workflow { background:#e0e1e6; border-color:#e0e1e6; color:#26272c; font-weight:600; }
        QPushButton#workflow:hover { background:#f1f1f4; border-color:#f1f1f4; }
        QPushButton#workflow:disabled { background:#373940; border-color:#494b53; color:#92959e; }
        QListWidget#navigation { background:#28292d; border:0; border-radius:0; padding:8px; outline:0; }
        QListWidget#navigation::item { border-radius:6px; padding:6px 10px; margin:3px 0; color:#b8bac2; }
        QListWidget#navigation::item:hover { background:#33353c; }
        QListWidget#navigation::item:selected { background:#3e4047; color:#e0e1e6; }
        QWidget#navigationGroup { background:#28292d; }
        QLabel#navigationGroupTitle { background:transparent; color:#a0a3ad; font-size:12px; }
        QFrame#navigationDivider { background:#42444b; border:0; min-height:1px; max-height:1px; }
        QToolButton#sectionToggle { text-align:left; background:#28292d; border:1px solid #42444b; border-radius:6px; padding:10px; font-weight:600; }
        QToolButton#sectionToggle:hover { border-color:#7e818d; }
        QTabWidget::pane { border:0; }
        QTabBar::tab { padding:10px 24px; background:#28292d; } QTabBar::tab:selected { background:#3e4047; }
        QScrollArea { border:0; }
        QScrollBar:vertical { background:transparent; width:6px; margin:0; }
        QScrollBar::handle:vertical { background:#737782; border-radius:3px; min-height:30px; }
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:none; }
        QScrollBar:horizontal { background:#202124; height:10px; margin:0; }
        QScrollBar::handle:horizontal { background:#494b53; border-radius:4px; min-width:30px; }
        QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal { width:0; }
        QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal { background:none; }
        QSplitter::handle { background:#42444b; }
    """.replace('__ASSETS__',(resource_root()/'assets').as_posix()))


def main():
    app = W.QApplication(sys.argv)
    configure_app(app)
    window = Window()
    window.show()
    if '--smoke-test' in sys.argv:
        def smoke():
            if window.workers:
                C.QTimer.singleShot(300,smoke)
                return
            window.grab().save(str(app_home()/'smoke.png'))
            resources = ('assets/app-icon.png', 'scripts/blender_surface_layer.py',
                         'plugins/unreal/MMDBridgeViewport/Binaries/Win64/UnrealEditor.modules')
            atomic_json(app_home()/'smoke.json',{'ok':True,'blender_count':window.inputs['blender'].count(),
                        'unreal_count':window.inputs['unreal'].count(), 'name':'MMDBridgeForUE',
                        'version':__version__, 'frozen':bool(getattr(sys,'frozen',False)),
                        'resources':{name:(resource_root()/name).is_file() for name in resources},
                        'mmm_plugin':window._mmm_build_dll().is_file()})
            window.close()
            app.quit()
        C.QTimer.singleShot(1000,smoke)
    return app.exec()
