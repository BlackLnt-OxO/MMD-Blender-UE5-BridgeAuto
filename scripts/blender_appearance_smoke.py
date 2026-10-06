"""Run with Blender 5.2 --background --python this_file. Native checks, no mocks."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import bpy
import numpy as np
import openvdb
from mmd_bridge import blender_appearance as a
from mmd_bridge.core import atomic_json

OUT = ROOT/'work/scene_appearance_smoke'
OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8)
obj = bpy.context.object
material = bpy.data.materials.new('Procedural volume')
material.use_nodes = True
tree = material.node_tree
tree.nodes.clear()
out = tree.nodes.new('ShaderNodeOutputMaterial')
volume = tree.nodes.new('ShaderNodeVolumeScatter')
volume.inputs['Density'].default_value=.02
volume.inputs['Color'].default_value=(.7,.8,.9,1)
tree.links.new(volume.outputs[0],out.inputs['Volume'])
obj.data.materials.append(material)
assert a.material_channels(material,'CYCLES')[0] is False
assert a.uniform_volume(out.inputs['Volume']) is None  # Colored scattering needs RGB extinction.
noise = tree.nodes.new('ShaderNodeTexNoise')
noise.inputs['Scale'].default_value=3
tree.links.new(noise.outputs['Fac'],volume.inputs['Density'])
from mathutils import Vector
bpy.ops.mesh.primitive_plane_add(size=20,location=(0,0,-1.2))
floor=bpy.context.object
floor.name='Surface floor'
floor_mat=bpy.data.materials.new('Floor')
floor_mat.use_nodes=True
floor.data.materials.append(floor_mat)
camera_data=bpy.data.cameras.new('Reference')
camera=bpy.data.objects.new('Reference',camera_data)
scene.collection.objects.link(camera)
camera.location=(4,-6,3)
camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler()
scene.camera=camera
scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage=640,360,100
light_data=bpy.data.lights.new('Reference point','POINT')
light_data.energy=100
light=bpy.data.objects.new(light_data.name,light_data)
scene.collection.objects.link(light)
light.location=(0,0,2)
# Native extraction must retain HDR shader energy and transformed rectangle dimensions.
area_data=bpy.data.lights.new('Extraction check','AREA')
area_data.shape='RECTANGLE'
area_data.size,area_data.size_y=2,4
area_data.energy,area_data.exposure=100,1
area_data.use_nodes=True
emitter=next(n for n in area_data.node_tree.nodes if n.type=='EMISSION')
emitter.inputs['Color'].default_value=(2,.5,.25,1)
emitter.inputs['Strength'].default_value=3
area=bpy.data.objects.new(area_data.name,area_data)
scene.collection.objects.link(area)
area.scale=(2,3,1)
bpy.context.view_layer.update()
record=a.light_record(area,'CYCLES',.01)
assert record['energy']==600 and record['color']==[2,.5,.25]
assert abs(record['size']-.04)<1e-6 and abs(record['size_y']-.12)<1e-6
area_data.shape='DISK'
record=a.light_record(area,'CYCLES',1)
assert record['size']==4 and record['size_y']==6 and record['needs_review']
bpy.data.objects.remove(area,do_unlink=True)
bpy.data.lights.remove(area_data)
from mmd_bridge.blender_materials import Materials
glass_test=bpy.data.materials.new('Glass mask extraction check')
glass_test.use_nodes=True
ga,gb=(glass_test.node_tree.nodes.new('ShaderNodeBsdfGlass') for _ in range(2))
ga.inputs['Roughness'].default_value=.06
gb.inputs['Roughness'].default_value=.5
mix=glass_test.node_tree.nodes.new('ShaderNodeMixShader')
glass_test.node_tree.links.new(ga.outputs[0],mix.inputs[1])
glass_test.node_tree.links.new(gb.outputs[0],mix.inputs[2])
extractor=Materials(OUT,'native glass smoke')
channels=extractor.glass_mix(mix)
assert channels and abs(channels['roughness']['a']['value']-.06)<1e-6
assert channels['roughness']['b']['value']==.5
gb.inputs['IOR'].default_value=2
assert extractor.glass_mix(mix) is None
bpy.data.materials.remove(glass_test)
world=bpy.data.worlds.new('Reference world')
world.use_nodes=True
scene.world=world
world.node_tree.nodes['Background'].inputs['Color'].default_value=(.03,.05,.08,1)
world_volume=world.node_tree.nodes.new('ShaderNodeVolumePrincipled')
world_volume.inputs['Density'].default_value=.002
world.node_tree.links.new(world_volume.outputs[0],world.node_tree.nodes['World Output'].inputs['Volume'])
compositor=bpy.data.node_groups.new('Reference compositor','CompositorNodeTree')
compositor.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
scene.compositing_node_group=compositor
render=compositor.nodes.new('CompositorNodeRLayers')
correction=compositor.nodes.new('CompositorNodeColorCorrection')
correction.inputs['Saturation'].default_value=1.1
result=compositor.nodes.new('NodeGroupOutput')
compositor.links.new(render.outputs['Image'],correction.inputs['Image'])
compositor.links.new(correction.outputs['Image'],result.inputs['Image'])
source = OUT/'fixture.blend'
floor_name=floor.name
child=bpy.data.objects.new('Mesh',floor.data)
scene.collection.objects.link(child)
child.parent=floor
child.location=(1,0,1)
child.scale=(.1,.1,.1)
bpy.ops.wm.save_as_mainfile(filepath=str(source))

class Job:
    folder=OUT
    data={'settings':{'scene':str(source)}}
    def status(self,*args,**kwargs):
        print('APPEARANCE',*args,flush=True)

record={'id':'native_noise','name':obj.name,'kind':'local_grid','bounds':[[-1,-1,-1],[1,1,1]],
        'matrix':[list(r) for r in obj.matrix_world]}
a.bake_volume(record,out.inputs['Volume'],obj.data,OUT,Job(),24,1)
grid = openvdb.read(record['path'],'extinction_r')
values=np.empty((24,24,24),dtype='float32')
grid.copyToArray(values)
assert values.max()>0 and values.min()==0
assert values[0,0,0]==0 and values[12,12,12]>0
assert np.std(values[values>0])>0
assert np.allclose(grid.transform.indexToWorld((0,0,0)),(-1+1/24,)*3)
first=record['path']
assert a.light_record(light,'CYCLES',1)['energy']==100
# Same metadata and input reuses the cache without sampling.
record2={'id':'native_noise','name':obj.name,'kind':'local_grid','bounds':[[-1,-1,-1],[1,1,1]],
         'matrix':[list(r) for r in obj.matrix_world]}
a.bake_volume(record2,out.inputs['Volume'],obj.data,OUT,Job(),24,1)
assert record2['cache_hit']
# Object scale is baked into an isotropic lattice; integrated density is resolution-independent.
from mathutils import Matrix
for link in list(volume.inputs['Density'].links):
    tree.links.remove(link)
volume.inputs['Density'].default_value=.02
scaled=[]
for resolution in (16,32):
    sample={'id':'native_scale_'+str(resolution),'name':obj.name,'kind':'local_grid',
            'bounds':[[-1,-1,-1],[1,1,1]],'matrix':[list(r) for r in Matrix.Diagonal((2.,4.,1.,1.))]}
    a.bake_volume(sample,out.inputs['Volume'],obj.data,OUT,Job(),resolution,1)
    data=np.empty(tuple(sample['resolution']),dtype='float32')
    openvdb.read(sample['path'],'extinction_r').copyToArray(data)
    assert np.allclose(sample['bounds'],[[-2,-4,-1],[2,4,1]])
    assert np.allclose(sample['matrix'],np.eye(4))
    assert sample['resolution']==[resolution//2,resolution,resolution//4]
    scaled.append(data.max()/sample['voxel_size'])
assert np.allclose(scaled,[.014,.014])
tree.links.new(noise.outputs['Fac'],volume.inputs['Density'])
# Native blackbody evaluation and emission-only grids retain useful bounds.
thermal=tree.nodes.new('ShaderNodeVolumePrincipled')
thermal.inputs['Density'].default_value=0
thermal.inputs['Blackbody Intensity'].default_value=1
thermal.inputs['Temperature'].default_value=1500
tree.links.new(thermal.outputs[0],out.inputs['Volume'])
heat=dict(record2,id='native_blackbody')
heat.pop('cache_hit',None)
a.bake_volume(heat,out.inputs['Volume'],obj.data,OUT,Job(),8,1)
assert heat['emission_path']
thermal_grid=openvdb.read(heat['emission_path'],'emission_r')
thermal_values=np.empty((8,8,8),dtype='float32')
thermal_grid.copyToArray(thermal_values)
assert thermal_values.max()>0 and thermal_values[0,0,0]==0
tree.links.new(volume.outputs[0],out.inputs['Volume'])
tree.nodes.remove(thermal)
# Existing VDB density attributes use native grids and preserve their transform.
density=openvdb.FloatGrid()
density.name='density'
density.transform=openvdb.createLinearTransform(voxelSize=.25)
density.copyFromArray(np.ones((8,8,8),dtype='float32')*.02)
existing=OUT/'existing.vdb'
openvdb.write(str(existing),grids=[density])
vd=bpy.data.volumes.new('Existing VDB')
vd.filepath=str(existing)
vo=bpy.data.objects.new(vd.name,vd)
scene.collection.objects.link(vo)
vm=bpy.data.materials.new('VDB material')
vm.use_nodes=True
vp=vm.node_tree.nodes.new('ShaderNodeVolumePrincipled')
vp.inputs['Density'].default_value=1
vm.node_tree.links.new(vp.outputs[0],vm.node_tree.nodes['Material Output'].inputs['Volume'])
vd.materials.append(vm)
vr,vs,mesh=a.freeze_vdb(vo,vo.matrix_world,{'scene':str(source),'source_renderer':'CYCLES'},0)
a.bake_volume(vr,vs,mesh,OUT,Job(),16,1)
data=np.empty(tuple(vr['resolution']),dtype='float32')
openvdb.read(vr['path'],'extinction_r').copyToArray(data)
assert abs(data.max()-.02*vr['voxel_size'])<.00001
bpy.data.objects.remove(vo,do_unlink=True)
bpy.data.volumes.remove(vd)
# Stop is checked even when a cached volume exists.
from mmd_bridge.core import Cancelled
(OUT/'stop').touch()
try:
    a.bake_volume(record2,out.inputs['Volume'],obj.data,OUT,Job(),24,1)
    raise AssertionError('Cached volume ignored stop')
except Cancelled:
    pass
finally:
    (OUT/'stop').unlink()
# Cancellation must also escape the scene-level recovery boundary.
(OUT/'stop').touch()
try:
    a.export_appearance(scene,[(record2,out.inputs['Volume'],obj.data.copy())],OUT,Job(),1,None)
    raise AssertionError('Appearance export swallowed cancellation')
except Cancelled:
    pass
finally:
    (OUT/'stop').unlink()
look=a.bake_look(scene,OUT,{})
import PyOpenColorIO as ocio
config=ocio.Config.CreateFromFile(look['config'])
cpu=config.getProcessor(look['input'],look['output']).getDefaultCPUProcessor()
pixel=[4.,2.,1.]
cpu.applyRGB(pixel)
assert all(np.isfinite(pixel))
atomic_json(OUT/'result.json',{'complete':True,'capabilities':a.capabilities(),'volume':record,'look':look,
                             'nonzero_voxels':int(np.count_nonzero(values)),'max_extinction_per_voxel':float(values.max())})
print('APPEARANCE_NATIVE_SMOKE_PASS',flush=True)
from mmd_bridge.core import Job as BridgeJob, Settings, read_json
from mmd_bridge.blender_usd import export_usd_scene
settings=Settings(flow='scene',scene=str(source),blender=bpy.app.binary_path,output_override=str(OUT),volume_resolution=24,
                  content_root='/Game/TEST_ON_928/AppearanceSmoke',bake_resolution=1024)
job=BridgeJob.create(OUT/'jobs',settings)
export_usd_scene(job)
manifest=read_json(job.folder/'manifest.json')
assert len(manifest['scene']['instances'])==2
hierarchy=manifest['scene']['hierarchy']
owners={item['prim_path'] for item in hierarchy}
assert all(part['prim_path'] not in owners or part['prim_path']==item['prim_path']
           for item in hierarchy for part in item['parts'])
floor_entry=next(item for item in hierarchy if item['name']==floor_name)
assert floor_entry['parts'][0]['prim_path']!=floor_entry['prim_path']+'/Mesh'
assert all(v['status']=='converted' for v in manifest['scene']['volumes'])
from pxr import Usd,UsdVol
stage=Usd.Stage.Open(manifest['scene']['usd']['root_layer'])
volume=UsdVol.Volume(stage.GetPrimAtPath(next(v for v in manifest['scene']['volumes'] if v.get('prim_path'))['prim_path']))
assert len(volume.GetFieldPaths())==7
atomic_json(OUT/'export_result.json',{'complete':True,'job':str(job.folder),'volume_prim':str(volume.GetPath())})
print('APPEARANCE_FULL_EXPORT_PASS',flush=True)
