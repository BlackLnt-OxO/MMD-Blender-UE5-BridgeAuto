from mmd_bridge.core import Settings
from mmd_bridge.material_policy import prefer_basic_pbr_bake


def test_observed_transmission_mismatch_selects_opaque_retry_without_compile_error():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    records=[{'id':'glass','name':'Glass','transmission_candidate':True}, {'id':'metal','name':'Metal'}]
    report={'checks':{'material_visual_findings':{
        key:{'result':'appearance_mismatch'} for key in ('glass','metal')}}}
    retries=basic_pbr_retry_sources(records,report,'scene.blend')
    assert len(retries)==1
    assert next(iter(retries.values()))['opaque_fallback'] is True
    assert next(iter(retries.values()))['scope']=='visual_transmission'


def test_local_unsupported_operations_never_trigger_whole_pbr_bake():
    supported={'type':'ShaderNodeMath','capability':{'route':'nodes'}}
    assert not prefer_basic_pbr_bake({'nodes':[supported]*1000,'name':'ComplicatedPreset'})
    assert not prefer_basic_pbr_bake({'nodes':[{'type':'Unrelated','capability':{'route':'local_cache'}}]})
    assert not prefer_basic_pbr_bake({'nodes':[{'type':'Unrelated','capability':{'route':'unsupported'}}]})
    assert not prefer_basic_pbr_bake({'nodes':[]})
    assert not prefer_basic_pbr_bake({'scope':'displacement'})
    assert not prefer_basic_pbr_bake({'scope':'material_channel'})
    assert prefer_basic_pbr_bake({'scope':'complete_surface'})
    assert prefer_basic_pbr_bake({'scope':'ue_material_compile'})
    assert prefer_basic_pbr_bake({'scope':'conversion_timeout'})


def test_default_settings_enable_pbr_texture_fallback():
    defaults=Settings()
    assert defaults.material_bake_fallback is True
    assert defaults.material_complex_strategy=='pbr_bake'
    assert defaults.material_conversion_timeout == 120.0
    assert Settings(material_bake_fallback=False, material_complex_strategy='nodes').material_bake_fallback is False


def test_only_real_compile_failures_select_source_materials_and_aliases():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    from mmd_bridge.core import stable_id
    records=[{'id':'one','name':'Preset','source_aliases':['Preset','Renamed'], 'strategy_requested':'auto'},
             {'id':'two','name':'Reliable','strategy_requested':'auto'},
             {'id':'three','name':'Strict','strategy_requested':'nodes'},
             {'id':'four','name':'AlreadyBaked','basic_pbr_bake':{'status':'failed'}}]
    report={'failures':[{'stage':'material','material_id':'one','error':'原生材质编译失败 operator limit'},
                        {'stage':'material','material_id':'two','error':'asset save failure'},
                        {'stage':'material','material_id':'three','error':'原生材质编译失败'},
                        {'stage':'material','material_id':'four','error':'原生材质编译失败'}]}
    result=basic_pbr_retry_sources(records,report,'source.blend')
    assert set(result)=={stable_id('source.blend','Preset'),stable_id('source.blend','Renamed')}
    assert all(r['scope']=='ue_material_compile' for r in result.values())


def test_failed_shared_parent_selects_all_affected_materials():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    records=[{'id':'one','name':'First'},{'id':'two','name':'Second'}]
    report={'material_routes':{key:{'parent':'/Game/Run/M_Parent.M_Parent'} for key in ('one','two')},
            'checks':{'material_compile_errors':['Failed to compile Material /Game/Run/M_Parent']}}
    assert len(basic_pbr_retry_sources(records,report,'source.blend'))==2


def test_ue_conversion_problems_select_only_the_failed_material():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    from mmd_bridge.core import stable_id
    records = [{'id': 'slow', 'name': 'Slow'}, {'id': 'ok', 'name': 'Good'}]
    for error in ('原生材质转换超时：超过单材质时间预算', '原生材质转换失败：无法连接表面闭包图'):
        report = {'failures': [{'stage': 'material', 'material_id': 'slow', 'error': error}]}
        assert set(basic_pbr_retry_sources(records, report, 'source.blend')) == {stable_id('source.blend', 'Slow')}


def test_compile_identity_cannot_select_an_unrelated_material_with_same_name_as_id():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    from mmd_bridge.core import stable_id
    records=[{'id':'identity','name':'Anonymous A'}, {'id':'other','name':'identity'}]
    report={'failures':[{'stage':'material','material_id':'identity','error':'原生材质编译失败'}]}
    assert set(basic_pbr_retry_sources(records,report,'different.blend'))=={stable_id('different.blend','Anonymous A')}


def test_shared_parent_prefix_cannot_trigger_unrelated_surface_bakes():
    from mmd_bridge.material_policy import basic_pbr_retry_sources
    from mmd_bridge.core import stable_id
    records=[{'id':'short','name':'Anonymous A'}, {'id':'long','name':'Anonymous B'}]
    report={'material_routes':{'short':{'parent':'/Game/Test/M_Parent.M_Parent'},
        'long':{'parent':'/Game/Test/M_ParentExtra.M_ParentExtra'}},
        'checks':{'material_compile_errors':['Failed to compile Material /Game/Test/M_ParentExtra.M_ParentExtra']}}
    assert set(basic_pbr_retry_sources(records,report,'different.blend'))=={stable_id('different.blend','Anonymous B')}
