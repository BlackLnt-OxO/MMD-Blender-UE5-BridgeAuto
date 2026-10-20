from dataclasses import asdict
import pytest
from mmd_bridge.core import Settings
from mmd_bridge.character_geometry import geometry_policy, check_geometry_checkpoint, target_material_indices


def config(**extra):
    return {**asdict(Settings()), 'character_backend':'mmdbridge_fbx', 'character_geometry_mode':None, **extra}


def test_explicit_new_jobs_and_legacy_cache_isolation():
    cfg=config()
    assert geometry_policy(cfg)['subdivision_enabled'] is False
    legacy=dict(cfg);legacy.pop('character_subdivision_enabled')
    assert geometry_policy(legacy) is None
    check_geometry_checkpoint({},legacy)
    with pytest.raises(ValueError):check_geometry_checkpoint({},cfg)
    saved={'geometry_policy':geometry_policy(cfg)}
    check_geometry_checkpoint(saved,cfg)
    cfg['character_subdivision_enabled']=True
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)
    assert geometry_policy(cfg)['subdivision_enabled'] is True
    assert geometry_policy(dict(cfg,character_backend='fbx_legacy')) is None


def test_effective_types_and_actual_bindings_select_only_body_hosiery():
    records=[{'asset_name':n,'character_material':{'pbr':{'type':t}}} for n,t in
             [('Body','skin'),('Lip','face'),('Face','face'),('Sock','hosiery'),('Hair','hair')]]
    assert target_material_indices(['Hair','Sock','Face','Body','Lip','Unknown'],records)==[1,3]
    assert target_material_indices(['Face','Lip'],records)==[]
    records[0]['character_material']['pbr']['type']='cloth'
    assert target_material_indices(['Body','Sock'],records)==[1]


def test_geometry_version_and_manual_type_overrides_invalidate_checkpoint(tmp_path):
    import hashlib
    model=tmp_path/'model.pmx';model.write_bytes(b'local model')
    digest=hashlib.sha256(model.read_bytes()).hexdigest()
    cfg=config(model=str(model));saved={'geometry_policy':geometry_policy(cfg)}
    saved['geometry_policy']['version']-=1
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)
    saved={'geometry_policy':geometry_policy(cfg)}
    cfg['character_material_overrides']={'different-model':{'1':{'type':'cloth'}}}
    check_geometry_checkpoint(saved,cfg)
    cfg['character_material_overrides'][digest]={'1':{'type':'auto','params':{'Roughness':.8}}}
    check_geometry_checkpoint(saved,cfg)
    cfg['character_material_overrides'][digest]['1']['type']='cloth'
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)
    cfg['character_material_overrides'].pop(digest)
    cfg['character_pbr_options']={'type':'cloth'}
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)


def test_candidate_scope_and_separate_geometry_overrides_are_model_isolated(tmp_path):
    import hashlib
    model=tmp_path/'identity.pmx';model.write_bytes(b'identity fixture')
    digest=hashlib.sha256(model.read_bytes()).hexdigest()
    cfg=config(model=str(model),character_geometry_candidate_scope='local')
    saved={'geometry_policy':geometry_policy(cfg)}
    assert saved['geometry_policy']['candidate']['scope']=='local'
    cfg['character_geometry_overrides']={'other':{'1':{'local':'include'}}}
    check_geometry_checkpoint(saved,cfg)
    cfg['character_geometry_overrides'][digest]={'1':{'local':'include','host':2}}
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)
    cfg['character_geometry_overrides'].pop(digest)
    cfg['character_geometry_candidate_scope']='all'
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)
    cfg['character_geometry_candidate_scope']='local'
    texture=tmp_path/'alpha.png';texture.write_bytes(b'alpha source')
    cfg['character_material_overrides']={digest:{'1':{'textures':{'base_color':{'path':str(texture)}}}}}
    saved={'geometry_policy':geometry_policy(cfg)}
    texture.write_bytes(b'changed alpha source')
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg)


def test_resolved_targets_and_attachment_mapping_cannot_be_silently_reused():
    import copy
    from mmd_bridge.character_geometry import resolved_geometry_signature
    cfg=config(character_geometry_candidate_scope='local');policy=geometry_policy(cfg)
    receipt={'geometry_policy':policy,'scope':'local','geometry_roles':[{'role':'body','pmx_index':8}],
             'meshes':[{'target_pmx_indices':[8,9],'attachments':[{'pmx_index':9,'host_pmx_index':8,'source_face_pairs':[[2,3]]}],
                        'attachment_reviews':[]}]}
    saved={'geometry_policy':policy,'geometry_resolved_sha256':resolved_geometry_signature(receipt)}
    check_geometry_checkpoint(saved,cfg,receipt)
    changed=copy.deepcopy(receipt);changed['meshes'][0]['attachments'][0]['source_face_pairs']=[[2,4]]
    with pytest.raises(ValueError):check_geometry_checkpoint(saved,cfg,changed)
    assert resolved_geometry_signature({'geometry_policy':None}) is None


def test_candidates_report_geometry_and_appearance_review_separately_from_import():
    from mmd_bridge.character_geometry import candidate_review_items
    assert candidate_review_items({'geometry_policy':None})==[]
    r={'geometry_policy':{'candidate':{'scope':'local'}},'scope':'local','appearance_status':'not_reviewed',
       'geometry_roles':[{'name':'mixed slot','needs_review':True,'reasons':['证据冲突']}],
       'meshes':[{'attachments':[{'pmx_index':4,'complete':False,'reason':'映射失败'}],'attachment_reviews':[]}]}
    result=candidate_review_items(r)
    assert len(result)==3
    assert any('编译' in x['reason'] for x in result)
    assert any(x['name']=='mixed slot' for x in result)
