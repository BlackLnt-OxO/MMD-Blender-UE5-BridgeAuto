"""Native Blender fixtures for world context, static light fields and mixed fog."""
import sys
from pathlib import Path
import bpy
from mathutils import Matrix
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,atomic_json,file_hash
from mmd_bridge.blender_appearance import (expression_value,light_record,world_diagnostics,
    bake_environment,bake_volume,coefficients,angular_light_field)
import numpy as np
OUT=ROOT/'work/world_appearance/native_regression';OUT.mkdir(parents=True,exist_ok=True)
(OUT/'Volumes').mkdir(exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene;scene.render.engine='CYCLES'
world=bpy.data.worlds.new('NestedWorld');world.use_nodes=True;scene.world=world
tree=world.node_tree;tree.nodes.clear();terminal=tree.nodes.new('ShaderNodeOutputWorld')
group=bpy.data.node_groups.new('NestedBackground','ShaderNodeTree')
group.interface.new_socket(name='Color',in_out='INPUT',socket_type='NodeSocketColor')
group.interface.new_socket(name='Strength',in_out='INPUT',socket_type='NodeSocketFloat')
group.interface.new_socket(name='Shader',in_out='OUTPUT',socket_type='NodeSocketShader')
gi=group.nodes.new('NodeGroupInput');go=group.nodes.new('NodeGroupOutput');bg=group.nodes.new('ShaderNodeBackground')
group.links.new(gi.outputs['Color'],bg.inputs['Color']);group.links.new(gi.outputs['Strength'],bg.inputs['Strength']);group.links.new(bg.outputs[0],go.inputs[0])
instances=[]
for color,strength in [((.1,.2,.3,1),1.),((.3,.2,.1,1),2.)]:
 n=tree.nodes.new('ShaderNodeGroup');n.node_tree=group;n.inputs['Color'].default_value=color;n.inputs['Strength'].default_value=strength;instances.append(n)
add=tree.nodes.new('ShaderNodeAddShader');tree.links.new(instances[0].outputs[0],add.inputs[0]);tree.links.new(instances[1].outputs[0],add.inputs[1]);tree.links.new(add.outputs[0],terminal.inputs['Surface'])
diagnostic=world_diagnostics(scene,terminal)
assert len(diagnostic['backgrounds'])==2 and sorted(x['strength'] for x in diagnostic['backgrounds'])==[1.,2.]
light=bpy.data.lights.new('BlackbodyPoint','POINT');light.use_nodes=True;light.energy=100
obj=bpy.data.objects.new('BlackbodyPoint',light);scene.collection.objects.link(obj)
lt=light.node_tree;em=next(n for n in lt.nodes if n.type=='EMISSION');bb=lt.nodes.new('ShaderNodeBlackbody');bb.inputs[0].default_value=3000
lt.links.new(bb.outputs[0],em.inputs['Color']);math=lt.nodes.new('ShaderNodeMath');math.operation='MULTIPLY';math.inputs[0].default_value=2;math.inputs[1].default_value=3;lt.links.new(math.outputs[0],em.inputs['Strength'])
evaluated=light_record(obj,'CYCLES',1.)
assert not evaluated['needs_review'] and abs(evaluated['energy']-600)<1e-5 and evaluated['color'][0]>evaluated['color'][2]
assert expression_value(em.inputs['Strength'])==6
math.inputs[1].default_value=4;assert expression_value(em.inputs['Strength'])==8
math.inputs[1].default_value=3
value_node=lt.nodes.new('ShaderNodeValue');value_node.outputs[0].default_value=3
lt.links.new(value_node.outputs[0],math.inputs[1]);assert expression_value(em.inputs['Strength'])==6
value_node.outputs[0].default_value=4;assert expression_value(em.inputs['Strength'])==8
lt.links.remove(math.inputs[1].links[0]);math.inputs[1].default_value=3
geo=lt.nodes.new('ShaderNodeNewGeometry');lt.links.new(geo.outputs['Position'],em.inputs['Color']);assert expression_value(em.inputs['Color']) is None
lt.links.remove(em.inputs['Color'].links[0]);lt.links.new(bb.outputs[0],em.inputs['Color'])
geometry=lt.nodes.new('ShaderNodeNewGeometry');split=lt.nodes.new('ShaderNodeSeparateXYZ');lt.links.new(geometry.outputs['Incoming'],split.inputs[0])
angular=angular_light_field(split.outputs['Z'])
assert angular['op']=='component' and angular['value']['op']=='incoming'
divide=lt.nodes.new('ShaderNodeMath');divide.operation='DIVIDE'
lt.links.new(split.outputs['Z'],divide.inputs[0]);divide.inputs[1].default_value=0
try:angular_light_field(divide.outputs[0])
except ValueError:pass
else:raise AssertionError('Angular division by zero must be rejected')
divide.inputs[1].default_value=2
assert angular_light_field(divide.outputs[0])['args'][1]['value']==2
ies=lt.nodes.new('ShaderNodeTexIES');ies.mode='EXTERNAL'
profile=OUT/'profile.ies'
profile.write_text('IESNA:LM-63-2002\n[TEST] MMDBridge\nTILT=NONE\n1 1000 1 5 1 1 2 .1 .1 .1\n1 1 10\n0 45 90 135 180\n0\n100 80 50 10 0\n',encoding='ascii')
ies.filepath=str(profile)
ies.inputs['Strength'].default_value=3
assert angular_light_field(ies.outputs[0])['gain']==3
lt.links.new(ies.outputs[0],em.inputs['Strength'])
ies_light=light_record(obj,'CYCLES',1.)
assert ies_light['directional']['op']=='ies' and abs(ies_light['energy']-300)<1e-5
lt.links.new(math.outputs[0],em.inputs['Strength'])
source=OUT/'fixture.blend';bpy.ops.wm.save_as_mainfile(filepath=str(source));before=file_hash(source)
atomic_json(OUT/'job.json',{'settings':{'scene':str(source),'world_volume_distance':100,'ue_vram_pause_percent':90,'ue_vram_resume_percent':80}});job=Job(OUT)
environment=bake_environment(scene,OUT,job)
assert environment['surface'] and Path(environment['path']).is_file() and len(environment['source_world']['backgrounds'])==2
# Camera and illumination branches must remain distinct; source groups stay untouched.
ray=tree.nodes.new('ShaderNodeLightPath');blend=tree.nodes.new('ShaderNodeMixShader')
red=tree.nodes.new('ShaderNodeBackground');blue=tree.nodes.new('ShaderNodeBackground')
red.inputs['Color'].default_value=(1,0,0,1);blue.inputs['Color'].default_value=(0,0,1,1)
tree.links.new(ray.outputs['Is Camera Ray'],blend.inputs[0]);tree.links.new(blue.outputs[0],blend.inputs[1]);tree.links.new(red.outputs[0],blend.inputs[2]);tree.links.new(blend.outputs[0],terminal.inputs['Surface'])
rayfolder=OUT/'RayEnvironment';rayfolder.mkdir(exist_ok=True)
ray_environment=bake_environment(scene,rayfolder,job)
import OpenImageIO as oiio
def mean_rgb(path):
 image=oiio.ImageInput.open(path);values=np.asarray(image.read_image(format=oiio.FLOAT));image.close();return values[:,:,:3].mean(axis=(0,1))
camera_rgb=mean_rgb(ray_environment['background_path']);lighting_rgb=mean_rgb(ray_environment['path'])
assert camera_rgb[0]>camera_rgb[2]*10 and lighting_rgb[2]>lighting_rgb[0]*10
assert ray.outputs['Is Camera Ray'].is_linked
import mmd_bridge.blender_appearance as appearance
original_diagnostics=appearance.world_diagnostics
def broken_diagnostics(*args):raise ValueError('fixture diagnostic failure')
appearance.world_diagnostics=broken_diagnostics
try:
 failfolder=OUT/'DiagnosticFailure';failfolder.mkdir(exist_ok=True)
 retained=bake_environment(scene,failfolder,job)
 assert retained['surface'] and Path(retained['path']).is_file() and retained['source_world']['diagnostic_error']
finally:appearance.world_diagnostics=original_diagnostics
volume1=tree.nodes.new('ShaderNodeVolumeScatter');volume2=tree.nodes.new('ShaderNodeVolumeScatter')
for n,g in [(volume1,0.),(volume2,.75)]:n.inputs['Density'].default_value=.01;n.inputs['Anisotropy'].default_value=g;n.inputs['Color'].default_value=(1,1,1,1)
mix=tree.nodes.new('ShaderNodeAddShader');tree.links.new(volume1.outputs[0],mix.inputs[0]);tree.links.new(volume2.outputs[0],mix.inputs[1]);tree.links.new(mix.outputs[0],terminal.inputs['Volume'])
volume_density=tree.nodes.new('ShaderNodeValue');volume_density.outputs[0].default_value=.01
volume_color=tree.nodes.new('ShaderNodeRGB');volume_color.outputs[0].default_value=(1,1,1,1)
for node in (volume1,volume2):
 tree.links.new(volume_density.outputs[0],node.inputs['Density'])
 tree.links.new(volume_color.outputs[0],node.inputs['Color'])
record=dict(id='mixed',name='MixedWorld',kind='world_grid',bounds=[[-1,-1,-1],[1,1,1]],matrix=[list(r) for r in Matrix.Identity(4)])
bake_volume(record,terminal.inputs['Volume'],None,OUT/'Volumes',job,8,1.)
assert record['status']=='converted' and record['phase_approximation'] and len(record['source_components'])==2
import openvdb as vdb
grids=vdb.readAll(record['path']);grids=grids[0] if isinstance(grids,tuple) else grids
grid=next(g for g in grids if g.name=='extinction_r')
voxels=np.empty((8,8,8),dtype='float32');grid.copyToArray(voxels)
assert np.allclose(voxels,.02*.25)
assert record['sample_statistics']['occupied_voxels']==8**3
assert abs(record['sample_statistics']['phase_range'][0]-.375)<1e-6
assert record['sample_statistics']['occupied_fraction']==1.
from mmd_bridge.blender_appearance import volume_specs
specs,items=volume_specs(terminal.inputs['Volume'],allow_mixed=True)
from mmd_bridge.blender_appearance import constant
values={name:np.array(constant(s,context)[:3] if vector else constant(s,context)) for name,s,context,vector in specs}
extinction,albedo,emission,g=coefficients(values,items,())
assert np.allclose(extinction,[.02]*3) and np.allclose(albedo,[1]*3) and abs(float(g)-.375)<1e-6
assert file_hash(source)==before
atomic_json(OUT/'result.json',dict(complete=True,nested_context=True,group_instances_distinct=True,
    static_blackbody_light=evaluated,unsupported_coordinate_rejected=True,expression_cache_invalidates=True,
    angular_graph=angular,environment=environment,ray_environment=ray_environment,diagnostic_failure_retains_hdr=True,
    mixed_volume=record,source_unchanged=True))
print('WORLD_APPEARANCE_NATIVE_PASS',flush=True)
