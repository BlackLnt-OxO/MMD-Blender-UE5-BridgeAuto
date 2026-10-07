import copy
import pytest
from mmd_bridge.core import Job, Settings, atomic_json, read_json, file_hash
from mmd_bridge.material_workflow import LEGACY_MAIN, SEMANTIC, workflow, white_materials
from mmd_bridge.material_repair import prepare, verify_request, commit_records, restore_records, add_sublayers, rollback_interrupted_records
from mmd_bridge.controller import Controller


def fixture(tmp_path):
    source=tmp_path/'source.blend';source.write_bytes(b'original source')
    root=tmp_path/'Scene.usda';root.write_text('#usda 1.0\n( subLayers = [@Geometry.usda@, @ToolMaterials.usda@] )',encoding='utf-8')
    job=Job(tmp_path/'job')
    atomic_json(job.path,{'settings':{'scene':str(source),'scene_format':'usd_stage','material_workflow':LEGACY_MAIN},
                          'inputs':{'scene':file_hash(source)},'asset_id':'asset'})
    bad={'id':'bad','name':'SharedName','channels':{'base_color':{'op':'constant','value':[.5,.5,.5,1]}},
         'material_error':'Generated coordinate unavailable','strategy':'nodes','requires_visual_review':True}
    good={'id':'good','name':'SharedName','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}},
          'strategy':'nodes','requires_visual_review':True}
    manifest={'scene':{'route':'usd_stage','materials':[bad,good],
        'prototypes':[{'id':'p','prim_path':'/World/P','material_slots':['bad','good']}],
        'instances':[{'name':'Object','source_name':'NativeObject','prototype_id':'p','prim_path':'/World/P'}]}}
    report={'assets':{'level':'/Game/Root/Maps/Preview','materials':{'bad':'/Game/Root/Bad.Bad','good':'/Game/Root/Good.Good'}},
            'root':'/Game/Root','checks':{'usd_stage':{'root_layer':str(root)}},'failures':[]}
    atomic_json(job.folder/'manifest.json',manifest);atomic_json(job.folder/'ue_report.json',report)
    return job,manifest,report


def test_default_new_workflow_does_not_migrate_old_jobs():
    assert Settings().material_workflow==LEGACY_MAIN
    assert workflow({})==SEMANTIC


def test_white_selection_uses_failed_record_not_name_color_or_review(tmp_path):
    _,manifest,report=fixture(tmp_path)
    assert [r['id'] for r in white_materials(manifest,report)]==['bad']
    report['checks']['material_binding_failures']=[{'material_id':'good','error':'binding missing'}]
    assert {r['id'] for r in white_materials(manifest,report)}=={'bad','good'}


def test_repair_scope_keeps_same_name_good_record_and_preserves_scene(tmp_path):
    job,manifest,report=fixture(tmp_path)
    folder=prepare(job,['bad']);request=verify_request(job,folder)
    assert request['targets'][0]['placements'][0]['slots']==[0]
    assert request['protected_materials']['good']['name']=='SharedName'
    assert read_json(job.folder/'manifest.json')==manifest
    assert read_json(job.folder/'ue_report.json')==report
    with pytest.raises(ValueError):prepare(job,['good'])


def test_source_and_manifest_change_block_stale_patch(tmp_path):
    job,_,_=fixture(tmp_path);folder=prepare(job)
    atomic_json(job.folder/'manifest.json',{'changed':True})
    with pytest.raises(ValueError,match='记录已改变'):verify_request(job,folder)


def test_report_and_frame_change_block_stale_patch(tmp_path):
    job,_,report=fixture(tmp_path);folder=prepare(job)
    atomic_json(job.folder/'ue_report.json', dict(report, user_review=True))
    with pytest.raises(ValueError, match='报告已改变'):verify_request(job, folder)
    atomic_json(job.folder/'ue_report.json', report)
    data=job.data;data['settings']['scene_frame']=2;atomic_json(job.path,data)
    with pytest.raises(ValueError, match='源帧已改变'):verify_request(job, folder)


def test_commit_changes_only_selected_record_and_guarded_undo(tmp_path):
    job,manifest,report=fixture(tmp_path);folder=prepare(job)
    good=copy.deepcopy(manifest['scene']['materials'][1])
    repaired={'id':'bad','name':'SharedName','strategy':'hybrid','channels':{'base_color':{'op':'constant','value':[.2,.3,.4,1]}},
              'requires_visual_review':True}
    commit_records(job,folder,{'bad':{'record':repaired,'asset':'/Game/Root/MaterialRepairs/Fixed.Fixed'}})
    after=read_json(job.folder/'manifest.json')
    assert after['scene']['materials'][1]==good
    assert read_json(job.folder/'ue_report.json')['assets']['materials']['good']==report['assets']['materials']['good']
    assert read_json(job.folder/'white_materials.json')['materials']==[]
    hashes={name:file_hash(job.folder/name) for name in ('manifest.json','ue_report.json')}
    atomic_json(folder/'receipt.json',{'committed_hashes':hashes})
    restore_records(job,folder)
    assert read_json(job.folder/'manifest.json')==manifest
    atomic_json(job.folder/'manifest.json',{'manual_edit':True})
    with pytest.raises(ValueError,match='拒绝覆盖'):restore_records(job,folder)


def test_overlay_is_stronger_and_refuses_invalid_path():
    result=add_sublayers('#usda 1.0\n( subLayers = [@Geometry.usda@] )',['MaterialRepairs/R/a.usda'])
    assert result.index('@MaterialRepairs')<result.index('@Geometry')
    with pytest.raises(ValueError):add_sublayers('not a root',['a.usda'])


def test_interrupted_commit_restores_only_transaction_versions(tmp_path):
    job, manifest, report = fixture(tmp_path); folder = prepare(job)
    before = {name:file_hash(job.folder/name) for name in ('manifest.json','ue_report.json')}
    atomic_json(folder/'journal.json', {'state':'applying','metadata_before':before})
    repaired = {'id':'bad','name':'SharedName','strategy':'nodes','channels':{}}
    commit_records(job,folder,{'bad':{'record':repaired,'asset':'/Game/Root/Repair.Repair'}})
    # A crash can leave just one of the two new files committed.
    atomic_json(job.folder/'ue_report.json', report)
    rollback_interrupted_records(job, folder, read_json(folder/'journal.json'))
    assert read_json(job.folder/'manifest.json') == manifest
    assert read_json(job.folder/'ue_report.json') == report
    atomic_json(job.folder/'manifest.json', {'user_edit':True})
    with pytest.raises(ValueError, match='拒绝覆盖'):
        rollback_interrupted_records(job, folder, read_json(folder/'journal.json'))


def test_main_workflow_never_auto_repairs(tmp_path):
    c=Controller(tmp_path/'home')
    assert not c._poll_material_bake_retry('needs_review',{'material_workflow':LEGACY_MAIN})


def test_dead_editor_heartbeat_does_not_count_as_connection(tmp_path, monkeypatch):
    import time
    import mmd_bridge.controller as controller_module
    c=Controller(tmp_path/'home'); c.mailbox=tmp_path/'mailbox'
    project=tmp_path/'Project.uproject';project.write_text('{}')
    atomic_json(c.mailbox/'heartbeat.json', {'project':str(project),'time':time.time(),'pid':12345})
    monkeypatch.setattr(controller_module.process, 'pid_exists', lambda pid: False)
    assert not c._heartbeat_fresh({'project':str(project)})
    monkeypatch.setattr(controller_module.process, 'pid_exists', lambda pid: True)
    assert c._heartbeat_fresh({'project':str(project)})
