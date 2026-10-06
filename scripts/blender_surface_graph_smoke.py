"""Native closure/mask checks; run in Blender --background --factory-startup."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json

out = ROOT/'work/surface_graph_smoke'
out.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=2)
obj = bpy.context.object
mat = bpy.data.materials.new('Arbitrary A93')
mat.use_nodes = True
obj.data.materials.append(mat)
tree = mat.node_tree
tree.nodes.clear()
output = tree.nodes.new('ShaderNodeOutputMaterial')
base = tree.nodes.new('ShaderNodeBsdfPrincipled')
reflection = tree.nodes.new('ShaderNodeBsdfGlossy')
mix = tree.nodes.new('ShaderNodeMixShader')
noise = tree.nodes.new('ShaderNodeTexNoise')
noise.inputs['Scale'].default_value=4
fresnel = tree.nodes.new('ShaderNodeFresnel')
factor = tree.nodes.new('ShaderNodeMath')
factor.operation='MULTIPLY'
for source,target in ((noise.outputs['Fac'],reflection.inputs['Roughness']),
    (noise.outputs['Fac'],factor.inputs[0]),(fresnel.outputs[0],factor.inputs[1]),
    (factor.outputs[0],mix.inputs[0]),(base.outputs[0],mix.inputs[1]),
    (reflection.outputs[0],mix.inputs[2]),(mix.outputs[0],output.inputs['Surface'])):
    tree.links.new(source,target)
source = out/'source.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(source))
converter = Materials(out,str(source),256)
converter.restore_scene_appearance=True
record = converter.analyze(mat,obj)
assert record['surface_graph']['b']['kind']=='bsdf_glossy'
assert record['surface_graph']['factor']['b']['op']=='fresnel'
assert len(record['baked_channels'])==1 and len(obj.data.uv_layers)==2
assert record['surface_graph']['b']['channels']['roughness']['uv']['index']==1
assert not record.get('special_masks')
rgb=tree.nodes.new('ShaderNodeRGB')
rgb.outputs[0].default_value=(.2,.7,.4,1)
tree.links.new(rgb.outputs[0],factor.inputs[0])
scalar=converter.expr(factor.inputs[0])
assert scalar['op']=='to_scalar' and scalar['weights']==[.2126,.7152,.0722]
tree.links.new(noise.outputs['Fac'],factor.inputs[0])
# Native fields must contain a spatially varying result.
image = bpy.data.images.load(record['surface_graph']['b']['channels']['roughness']['image']['path'])
assert max(image.pixels[0::4])-min(image.pixels[0::4])>.1
record['asset_name']='M_ClosureSmoke_Mix'
records=[record]
# Add closures and group/reroute outputs use the same tree traversal.
group = bpy.data.node_groups.new('Anonymous B71','ShaderNodeTree')
group.interface.new_socket(name='Output',in_out='OUTPUT',socket_type='NodeSocketShader')
group_output = group.nodes.new('NodeGroupOutput')
diffuse = group.nodes.new('ShaderNodeBsdfDiffuse')
emission = group.nodes.new('ShaderNodeEmission')
emission.inputs['Color'].default_value=(2,.4,.1,1)
add = group.nodes.new('ShaderNodeAddShader')
for a,b in ((diffuse.outputs[0],add.inputs[0]),(emission.outputs[0],add.inputs[1]),(add.outputs[0],group_output.inputs[0])):
    group.links.new(a,b)
group_node = tree.nodes.new('ShaderNodeGroup')
group_node.node_tree=group
reroute = tree.nodes.new('NodeReroute')
tree.links.new(group_node.outputs[0],reroute.inputs[0])
tree.links.new(reroute.outputs[0],output.inputs['Surface'])
converter.group_inputs={}
converter.group_stack=[]
graph=converter.closure(output.inputs['Surface'])
assert graph['kind']=='add' and graph['b']['channels']['emission']['value'][0]==2
records.append({'id':'closure-add','name':'Anonymous B71','asset_name':'M_ClosureSmoke_Add',
    'surface':'pbr','strategy':'nodes','channels':graph['a']['channels'],'surface_graph':graph})
# Noise inside a shader group is baked through a temporary native group output.
inner_noise=group.nodes.new('ShaderNodeTexNoise')
group.links.new(inner_noise.outputs['Fac'],diffuse.inputs['Roughness'])
converter.field_cache={}
scratch={'baked_channels':[]}
converter.field_bake=lambda socket:converter.procedural_field(socket,mat,obj,'group-check',scratch)
graph=converter.closure(output.inputs['Surface'])
assert graph['a']['channels']['roughness']['op']=='texture' and len(scratch['baked_channels'])==1
assert len(group.interface.items_tree)==1  # Temporary outputs are removed.
converter.field_bake=None
# Unsupported leaf closures report a loss rather than a successful approximation.
toon=tree.nodes.new('ShaderNodeBsdfToon')
tree.links.new(toon.outputs[0],mix.inputs[2])
tree.links.new(mix.outputs[0],output.inputs['Surface'])
try:
    converter.closure(output.inputs['Surface'])
    raise AssertionError('Unknown closure accepted')
except ValueError as exc:
    assert 'ShaderNodeBsdfToon' in str(exc)
atomic_json(out/'records.json',records)
# Scene display settings must not alter scalar fields or clip HDR bake values.
import numpy as np
import OpenImageIO as oiio
bpy.context.scene.view_settings.view_transform='Filmic'
bpy.context.scene.view_settings.exposure=.5
value_node=tree.nodes.new('ShaderNodeValue')
for strength in (.18,4.):
    value_node.outputs[0].default_value=strength
    baked=converter.bake(obj,mat,None,'field_linear_'+str(strength),
                         'linear-check-'+str(strength),from_output=value_node.outputs[0])
    assert Path(baked['image']['path']).read_bytes()[:4]==b'\x76\x2f\x31\x01'
    inp=oiio.ImageInput.open(baked['image']['path'])
    pixels=inp.read_image(format=oiio.FLOAT)
    inp.close()
    assert np.allclose(pixels[pixels[:,:,3]>.9,:3],strength,atol=1e-5),(strength,pixels[128,128].tolist())
print('SURFACE_GRAPH_NATIVE_PASS',flush=True)
