from __future__ import annotations
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from .core import Settings, read_json, atomic_json
from .controller import Controller, app_home, bridge_status, install_ue_bridge, running_projects
from .controller import MissingUpstreamPlugins, install_upstream_plugins
from .discovery import discover


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
    WARNING_PATTERN = re.compile(r"(?:ue_wait_vram|显存|已暂停)", re.IGNORECASE)

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
        self.setWindowTitle("MMD Bridge · Blender → Unreal")
        self.resize(1120,850)
        self.controller = Controller()
        self.workers = []
        self.manual_paths = read_json(self.controller.home/'software.json') if (self.controller.home/'software.json').exists() else []
        self.inputs = {}
        self.last_state = None
        self.last_camera_cut_state = None
        self.camera_cut_log_offsets = {}
        self.camera_cut_stage = None
        self.camera_cut_detect_frames = []
        self.main_flow_locked = False
        self.vram_wait_prompted = False
        self.discovering_projects = False
        self.project_prompted = set()
        widget = W.QWidget()
        self.setCentralWidget(widget)
        layout = W.QVBoxLayout(widget)
        heading = W.QLabel("MMD Bridge")
        heading.setStyleSheet("font-size:28px;font-weight:700;color:#87ded1")
        layout.addWidget(heading)
        layout.addWidget(W.QLabel("角色与相机：MMD2Unreal 官方窗口导入；场景：Blender USD。"))
        self.tabs = W.QTabWidget()
        layout.addWidget(self.tabs,1)
        files, form = self.page("素材与软件")
        for kind,label in (("blender","Blender"),("unreal","Unreal Engine")):
            combo = W.QComboBox()
            combo.setMinimumWidth(640)
            self.inputs[kind] = combo
            form.addRow(label,combo)
        tools = W.QHBoxLayout()
        self.button(tools,"重新检测",self.scan)
        self.button(tools,"添加软件路径",self.add_executable)
        form.addRow(tools)
        for key,label,pattern in (("model","PMX 模型","PMX (*.pmx)"),("motion","动作 VMD","VMD (*.vmd)"),
            ("lipsync","口型 VMD（可选）","VMD (*.vmd)"),("camera","镜头 VMD（可选）","VMD (*.vmd)"),
            ("scene","场景（可选）","Blender (*.blend)"),("working_blend","角色工作文件（可选）","Blender (*.blend)"),
            ("project","UE 项目（可稍后选择）","Unreal (*.uproject)")):
            self.file_field(form,key,label,pattern)
        self.bridge_status_label = W.QLabel("未选择 UE 项目")
        self.bridge_status_label.setWordWrap(True)
        form.addRow("UE 桥接状态",self.bridge_status_label)
        label = W.QLabel("未选择角色工作文件时，角色 FBX 默认导出到桌面；选择后导出到其同目录。\n场景会单独转换，不覆盖源 .blend。新项目首次连接需安装桥接插件并重启 UE。")
        label.setWordWrap(True)
        form.addRow(label)
        presets = W.QHBoxLayout()
        self.button(presets,"载入本机验收素材",self.sample)
        self.button(presets,"保存配置",self.save_preset)
        self.button(presets,"载入配置",self.load_preset)
        self.button(presets,"素材预检",self.preflight)
        form.addRow(presets)
        params,p = self.page("参数")
        backend = W.QComboBox()
        backend.addItem("MMD2Unreal 官方窗口（推荐）", "mmd2unreal")
        backend.addItem("历史 Blender / FBX 兼容", "fbx_legacy")
        self.inputs["character_backend"] = backend
        p.addRow("角色与相机导入", backend)
        hint = W.QLabel("MMD2Unreal 的模型缩放、相机构图和导入帧率在官方窗口设置。\n下方帧率用于组合序列；MMD 缩放及相机 XYZ 参数仅用于历史 FBX 通路。")
        hint.setWordWrap(True)
        p.addRow(hint)
        self.number(p,"scale","历史 FBX：MMD 导入缩放",.08,.0001,100,4)
        presets = W.QHBoxLayout()
        self.button(presets,"0.08",lambda:self.inputs["scale"].setValue(.08))
        self.button(presets,"0.1",lambda:self.inputs["scale"].setValue(.1))
        p.addRow("快捷缩放",presets)
        for key,label,val,lo,hi in (("fps","帧率（保持动作时长）",30,1,240),("start","导出开始帧",0,0,1000000),
            ("end","导出结束帧（0=自动）",0,0,1000000),("motion_offset","动作偏移（输出帧）",0,-100000,100000),
            ("lipsync_offset","口型偏移（输出帧）",0,-100000,100000),("camera_offset","镜头偏移（输出帧）",0,-100000,100000),
            ("width","画幅宽",1920,1,16384),("height","画幅高",1080,1,16384),("scene_frame","场景参考帧",1,0,1000000)):
            self.number(p,key,label,val,lo,hi)
        self.number(p,"camera_scale","镜头构图比例",1,.001,100,3)
        for axis in "xyz":
            self.number(p,"camera_"+axis,"镜头 "+axis.upper()+" 偏移（米）",0,-10000,10000,3)
        self.number(p,"scene_scale","场景比例（独立于 MMD）",1,.0001,10000,4)
        scene_format = W.QComboBox()
        scene_format.addItem("USD Stage（推荐）", "usd_stage")
        scene_format.addItem("第一阶段 FBX 兼容通路", "fbx_legacy")
        self.inputs["scene_format"] = scene_format
        p.addRow("场景导入通路", scene_format)
        material_strategy = W.QComboBox()
        for label, value in (("自动判断", "auto"), ("优先 UE 节点", "nodes"),
                             ("混合还原", "hybrid"), ("完整烘焙", "bake")):
            material_strategy.addItem(label, value)
        self.inputs["material_strategy"] = material_strategy
        p.addRow("场景材质策略", material_strategy)
        scene_master = W.QLineEdit("/Game/SelfMakeMaterial/Material/M_00")
        self.inputs["scene_master_material"] = scene_master
        p.addRow("场景通用母材质（可选）", scene_master)
        use_scene_master = W.QCheckBox("简单贴图 PBR 匹配时优先创建用户母材质实例")
        use_scene_master.setChecked(False)
        self.inputs["use_scene_master_material"] = use_scene_master
        p.addRow("母材质实例", use_scene_master)
        resolution = W.QComboBox()
        resolution.addItem("自动（1K/2K/4K）",0)
        for n in (1024,2048,4096,8192):
            resolution.addItem(f"{n//1024}K",n)
        resolution.setCurrentIndex(0)
        self.inputs["bake_resolution"] = resolution
        p.addRow("新增材质烘焙分辨率",resolution)
        self.number(p,"usd_payload_triangle_budget","USD 单 Payload 三角面预算",1000000,10000,100000000)
        exact_collision = W.QCheckBox("所有可见静态模型使用显示三角面碰撞")
        exact_collision.setChecked(True)
        self.inputs["exact_collision"] = exact_collision
        p.addRow("精确碰撞", exact_collision)
        self.number(p,"ue_vram_pause_percent","UE 总GPU内存暂停阈值（专用+共享，%）",90,1,99)
        self.number(p,"ue_vram_resume_percent","UE 总GPU内存恢复阈值（专用+共享，%）",80,1,99)
        content = W.QLineEdit("/Game/MMDBridge")
        self.inputs["content_root"] = content
        p.addRow("UE 内容目录",content)
        defaults = W.QHBoxLayout()
        defaults.addStretch(1)
        self.restore_defaults_button = self.button(defaults,"恢复默认设置",self.restore_default_settings)
        p.addRow(defaults)
        self.build_camera_cut_page()
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
        self.button(actions,"重开导出窗口",self.reopen)
        self.button(actions,"关联已导出 FBX",self.attach)
        self.button(actions,"打开任务文件夹",self.open_job)
        self.button(actions,"读取结果报告",self.report)
        progress_layout.addRow(actions)
        connections = W.QHBoxLayout()
        self.button(connections,"安装 UE 桥接插件",self.install)
        self.button(connections,"连接所选项目",self.connect)
        self.resume_button = self.button(connections,"恢复已有任务",self.resume)
        progress_layout.addRow(connections)
        footer = W.QHBoxLayout()
        self.summary = W.QLabel("支持适配：Blender 4.3.x / UE 5.8.x · 实测版本见验收报告")
        footer.addWidget(self.summary,1)
        self.start_button = self.button(footer,"开始流程",self.start)
        self.start_button.setObjectName("primary")
        self.button(footer,"停止任务",self.stop)
        self.tabs.currentChanged.connect(self._update_main_start_button_state)
        self._update_main_start_button_state()
        layout.addLayout(footer)
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        self.scan()

    def page(self,title):
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        widget = W.QWidget()
        form = W.QFormLayout(widget)
        form.setSpacing(13)
        form.setContentsMargins(20,20,20,20)
        scroll.setWidget(widget)
        self.tabs.addTab(scroll,title)
        # Return the actual tab page so indexOf() and currentChanged handling
        # refer to the QTabWidget page, not its inner form widget.
        return scroll,form

    def build_camera_cut_page(self):
        """独立CameraCutAuto工具：不读取主自动化素材，也不改变主流程状态。"""
        page, form = self.page("CameraCutAuto")
        self.camera_cut_tab_index = self.tabs.indexOf(page)
        intro = W.QLabel(
            "独立工具流程：Probe → Detect → 人工复核/编辑帧序列 → Bake → Verify。\n"
            "请先在 UE 中打开目标 Level Sequence；本工具不会自动加入“开始流程”。"
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
        row.addWidget(edit)
        def browse():
            path,_ = W.QFileDialog.getOpenFileName(self,label,edit.text(),pattern)
            if path:
                edit.setText(path)
        self.button(row,"选择…",browse).setProperty('input_key',key)
        form.addRow(label,row)

    def number(self,form,key,label,value,low,high,decimals=0):
        widget = W.QDoubleSpinBox() if decimals else W.QSpinBox()
        if decimals:
            widget.setDecimals(decimals)
            widget.setSingleStep(10**-min(decimals,2))
        widget.setRange(low,high)
        widget.setValue(value)
        self.inputs[key] = widget
        form.addRow(label,widget)

    def execute(self,fn,callback):
        worker = Worker(fn)
        self.workers.append(worker)
        def finished(result):
            if isinstance(result,Exception):
                self.error(result)
            else:
                callback(result)
        worker.result.connect(finished)
        worker.finished.connect(lambda:self.workers.remove(worker))
        worker.start()

    def error(self,exc):
        self.log.appendPlainText(str(exc))
        W.QMessageBox.warning(self,"MMD Bridge",str(exc))

    def camera_cut_error(self, exc):
        """Keep camera-cut failures visible in the shared task log."""
        self.error(exc)
        self.tabs.setCurrentIndex(self.task_tab_index)

    def scan(self):
        self.execute(lambda:discover(self.manual_paths),self.show_software)

    def show_software(self,items):
        for kind in ("blender","unreal"):
            combo = self.inputs[kind]
            old = combo.currentData()
            combo.clear()
            for item in items:
                if item["kind"] == kind:
                    combo.addItem(f"{item['version']} · {item['source']} · {item['compatibility']} · {item['path']}",item["path"])
            if old and combo.findData(old)>=0:
                combo.setCurrentIndex(combo.findData(old))

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

    def apply_settings(self,data):
        for key,value in data.items():
            if key not in self.inputs:
                continue
            w = self.inputs[key]
            if isinstance(w,W.QLineEdit):
                w.setText(str(value))
            elif isinstance(w,W.QComboBox):
                if w.findData(value)>=0:
                    w.setCurrentIndex(w.findData(value))
            elif isinstance(w,W.QCheckBox):
                w.setChecked(bool(value))
            else:
                w.setValue(value)
        for axis,val in zip("xyz",data.get("camera_offset_xyz",[0,0,0])):
            self.inputs["camera_"+axis].setValue(val)

    def restore_default_settings(self):
        """Restore only fields shown on the Parameters page."""
        defaults = asdict(Settings())
        keys = {
            "scale", "fps", "start", "end", "motion_offset", "lipsync_offset",
            "camera_offset", "camera_scale", "camera_offset_xyz", "width", "height",
            "scene_scale", "scene_frame", "scene_format", "material_strategy",
            "scene_master_material", "use_scene_master_material", "bake_resolution",
            "usd_payload_triangle_budget", "exact_collision", "content_root",
            "ue_vram_pause_percent", "ue_vram_resume_percent",
        }
        self.apply_settings({key: defaults[key] for key in keys})
        W.QMessageBox.information(self,"参数","参数已恢复为默认设置。素材和软件路径未更改。")

    def sample(self):
        self.apply_settings({"model":r"F:\MMD_Model\Miku\Sano_YYB·Era·Miku（二分裙）\final.pmx",
            "motion":r"F:\MMD_Motion\YoidoreSirazu\YoidoreSirazu_Motion_V103\YoidoreSirazu_Motion\Yoidore_DanceMotion.vmd",
            "lipsync":r"F:\MMD_Motion\YoidoreSirazu\YoidoreSirazu_Motion_V103\YoidoreSirazu_Motion\Yoidore_LipSync.vmd",
            "camera":r"F:\MMD_Motion\YoidoreSirazu\酔いどれ知らずカメラ(ミナ)\かめら(185cm動画使用).vmd",
            "scene":r"F:\MMD_Scene\Blender\SableFiSh\MyGO-ParkingLots\It'sMyGO!!!!!-ParkingLots.blend"})

    def preflight(self):
        try:
            info = self.settings().validate()
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
                self.apply_settings(read_json(path))
            except Exception as exc:
                self.error(exc)

    def lock(self,value):
        self.main_flow_locked = value
        for w in self.inputs.values():
            w.setEnabled(not value)
        self.inputs["project"].setEnabled(True)
        self._update_main_start_button_state()
        self.resume_button.setEnabled(not value)
        # Disable the containing pages' action buttons too: preset loads and scan
        # callbacks must not change the visibly selected software during a task.
        for page_index in (0,1):
            for button in self.tabs.widget(page_index).findChildren(W.QPushButton):
                button.setEnabled(not value or button.property('input_key')=='project')

    def _update_main_start_button_state(self, index=None):
        if not hasattr(self, "start_button"):
            return
        on_camera_cut_page = self.tabs.currentIndex() == self.camera_cut_tab_index
        self.start_button.setEnabled(not self.main_flow_locked and not on_camera_cut_page)

    def start(self):
        try:
            settings = self.settings()
            settings.validate()
            self.controller.start(settings)
            self.vram_wait_prompted = False
            self.lock(True)
            self.tabs.setCurrentIndex(self.task_tab_index)
            self.log.appendPlainText("新任务："+str(self.controller.job.folder))
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
        self.controller.stop()
        if self.controller.camera_cut_session is not None and self.camera_cut_stage not in (None, "complete", "failed", "stopped"):
            self.controller.stop_camera_cut()
            self.camera_cut_stage = "stopped"
            self.camera_cut_bake_button.setEnabled(False)
        self.lock(False)

    def reopen(self):
        if self.controller.job:
            self.controller.job.command("reopen_export")

    def attach(self):
        if self.controller.job:
            path,_ = W.QFileDialog.getOpenFileName(self,"关联当前等待的 FBX","","FBX (*.fbx)")
            if path:
                self.controller.job.command("attach_export",path=path)

    def open_job(self):
        if self.controller.job:
            G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(self.controller.job.folder)))

    def report(self):
        if self.controller.job:
            path = self.controller.job.folder/"ue_report.json"
            if path.exists():
                self.log.setPlainText(path.read_text(encoding="utf-8"))
            else:
                self.log.appendPlainText("UE 报告尚未生成；可在任务目录查看 Blender 日志与 manifest.json。")

    def install(self):
        try:
            path = self.inputs["project"].text().strip()
            install_ue_bridge(path,self.controller.home)
            self.update_bridge_status()
            W.QMessageBox.information(self,"插件已安装","已安装到该项目的 Plugins/MMDBridge。请保存当前 UE 内容并重启编辑器，然后点击连接所选项目。")
        except Exception as exc:
            self.error(exc)

    def connect(self):
        try:
            self.controller.connect(self.inputs["project"].text().strip())
            self.update_bridge_status()
        except Exception as exc:
            self.error(exc)

    def resume(self):
        path,_ = W.QFileDialog.getOpenFileName(self,"选择任务 job.json",str(self.controller.home/"jobs"),"Job (job.json)")
        if path:
            try:
                self.controller.resume(Path(path).parent)
                self.last_state = None
                self.vram_wait_prompted = False
                self.apply_settings(self.controller.job.data["settings"])
                self.lock(True)
            except Exception as exc:
                self.error(exc)

    def poll(self):
        try:
            self.update_bridge_status()
            status = self.controller.poll()
            signature = (status.get("state"),status.get("message"))
            if signature != self.last_state:
                self.last_state = signature
                self.status.setText(status.get("message","就绪"))
                if status:
                    self.log.appendPlainText(f"[{status['state']}] {status['message']}")
                    match=re.search(r'(\d+)/(\d+)',status['message'])
                    if match and int(match[2]):
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
                    W.QMessageBox.warning(
                        self,"UE 导入已暂停",
                        status.get("message","剩余显存不足，已保存检查点；释放显存后将自动恢复。"))
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
        except Exception as exc:
            self.log.appendPlainText(str(exc))

    def closeEvent(self,event):
        if self.workers:
            event.ignore()
            self.status.setText("正在完成软件探测，请稍后关闭")
        else:
            event.accept()


def configure_app(app):
    for name in ('msyh.ttc','segoeui.ttf'):
        path = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
        G.QFontDatabase.addApplicationFont(str(path))
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background:#17212b; color:#e3eaf0; font-family:'Microsoft YaHei UI'; font-size:13px; }
        QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QPlainTextEdit { background:#0f1923; border:1px solid #3a4b5c; padding:6px; border-radius:4px; }
        QPushButton { background:#2b3d4e; border:1px solid #425b70; border-radius:5px; padding:8px 13px; }
        QPushButton:hover { background:#3b5368; } QPushButton:disabled { color:#718090; }
        QPushButton#primary { background:#287f73; color:white; font-weight:600; }
        QPushButton#primary:disabled { background:#59636d; border-color:#6d7882; color:#d1d7dc; }
        QTabBar::tab { padding:10px 24px; background:#22313f; } QTabBar::tab:selected { background:#315365; }
        QScrollArea { border:0; }
    """)


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
            atomic_json(app_home()/'smoke.json',{'ok':True,'blender_count':window.inputs['blender'].count(),
                        'unreal_count':window.inputs['unreal'].count()})
            window.close()
            app.quit()
        C.QTimer.singleShot(1000,smoke)
    return app.exec()
