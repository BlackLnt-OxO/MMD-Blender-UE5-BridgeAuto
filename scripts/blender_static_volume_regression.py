"""Native static fog fixtures: homogeneous, procedural, scaled, VDB and surface+volume."""
import sys
from pathlib import Path
import bpy
from mathutils import Matrix
import openvdb as vdb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job, atomic_json, file_hash
from mmd_bridge.blender_appearance import bake_volume, uniform_volume
OUT=ROOT/'work/material_restoration/static_volume_v1';OUT.mkdir(parents=True,exist_ok=True)
(OUT/'Volumes').mkdir(exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene;scene.render.engine='CYCLES'
world=bpy.data.worlds.new('UniformWorldFog');world.use_nodes=True;scene.world=world
volume=world.node_tree.nodes.new('ShaderNodeVolumeScatter');volume.inputs['Density'].default_value=.01
world.node_tree.links.new(volume.outputs[0],world.node_tree.nodes.get('World Output').inputs['Volume'])
assert uniform_volume(world.node_tree.nodes.get('World Output').inputs['Volume'])['extinction']>0
def material(name,procedural=False,surface=False):
    mat=bpy.data.materials.new(name);mat.use_nodes=True;nodes=mat.node_tree.nodes;links=mat.node_tree.links
    output=nodes.get('Material Output');scatter=nodes.new('ShaderNodeVolumeScatter');scatter.inputs['Density'].default_value=.02
    scatter.inputs['Color'].default_value=(.6,.8,1,1);scatter.inputs['Anisotropy'].default_value=.2
    links.new(scatter.outputs[0],output.inputs['Volume'])
    if not surface: links.remove(output.inputs['Surface'].links[0])
    if procedural:
        tex=nodes.new('ShaderNodeTexNoise');tex.inputs['Scale'].default_value=3
        position=nodes.new('ShaderNodeNewGeometry');links.new(position.outputs['Position'],tex.inputs['Vector'])
        multiply=nodes.new('ShaderNodeMath');multiply.operation='MULTIPLY';multiply.inputs[1].default_value=.05
        links.new(tex.outputs['Fac'],multiply.inputs[0]);links.new(multiply.outputs[0],scatter.inputs['Density'])
    return mat,output.inputs['Volume']
local,socket=material('ProceduralFog',True,True)
bpy.ops.mesh.primitive_cube_add(size=2);obj=bpy.context.object;obj.name='SurfaceAndFog';obj.data.materials.append(local)
obj.scale=(2,1,.5)
source=OUT/'Fixture.blend';bpy.ops.wm.save_as_mainfile(filepath=str(source))
before=file_hash(source)
job=Job(OUT/'job');atomic_json(job.path,{'settings':{'scene':str(source),'material_workflow':'legacy_main_v1','scene_import_profile':'render_optimized'}})
record={'id':'noise','name':'ScaledNoiseFog','kind':'mesh_grid','bounds':[[-1,-1,-1],[1,1,1]],'matrix':[list(r) for r in Matrix.Diagonal((2,1,.5,1))]}
bake_volume(record,socket,obj.data,OUT/'Volumes',job,24,1.)
assert record['status']=='converted' and record['coefficient_units']=='per_voxel'
grids=vdb.readAll(record['path']);grids=grids[0] if isinstance(grids,tuple) else grids
assert any(g.activeVoxelCount()>0 for g in grids)
bad=obj.data.copy();bad.polygons[0].select=True
import bmesh
bm=bmesh.new();bm.from_mesh(bad);bm.faces.ensure_lookup_table();bmesh.ops.delete(bm,geom=[bm.faces[0]],context='FACES');bm.to_mesh(bad);bm.free()
try:
    bake_volume(dict(record,id='open',name='OpenFog',status='pending'),socket,bad,OUT/'Volumes',job,16,1.)
except ValueError as exc: assert '闭合' in str(exc)
else: raise AssertionError('open volume was incorrectly filled')
grid=vdb.FloatGrid();grid.name='density';accessor=grid.getAccessor()
for x in range(5):
    for y in range(5):
        for z in range(5):accessor.setValueOn((x,y,z),.02)
vdb_path=OUT/'Density.vdb';vdb.write(str(vdb_path),grids=[grid])
vmat=bpy.data.materials.new('StaticVDBFog');vmat.use_nodes=True
tree=vmat.node_tree;tree.nodes.clear();out=tree.nodes.new('ShaderNodeOutputMaterial');pv=tree.nodes.new('ShaderNodeVolumePrincipled');tree.links.new(pv.outputs[0],out.inputs['Volume'])
data=bpy.data.volumes.new('Density');data.filepath=str(vdb_path);vobj=bpy.data.objects.new('StaticVDB',data);scene.collection.objects.link(vobj);data.materials.append(vmat)
from mmd_bridge.blender_appearance import freeze_vdb
vrecord,vsocket,_=freeze_vdb(vobj,vobj.matrix_world,{'scene':str(source),'source_renderer':'CYCLES'},0)
bake_volume(vrecord,vsocket,None,OUT/'Volumes',job,16,1.)
assert vrecord['status']=='converted'
assert file_hash(source)==before
atomic_json(OUT/'result.json',{'complete':True,'homogeneous_world':True,'scaled_procedural':record,'static_vdb':vrecord,'open_container_rejected':True,'surface_volume_connected':True,'source_unchanged':True})
print('STATIC_VOLUME_PASS',flush=True)
