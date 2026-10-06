"""Measure baked source channel values, coverage, color space and cleanup."""
import json
from pathlib import Path
import sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json

OUT = ROOT/'work/basic_pbr_values_regression'
OUT.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine = 'CYCLES'
bpy.context.scene.cycles.device = 'CPU'
bpy.ops.mesh.primitive_plane_add()
obj = bpy.context.object
obj['_mmd_uv_pinned'] = True
obj['_mmd_geometry_identity'] = 'fixture-identical-plane-and-uv'
mat = bpy.data.materials.new('AnonymousCoverageFixture'); mat.use_nodes = True
obj.data.materials.append(mat)
nodes, links = mat.node_tree.nodes, mat.node_tree.links
a = nodes.get('Principled BSDF'); b = nodes.new('ShaderNodeBsdfPrincipled')
checker = nodes.new('ShaderNodeTexChecker')
checker.inputs['Color1'].default_value = (1,0,0,1)
checker.inputs['Color2'].default_value = (1,0,0,1)
links.new(checker.outputs['Color'], a.inputs['Base Color'])
b.inputs['Base Color'].default_value = (0,0,1,1)
for shader, rough, metal, alpha, color, strength in (
        (a,.2,0.,.2,(1,.5,0,1),2.), (b,.8,1.,.8,(0,.2,1,1),3.)):
    shader.inputs['Roughness'].default_value = rough
    shader.inputs['Metallic'].default_value = metal
    shader.inputs['Alpha'].default_value = alpha
    shader.inputs['Emission Color'].default_value = color
    shader.inputs['Emission Strength'].default_value = strength
mix = nodes.new('ShaderNodeMixShader'); mix.inputs[0].default_value = .75
links.new(a.outputs[0], mix.inputs[1]); links.new(b.outputs[0], mix.inputs[2])
output = mat.node_tree.get_output_node('CYCLES')
links.new(mix.outputs[0], output.inputs['Surface'])
before_nodes = len(nodes)
converter = Materials(OUT, 'anonymous-coverage-fixture', 32, 'auto',
                      bake_fallback=True, complex_strategy='pbr_bake')
converter.isolated_baking = True
def failed_closure(*args, **kwargs):
    raise ValueError('Fixture complete shader conversion failed')
converter.closure = failed_closure
converter.restore_scene_appearance = True
record = converter.analyze(mat, obj, source_name='AnonymousCoverageFixture')
atomic_json(OUT/'record.json',record)
assert record['basic_pbr_bake']['status'] == 'baked'
# The temporary extraction graph and all links to the source shader must be restored.
assert len(nodes) == before_nodes
assert output.inputs['Surface'].links[0].from_node == mix
coverage = .25*.2 + .75*.8
weight = .75*.8/coverage
expected = {'base_color':[1-weight,0,weight], 'roughness':[.2*(1-weight)+.8*weight]*3,
            'metallic':[weight]*3, 'opacity':[coverage]*3,
            'emission':[2*(1-weight),(1-weight)+.6*weight,3*weight], 'normal':[.5,.5,1]}
measured = {}
for channel, expression in record['channels'].items():
    assert expression['op'] == 'texture', (channel, expression)
    if expression['op']=='constant':
        value = np.asarray(expression['value']).reshape(-1)
        if value.size==1: value=np.repeat(value,3)
        assert np.allclose(value[:3],expected[channel],atol=1e-5), (channel,value,expected[channel])
        measured[channel] = {'expected':expected[channel], 'measured':value[:3].tolist(), 'representation':'constant'}
        continue
    info = expression['image']
    image = bpy.data.images.load(info['path'], check_existing=False)
    image.colorspace_settings.name = info['color_space']
    pixels = np.asarray(image.pixels[:], dtype=np.float32).reshape(image.size[1], image.size[0], 4)
    value = np.median(pixels[12:20,12:20,:3], axis=(0,1))
    if info['color_space']=='sRGB':
        value = np.where(value<=.04045, value/12.92, ((value+.055)/1.055)**2.4)
    tolerance = .025 if channel != 'emission' else .01
    assert np.allclose(value, expected[channel], atol=tolerance), (channel,value,expected[channel],info)
    measured[channel] = {'expected':expected[channel], 'measured':value.tolist(),
                         'color_space':info['color_space'], 'path':info['path']}
    bpy.data.images.remove(image)
clone = obj.copy(); clone.data=obj.data.copy()
clone.location.x = 5.
clone_mat=mat.copy();clone.data.materials.clear();clone.data.materials.append(clone_mat)
reused = converter.analyze(clone_mat,clone,source_name='AnonymousCoverageFixture')
assert reused['channels']['base_color']['image']['path']==record['channels']['base_color']['image']['path']
assert converter.bake_stats['reused']>=1
uv_changed=obj.copy();uv_changed.data=obj.data.copy()
uv_mat=mat.copy();uv_changed.data.materials.clear();uv_changed.data.materials.append(uv_mat)
for item in uv_changed.data.uv_layers['MMD_BakeUV'].data:item.uv=item.uv*.5
uv_record=converter.analyze(uv_mat,uv_changed,source_name='AnonymousCoverageFixture')
assert uv_record['channels']['base_color']['image']['path']!=record['channels']['base_color']['image']['path']
geo = clone_mat.node_tree.nodes.new('ShaderNodeNewGeometry')
clone_mat.node_tree.links.new(geo.outputs['Position'],clone_mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
world_one=converter.analyze(clone_mat,clone,source_name='WorldDependentFixture')
other=clone.copy();other.data=clone.data.copy();other.location.x=10.
other_mat=clone_mat.copy();other.data.materials.clear();other.data.materials.append(other_mat)
world_two=converter.analyze(other_mat,other,source_name='WorldDependentFixture')
assert world_one['channels']['base_color']['image']['path']!=world_two['channels']['base_color']['image']['path']
atomic_json(OUT/'report.json', {'complete':True, 'channels':measured,
                              'source_shader_restored':True, 'compatible_texture_reused':True,
                              'world_dependency_isolated':True, 'changed_uv_isolated':True,
                              'record':record,'bake_stats':converter.bake_stats})
print('BASIC_PBR_VALUES='+json.dumps({'complete':True,'channels':len(measured)}))
