from dataclasses import asdict
from types import SimpleNamespace
import copy
import pytest
from mmd_bridge.core import Settings, Job, atomic_json, read_json, file_hash
from mmd_bridge.character_geometry import geometry_policy, check_geometry_checkpoint, MODES
from mmd_bridge.character_geometry_roles import purpose
from mmd_bridge import character_geometry_review as review


def cfg(**values):
    return {**asdict(Settings()), 'character_backend':'mmdbridge_fbx', **values}


def test_four_modes_have_distinct_cache_and_missing_mode_keeps_old_policy():
    policies = [geometry_policy(cfg(character_geometry_mode=m)) for m in MODES]
    assert len({repr(p) for p in policies})==4
    for i,p in enumerate(policies):
        for j,m in enumerate(MODES):
            if i==j:check_geometry_checkpoint({'geometry_policy':p},cfg(character_geometry_mode=m))
            else:
                with pytest.raises(ValueError):check_geometry_checkpoint({'geometry_policy':p},cfg(character_geometry_mode=m))
    old = cfg();old.pop('character_geometry_mode')
    assert geometry_policy(old)['version']==2
    assert 'mode' not in geometry_policy(old)
    assert geometry_policy(cfg(character_geometry_mode='original',character_backend='fbx_legacy')) is None


@pytest.mark.parametrize('mode',['original','weld_weighted'])
def test_overlay_inputs_invalidate_non_subdivided_exports(mode,tmp_path):
    model=tmp_path/'identity.pmx';model.write_bytes(b'identity fixture')
    digest=file_hash(model)
    c=cfg(model=str(model),character_geometry_mode=mode)
    policy=geometry_policy(c)
    assert policy['version']==4 and policy['overlay_repair']['offset_m']==.0001
    old=copy.deepcopy(policy);old['version']=3;old.pop('overlay_repair')
    with pytest.raises(ValueError):check_geometry_checkpoint({'geometry_policy':old},c)
    c['character_geometry_overrides']={'another-model':{'1':{'host':2}}}
    check_geometry_checkpoint({'geometry_policy':policy},c)
    c['character_geometry_overrides'][digest]={'1':{'host':2}}
    with pytest.raises(ValueError):check_geometry_checkpoint({'geometry_policy':policy},c)
    texture=tmp_path/'alpha.png';texture.write_bytes(b'alpha')
    c['character_material_overrides']={digest:{'1':{'textures':{'base_color':{'path':str(texture)}}}}}
    saved={'geometry_policy':geometry_policy(c)}
    texture.write_bytes(b'updated alpha')
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,c)


@pytest.mark.parametrize('mode',['original','weld_weighted'])
def test_non_subdivided_overlay_mapping_and_review_are_recorded(mode):
    from mmd_bridge.character_geometry import resolved_geometry_signature,candidate_review_items
    c=cfg(character_geometry_mode=mode)
    receipt={'geometry_policy':geometry_policy(c),'overlay_repair':{
        'version':1,'appearance_status':'not_reviewed','meshes':[{'attachments':[
            {'pmx_index':7,'host_pmx_index':2,'complete':True,'export_vertex_pairs':[[10,3]]}],
            'attachment_reviews':[]}]}}
    saved={'geometry_policy':receipt['geometry_policy'],'geometry_resolved_sha256':resolved_geometry_signature(receipt)}
    check_geometry_checkpoint(saved,c,receipt)
    reviewed=copy.deepcopy(receipt);reviewed['overlay_repair']['appearance_status']='passed'
    check_geometry_checkpoint(saved,c,reviewed)
    changed=copy.deepcopy(receipt)
    changed['overlay_repair']['meshes'][0]['attachments'][0]['export_vertex_pairs']=[[10,4]]
    with pytest.raises(ValueError,match='贴层映射'):check_geometry_checkpoint(saved,c,changed)
    assert len(candidate_review_items(receipt))==1
    changed['overlay_repair']['meshes'][0]['attachment_reviews']=[{'pmx_index':9,'reason':'多个宿主'}]
    assert any(r['reason']=='多个宿主' for r in candidate_review_items(changed))


@pytest.mark.parametrize('name,kind,selected',[
    ('Neck','skin',True),('脖子','skin',True),('首','skin',True),('Lips','face',True),
    ('Tight Shirt','cloth',True),('紧身上衣','cloth',True),('Gloves','cloth',True),('手袋','cloth',True),
    ('Body','skin',True),('stocking','hosiery',True),('Loose Shirt','cloth',False),
    ('Skirt','cloth',False),('Necklace','metal',False),('Shoes','rubber',False),
    ('Eye','iris',False),('Eyelash','lash',False),('Teeth','teeth',False),('Hair','hair',False)])
