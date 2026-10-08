import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from mmd_bridge.app import Window
from mmd_bridge.core import Job, Settings, atomic_json, read_json
from mmd_bridge.controller import Controller, connection_folder


def test_main_workflow_and_experimental_repair_are_separate_controls(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window(); window.timer.stop()
    try:
        assert window.inputs['material_workflow'].currentData() == 'legacy_main_v1'
        assert window.inputs['fog_occlusion_quality'].currentData() == 'vsm_finer'
        assert window.inputs['fog_occlusion_quality'].count()==4
        assert all(window.inputs['fog_occlusion_quality'].findData(v)<0 for v in ('native_balanced','native_quality','native_detail'))
        window.inputs['fog_occlusion_quality'].setCurrentIndex(1)
        assert window.settings().fog_occlusion_quality == 'vsm_nocache'
        window.apply_settings({'fog_occlusion_quality':'native_detail'})
        assert window.settings().fog_occlusion_quality == 'vsm_finer'
        window.apply_settings({'fog_occlusion_quality':'legacy'})
        assert window.inputs['fog_occlusion_quality'].currentData() == 'legacy'
        for option in ('vsm_finer','vsm_nocache'):
            window.apply_settings({'fog_occlusion_quality':option})
            assert window.settings().fog_occlusion_quality==option
            assert 'VSM' in window.inputs['fog_occlusion_quality'].currentText()
        window.inputs['fog_occlusion_quality'].setCurrentIndex(1)
        window.apply_settings({'scene_format':'usd_stage'}, existing_job=True)
        assert window.inputs['fog_occlusion_quality'].currentData() == 'legacy'
        assert window.inputs['material_workflow'].currentData() == 'semantic_plan_v1'
        window.apply_settings({'scene_format':'usd_stage'})
        assert window.inputs['material_workflow'].currentData() == 'legacy_main_v1'
        assert '实验性' in window.white_repair_button.text()
        job = Job(tmp_path/'scene_job')
        atomic_json(job.path, {'settings':{}, 'inputs':{}})
        atomic_json(job.folder/'manifest.json', {'scene':{'route':'usd_stage','materials':[
            {'id':'normal','name':'White','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}},'requires_visual_review':True}]}})
        atomic_json(job.folder/'ue_report.json', {'assets':{'level':'/Game/Test/Maps/Preview','materials':{}},'checks':{},'failures':[]})
        window.controller.job = job; window.update_scene_actions()
        assert not window.white_repair_button.isEnabled()
        atomic_json(job.folder/'manifest.json', {'scene':{'route':'usd_stage','materials':[
            {'id':'failed','name':'Paint','channels':{},'material_error':'unsupported field'}]}})
        window.update_scene_actions(); assert window.white_repair_button.isEnabled()
        window.main_flow_locked = True; window.update_scene_actions()
        assert not window.white_repair_button.isEnabled()
    finally:
        window.controller.job = None; window.close()


def test_many_white_variants_are_scrollable_and_cancel_preserves_scene(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app=W.QApplication.instance() or W.QApplication([]);window=Window();window.timer.stop()
    job=Job(tmp_path/'Job');atomic_json(job.path,{'settings':{},'inputs':{}})
    manifest={'scene':{'route':'usd_stage','materials':[
        {'id':str(i),'name':'Tree','channels':{},'material_error':'Controlled failure','source_objects':['Tree.'+str(i)]} for i in range(60)]}}
    atomic_json(job.folder/'manifest.json',manifest);atomic_json(job.folder/'ue_report.json',{'assets':{},'checks':{},'failures':[]})
    window.controller.job=job
    seen=[]
    def inspect(dialog):
        assert len(dialog.findChildren(W.QCheckBox))==60
        assert dialog.findChild(W.QScrollArea) is not None
        assert dialog.height()<=600
        seen.append(True);return W.QDialog.Rejected
    monkeypatch.setattr(W.QDialog,'exec',inspect)
    try:
        window.repair_white_materials()
        assert seen and window.controller.material_repair is None
        assert read_json(job.folder/'manifest.json')==manifest
    finally:
        window.controller.job=None;window.close()


def test_scene_controls_persist_dispatch_compare_and_legacy_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    monkeypatch.setattr(W.QMessageBox, 'information', lambda *a: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    window.timer.stop()
    errors = []
    monkeypatch.setattr(window, 'error', lambda exc: errors.append(str(exc)))
    try:
        # Every visible action has an actual Qt signal route, including browse buttons.
        buttons = window.findChildren(W.QPushButton)
        assert len(buttons) >= 45
        assert all(b.receivers(C.SIGNAL('clicked(bool)')) for b in buttons)
        blender = tmp_path / 'blender.exe'; blender.touch()
        scene = tmp_path / 'scene.blend'; scene.touch()
        window.show_software([{'kind':'blender','path':str(blender),'version':'5.2.2','source':'test','compatibility':'test'}])
        window.inputs['scene'].setText(str(scene))
        window.inputs['fog_density_scale'].setValue(1.7)
        assert window.settings().material_bake_fallback
        assert window.settings().material_complex_strategy == 'pbr_bake'
        window.inputs['material_bake_fallback'].click()
        assert not window.settings().material_bake_fallback
        window.inputs['material_bake_fallback'].click()
        window.inputs['material_conversion_timeout'].setValue(45)
        window.inputs['scene_exposure_offset'].setValue(-.5)
        window.inputs['volume_resolution'].setValue(128)
        window.inputs['ue_render_memory_profile'].setCurrentIndex(1)
        preset = tmp_path / 'preset.json'
        monkeypatch.setattr(W.QFileDialog, 'getSaveFileName', lambda *a: (str(preset), ''))
        next(b for b in buttons if b.text() == '保存配置').click()
        assert read_json(preset)['fog_density_scale'] == 1.7
        assert read_json(preset)['material_bake_fallback'] is True
        assert read_json(preset)['material_conversion_timeout'] == 45
        assert read_json(preset)['ue_render_memory_profile'] == 'low'
        window.inputs['fog_density_scale'].setValue(5)
        monkeypatch.setattr(W.QFileDialog, 'getOpenFileName', lambda *a: (str(preset), ''))
        next(b for b in buttons if b.text() == '载入配置').click()
        assert window.settings().fog_density_scale == 1.7
        assert window.settings().material_bake_fallback
        window.apply_settings({'scene_format': 'usd_stage'})
        assert window.settings().material_bake_fallback
        assert window.settings().material_conversion_timeout == 120
        window.apply_settings(read_json(preset))
        next(b for b in buttons if b.text() == '素材预检').click()
        assert not errors  # Default character backend must not block a scene-only preflight.
        captured = []
        monkeypatch.setattr(window.controller, 'start', lambda cfg: (captured.append(cfg), setattr(window.controller, 'job', Job(tmp_path))))
        atomic_json(tmp_path / 'job.json', {'settings': {'character_output_dir': str(tmp_path)}})
        window.scene_button.click()
        assert captured[0].restore_scene_appearance and captured[0].volume_resolution == 128
        assert captured[0].scene_exposure_offset == -.5
        window.lock(False)
        atomic_json(tmp_path / 'manifest.json', {'scene': {'route':'usd_stage','reference_camera':{'resolution':[16,8]}}})
        atomic_json(tmp_path / 'ue_report.json', {'assets':{'level':'/Game/Test','sequence':'/Game/Sequence',
                    'render_preset':'/Game/Base'},'complete':False})
        atomic_json(tmp_path / 'status.json', {'state':'needs_review'})
        visual = tmp_path / 'visual'; visual.mkdir()
        atomic_json(visual / 'ue_render.json', {'renderer':{'mode':'lumen','pass':'MoviePipelineDeferredPassBase'}})
        for name, color in (('blender','black'),('ue','white')):
            image = G.QImage(16,8,G.QImage.Format_RGB32); image.fill(G.QColor(color)); image.save(str(visual / (name+'.png')))
        window.update_scene_actions()
        assert not window.reopen_button.isEnabled() and not window.attach_button.isEnabled()
        requests = []
        monkeypatch.setattr(window.controller, 'render_reference', lambda kind, pipeline=None, memory_profile=None:
                            requests.append((kind, pipeline, memory_profile)))
        window.inputs['scene_render_pipeline'].setCurrentIndex(window.inputs['scene_render_pipeline'].findData('path_tracing'))
        assert '路径追踪' in window.ue_reference_button.text()
        window.ue_reference_button.click()
        window.lock(False)
        window.inputs['scene_render_pipeline'].setCurrentIndex(window.inputs['scene_render_pipeline'].findData('lumen'))
        window.ue_reference_button.click()
        window.lock(False)
        assert requests == [('ue', 'path_tracing', 'low'), ('ue', 'lumen', 'low')]
        monkeypatch.setattr(window, 'show_scene_visual', lambda name: captured.append(name))
        window.comparison_button.click()
        for worker in list(window.workers): worker.wait(5000)
        app.processEvents()
        assert (visual / 'comparison.png').is_file()
        assert read_json(visual / 'difference.json')['mean_absolute_display_difference'] == 1
        assert read_json(visual / 'difference.json')['visual_status'] == 'needs_review'
        assert read_json(visual / 'difference.json')['renderer']['mode'] == 'lumen'
        assert captured[-1] == 'comparison.png'
        window.scene_report_button.click()
        assert window.tabs.currentIndex() == window.task_tab_index
        atomic_json(tmp_path / 'status.json', {'state':'export_paused'})
        window.update_scene_actions()
        window.reopen_button.click()
        assert read_json(tmp_path / 'command.json')['name'] == 'reopen_export'
        fbx = tmp_path / 'model.fbx'; fbx.touch()
        monkeypatch.setattr(W.QFileDialog, 'getOpenFileName', lambda *a: (str(fbx), ''))
        window.attach_button.click()
        assert read_json(tmp_path / 'command.json')['path'] == str(fbx)
        window.apply_settings({'scene_format':'usd_stage'})
        assert not window.settings().restore_scene_appearance  # Legacy preset keeps its original behavior.
        assert not errors
    finally:
        window.controller.job = None
        window.close()


def test_reference_dispatch_is_separate_from_import_and_gpu_resume(tmp_path, monkeypatch):
    controller = Controller(tmp_path / 'home')
    folder = tmp_path / 'job'; folder.mkdir()
    project = tmp_path / 'Demo.uproject'; atomic_json(project, {'FileVersion':3})
    engine = tmp_path / 'UnrealEditor.exe'; engine.touch()
    blender = tmp_path / 'blender.exe'; blender.touch()
    source = tmp_path / 'source.blend'; source.touch()
    from dataclasses import asdict
    cfg = asdict(Settings(flow='scene', scene=str(source), blender=str(blender), unreal=str(engine), project=str(project)))
    atomic_json(folder / 'job.json', {'id':'fixture','settings':cfg})
    atomic_json(folder / 'manifest.json', {'scene':{'route':'usd_stage','reference_camera':{'resolution':[16,8]}}})
    atomic_json(folder / 'ue_report.json', {'assets':{'level':'/Game/Test','sequence':'/Game/Sequence','render_preset':'/Game/Preset'}})
    atomic_json(folder / 'status.json', {'state':'needs_review'})
    controller.load_scene_results(folder)
    monkeypatch.setattr(controller, '_same_project_running', lambda cfg: False)
    launched = []
    monkeypatch.setattr('mmd_bridge.process.popen', lambda *a, **kw: launched.append(a))
    original = (folder / 'job.json').read_bytes()
    manifest = (folder / 'manifest.json').read_bytes()
    controller.render_reference('ue', pipeline='path_tracing')
    request = read_json(controller.mailbox / 'render_request.json')
    assert request['job'] == str(folder) and launched
    assert request['pipeline'] == 'path_tracing'
    assert (folder / 'job.json').read_bytes() == original and (folder / 'manifest.json').read_bytes() == manifest
    assert not controller.processes  # No Blender export or material bake for a renderer switch.
    assert not (controller.mailbox / 'request.json').exists()
    atomic_json(folder / 'status.json', {'state':'ue_wait_vram','operation':'reference','updated':__import__('time').time()})
    monkeypatch.setattr(controller, '_poll_vram_resume', lambda *a: (_ for _ in ()).throw(AssertionError('render must not reimport')))
    controller.poll()
    # Stop must wait for the matching native acknowledgement before another render.
    controller.stop()
    assert controller.reference_busy()
    atomic_json(controller.mailbox / 'render_response.json', {'id':request['id'],'state':'stopped'})
    assert not controller.reference_busy()


def test_scene_results_show_actual_renderer_config_images_and_missing_assets(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(Window, 'scan', lambda self: None)
    app = W.QApplication.instance() or W.QApplication([])
    window = Window()
    window.timer.stop()
    try:
        job = tmp_path / 'job'; job.mkdir()
        atomic_json(job/'job.json', {'settings':{'scene_format':'usd_stage','scene_render_pipeline':'lumen',
                    'blender':str(tmp_path/'source_blender.exe'),'unreal':str(tmp_path/'source_ue.exe')}})
        atomic_json(job/'manifest.json', {'scene':{'route':'usd_stage','reference_camera':{'resolution':[16,8]}}})
        assets = {'level':'/Game/Map','sequence':'/Game/Sequence','render_preset':'/Game/Base',
                  'render_presets':{'lumen':'/Game/MRQ_Lumen.MRQ_Lumen'}}
        atomic_json(job/'ue_report.json', {'assets':assets})
        atomic_json(job/'status.json', {'state':'needs_review'})
        visual = job/'visual'; visual.mkdir()
        atomic_json(visual/'ue_render.json', {'complete':True,'renderer':{'mode':'path_tracing'},
                   'source_camera':{'focal_length':50.,'aperture':.8,'focus_distance_cm':700.,'focus_method':'Manual'}})
        atomic_json(visual/'difference.json', {'visual_status':'needs_review'})
        for name in ('blender','ue'):
            image = G.QImage(16,8,G.QImage.Format_RGB32); image.fill(G.QColor('black')); image.save(str(visual/(name+'.png')))
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:(str(job/'job.json'),''))
        window.scene_result_button.click()
        assert window.inputs['blender'].currentData() == str(tmp_path/'source_blender.exe')
        window.show_software([{'kind':'blender','path':'other.exe','version':'5.2.2','source':'test','compatibility':'test'}])
        assert window.settings().blender == str(tmp_path/'source_blender.exe')
        assert window.settings().unreal == str(tmp_path/'source_ue.exe')
        assert window.tabs.widget(window.scene_appearance_tab_index).isAncestorOf(window.inputs['fog_density_scale'])
        assert '路径追踪' in window.scene_render_label.text()  # Selected Lumen must not relabel an older PT image.
        assert 'f/0.8' in window.scene_render_label.text() and '7 m' in window.scene_render_label.text()
        window.copy_render_config_button.click()
        assert app.clipboard().text() == assets['render_presets']['lumen']
        shown = []
        monkeypatch.setattr(window,'show_scene_visual',shown.append)
        window.show_blender_button.click(); window.show_ue_button.click()
        assert shown == ['blender.png','ue.png']
        urls = []
        monkeypatch.setattr(G.QDesktopServices,'openUrl',lambda url:urls.append(url.toLocalFile()))
        window.scene_folder_button.click()
        assert [Path(value) for value in urls] == [job]
        window.scene_report_button.click()
        assert 'visual_comparison' in window.log.toPlainText() and 'path_tracing' in window.log.toPlainText()
        window.inputs['scene_render_pipeline'].setCurrentIndex(window.inputs['scene_render_pipeline'].findData('path_tracing'))
        assert not window.copy_render_config_button.isEnabled()
        atomic_json(job/'ue_report.json', {'assets':{}})
        window.update_scene_actions()
        assert not window.ue_reference_button.isEnabled()
        before = (job/'job.json').read_bytes()
        window.inputs['fog_density_scale'].setValue(2.)
        assert (job/'job.json').read_bytes() == before
    finally:
        window.controller.job = None
        window.close()


def test_software_browse_bridge_and_task_buttons_have_effects(tmp_path, monkeypatch):
    from dataclasses import asdict
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setattr(W.QMessageBox,'information',lambda *a:None)
    software = tmp_path/'blender.exe'; software.touch()
    items = [{'kind':'blender','path':str(software),'version':'5.2.2','source':'test','compatibility':'test'}]
    detections = []
    monkeypatch.setattr('mmd_bridge.app.discover',lambda paths:(detections.append(list(paths)) or items))
    app = W.QApplication.instance() or W.QApplication([])
    window = Window(); window.timer.stop()
    assert not window.scene_result_button.isEnabled() and not window.scene_button.isEnabled()
    for worker in list(window.workers): worker.wait(5000)
    app.processEvents()
    errors = []
    monkeypatch.setattr(window,'error',lambda exc:errors.append(str(exc)))
    try:
        buttons = window.findChildren(W.QPushButton)
        def click(text):
            next(button for button in buttons if button.text()==text).click()
        # File browse controls must update their own field, with cancel preserving it.
        for button in buttons:
            key = button.property('input_key')
            if not key:
                continue
            target = tmp_path/(key+'.file')
            monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a,p=target:(str(p),''))
            button.click()
            assert window.inputs[key].text()==str(target)
            monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:('',''))
            button.click()
            assert window.inputs[key].text()==str(target)
        for button,key in zip((b for b in buttons if b.text()=='选择目录…' and window.tabs.widget(0).isAncestorOf(b)),('mmdbridge_path','character_output_dir')):
            monkeypatch.setattr(W.QFileDialog,'getExistingDirectory',lambda *a:str(tmp_path))
            button.click()
            assert window.inputs[key].text()==str(tmp_path)
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:(str(software),''))
        click('添加软件路径')
        for worker in list(window.workers): worker.wait(5000)
        app.processEvents()
        assert read_json(window.controller.home/'software.json')==[str(software)]
        assert window.inputs['blender'].findData(str(software))>=0
        click('重新检测')
        for worker in list(window.workers): worker.wait(5000)
        app.processEvents()
        assert detections[-1]==[str(software)] and len(detections)==3
        click('0.1'); assert window.settings().scale==.1
        click('0.08'); assert window.settings().scale==.08
        window.inputs['fog_density_scale'].setValue(2)
        click('恢复默认设置'); assert window.settings().fog_density_scale==1
        click('载入本机验收素材'); assert 'ParkingLots' in window.settings().scene
        project = tmp_path/'UE/Demo.uproject'
        atomic_json(project,{'FileVersion':3,'Plugins':[{'Name':'Preserve','Enabled':True}]})
        window.inputs['project'].setText(str(project))
        material = project.parent/'Content/Shared/M_Master.uasset'
        material.parent.mkdir(parents=True); material.touch()
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:(str(material),''))
        for button,key in zip((b for b in buttons if b.text()=='选择 UE 材质…'),('character_master_material','eye_master_material')):
            button.click()
            assert window.inputs[key].text()=='/Game/Shared/M_Master'
        monkeypatch.setattr(window,'update_bridge_status',lambda:None)
        click('安装 UE 桥接插件')
        assert (project.parent/'Plugins/MMDBridge/Content/Python/mmd_bridge/ue_appearance.py').is_file()
        assert any(plugin['Name']=='Preserve' for plugin in read_json(project)['Plugins'])
        folder = tmp_path/'job'
        source = tmp_path/'scene.blend'; source.touch()
        from mmd_bridge.core import file_hash
        atomic_json(folder/'job.json',{'settings':asdict(Settings(flow='scene',scene=str(source))),
                                     'inputs':{'scene':file_hash(source)}})
        atomic_json(folder/'manifest.json',{'scene':{'route':'usd_stage'}})
        atomic_json(folder/'status.json',{'state':'needs_review'})
        window.controller.job=Job(folder)
        click('连接所选项目')
        assert Path(read_json(folder/'job.json')['settings']['project'])==project
        assert read_json(folder/'status.json')['state']=='awaiting_ue'
        monkeypatch.setattr('mmd_bridge.controller.ensure_blender_job_stopped',lambda folder:None)
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:(str(folder/'job.json'),''))
        window.lock(False)
        click('恢复已有任务')
        assert window.main_flow_locked and read_json(folder/'status.json')['state']=='scene_done', errors
        click('停止任务')
        assert (folder/'stop').is_file() and not window.main_flow_locked
        assert not errors
    finally:
        window.controller.job=None
        window.close()


def test_auxiliary_buttons_operate_on_isolated_files_and_reviewed_cut_frames(tmp_path, monkeypatch):
    from tests.test_desktop import _mmm_window, _fake_mmm, _drain
    from tests.test_pmx_physics_displayframe import make_pmx
    from mmd_bridge import mmm_runtime as rt
    window = _mmm_window(tmp_path,monkeypatch); window.timer.stop()
    errors = []
    monkeypatch.setattr(window,'error',lambda exc:errors.append(str(exc)))
    try:
        def click(text):
            next(button for button in window.findChildren(W.QPushButton) if button.text()==text).click()
        mmm = _fake_mmm(tmp_path)
        monkeypatch.setattr(W.QFileDialog,'getExistingDirectory',lambda *a:str(mmm))
        next(button for button in window.tabs.widget(window.mmm_tab_index).findChildren(W.QPushButton)
             if button.text()=='选择目录…').click()
        click('检测目录'); assert '✗' not in window.mmm_status.text()
        monkeypatch.setattr(rt,'discover_mmm',lambda:[rt.validate_mmm_dir(mmm)])
        click('自动搜索'); _drain(window)
        assert Path(window.inputs['mmm_path'].text())==mmm
        opened = []
        monkeypatch.setattr(window,'reveal',lambda path:opened.append(Path(path)))
        click('打开目录'); assert opened[-1]==mmm
        window.mmm_prepare_button.click(); _drain(window)
        runtime = Path(rt.runtime_status()['runtime_dir'])
        assert (runtime/'MikuMikuMoving64.exe').is_file()
        click('打开 Runtime 目录'); assert opened[-1]==runtime
        dll = tmp_path/'build/telemetry.dll'; dll.parent.mkdir(); dll.write_bytes(b'MZ'+bytes(512))
        monkeypatch.setattr(window,'_mmm_build_dll',lambda:dll)
        click('安装插件'); assert (runtime/'Plugins'/rt.PLUGIN_DLL_NAME).is_file()
        click('卸载插件'); assert not (runtime/'Plugins'/rt.PLUGIN_DLL_NAME).exists()
        source = tmp_path/'model.pmx'
        source.write_bytes(make_pmx(bones=[('センター',True),('スカート01',True)],
                                  rigid_bodies=[('スカート01',1,1)],display_frames=[('Root',[0])]))
        original = source.read_bytes()
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a:(str(source),''))
        click('选择 PMX…'); assert Path(window.physics_bake_model.text())==source
        window.physics_bake_analyze_button.click(); _drain(window)
        assert window.physics_bake_repair_button.isEnabled()
        window.physics_bake_repair_button.click(); _drain(window)
        assert (tmp_path/'model_PhysicsBakeFixed.pmx').is_file() and source.read_bytes()==original
        project = tmp_path/'Camera.uproject'; atomic_json(project,{'FileVersion':3})
        from mmd_bridge.controller import install_ue_bridge
        install_ue_bridge(project,window.controller.home)
        window.inputs['project'].setText(str(project))
        click('开始 Probe + Detect')
        session = window.controller.camera_cut_session
        assert session is not None and window.camera_cut_stage=='probe'
        mailbox = connection_folder(window.controller.home,project)
        atomic_json(mailbox/'heartbeat.json',{'project':str(project),'time':__import__('time').time()})
        window.poll_camera_cut()
        assert read_json(mailbox/'camera_cut_request.json')['mode']=='probe'
        Job(session).status('camera_cut_complete','Probe complete',mode='probe')
        window.poll_camera_cut()
        assert read_json(mailbox/'camera_cut_request.json')['mode']=='detect'
        atomic_json(session/'detect_01.json',{'candidates':[10,20]})
        Job(session).status('camera_cut_complete','Detect complete',mode='detect')
        window.poll_camera_cut()
        assert window.camera_cut_stage=='review'
        window.camera_cut_frame_edit.setText('30'); click('添加帧')
        assert window._camera_cut_frames_from_list()==[10,20,30]
        window.camera_cut_delete_frame_edit.setText('20'); click('删除填写帧')
        assert window._camera_cut_frames_from_list()==[10,30]
        click('恢复 Detect 结果')
        window.camera_cut_frame_list.item(0).setSelected(True); click('删除选中帧')
        click('确认帧序列并 Bake')
        request = read_json(mailbox/'camera_cut_request.json')
        assert request['mode']=='bake' and request['cut_frames']==[20]
        click('停止任务')
        assert read_json(session/'status.json')['state']=='stopped'
        assert not errors
    finally:
        window.controller.job=None
        window.close()


def test_comparison_and_viewer_reject_stale_images(tmp_path, monkeypatch):
    from mmd_bridge.scene_reference import compare
    from mmd_bridge.core import file_hash
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app = W.QApplication.instance() or W.QApplication([])
    visual = tmp_path/'visual'; visual.mkdir()
    image = G.QImage(16,8,G.QImage.Format_RGB32); image.fill(G.QColor('black'))
    for name in ('blender','ue'): image.save(str(visual/(name+'.png')))
    receipt = {'complete':True,'renderer':{'mode':'lumen'},'image_sha256':file_hash(visual/'ue.png')}
    atomic_json(visual/'ue_render.json',receipt)
    compare(tmp_path)
    assert read_json(visual/'difference.json')['image_hashes']['ue']==receipt['image_sha256']
    image.fill(G.QColor('white')); image.save(str(visual/'ue.png'))
    import pytest
    with pytest.raises(ValueError,match='回执不匹配'): compare(tmp_path)
    window=Window(); window.timer.stop()
    errors=[]
    monkeypatch.setattr(window,'error',lambda exc:errors.append(str(exc)))
    window.controller.job=Job(tmp_path)
    try:
        window.show_scene_visual('comparison.png')
        assert errors==['参考帧已更新，请重新生成对照与差分']
        assert not window.findChildren(W.QDialog)
        window.show_scene_visual('ue.png')
        assert errors[-1]=='UE 图片与成功回执不匹配，请等待渲染完成或重新渲染'
        assert not window.findChildren(W.QDialog)
    finally:
        window.controller.job=None
        window.close()
