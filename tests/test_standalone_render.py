from pathlib import Path
from types import SimpleNamespace as N
import ast
import pytest
from mmd_bridge.core import Job, atomic_json, read_json
from mmd_bridge.controller import Controller
from mmd_bridge.standalone_render import asset_path, make_plan, finish_result


def job_fixture(tmp_path):
    job = Job(tmp_path / 'job')
    project = tmp_path / 'Test.uproject'; project.write_text('{}')
    editor = tmp_path / 'UnrealEditor.exe'; editor.touch()
    editor.with_name('UnrealEditor-Cmd.exe').touch()
    atomic_json(job.path, {'settings': {'project': str(project), 'unreal': str(editor)}})
    atomic_json(job.folder / 'ue_report.json', {'assets': {
        'level': '/Game/Task/Maps/Main', 'sequence': '/Game/Task/Sequences/LS_MMD',
        'render_preset': '/Game/Task/Materials/MRQ_SourceLook'}})
    job.status('needs_review', 'import still requires visual review')
    return job


def test_custom_preset_and_copy_reference_are_validated():
    assert asset_path("MoviePipelinePrimaryConfig'/Game/User/MRQ_High.MRQ_High'") == '/Game/User/MRQ_High.MRQ_High'
    for path in ('/Game/../Other', '/Game/Preset\n-exec=bad', 'F:/Thing.uasset', '/Game/Bad"Command'):
        with pytest.raises(ValueError): asset_path(path)


def test_resume_fingerprint_covers_umap_and_external_material_dependencies(tmp_path):
    from mmd_bridge.standalone_render import source_fingerprint
    job=job_fixture(tmp_path)
    report=read_json(job.folder/'ue_report.json')
    report['assets'].update(materials={'leaf':'/Game/Shared/MI_Leaf.MI_Leaf'},
                           source_packages=['/Game/Shared/M_Leaf','/Game/Textures/T_Leaf'])
    atomic_json(job.folder/'ue_report.json',report)
    request=dict(project=job.data['settings']['project'],level=report['assets']['level'],
                 sequence=report['assets']['sequence'],preset=report['assets']['render_preset'])
    for relative in ('Task/Maps/Main.umap','Shared/M_Leaf.uasset','Textures/T_Leaf.uasset'):
        path=tmp_path/'Content'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'original')
    before=source_fingerprint(job,request)
    for relative in ('Task/Maps/Main.umap','Shared/M_Leaf.uasset','Textures/T_Leaf.uasset'):
        path=tmp_path/'Content'/relative;path.write_bytes(b'edited')
        assert source_fingerprint(job,request)!=before
        path.write_bytes(b'original')


def test_isolated_plan_preserves_import_state_and_uses_game_runtime(tmp_path):
    job = job_fixture(tmp_path)
    before = (job.folder / 'status.json').read_bytes()
    run, request, command = make_plan(job, '/Game/User/MRQ_High', frame=7)
    assert '-game' in command and '-RenderOffscreen' in command
    assert '-ExecutorPythonClass=/Engine/PythonTypes.MMDBridgeRuntimeExecutor' in command
    assert '/Engine/Maps/Entry' in command
    assert '-notexturestreaming' not in command
    assert request['frame'] == 7 and Path(request['output']).is_dir()
    assert request['render_vram_guard_enabled'] is False
    other, _, _ = make_plan(job, '/Game/User/MRQ_High')
    assert other != run
    assert (job.folder / 'status.json').read_bytes() == before


def test_experimental_fog_plan_carries_source_prim_targets(tmp_path):
    job=job_fixture(tmp_path)
    data=job.data
    data['settings'].update(fog_occlusion_quality='vsm_finer',width=1280,height=720)
    atomic_json(job.path,data)
    volume=dict(status='converted',kind='local_grid',prim_path='/World/Volumes/Fog')
    atomic_json(job.folder/'manifest.json',{'scene':{'volumes':[volume]}})
    before=job.path.read_bytes()
    _,request,_=make_plan(job,'/Game/Task/Materials/MRQ_SourceLook')
    assert request['fog_volume_sources']==[volume]
    assert request['fog_occlusion_quality']=='vsm_finer'
    assert job.path.read_bytes()==before