def test_extended_selection_and_original_legacy_semantics(name,kind,selected):
    record={'name':name,'character_material':{'pbr':{'type':kind}}}
    evidence={'humanoid_rig':True,'head_weight_ratio':.9,'body_weight_ratio':.9,
              'leg_weight_ratio':.8,'near_leg_ratio':.9,'hand_weight_ratio':.8,
              'arm_weight_ratio':.1,'trunk_weight_ratio':0.,'near_body_ratio':.9}
    assert purpose(record,evidence,extended=True)['local_selected']==selected
    if name=='Neck':assert purpose(record,evidence)['needs_review']
    if name=='Gloves':assert purpose(record,evidence)['needs_review']


def snapshot(model, names):
    rows=[]
    for i,(name,role,selected,optional) in enumerate(names):
        rows.append({'pmx_index':i,'name':name,'pbr':{'type':'generic'},'geometry_purpose':{
            'role':role,'local_selected':selected,'needs_review':role=='unknown',
            'optional_region':optional,'reasons':['test evidence']}})
    return {'model':str(model),'sha256':file_hash(model),'materials':rows}


def test_only_missing_anatomy_prompts_and_other_unknowns_are_silent(monkeypatch,tmp_path):
    model=tmp_path/'one.pmx';model.write_bytes(b'model')
    from mmd_bridge import character_pbr
    data=snapshot(model,[('Body','body',True,None),('Neck','neck',True,'neck'),
                         ('Lip','face',True,'lips'),('Face','face',True,None),('Anonymous','unknown',False,None)])
    monkeypatch.setattr(character_pbr,'inspect_model',lambda *args:copy.deepcopy(data))
    c=cfg(model=str(model),character_geometry_mode='local_subdivision')
    report=review.build_review(c)
    assert not report['pending'] and len(report['warnings'])==1
    assert not report['models'][0]['slots'][4]['pending']
    data['materials'][1]['geometry_purpose'].update(role='unknown',needs_review=True,local_selected=False)
    report=review.build_review(c)
    assert report['pending']
    assert report['models'][0]['missing_regions']==['脖子']
    result=review.apply_selection(c,report,{file_hash(model):[0,1,2,3,4]})
    assert result['character_geometry_overrides'][file_hash(model)]['1']['local']=='include'
    result=review.apply_selection(c,report,{file_hash(model):[0,2,3]})
    assert result['character_geometry_overrides'][file_hash(model)]['1']['local']=='exclude'
    assert result['character_geometry_overrides'][file_hash(model)]['4']['local']=='exclude'
    with pytest.raises(ValueError):review.apply_selection(c,report,{file_hash(model):[99]})
    model.write_bytes(b'changed')
    with pytest.raises(ValueError,match='模型已改变'):review.apply_selection(c,report,{report['models'][0]['sha256']:[0]})


def test_selection_preserves_other_models_hosts_and_overrides(tmp_path):
    model=tmp_path/'one.pmx';model.write_bytes(b'model');digest=file_hash(model)
    c=cfg(model=str(model),character_geometry_overrides={'other':{'2':{'local':'include'}},digest:{'0':{'host':2}}})
    report={'models':[{'model':str(model),'sha256':digest,'slots':[{'pmx_index':0,'selected':False,'pending':True}]}]}
    result=review.apply_selection(c,report,{digest:[0]})
    assert result['character_geometry_overrides']['other']==c['character_geometry_overrides']['other']
    assert result['character_geometry_overrides'][digest]['0']=={'host':2,'local':'include'}
    assert c['character_geometry_overrides'][digest]['0']=={'host':2}


