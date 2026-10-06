from mmd_bridge.material_contract import closure_features, finalize_restoration, asset_name_for_path
from mmd_bridge.material_graph import material_template
from mmd_bridge.ocio_cache import portable_config


def test_placeholder_import_is_not_source_material_success():
    record={'name':'Metal','surface':'pbr','material_error':'Generated source missing',
            'basic_pbr_bake':{'status':'failed','error':'coordinate mismatch'}}
    finalize_restoration(record,'GNHost')
    assert record['conversion_status']=='placeholder'
    assert record['issues'][0]['severity']=='error'
    assert record['issues'][0]['source_objects']==['GNHost']


def test_visual_review_does_not_mean_conversion_failure():
    record={'name':'Glass','surface':'pbr','requires_visual_review':True}
    finalize_restoration(record,'Standee')
    finalize_restoration(record,'OtherStandee')
    assert record['conversion_status']=='converted'
    assert record['visual_status']=='needs_review'
    assert record['source_objects']==['Standee','OtherStandee']


def test_render_appearance_mismatch_survives_successful_native_import():
    from mmd_bridge.material_results import restoration_rows
    manifest={'scene':{'materials':[{'id':'glass','name':'Standee','surface':'pbr'}]}}
    report={'assets':{'materials':{'glass':'/Game/MI'}},'checks':{'material_visual_findings':{
        'glass':{'result':'appearance_mismatch','reasons':['Printed face is white']}}}}
    row=restoration_rows(manifest,report)[0]
    assert row['ue_verified'] and row['conversion_status']=='converted'
    assert row['visual_result']=='appearance_mismatch'
    assert row['visual_status']=='needs_review'
    assert 'Printed face is white' in row['reasons']


def test_omitted_coat_is_explicit_approximation():
    record={'name':'Layered','surface':'pbr','basic_pbr_bake':{'status':'baked','omitted_effects':['Coat Weight']}}
    finalize_restoration(record)
    assert record['conversion_status']=='approximated'
    assert any(issue.get('channel')=='Coat Weight' for issue in record['issues'])


def test_shared_parent_keeps_closure_features_static_and_discriminates_topology():
    graph={'kind':'bsdf_principled','features':['subsurface'],
        'channels':{'transmission':{'op':'constant','value':0},'subsurface':{'op':'constant','value':.4}}}
    record={'surface_graph':graph,'channels':{}}
    state={'masked':False}
    template=material_template(record,state)
    assert closure_features(template['surface_graph'])=={'subsurface'}
    glass=material_template({**record,'surface_graph':{**graph,'features':['transmission']}},state)
    assert glass['_parent_key']!=template['_parent_key']


def test_unicode_ocio_source_copies_whole_bundle_to_hash_addressed_ascii_path(tmp_path):
    source=tmp_path/'原始配置';source.mkdir()
    (source/'config.ocio').write_text('src: Display.cube',encoding='utf-8')
    (source/'Display.cube').write_text('first LUT',encoding='ascii')
    config=portable_config(source/'config.ocio',tmp_path/'Project')
    assert str(config).isascii()
    assert (config.parent/'Display.cube').read_text()=='first LUT'
    assert portable_config(source/'config.ocio',tmp_path/'Project')==config
    (source/'Display.cube').write_text('second LUT',encoding='ascii')
    updated=portable_config(source/'config.ocio',tmp_path/'Project')
    assert updated!=config
    assert (config.parent/'Display.cube').read_text()=='first LUT'


def test_deep_asset_root_preserves_unique_names_within_native_windows_path_limit(tmp_path):
    content=tmp_path/'Project/Content'
    folder='/Game/Validation/FullScene/A_Scene_It_sMyGO_ParkingLots_01_6b41dcfa/Textures'
    first='T_Bake_field_c55ddf7e716fefae048f7aeb__01_D_v2_b15b2e5b2ef0'
    second=first.replace('__01','__02')
    a=asset_name_for_path(folder,first,content);b=asset_name_for_path(folder,second,content)
    assert a!=b
    assert len(str(content/folder[6:]/(a+'.uasset')))<=240
    assert len(folder+'/'+a)<=144
    assert asset_name_for_path(folder,first,content)==a


def test_legacy_zero_ue_failures_does_not_hide_source_placeholder():
    from mmd_bridge.material_results import restoration_rows
    manifest={'scene':{'materials':[{'id':'one','name':'Glass','surface':'pbr','material_error':'missing Glass'}]}}
    report={'failures':[],'assets':{'materials':{'one':'/Game/Fallback'}}}
    rows=restoration_rows(manifest,report)
    assert rows[0]['conversion_status']=='placeholder'
    assert rows[0]['ue_verified']
