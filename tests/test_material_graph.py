import copy
import pytest
from mmd_bridge.material_graph import FunctionLibrary,select_candidate


def test_material_parent_shares_values_and_images_but_not_state_or_samplers():
    from mmd_bridge.material_graph import material_template
    state={'masked':False,'two_sided':False}
    a={'surface_graph':{'kind':'slab','channels':{'color':{'op':'texture','image':{
        'id':'first','path':'first.png','color_space':'sRGB','hdr':False}},
        'roughness':{'op':'constant','value':.2}}},'channels':{}}
    b=copy.deepcopy(a);b['surface_graph']['channels']['color']['image'].update(id='second',path='second.png')
    b['surface_graph']['channels']['roughness']['value']=.8
    one=material_template(a,state);two=material_template(b,state)
    assert one['_parent_key']==two['_parent_key']
    assert one['_bindings']!=two['_bindings']
    assert material_template(a,{**state,'two_sided':True})['_parent_key']!=one['_parent_key']
    b['surface_graph']['channels']['color']['image']['color_space']='Non-Color'
    assert material_template(b,state)['_parent_key']!=one['_parent_key']


def test_function_identity_ignores_group_labels_ports_and_instance_values():
    lib=FunctionLibrary()
    body={'op':'math','mode':'MULTIPLY','a':{'op':'function_input','id':'a'},
          'b':{'op':'function_input','id':'b'}}
    one=lib.intern(body,{'a':({'op':'vertex_color'},'RGBA','Color'),
                         'b':({'op':'constant','value':.5},'VALUE','Strength')},'Tree.001')
    two=lib.intern({**body,'a':{'op':'function_input','id':'x'},'b':{'op':'function_input','id':'y'}},
        {'x':({'op':'vertex_color'},'RGBA','Renamed'), 'y':({'op':'constant','value':.8},'VALUE','Other')},'Arbitrary')
    assert one['function']==two['function']
    assert len(lib.definitions)==1
    assert lib.expand(one)['b']['value']==.5
    assert lib.expand(two)['b']['value']==.8


def test_nested_functions_retain_dependencies_and_bound_fields():
    lib=FunctionLibrary()
    inner=lib.intern({'op':'function_input','id':'field'},
                     {'field':({'op':'texture','image':{'id':'one'}},'RGBA','Cache')})
    outer=lib.intern({'op':'math','mode':'ADD','a':inner,'b':{'op':'constant','value':1}}, {})
    assert len(lib.reachable(outer))==2
    assert lib.expand(outer)['a']['image']['id']=='one'
    changed=copy.deepcopy(lib.definitions[inner['function']]['body'])
    changed={'op':'math','mode':'ADD','a':changed,'b':{'op':'constant','value':.1}}
    other=lib.intern(changed,{'0':({'op':'texture','image':{'id':'one'}},'RGBA','Cache')})
    assert other['function']!=inner['function']


def test_texture_resources_are_caller_bindings_and_paths_do_not_split_functions():
    lib=FunctionLibrary()
    def body(path):return {'op':'texture','texture':{'op':'texture_object','image':{
        'id':'texture','path':path,'color_space':'sRGB','hdr':False}},'uv':{'op':'uv'},'output':'Color'}
    a=lib.intern(body('a.png'),{});b=lib.intern(body('b.png'),{})
    assert a['function']==b['function']
    assert lib.definitions[a['function']]['inputs'][0]['type']=='TEXTURE2D'
    assert a['args'][0]['image']['path']=='a.png'
    assert b['args'][0]['image']['path']=='b.png'


def candidate(identity,**overrides):
    return dict(id=identity,compiled=True,semantic_valid=True,ray_tracing_valid=True,
                scene_peak_mb=6000.,gpu_ms=10.,baked_bytes=0,import_ms=0,**overrides)


def test_memory_budget_is_a_constraint_before_speed():
    bad=candidate('fast');bad.update(scene_peak_mb=7200,gpu_ms=1)
    result=select_candidate([bad,candidate('safe')],budget_mb=8192)
    assert result['selected']=='safe'
    assert result['rejected']==[{'id':'fast','reason':'memory_budget'}]


@pytest.mark.parametrize('missing',['compiled','semantic_valid','ray_tracing_valid','gpu_ms','scene_peak_mb'])
def test_unverified_or_unmeasured_candidate_never_wins(missing):
    c=candidate('unknown');c.pop(missing)
    assert select_candidate([c],budget_mb=8192)['selected'] is None


def test_similar_time_prefers_fewer_baked_resources_and_is_order_independent():
    bake=candidate('bake');bake['baked_bytes']=1000000
    nodes=candidate('nodes');nodes['gpu_ms']=10.4
    for candidates in ([bake,nodes],[nodes,bake]):
        assert select_candidate(candidates,budget_mb=8192)['selected']=='nodes'
    nodes['gpu_ms']=10.6
    assert select_candidate([bake,nodes],budget_mb=8192)['selected']=='bake'


def test_only_shared_memory_controls_gpu_protection():
    from mmd_bridge.process import gpu_budget_percent
    info=dict(total_mb=16000,used_mb=7900,dedicated_total_mb=8000,dedicated_used_mb=7500,
              shared_total_mb=8000,shared_used_mb=400)
    assert gpu_budget_percent(info)==5
    info.update(dedicated_used_mb=1000,local_used_mb=6000,local_budget_mb=6500)
    assert gpu_budget_percent(info)==5
    info.update(shared_used_mb=7200)
    assert gpu_budget_percent(info)==90


def test_source_random_values_preserve_object_and_island_scopes():
    from mmd_bridge.scene_shader_data import object_random,island_random,hash_uint,uint_to_float
    values=island_random(8,[(0,1),(1,2),(2,3),(4,5),(5,6),(6,7)],[0,2,4,7])
    assert values[:2]==[uint_to_float(hash_uint(0))]*2
    assert values[2:]==[uint_to_float(hash_uint(4))]*2
    assert values[0]!=values[2]
    assert object_random('One')!=object_random('Two')
    assert object_random('One',123)==object_random('Two',123)
    assert object_random('One',123)!=values[0]


def test_high_system_ram_does_not_stop_gpu_work():
    from mmd_bridge.process import gpu_budget_percent
    assert gpu_budget_percent(dict(dedicated_used_mb=4000,dedicated_total_mb=8000,
        shared_used_mb=1000,shared_total_mb=8000,system_used_mb=15900,system_total_mb=16000))==12.5
