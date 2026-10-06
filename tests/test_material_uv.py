from mmd_bridge.material_graph import FunctionLibrary
from mmd_bridge.material_uv import used_channels,relocate_record


def test_uv_usage_follows_functions_and_bindings_not_inactive_presets():
    library=FunctionLibrary()
    body={'op':'add','a':{'op':'mesh_scalar','uv_index':5,'component':1},
          'b':{'op':'function_input','id':'input'}}
    call=library.intern(body,{'input':({'op':'uv','index':6},'VECTOR','Input')})
    record={'id':'source','channels':{'base_color':call},
            'source_preset':{'unused':{'op':'uv','index':7}}}
    assert used_channels([record],library)==[0,5,6]
    packed=relocate_record(record,{0:0,5:1,6:2},library)
    assert packed['id']!=record['id']
    rewritten=packed['channels']['base_color']
    assert rewritten['args'][0]['index']==2 and rewritten['function']!=call['function']
    assert library.definitions[rewritten['function']]['body']['a']['uv_index']==1
    assert library.definitions[call['function']]['body']['a']['uv_index']==5


def test_same_layout_keeps_material_identity_and_repeated_relocation_is_deterministic():
    library=FunctionLibrary();record={'id':'original','channels':{'opacity':{'op':'uv','index':0}}}
    assert relocate_record(record,{0:0},library) is record
    record['channels']['opacity']['index']=4
    a=relocate_record(record,{0:0,4:1},library)
    b=relocate_record(record,{0:0,4:1},library)
    assert a==b and record['channels']['opacity']['index']==4
