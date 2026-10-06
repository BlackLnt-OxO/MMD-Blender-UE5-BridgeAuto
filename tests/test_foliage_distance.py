import ast
import json
import math
import re
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from test_ue_render_defaults import Asset, Instance, Texture, native_namespace


def material_methods():
    tree = ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_materials.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='Materials')
    namespace = {'math':math,'json':json,'re':re,'__name__':'mmd_bridge.ue_materials',
        'u':SimpleNamespace(AssetEditorSubsystem=object(),get_editor_subsystem=lambda kind:
            SimpleNamespace(close_all_editors_for_asset=lambda material:None))}
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('expr','coverage_expr','connect','clear_expressions')]
    exec(compile(ast.Module(body=methods,type_ignores=[]),'ue_materials.py','exec'),namespace)
    return namespace


def test_texture_bias_uses_exposed_native_pin_and_rebuild_discards_deleted_nodes():
    methods=material_methods()
    material=object();deleted=[];connections=[]
    def connect(source,output,target,pin):
        connections.append(pin)
        return pin=='Bias'
    counts=iter([4,2,0])
    lib=SimpleNamespace(get_material_expression_input_names=lambda target:['UVs','Tex','Bias','Apply View MipBias'],
        connect_material_expressions=connect,delete_all_material_expressions=deleted.append,
        get_num_material_expressions=lambda material:next(counts))
    builder=SimpleNamespace(lib=lib,_expression_material=material,_expression_cache={'graph':object()})
    methods['connect'](builder,(object(),''),(object(),''),'MipBias')
    assert connections==['Bias']
    methods['clear_expressions'](builder,material)
    assert deleted==[material,material] and builder._expression_cache=={}
    assert builder._expression_material is material


def test_shared_color_graph_is_independent_of_coverage_bias_and_exception_restores_context():
    methods=material_methods()
    builder=SimpleNamespace(material=object(),cfg={'foliage_distance_scale':2},check_conversion_budget=lambda:None)
    builder.expr=MethodType(methods['expr'],builder)
    builder.coverage_expr=MethodType(methods['coverage_expr'],builder)
    def expression(value,**kwargs):
        if value.get('fail'):raise RuntimeError('Unsupported mask')
        return SimpleNamespace(bias=getattr(builder,'_coverage_mip_bias',None))
    builder.expression=expression
    graph={'op':'mix','factor':{'op':'texture','output':'Color'}}
    color=builder.expr(graph,data=True)
    mask=builder.coverage_expr(graph)
    assert mask is not color and mask.bias==-1 and color.bias is None
    assert builder.coverage_expr(graph) is mask
    assert builder.expr(graph,data=True) is color
    with pytest.raises(RuntimeError):builder.coverage_expr({'op':'texture','fail':True})
    assert builder._coverage_mip_bias is None
    builder.cfg['foliage_distance_scale']=4
    assert builder.coverage_expr(graph).bias==-2


class Component(Asset):pass
class Instanced(Component):
    def set_cull_distances(self,start,end):
        self.properties.update(instance_start_cull_distance=start,instance_end_cull_distance=end)
class Hierarchical(Instanced):pass


def test_owned_masked_components_keep_distance_without_modifying_foreign_assets_or_collision():
    root='/Game/Task'
    masked=Asset(blend_mode='masked',two_sided=True)
    leaf=Asset(root+'/Leaf',nanite_settings=Asset(enabled=False),
               static_materials=[SimpleNamespace(material_interface=masked)])
    foreign=Asset('/Game/User/Leaf',nanite_settings=Asset(enabled=False),
                  static_materials=[SimpleNamespace(material_interface=masked)])
    def component(path,mesh):
        return Hierarchical(path,static_mesh=mesh,never_distance_cull=False,
            allow_cull_distance_volume=True,min_draw_distance=12.,ld_max_draw_distance=500.,
            instance_start_cull_distance=300,instance_end_cull_distance=500,
            enable_density_scaling=True,collision_enabled='original')
    owned=component('owned',leaf);other=component('foreign',foreign)
    native=SimpleNamespace(MaterialInstanceConstant=Instance,BlendMode=SimpleNamespace(BLEND_MASKED='masked'),
        StaticMeshComponent=Component,InstancedStaticMeshComponent=Instanced,
        HierarchicalInstancedStaticMeshComponent=Hierarchical)
    configure=native_namespace('ue_foliage.py',native)['configure_distance_preservation']
    result=configure({}, {}, root, [leaf,foreign],components=[owned,owned,other])
    assert result['components_checked']==result['components_changed']==1
    assert owned.properties['never_distance_cull']
    assert owned.properties['instance_end_cull_distance']==0
    assert not owned.properties['enable_density_scaling']
    assert owned.properties['collision_enabled']=='original'
    assert other.properties['instance_end_cull_distance']==500
    again=configure({}, {}, root, [leaf],components=[owned])
    assert again['components_checked']==1 and again['components_changed']==0


def test_coverage_updates_shader_texture_version_and_preserves_asset_guard():
    root='/Game/Task'
    material=Instance(root+'/Materials/MI_Leaf',parent=Asset(
        blend_mode='masked',two_sided=True,opacity_mask_clip_value=.33))
    texture=Texture(root+'/Textures/T_Leaf_D_v2_content',do_scale_mips_for_alpha_coverage=False,
        alpha_coverage_thresholds=SimpleNamespace(x=0,y=0,z=0,w=.75))
    events=[]
    native=SimpleNamespace(MaterialInstanceConstant=Instance,Texture2D=Texture,
        BlendMode=SimpleNamespace(BLEND_MASKED='masked'),
        load_asset={material.path:material,texture.path:texture}.get,
        Vector4=lambda x,y,z,w:SimpleNamespace(x=x,y=y,z=z,w=w),
        EditorAssetLibrary=SimpleNamespace(save_loaded_asset=lambda asset:events.append('save') or True))
    builder=SimpleNamespace(texture=lambda image,**kw:texture,
        preserve_mask_distance=lambda *args:None,
        verify_asset=lambda asset:events.append('verify'),
        protect_asset=lambda asset:events.append('protect'))
    scene={'materials':[{'id':'leaf','channels':{'opacity':{
        'op':'texture','output':'Alpha','image':{'id':'leaf','asset_name':'T_Leaf'}}}}]}
    report={'assets':{'materials':{'leaf':material.path}}}
    configure=native_namespace('ue_foliage.py',native)['configure_distance_preservation']
    result=configure(scene,report,root,[],material_builder=builder)
    assert result['textures'][0]['path']==texture.path
    assert events==['verify','save','protect']
    assert texture.properties['alpha_coverage_thresholds'].w==.33
    events.clear()
    configure(scene,report,root,[],material_builder=builder)
    assert events==['verify']
    def modified(asset):raise RuntimeError('User modified texture')
    builder.verify_asset=modified
    with pytest.raises(RuntimeError,match='User modified'):
        configure(scene,report,root,[],material_builder=builder)
    assert events==['verify']
