import ast
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


def methods(names,native):
    path=Path(__file__).parents[1]/'mmd_bridge/ue_materials.py'
    cls=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
             if isinstance(n,ast.ClassDef) and n.name=='Materials')
    selected=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
    namespace={'u':native,'math':math}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(path),'exec'),namespace)
    return namespace


def test_ue_conversion_budget_raises_and_clears_deadline(monkeypatch):
    import time
    namespace = methods({'build', 'check_conversion_budget'}, SimpleNamespace())
    ticks = iter((0., 2.))
    monkeypatch.setattr(time, 'perf_counter', lambda: next(ticks))
    builder = SimpleNamespace(cfg={'material_conversion_timeout': 1.},
                              _build=lambda record, cache: 'material')
    builder.check_conversion_budget = lambda: namespace['check_conversion_budget'](builder)
    with pytest.raises(RuntimeError, match='原生材质转换超时'):
        namespace['build'](builder, {'pmx_index': None}, None)
    assert builder._conversion_deadline is None


def test_ue_baked_fallback_has_no_conversion_deadline(monkeypatch):
    import time
    namespace = methods({'build', 'check_conversion_budget'}, SimpleNamespace())
    monkeypatch.setattr(time, 'perf_counter', lambda: 999.)
    builder = SimpleNamespace(cfg={'material_conversion_timeout': 1.},
                              _build=lambda record, cache: 'baked material')
    builder.check_conversion_budget = lambda: namespace['check_conversion_budget'](builder)
    assert namespace['build'](builder, {'basic_pbr_bake': {'status': 'baked'}}, None) == 'baked material'
    assert builder._conversion_deadline is None


def test_gpu_pause_commits_only_new_pending_owned_assets():
    assets={'/Game/New':SimpleNamespace(name='new'),'/Game/Foreign':SimpleNamespace(name='foreign'),
        '/Game/UserEdit':SimpleNamespace(name='user')}
    saved=[];tags=[];protected=[]
    native=SimpleNamespace(load_asset=assets.get,EditorAssetLibrary=SimpleNamespace(
        get_metadata_tag=lambda asset,key:'task' if asset.name=='new' else 'user',
        set_metadata_tag=lambda *args:tags.append(args),
        save_loaded_asset=lambda asset:saved.append(asset.name) or True))
    builder=SimpleNamespace(owner='task',_pending_created={'/Game/New','/Game/Foreign'},protect_asset=protected.append)
    result=methods({'checkpoint_interrupted_build'},native)['checkpoint_interrupted_build'](builder)
    assert result==['/Game/New'] and saved==['new']
    assert protected==[assets['/Game/New']]
    assert tags[0][-1]=='interrupted'


def test_cached_ownership_does_not_dirty_package_but_rejects_foreign_owner():
    tags={'owner':'task'};writes=[]
    native=SimpleNamespace(EditorAssetLibrary=SimpleNamespace(
        get_metadata_tag=lambda asset,key:tags['owner'],
        set_metadata_tag=lambda *args:writes.append(args)))
    managed=methods({'managed'},native)['managed']
    asset=SimpleNamespace(get_path_name=lambda:'/Game/Task/T.T')
    builder=SimpleNamespace(owner='task')
    for _ in range(4):managed(builder,asset)
    assert not writes
    tags['owner']='user'
    with pytest.raises(RuntimeError,match='归属冲突'):managed(builder,asset)
    assert not writes
    tags['owner']=''
    managed(builder,asset)
    assert len(writes)==1 and writes[0][-1]=='task'


def test_texture_object_uses_actual_native_tex_port_and_rejects_failed_connection():
    calls=[]
    native=SimpleNamespace()
    namespace=methods({'connect'},native)
    namespace['re']=re
    builder=SimpleNamespace(lib=SimpleNamespace(
        get_material_expression_input_names=lambda target:['UVs','Tex','Apply View MipBias'],
        connect_material_expressions=lambda *args:calls.append(args) or True))
    namespace['connect'](builder,('source',''),('target',''),'TextureObject')
    assert calls[-1]==('source','','target','Tex')
    builder.lib.connect_material_expressions=lambda *args:False
    with pytest.raises(RuntimeError,match='连接失败'):
        namespace['connect'](builder,('source',''),('target',''),'TextureObject')


def test_import_log_follows_actual_native_abslog_with_spaces(tmp_path):
    path=Path(__file__).parents[1]/'mmd_bridge/ue_host.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
              if isinstance(n,ast.FunctionDef) and n.name=='active_import_log')
    parameters={'AbsLog':str(tmp_path/'Folder with spaces/actual.log')}
    native=SimpleNamespace(SystemLibrary=SimpleNamespace(get_command_line=lambda:'native command',
        parse_command_line=lambda command:([],[],parameters)),
        Paths=SimpleNamespace(convert_relative_path_to_full=lambda value:value))
    namespace={'u':native,'Path':Path}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    project=tmp_path/'Test.uproject'
    assert namespace['active_import_log'](project)==Path(parameters['AbsLog'])
    parameters.clear()
    assert namespace['active_import_log'](project)==tmp_path/'Saved/Logs/Test.log'


