"""Two failed slots share one mesh; added UV channels must not collide."""
import sys,time
from pathlib import Path
import bpy
import numpy as np
from pxr import Usd,UsdGeom,UsdShade,Sdf,Vt
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,atomic_json,read_json,file_hash
from mmd_bridge.material_repair import prepare
from mmd_bridge.blender_material_repair import export
OUT=ROOT/'work/material_restoration/repair_uv_allocation_v1'/str(time.time_ns());OUT.mkdir(parents=True)
bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene;scene.render.engine='CYCLES'
bpy.ops.mesh.primitive_cube_add();obj=bpy.context.object;obj.name='Shared';mesh=obj.data
second=mesh.uv_layers.new(name='OtherUV');first=mesh.uv_layers[0]
uv0=np.array([tuple(v.uv) for v in first.data],dtype=np.float32);uv1=uv0*2
second.data.foreach_set('uv',uv1.reshape(-1))
image=bpy.data.images.new('Grid',width=2,height=2);image.pixels.foreach_set([.1,.2,.3,1]*4);image.pack()
for name,uv_name in [('A',first.name),('B',second.name)]:
    material=bpy.data.materials.new(name);material.use_nodes=True
    tex=material.node_tree.nodes.new('ShaderNodeTexImage');tex.image=image
    uv=material.node_tree.nodes.new('ShaderNodeUVMap');uv.uv_map=uv_name
    material.node_tree.links.new(uv.outputs[0],tex.inputs['Vector'])
    material.node_tree.links.new(tex.outputs['Color'],material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
    mesh.materials.append(material)
for polygon in mesh.polygons:polygon.material_index=polygon.index%2
source=OUT/'Fixture.blend';bpy.ops.wm.save_as_mainfile(filepath=str(source));source_hash=file_hash(source)
root=OUT/'Scene.usda';geometry=OUT/'Geometry.usda';looks=OUT/'ToolMaterials.usda'
stage=Usd.Stage.CreateNew(str(geometry));UsdGeom.Xform.Define(stage,'/World');UsdGeom.Xform.Define(stage,'/World/Objects/Shared')
prim=UsdGeom.Mesh.Define(stage,'/World/Objects/Shared/Mesh')
prim.CreatePointsAttr([tuple(v.co) for v in mesh.vertices]);prim.CreateFaceVertexCountsAttr([p.loop_total for p in mesh.polygons]);prim.CreateFaceVertexIndicesAttr([l.vertex_index for l in mesh.loops])
var=UsdGeom.PrimvarsAPI(prim).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.faceVarying)
var.Set(Vt.Vec2fArray.FromNumpy(uv0+.125));var.GetAttr().SetCustomDataByKey('MMDBridge:sourceName',first.name)
for name in ('A','B'):
    subset=UsdGeom.Subset.Define(stage,prim.GetPath().AppendChild('Slot'+name))
    subset.CreateElementTypeAttr(UsdGeom.Tokens.face);subset.CreateFamilyNameAttr('materialBind')
    subset.CreateIndicesAttr([p.index for p in mesh.polygons if p.material_index==('AB'.index(name))])
    subset.GetPrim().ApplyAPI(UsdShade.MaterialBindingAPI)
    UsdShade.MaterialBindingAPI(subset.GetPrim()).GetDirectBindingRel().SetTargets(['/World/Materials/M_'+name])
stage.GetRootLayer().Save()
stage=Usd.Stage.CreateNew(str(looks));UsdGeom.Xform.Define(stage,'/World')
for name in ('A','B'):
    material=UsdShade.Material.Define(stage,'/World/Materials/M_'+name)
    shader=UsdShade.Shader.Define(stage,material.GetPath().AppendChild('UnrealShader'))
    shader.SetSourceAsset(Sdf.AssetPath('/Game/Fixture/Bad'+name+'.Bad'+name),'unreal')
    material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out',Sdf.ValueTypeNames.Token))
stage.GetRootLayer().Save()
root.write_text('#usda 1.0\n( subLayers = [@Geometry.usda@, @ToolMaterials.usda@] )',encoding='utf-8');root_hash=file_hash(root)
job=Job(OUT/'Job');identity=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
atomic_json(job.path,{'settings':{'scene':str(source),'scene_frame':1,'scene_scale':1,'bake_resolution':128,'scene_format':'usd_stage'},'inputs':{'scene':source_hash},'asset_id':'Fixture'})
atomic_json(job.folder/'manifest.json',{'scene':{'route':'usd_stage','materials':[
    {'id':name,'name':name,'source_name':name,'channels':{},'material_error':'Controlled fixture fault'} for name in ('A','B')],
    'prototypes':[{'id':'P','prim_path':'/World/Objects/Shared','material_slots':['A','B']}],
    'instances':[{'name':'Shared','source_name':'Shared','prototype_id':'P','prim_path':'/World/Objects/Shared','matrix':identity,'source_matrix':identity}]}})
atomic_json(job.folder/'ue_report.json',{'root':'/Game/Fixture','assets':{'level':'/Game/Fixture/Maps/Preview','materials':{name:'/Game/Fixture/Bad'+name+'.Bad'+name for name in ('A','B')}},'checks':{'usd_stage':{'root_layer':str(root)}},'failures':[]})
folder=prepare(job);export(job,folder);result=read_json(folder/'export.json')
assert not result['failures'],result['failures']
assert result['layers']['A']['added_uv_channels']==[1]
assert result['layers']['B']['added_uv_channels']==[2]
combined=Usd.Stage.Open(str(root));combined.GetSessionLayer().subLayerPaths=[v['path'] for v in result['layers'].values()]
variables=UsdGeom.PrimvarsAPI(combined.GetPrimAtPath('/World/Objects/Shared/Mesh'))
assert np.allclose(np.asarray(variables.GetPrimvar('st').ComputeFlattened()),uv0+.125)
assert np.allclose(np.asarray(variables.GetPrimvar('st1').ComputeFlattened()),uv0)
assert np.allclose(np.asarray(variables.GetPrimvar('st2').ComputeFlattened()),uv1)
assert file_hash(source)==source_hash and file_hash(root)==root_hash
atomic_json(OUT.parent/'result.json',{'complete':True,'output':str(OUT),'shared_mesh_added_channels':[1,2],
    'original_uv_unchanged':True,'original_root_unchanged':True,'source_unchanged':True})
print('REPAIR_SHARED_UV_PASS',flush=True)
