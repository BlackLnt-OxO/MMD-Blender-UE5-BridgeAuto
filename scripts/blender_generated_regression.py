"""Cycles Generated coordinates versus preserved mesh carriers, negative transforms included."""
import sys
from pathlib import Path
import bpy,numpy as np,OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json
from mmd_bridge.scene_shader_data import ensure_generated_layers,pin_generated
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_usd import _mesh_data,_write_chunk
OUT=ROOT/'work/material_functions_implementation/generated';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.mesh.primitive_cube_add(size=3)
obj=bpy.context.object;obj.location=(30,-20,7);obj.scale=(-1.3,.7,2.1)
material=bpy.data.materials.new('ArbitraryGenerated');material.use_nodes=True;obj.data.materials.append(material)
tree=material.node_tree;p=next(n for n in tree.nodes if n.type=='BSDF_PRINCIPLED');coordinates=tree.nodes.new('ShaderNodeTexCoord')
tree.links.new(coordinates.outputs['Generated'],p.inputs['Base Color'])
converter=Materials(OUT,'generated-v1',resolution=64);converter.surface_policy={'category':'small'};converter.isolated_baking=True
def pixels(expr):
    image=oiio.ImageInput.open(expr['image']['path']);a=image.read_image(format=oiio.FLOAT);image.close();return a
reference=pixels(converter.bake(obj,material,p.inputs['Base Color'],'field_reference','native-v1'))
indices=ensure_generated_layers(obj,obj.data);pin_generated(tree,obj.data)
actual=pixels(converter.bake(obj,material,p.inputs['Base Color'],'field_adapter','carrier-v1'))
error=float(np.max(np.abs(reference-actual)));assert error<1e-5,error
converter.restore_scene_appearance=True
record=converter.analyze(material,obj);assert not record['baked_channels'],record['baked_channels']
group={'id':'generated','geometry':_mesh_data(obj.data),'materials':[record['id']],
       'polygon_materials':[0]*len(obj.data.polygons),'collision':False,'instances':[]}
_write_chunk(OUT/'mesh.usdc',0,[group],1,False,'/Game/MMDBridgeValidation/FunctionsV3','fixture',definitions_only=True,
             material_names={record['id']:record['asset_name']})
from pxr import Usd,UsdGeom
stage=Usd.Stage.CreateNew(str(OUT/'scene.usda'));UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.SetStageMetersPerUnit(stage,1.)
root=UsdGeom.Xform.Define(stage,'/World/Generated');root.GetPrim().GetReferences().AddReference('mesh.usdc',group['prim_path']);stage.GetRootLayer().Save()
atomic_json(OUT/'result.json',dict(complete=True,max_error=error,indices=indices,
    matrix=[list(r) for r in obj.matrix_world],material=record,texspace_center=list(obj.data.texspace_location),
    texspace_size=list(obj.data.texspace_size)))
print('GENERATED_NATIVE_PASS',error,indices,flush=True)