@pytest.mark.parametrize('mode',['POINT','TEXTURE','VECTOR','NORMAL'])
def test_uv_mapping_accepts_editable_xyz_rotation_using_same_3d_semantics(mode):
    native=SimpleNamespace()
    expression=methods({'expression'},native)['expression']
    seen=[]
    class Builder:
        def expr(self,value,**kwargs):seen.append(value);return ('mapped','')
        def node(self,*args,**kwargs):return ('xy','')
        def connect(self,*args):pass
        def op(self,*args):return args
        def constant(self,value):return value
    rotation={'op':'parameter','name':'EditableRotation','type':'vector'}
    value={'op':'mapping','uv':{'op':'uv','index':2},'vector_type':mode,
           'rotation':rotation,'offset':{'op':'constant','value':[.1,.2,.3]},
           'scale':{'op':'constant','value':[2,3,4]}}
    result=expression(Builder(),value)
    assert seen[0]['op']=='mapping_3d'
    assert seen[0]['rotation']==rotation and seen[0]['vector_type']==mode
    assert seen[0]['uv']=={'op':'blender_uv','uv':value['uv']}
    assert result==('Add',('Multiply',('xy',''),[1,-1]),[0,1])


@pytest.mark.parametrize('sheet,expected', [(True, 'masked'), (False, 'translucent')])
def test_sheet_coverage_and_scattering_keep_masked_shadow_path(sheet, expected):
    from mmd_bridge.material_policy import cutout_transmission
    properties = SimpleNamespace(**{key: key for key in (
        'MP_BASE_COLOR', 'MP_ROUGHNESS', 'MP_METALLIC', 'MP_SPECULAR', 'MP_NORMAL',
        'MP_EMISSIVE_COLOR', 'MP_OPACITY_MASK', 'MP_SUBSURFACE_COLOR', 'MP_OPACITY', 'MP_REFRACTION')})
    native = SimpleNamespace(Material=object, MaterialFactoryNew=object, MaterialProperty=properties,
        BlendMode=SimpleNamespace(BLEND_MASKED='masked', BLEND_OPAQUE='opaque', BLEND_TRANSLUCENT='translucent'),
        MaterialShadingModel=SimpleNamespace(MSM_TWO_SIDED_FOLIAGE='two_sided_foliage'),
        TranslucencyLightingMode=SimpleNamespace(TLM_SURFACE_PER_PIXEL_LIGHTING='forward'),
        EditorAssetLibrary=SimpleNamespace(set_metadata_tag=lambda *args: None))
    namespace = methods({'build', '_build', 'check_conversion_budget'}, native)
    namespace.update(cutout_transmission=cutout_transmission, prop=lambda asset, key, value: asset.__setitem__(key, value))
    connected = []
    class Builder:
        _build = namespace['_build']
        check_conversion_budget = namespace['check_conversion_budget']
        cfg = {'restore_scene_appearance': True}
        function_definitions = {}
        report = {'warnings': []}
        lib = SimpleNamespace(connect_material_property=lambda source, output, target: connected.append(target) or True)
        def build_from_master(self, record): return None
        def create(self, *args): return {}
        def clear_expressions(self, material): pass
        def expr(self, *args, **kwargs): return ('expression', '')
        def coverage_expr(self, *args): return ('coverage', '')
        def constant(self, *args): return ('constant', '')
        def scalar(self, *args): return ('scalar', '')
        def op(self, *args): return ('operator', '')
        def finish_material(self, record, material): return material
    record = {'name': 'unrelated_name', 'asset_name': 'M_test', 'surface': 'pbr',
        'surface_policy': {'size_m': [1, 2, 3], 'mesh_surface': {'thin_open_surface': sheet}},
        'channels': {'base_color': {'op': 'constant', 'value': [.2, .4, .1]},
                     'opacity': {'op': 'texture'}, 'transmission': {'op': 'texture'}}}
    result = namespace['build'](Builder(), record, None)
    assert result['blend_mode'] == expected
    if sheet:
        assert result['two_sided'] and result['shading_model'] == 'two_sided_foliage'
        assert 'MP_OPACITY_MASK' in connected and 'MP_SUBSURFACE_COLOR' in connected
        assert 'MP_REFRACTION' not in connected and 'MP_OPACITY' not in connected
    else:
        assert 'MP_REFRACTION' in connected
