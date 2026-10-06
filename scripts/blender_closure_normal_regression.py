"""Each mixed closure must bake its own normal, compared with native Cycles."""
import sys
from pathlib import Path
import bpy, numpy as np, OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/material_restoration/closure_normals';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.mesh.primitive_plane_add()
obj=bpy.context.object
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=1
mat=bpy.data.materials.new('TwoNormals');mat.use_nodes=True;obj.data.materials.append(mat)
tree=mat.node_tree;output=tree.get_output_node('CYCLES');a=tree.nodes.get('Principled BSDF')
b=tree.nodes.new('ShaderNodeBsdfPrincipled');mix=tree.nodes.new('ShaderNodeMixShader');mix.inputs[0].default_value=.5
uv=tree.nodes.new('ShaderNodeTexCoord');split=tree.nodes.new('ShaderNodeSeparateXYZ')
tree.links.new(uv.outputs['UV'],split.inputs[0])
for index,shader in enumerate((a,b)):
    bump=tree.nodes.new('ShaderNodeBump');bump.inputs['Distance'].default_value=.4
    tree.links.new(split.outputs[index],bump.inputs['Height']);tree.links.new(bump.outputs[0],shader.inputs['Normal'])
    tree.links.new(shader.outputs[0],mix.inputs[index+1])
converter=Materials(OUT,'normal-closure-fixture',64);converter.isolated_baking=True;converter.restore_scene_appearance=True
converter.surface_policy={}
def pixels(expression):
    image=oiio.ImageInput.open(expression['image']['path']);values=image.read_image(format=oiio.FLOAT);image.close();return values
references=[]
for index,shader in enumerate((a,b)):
    tree.links.new(shader.outputs[0],output.inputs['Surface'])
    references.append(pixels(converter.bake(obj,mat,None,'normal','reference-'+str(index))))
tree.links.new(mix.outputs[0],output.inputs['Surface'])
record=converter.analyze(mat,obj)
assert record.get('surface_graph')
actual=[pixels(record['surface_graph'][key]['channels']['normal']) for key in ('a','b')]
errors=[float(np.max(np.abs(reference-result))) for reference,result in zip(references,actual)]
assert max(errors)<=1/255+1e-6,errors
assert float(np.max(np.abs(actual[0]-actual[1])))>.05,'distinct branch normals collapsed'
atomic_json(OUT/'result.json',dict(complete=True,errors=errors,material=record))
print('CLOSURE_NORMAL_PASS',errors,flush=True)