def test_controller_wait_resume_and_confirm_before_any_native_process(monkeypatch,tmp_path):
    from mmd_bridge import controller
    model=tmp_path/'one.pmx';model.write_bytes(b'model')
    monkeypatch.setattr(controller,'ensure_blender_job_stopped',lambda p:None)
    job=Job(tmp_path/'job');job.folder.mkdir()
    c=cfg(model=str(model),character_geometry_mode='local_subdivision')
    atomic_json(job.path,{'settings':c,'inputs':{},'character_inputs':[]})
    digest=file_hash(model)
    report={'version':1,'signature':review.configuration_signature(c),'warnings':['anonymous warning'],'pending':True,
            'models':[{'model':str(model),'sha256':digest,'slots':[{'pmx_index':0,'selected':False,'pending':True}]}]}
    monkeypatch.setattr(review,'build_review',lambda _:copy.deepcopy(report))
    starts=[];monkeypatch.setattr(controller,'run_bake',lambda job:starts.append(job.folder))
    owner=controller.Controller(tmp_path/'home');owner.job=job
    owner._begin_geometry_review();owner.geometry_thread.join(5)
    assert read_json(job.folder/'status.json')['state']=='awaiting_geometry_selection'
    assert not starts and not owner.processes
    resumed=controller.Controller(tmp_path/'home2');resumed.resume(job.folder);resumed.geometry_thread.join(5)
    assert resumed.poll()['state']=='awaiting_geometry_selection'
    assert not starts
    resumed.confirm_geometry_selection({digest:[0]})
    assert not starts # only the next controller tick may launch a native stage
    resumed.poll();resumed.bake_thread.join(5)
    assert starts==[job.folder]
    assert read_json(job.path)['settings']['character_geometry_overrides'][digest]['0']['local']=='include'
    assert read_json(job.folder/'geometry_review_confirmed.json')['automatic'] is False


def test_cancel_review_starts_nothing_and_leaves_report(monkeypatch,tmp_path):
    from mmd_bridge.controller import Controller
    owner=Controller(tmp_path/'home');owner.job=Job(tmp_path/'job');owner.job.folder.mkdir()
    atomic_json(owner.job.path,{'settings':cfg()});atomic_json(owner.job.folder/'geometry_review.json',{'pending':True})
    owner.cancel_geometry_selection()
    assert owner.stopped and not owner.processes
    assert owner.poll()['state']=='cancelled'
    assert (owner.job.folder/'geometry_review.json').is_file()


def test_installed_bridge_can_read_geometry_receipts_without_blender_or_gui(tmp_path):
    import subprocess,sys
    from mmd_bridge.controller import install_ue_bridge,UE_CODE_MODULES
    project=tmp_path/'Bridge.uproject';project.write_text('{}',encoding='utf8')
    install_ue_bridge(project,tmp_path/'home')
    python=tmp_path/'Plugins/MMDBridge/Content/Python'
    for name in ('character_geometry.py','character_geometry_roles.py','blender_contour.py'):
        assert name in UE_CODE_MODULES and (python/'mmd_bridge'/name).is_file()
    script="import sys;sys.path.insert(0,sys.argv[1]);from mmd_bridge.character_geometry import candidate_review_items;assert candidate_review_items({'geometry_policy':None})==[];assert 'bpy' not in sys.modules and 'PySide6' not in sys.modules"
    result=subprocess.run([sys.executable,'-I','-c',script,str(python)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_resume_rejects_changed_mode_even_when_old_exports_exist(tmp_path):
    from mmd_bridge.controller import Controller
    model=tmp_path/'source.pmx';model.write_bytes(b'source')
    job=Job(tmp_path/'job');job.folder.mkdir()
    old=cfg(model=str(model),character_geometry_mode='original')
    atomic_json(job.path,{'settings':dict(old,character_geometry_mode='weld_weighted'),'inputs':{}})
    atomic_json(job.folder/'character_checkpoint.json',{'geometry_policy':geometry_policy(old)})
    atomic_json(job.folder/'manifest.json',{'files':{'model':{'path':'old.fbx'}}})
    owner=Controller(tmp_path/'home')
    with pytest.raises(ValueError,match='新建任务'):owner.resume(job.folder)
    assert not owner.processes and owner.bake_thread is None


def test_stale_review_does_not_reopen_or_start_native_tools(tmp_path):
    from mmd_bridge.controller import Controller
    model=tmp_path/'source.pmx';model.write_bytes(b'source')
    c=cfg(model=str(model),character_geometry_mode='local_subdivision')
    owner=Controller(tmp_path/'home');owner.job=Job(tmp_path/'job');owner.job.folder.mkdir()
    atomic_json(owner.job.path,{'settings':c})
    atomic_json(owner.job.folder/'geometry_review.json',{'signature':review.configuration_signature(c)})
    owner.job.status('awaiting_geometry_selection','review')
    model.write_bytes(b'changed')
    with pytest.raises(ValueError,match='新建任务'):owner.confirm_geometry_selection({})
    assert owner.poll()['state']=='failed'
    assert not owner.processes and owner.bake_thread is None
