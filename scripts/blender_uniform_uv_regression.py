"""Uniform sampling must not remap original albedo or opacity UV sockets."""
import sys
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
out=ROOT/'work/uniform_uv_regression';out.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.mesh.primitive_plane_add(size=2)
obj=bpy.context.object;mat=bpy.data.materials.new('Anonymous UV preservation');mat.use_nodes=True
obj.data.materials.append(mat);tree=mat.node_tree;p=tree.nodes.get('Principled BSDF')
image=bpy.data.images.new('Anonymous source image',4,4,alpha=True)
image.pixels=[v for i in range(16) for v in (.2,.6,.1,.2 if i%2 else .8)]
tex=tree.nodes.new('ShaderNodeTexImage');tex.image=image
hsv=tree.nodes.new('ShaderNodeHueSaturation');random=tree.nodes.new('ShaderNodeObjectInfo')
tree.links.new(tex.outputs['Color'],hsv.inputs['Color']);tree.links.new(random.outputs['Random'],hsv.inputs['Hue'])
tree.links.new(hsv.outputs['Color'],p.inputs['Base Color']);tree.links.new(tex.outputs['Alpha'],p.inputs['Alpha'])
converter=Materials(out,str(Path(__file__).resolve()),resolution=32);converter.isolated_baking=True
record=converter.analyze(mat,obj=obj)
opacity=record['channels']['opacity'];assert opacity['op']=='texture' and opacity['output']=='Alpha',opacity
assert opacity['uv']['op']=='uv' and opacity['uv']['index']==0,opacity
assert all(n.uv_map in obj.data.uv_layers for n in tree.nodes if n.type=='UVMAP')
assert not any(f['node_type']=='ShaderNodeUVMap' for f in record.get('baked_fields',[])),record
atomic_json(out/'result.json',{'complete':True,'version':bpy.app.version_string,'opacity_uv':opacity['uv']})
print('UNIFORM_UV_PRESERVATION_PASS',flush=True)
