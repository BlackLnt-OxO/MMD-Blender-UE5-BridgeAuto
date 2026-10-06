"""Native anonymous preset fixtures: expand groups before local/channel baking."""
import json
from pathlib import Path
import sys
import time

import bpy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
from mmd_bridge.material_graph import static_bake_decision

OUT=ROOT/'work/material_fallback_regression'
OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine='CYCLES'
bpy.context.scene.cycles.device='CPU'
bpy.ops.mesh.primitive_plane_add()
obj=bpy.context.object
results=[]


def preset(name, unsupported=False, nested=False):
    tree=bpy.data.node_groups.new(name,'ShaderNodeTree')
    tree.interface.new_socket(name='Tint',in_out='INPUT',socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Unused',in_out='OUTPUT',socket_type='NodeSocketColor')
    inp=tree.nodes.new('NodeGroupInput');out=tree.nodes.new('NodeGroupOutput')
    adjust=tree.nodes.new('ShaderNodeHueSaturation')
    tree.links.new(inp.outputs['Tint'],adjust.inputs['Color'])
    if unsupported:
        checker=tree.nodes.new('ShaderNodeTexChecker')
        tree.links.new(checker.outputs['Color'],adjust.inputs['Color'])
    if nested:
        inner=tree.nodes.new('ShaderNodeGroup');inner.node_tree=preset(name+'_Inner',unsupported)
        tree.links.new(inp.outputs['Tint'],inner.inputs['Tint'])
        tree.links.new(inner.outputs['Color'],adjust.inputs['Color'])
    tree.links.new(adjust.outputs['Color'],out.inputs['Color'])
    # An unused view-dependent output must not poison the used Color output.
    view=tree.nodes.new('ShaderNodeLayerWeight')
    tree.links.new(view.outputs['Facing'],out.inputs['Unused'])
    tree.nodes.new('ShaderNodeTexNoise')
    return tree


def material(name, tree, tint=(.2,.4,.6,1)):
    mat=bpy.data.materials.new(name);mat.use_nodes=True
    shader=mat.node_tree.nodes.get('Principled BSDF')
    group=mat.node_tree.nodes.new('ShaderNodeGroup');group.node_tree=tree
    group.inputs['Tint'].default_value=tint
    mat.node_tree.links.new(group.outputs['Color'],shader.inputs['Base Color'])
    obj.data.materials.clear();obj.data.materials.append(mat)
    return mat,group


def convert(mat, name, fail_local=False, strategy='auto', enabled=True, complex_strategy='nodes', fail_surface=False):
    converter=Materials(OUT/name,'anonymous-preset-fixture',32,strategy,bake_fallback=enabled,complex_strategy=complex_strategy)
    converter.isolated_baking=True
    if fail_surface:
        from mmd_bridge.blender_materials import SurfaceConversionError
        native_analyze=converter._analyze
        def failed_surface(*args,**kwargs):
            native_analyze(*args,**kwargs)
            raise SurfaceConversionError('Fixture complete surface conversion failed')
        converter._analyze=failed_surface
    if fail_local:
        native=converter.procedural_field
        def force_channel(socket,material,obj,identity,record,channel_override=None):
            if channel_override is None:
                raise ValueError('Fixture local-field adapter unavailable')
            return native(socket,material,obj,identity,record,channel_override=channel_override)
        converter.procedural_field=force_channel
    started=time.perf_counter()
    record=converter.analyze(mat,obj,source_name=name)
    atomic_json(OUT/(name+'.json'),record)
    results.append({'case':name,'seconds':time.perf_counter()-started,
                    'baked_channels':record['baked_channels'],'stats':converter.bake_stats})
    return record


supported=preset('AnonymousSupported',nested=True)
mat,group=material('Supported',supported)
one=convert(mat,'supported')
assert not one['baked_channels'] and one.get('function_definitions')
mat,group=material('UnrelatedName',supported,tint=(.7,.1,.3,1))
two=convert(mat,'renamed_parameters')
assert not two['baked_channels']
assert {d['id'] for d in one['function_definitions']}=={d['id'] for d in two['function_definitions']}

unsupported=preset('AnonymousProcedural',unsupported=True,nested=True)
mat,group=material('Partial',unsupported)
record=convert(mat,'local_field')
assert record['baked_channels'] and all(c.startswith('field_') for c in record['baked_channels'])
assert record.get('function_definitions') and record['channels']['roughness']['op']=='constant'
assert all(f['fallback']=='conservative_local_cache' for f in record['baked_fields'])

mat,group=material('Channel',unsupported)
record=convert(mat,'channel_escalation',fail_local=True)
assert record['baked_channels']==['base_color'],record
assert record['channels']['roughness']['op']=='constant'
assert any(a['scope']=='material_channel' and a['status']=='baked' for a in record['fallback_attempts'])

mat,group=material('Strict',unsupported)
for strategy,enabled in (('nodes',True),('auto',False)):
    try:convert(mat,'strict_'+strategy,fail_local=True,strategy=strategy,enabled=enabled)
    except ValueError:pass
    else:raise AssertionError('Strict/legacy mode incorrectly escalated to channel bake')

mat,group=material('ViewDependent',supported)
shader=mat.node_tree.nodes.get('Principled BSDF')
mat.node_tree.links.new(group.outputs['Unused'],shader.inputs['Base Color'])
assert not static_bake_decision(group.outputs['Unused'])['allowed']
try:convert(mat,'reject_view',fail_local=True)
except ValueError:pass
else:raise AssertionError('View-dependent fallback must remain unsupported')

mat,group=material('InactiveBranch',supported)
nodes=mat.node_tree.nodes;links=mat.node_tree.links
mix=nodes.new('ShaderNodeMixRGB');mix.blend_type='MIX';mix.inputs[0].default_value=0
links.new(group.outputs['Color'],mix.inputs[1])
links.new(group.outputs['Unused'],mix.inputs[2])
links.new(mix.outputs[0],nodes.get('Principled BSDF').inputs['Base Color'])
record=convert(mat,'inactive_branch')
assert not record['baked_channels']
assert 'view' not in record['material_decision']['domains']
mat,group=material('DirectPBR',unsupported)
record=convert(mat,'direct_basic_pbr',complex_strategy='pbr_bake')
assert not record.get('basic_pbr_bake'),record
assert all(c.startswith('field_') for c in record['baked_channels'])
mat,group=material('FailedCompleteSurface',unsupported)
record=convert(mat,'failed_surface_pbr',complex_strategy='pbr_bake',fail_surface=True)
assert record['basic_pbr_bake']['status']=='baked',record
assert set(record['baked_channels'])=={'base_color','metallic','roughness','normal','opacity','emission'}
assert not record.get('surface_graph') and not record.get('function_definitions')
assert record['channels']['base_color']['op']=='texture'
assert all(v['op']=='texture' for v in record['channels'].values())
atomic_json(OUT/'report.json',{'complete':True,'cases':results})
print('FALLBACK_REGRESSION='+json.dumps({'complete':True,'cases':len(results)}))