def test_native_fog_plan_carries_same_sources_without_rewriting_old_job(tmp_path):
    job=job_fixture(tmp_path)
    data=job.data;data['settings'].update(fog_occlusion_quality='native_balanced',width=2560,height=1080)
    atomic_json(job.path,data)
    volume=dict(status='converted',kind='world_grid',prim_path='/World/Volumes/Fog')
    atomic_json(job.folder/'manifest.json',{'scene':{'volumes':[volume]}})
    before=job.path.read_bytes()
    _,request,_=make_plan(job,'/Game/Task/Materials/MRQ_SourceLook')
    assert request['fog_volume_sources']==[volume] and request['fog_occlusion_quality']=='native_balanced'
    assert job.path.read_bytes()==before


def test_only_generated_source_preset_uses_requested_resolution(tmp_path):
    job = job_fixture(tmp_path)
    data = job.data
    data['settings'].update(width=2560, height=1080)
    atomic_json(job.path, data)
    _, source, _ = make_plan(job, '/Game/Task/Materials/MRQ_SourceLook')
    _, custom, _ = make_plan(job, '/Game/User/MRQ_High')
    assert source['expected_resolution'] == [2560, 1080]
    assert 'expected_resolution' not in custom
    assert source['renderer_override']=='lumen' and 'renderer_override' not in custom
    _, traced, _=make_plan(job,'/Game/Task/Materials/MRQ_SourceLook',pipeline='path_tracing')
    assert traced['renderer_override']=='path_tracing'


def test_path_tracer_actual_pass_can_complete_without_finalimage(tmp_path):
    from tests.test_render_frames import write_png
    job=job_fixture(tmp_path)
    run,request,_=make_plan(job,'/Game/User/MRQ_PathTracer',frame=1,pipeline='path_tracing')
    write_png(Path(request['output'])/'MMDBridge_PathTracer_0001.png')
    atomic_json(run/'runtime.json',{'success':True,'frame_contract':
        {'start':1,'end':2,'step':1,'resolution':[1,1],'passes':['PathTracer']}})
    assert finish_result(run,0)['complete']


def test_plan_includes_world_sun_and_expected_volume_bindings(tmp_path):
    job=job_fixture(tmp_path)
    report=read_json(job.folder/'ue_report.json')
    report['checks']={'world_lighting':{'sun':{'component':'/Game/Map.Map:PersistentLevel.Sun.DirectionalLightComponent0',
        'source':{'type':'SUN','energy':2.,'color':[1,1,1]}}},
        'usd_stage':{'volumes':[{'prim_path':'/World/Volumes/Fog'}]}}
    atomic_json(job.folder/'ue_report.json',report)
    _,request,_=make_plan(job,'/Game/User/MRQ')
    assert request['world_light_bindings'][0]['actor_name']=='Sun'
    assert request['world_light_bindings'][0]['source']['type']=='SUN'
    assert request['world_volume_paths']==['/World/Volumes/Fog']


def test_successful_render_with_valid_png_at_wrong_resolution_is_rejected(tmp_path):
    from tests.test_render_frames import write_png
    job = job_fixture(tmp_path)
    data = job.data
    data['settings'].update(width=2, height=1)
    atomic_json(job.path, data)
    run, request, _ = make_plan(job, '/Game/Task/Materials/MRQ_SourceLook', frame=1)
    write_png(Path(request['output']) / 'MMDBridge_FinalImage_0001.png')
    atomic_json(run / 'runtime.json', {'success': True, 'resolution': [1, 1],
        'frame_contract': {'start': 1, 'end': 2, 'step': 1, 'resolution': [1, 1]}})
    result = finish_result(run, 0)
    assert result['frames']['complete_frames'] == [1]
    assert not result['complete'] and not result['resolution_match']
    # A reported requested resolution also cannot hide a different frame contract.
    atomic_json(run / 'runtime.json', {'success': True, 'resolution': [2, 1],
        'frame_contract': {'start': 1, 'end': 2, 'step': 1, 'resolution': [1, 1]}})
    assert not finish_result(run, 0)['complete']


def test_runtime_default_material_replacement_rejects_render_success(tmp_path):
    job=job_fixture(tmp_path)
    run,request,_=make_plan(job,'/Game/User/MRQ_High',frame=1)
    request['frame_ledger']=False
    atomic_json(run/'request.json',request)
    (Path(request['output'])/'frame.png').write_bytes(b'output')
    atomic_json(run/'runtime.json',{'success':True})
    (run/'unreal.log').write_text('LogMaterial: Warning: Material /Game/MI missing usage flag Nanite! Default Material will be used in game.\n',encoding='utf-8')
    result=finish_result(run,0)
    assert not result['complete']
    assert len(result['material_runtime_failures'])==1


