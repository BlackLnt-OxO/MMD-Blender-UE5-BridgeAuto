"""Compare the Cycles kernel's actual random outputs with exported data."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np,OpenImageIO as oiio
from mmd_bridge.scene_shader_data import object_random,island_random
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
out=ROOT/'work/material_functions_implementation/shader_data';out.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh=bpy.data.meshes.new('TwoIslands')
mesh.from_pydata([(x,y,0) for base in (0,3) for x,y in ((base,0),(base+1,0),(base+1,1),(base,1))],[],[(0,1,2,3),(4,5,6,7)])
obj=bpy.data.objects.new('RandomValidation',mesh);bpy.context.scene.collection.objects.link(obj)
uv=mesh.uv_layers.new(name='MMD_BakeUV')
for polygon,x0,x1 in zip(mesh.polygons,(.05,.55),(.45,.95)):
    for index,point in zip(polygon.loop_indices,((x0,.05),(x1,.05),(x1,.95),(x0,.95))):uv.data[index].uv=point
material=bpy.data.materials.new('RandomField');material.use_nodes=True;mesh.materials.append(material)
tree=material.node_tree;geo=tree.nodes.new('ShaderNodeNewGeometry');info=tree.nodes.new('ShaderNodeObjectInfo')
bpy.context.scene.render.engine='CYCLES';bpy.context.scene.cycles.device='CPU'
converter=Materials(out,'random-compat-v1',resolution=32);converter.surface_policy={'category':'small'};converter.isolated_baking=True
checks=[]
for semantic,output,expected in (
    ('object',info.outputs['Random'],[object_random(obj.name)]),
    ('island',geo.outputs['Random Per Island'],island_random(len(mesh.vertices),[e.vertices[:] for e in mesh.edges],[0,4]))):
    expr=converter.bake(obj,material,None,'field_'+semantic,'random_'+semantic,from_output=output)
    image=oiio.ImageInput.open(expr['image']['path']);pixels=image.read_image(format=oiio.FLOAT);image.close()
    values=[float(pixels[16,x,0]) for x in (8,24)]
    target=expected*2 if semantic=='object' else expected
    error=max(abs(v-e) for v,e in zip(values,target))
    assert error<1e-6,(semantic,values,expected,error)
    checks.append({'semantic':semantic,'expected':target,'actual':values,'samples':[[8,16],[24,16]],'max_error':error})
atomic_json(out/'result.json',{'complete':True,'blender':bpy.app.version_string,'checks':checks})
print('SHADER_DATA_NATIVE_PASS',checks,flush=True)
