import importlib
import sys
from types import SimpleNamespace as N
from mmd_bridge.core import Job,atomic_json,read_json


def test_standalone_renderer_does_not_start_editor_mailbox(tmp_path, monkeypatch):
    callbacks=[]
    fake=N(UnrealEditorSubsystem=object(), get_editor_subsystem=lambda kind:None,
           register_slate_post_tick_callback=lambda cb:callbacks.append(cb))
    monkeypatch.setitem(sys.modules,'unreal',fake)
    host=importlib.import_module('mmd_bridge.ue_host')
    monkeypatch.setattr(host,'u',fake)
    box=tmp_path/'mailbox'
    host.start_bridge(box,str(tmp_path/'test.uproject'))
    assert callbacks==[] and not box.exists()


def test_completed_request_does_not_replay_after_bridge_restart(tmp_path,monkeypatch):
    callbacks=[]
    fake=N(register_slate_post_tick_callback=lambda cb:callbacks.append(cb) or len(callbacks),
           unregister_slate_post_tick_callback=lambda handle:None,
           AssetRegistryHelpers=N(get_asset_registry=lambda:N(is_loading_assets=lambda:False)),
           Paths=N(convert_relative_path_to_full=lambda p:p,get_project_file_path=lambda:str(tmp_path/'test.uproject')),
           SystemLibrary=N(get_engine_version=lambda:'5.7.4'),
           EditorLoadingAndSavingUtils=N(get_dirty_map_packages=lambda:[]),
           log_error=lambda message:None)
    monkeypatch.setitem(sys.modules,'unreal',fake)
    host=importlib.import_module('mmd_bridge.ue_host')
    monkeypatch.setattr(host,'u',fake)
    clock=[100.]
    monkeypatch.setattr(host.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(host,'LAST_TIME',0)
    monkeypatch.setattr(host,'TICK',None)
    job=Job(tmp_path/'job')
    atomic_json(job.path,{})
    job.status('awaiting_ue','waiting')
    box=tmp_path/'mailbox'
    atomic_json(box/'request.json',{'id':'first','job':str(job.folder)})
    calls=[]
    def execute(folder):
        calls.append(folder)
        Job(folder).status('complete','done')
    monkeypatch.setattr(host,'import_job',execute)
    host.start_bridge(box,str(tmp_path/'test.uproject'))
    callbacks[-1](0)
    assert read_json(box/'response.json')['state']=='complete'
    clock[0]+=2
    host.start_bridge(box,str(tmp_path/'test.uproject'))
    callbacks[-1](0)
    assert len(calls)==1
    atomic_json(box/'request.json',{'id':'second','job':str(job.folder)})
    clock[0]+=2
    callbacks[-1](0)
    assert len(calls)==2


def test_nonterminal_receipt_replays_after_bridge_restart(tmp_path,monkeypatch):
    callbacks=[]
    fake=N(register_slate_post_tick_callback=lambda cb:callbacks.append(cb) or len(callbacks),
           unregister_slate_post_tick_callback=lambda handle:None,
           AssetRegistryHelpers=N(get_asset_registry=lambda:N(is_loading_assets=lambda:False)),
           Paths=N(convert_relative_path_to_full=lambda p:p,get_project_file_path=lambda:str(tmp_path/'test.uproject')),
           SystemLibrary=N(get_engine_version=lambda:'5.7.4'),
           EditorLoadingAndSavingUtils=N(get_dirty_map_packages=lambda:[]),
           log_error=lambda message:None)
    monkeypatch.setitem(sys.modules,'unreal',fake)
    host=importlib.import_module('mmd_bridge.ue_host')
    monkeypatch.setattr(host,'u',fake)
    clock=[200.]
    monkeypatch.setattr(host.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(host,'LAST_TIME',0)
    monkeypatch.setattr(host,'TICK',None)
    job=Job(tmp_path/'job')
    atomic_json(job.path,{})
    job.status('awaiting_ue','waiting')
    box=tmp_path/'mailbox'
    atomic_json(box/'request.json',{'id':'interrupted','job':str(job.folder)})
    atomic_json(box/'response.json',{'id':'interrupted','state':'awaiting_ue'})
    calls=[]
    def execute(folder):
        calls.append(folder)
        Job(folder).status('complete','done')
    monkeypatch.setattr(host,'import_job',execute)

    host.start_bridge(box,str(tmp_path/'test.uproject'))
    callbacks[-1](0)

    assert calls==[str(job.folder)]
    assert read_json(box/'response.json')['state']=='complete'


def test_recoverable_failures_and_skips_are_persisted(tmp_path, monkeypatch):
    host=importlib.import_module('mmd_bridge.ue_host')
    logged=[]
    monkeypatch.setattr(host, 'u', N(log_error=logged.append, log_warning=logged.append))
    job=Job(tmp_path/'job')
    atomic_json(job.path,{})
    report={'warnings':[], 'failures':[], 'skipped_items':[]}
    try:
        raise RuntimeError('bad mesh')
    except RuntimeError as exc:
        host.record_import_failure(job, report, 'scene_mesh', 'SM_Bad', 'bad.fbx', exc)
    host.record_import_skip(job, report, 'scene_mesh', 'SM_Empty', 'empty.fbx', '0 vertices')

    saved=read_json(job.folder/'ue_report.json')
    assert saved['failures'][0]['name']=='SM_Bad'
    assert saved['failures'][0]['error']=='bad mesh'
    assert saved['skipped_items'][0]['reason']=='0 vertices'
    assert read_json(job.folder/'status.json')['state']=='ue_item_skipped'
    assert len(logged)==2