def test_runtime_missing_fog_cannot_pass_with_a_successful_image(tmp_path):
    from tests.test_render_frames import write_png
    job=job_fixture(tmp_path)
    run,request,_=make_plan(job,'/Game/User/MRQ_High',frame=1)
    write_png(Path(request['output'])/'MMDBridge_FinalImage_0001.png')
    atomic_json(run/'runtime.json',{'success':True,'frame_contract':
        {'start':1,'end':2,'step':1,'resolution':[1,1]},'runtime_world_validation':
        {'resource_match':False,'errors':['missing_volume_binding:/World/Fog']}})
    result=finish_result(run,0)
    assert not result['complete'] and result['world_runtime_failures']==['missing_volume_binding:/World/Fog']


def test_resume_preserves_valid_frames_and_quarantines_incomplete_files(tmp_path):
    from mmd_bridge.standalone_render import resume_plan
    from mmd_bridge.core import file_hash
    from tests.test_render_frames import write_png
    job=job_fixture(tmp_path)
    run,request,_=make_plan(job,'/Game/User/MRQ_High')
    output=Path(request['output'])
    good=output/'MMDBridge_FinalImage_0000.png';write_png(good)
    bad=output/'MMDBridge_FinalImage_0001.png';bad.write_bytes(b'partial PNG')
    digest=file_hash(good)
    atomic_json(run/'runtime.json',{'success':False,'frame_contract':
                                  {'start':0,'end':3,'step':1,'resolution':[1,1]}})
    resumed,new_request,_=resume_plan(job,run)
    assert new_request['ranges']==[[1,3]] and file_hash(good)==digest
    assert not bad.exists()
    quarantine=read_json(resumed/'quarantined_frames.json')
    assert Path(quarantine[0]['preserved']).read_bytes()==b'partial PNG'


def test_exit_zero_and_old_receipt_are_not_render_success(tmp_path):
    job = job_fixture(tmp_path)
    run, request, _ = make_plan(job, '/Game/User/MRQ_High', frame=1)
    atomic_json(run / 'runtime.json', {'success': True})
    assert not finish_result(run, 0)['complete']
    # A file with arbitrary bytes is not proof of a completed rendered frame.
    image = Path(request['output']) / 'frame.png'; image.write_bytes(b'new render')
    assert not finish_result(run,0)['complete']
    from tests.test_render_frames import write_png
    image.unlink();image=Path(request['output'])/'MMDBridge_FinalImage_0001.png';write_png(image)
    atomic_json(run/'runtime.json',{'success':True,'frame_contract':{'start':1,'end':2,'step':1,'resolution':[1,1]}})
    assert finish_result(run, 0)['complete']
    (run / 'unreal.log').write_text('No camera actor found on Eval Tick')
    assert not finish_result(run, 0)['complete']
    (run / 'unreal.log').write_text('DXGI_ERROR_DEVICE_REMOVED')
    assert not finish_result(run, 0)['complete']
    assert not finish_result(run, 1)['complete']


def test_running_editor_blocks_before_any_deploy_or_output(tmp_path, monkeypatch):
    controller = Controller(tmp_path / 'home'); controller.job = job_fixture(tmp_path)
    monkeypatch.setattr(controller, '_same_project_running', lambda cfg: True)
    with pytest.raises(ValueError, match='保存并关闭'):
        controller.render_background('/Game/User/MRQ_High')
    assert not (controller.job.folder / 'renders').exists()


def test_single_frame_does_not_calibrate_from_idle_gpu_samples(tmp_path):
    from tests.test_render_frames import write_png
    job=job_fixture(tmp_path)
    run,request,_=make_plan(job,'/Game/User/MRQ_High',frame=0)
    write_png(Path(request['output'])/'MMDBridge_FinalImage_0000.png')
    atomic_json(run/'runtime.json',{'success':True,'frame_contract':
                                  {'start':0,'end':1,'step':1,'resolution':[1,1]}})
    (run/'gpu.csv').write_text('GPUTime\n'+('0.1\n'*64))
    result=finish_result(run,0)
    assert result['complete'] and result['gpu_measurement']['status']=='unmeasured'


def test_poll_and_stop_only_own_render_preserve_import_state(tmp_path, monkeypatch):
    controller = Controller(tmp_path / 'home'); controller.job = job_fixture(tmp_path)
    before = (controller.job.folder / 'status.json').read_bytes()
    run, request, _ = make_plan(controller.job, '/Game/User/MRQ_High')
    child = N(pid=123, poll=lambda: None, wait=lambda timeout: None)
    log = (run / 'launcher.log').open('w')
    controller.standalone_render = {'active': True, 'run': run, 'request': request, 'process': child, 'log': log}
    controller.processes = [('standalone_render', child)]
    monkeypatch.setattr('mmd_bridge.controller.process.gpu_memory_info', lambda: None)
    atomic_json(run / 'runtime.json', {'state': 'rendering', 'progress': .25})
    assert controller.poll()['progress'] == .25
    stopped = []
    monkeypatch.setattr('mmd_bridge.controller.process.terminate_pid', lambda pid, **kwargs: stopped.append(pid))
    controller.stop()
    assert stopped == [123] and not controller.processes
    assert read_json(run / 'result.json')['state'] == 'stopped'
    assert not (controller.job.folder / 'stop').exists()
    assert (controller.job.folder / 'status.json').read_bytes() == before


def test_runtime_preset_copy_preserves_existing_settings_and_adds_only_missing(tmp_path):
    class Pass: 
        def is_enabled(self): return True
    class Output:
        def is_enabled(self): return True
    class Config: pass
    class Sequence: pass
    renderer, writer = Pass(), Output()
    output = N(output_resolution=N(x=640,y=360), use_custom_playback_range=False)
    config = N(get_all_settings=lambda: [renderer,writer],
        find_or_add_setting_by_class=lambda cls: output,
        initialize_transient_settings=lambda: None, find_setting_by_class=lambda cls: None)
    source = Config(); source.get_path_name = lambda: '/Game/User/MRQ'
    chosen=[]
    job = N(set_configuration=chosen.append, get_configuration=lambda: config)
    native=N(Paths=N(convert_relative_path_to_full=lambda p:p, get_project_file_path=lambda:str(tmp_path/'P.uproject')),
        load_asset=lambda path: source if path.endswith('MRQ') else Sequence(),
        MoviePipelinePrimaryConfig=Config, LevelSequence=Sequence, MoviePipelineExecutorJob=object,
        SoftObjectPath=str, MoviePipelineRenderPass=Pass, MoviePipelineOutputBase=Output,
        MoviePipelineOutputSetting=object, MoviePipelineConsoleVariableSetting=object,
        DirectoryPath=lambda path:N(path=path))
    request={'project':str(tmp_path/'P.uproject'),'preset':'/Game/User/MRQ','sequence':'/Game/LS',
        'level':'/Game/Map','pipeline':'lumen','output':str(tmp_path/'out'),'frame':None}
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_standalone_render.py').read_text(encoding='utf-8'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='prepare_job')
    ns={'u':native,'Path':Path,'_REQUEST':request,'record':lambda **kwargs:None}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<runtime>', 'exec'),ns)
    ns['prepare_job'](N(allocate_new_job=lambda cls:job))
    assert chosen == [source]
    assert (output.output_resolution.x,output.output_resolution.y)==(640,360)
    assert not output.use_custom_playback_range
    request['frame']=12
    ns['prepare_job'](N(allocate_new_job=lambda cls:job))
    assert (output.custom_start_frame,output.custom_end_frame)==(12,13)


def test_gui_background_selection_dispatch_and_failure_unlock(tmp_path, monkeypatch):
    monkeypatch.setenv('MMDBRIDGE_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window(); window.timer.stop()
    errors=[]
    monkeypatch.setattr(window,'error',lambda exc:errors.append(str(exc)))
    try:
        window.controller.job=job_fixture(tmp_path)
        window.update_scene_actions()
        assert window.background_preset.currentText()=='/Game/Task/Materials/MRQ_SourceLook'
        assert window.background_render_button.isEnabled()
        window.background_preset.setCurrentText('/Game/User/MRQ_Custom')
        window.update_scene_actions()
        assert window.background_preset.currentText()=='/Game/User/MRQ_Custom'
        calls=[]
        def launch(*args):
            calls.append(args)
            return tmp_path/'run'
        monkeypatch.setattr(window.controller,'render_background',launch)
        window.background_range.setCurrentIndex(1)
        window.background_frame.setValue(5)
        window.background_render_button.click()
        for worker in list(window.workers): worker.wait(5000)
        app.processEvents()
        assert calls==[('/Game/User/MRQ_Custom','',5,'lumen')]
        window.lock(False)
        def blocked(*args): raise ValueError('请先保存并关闭 UE')
        monkeypatch.setattr(window.controller,'render_background',blocked)
        window.background_render_button.click()
        for worker in list(window.workers): worker.wait(5000)
        app.processEvents()
        assert errors==['请先保存并关闭 UE'] and not window.main_flow_locked
    finally:
        window.close(); app.processEvents()
