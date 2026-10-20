"""Native cage-guided normals: identical geometry, keys, UV, editable inputs."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np
from mmd_bridge.blender_contour import prepare_contour
from mmd_bridge.blender_hair_normals import prepare_hair_normals
from mmd_bridge.core import atomic_json
OUT=Path(sys.argv[sys.argv.index('--')+1]);OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
material=bpy.data.materials.new('HairCageFixture');mesh=bpy.data.meshes.new('Cage')
points=[(x*.02,y*.03,.005*x*x+.001*y*y) for y in range(4) for x in range(3)]
faces=[]
for y in range(3):
 for x in range(2):
  a=y*3+x;faces.extend([(a,a+1,a+4),(a,a+4,a+3)])
mesh.from_pydata(points,[],faces);mesh.materials.append(material)
for p in mesh.polygons:p.use_smooth=True
uv=mesh.uv_layers.new(name='UVMap')
for p in mesh.polygons:
 for loop in p.loop_indices:
  v=mesh.vertices[mesh.loops[loop].vertex_index];uv.data[loop].uv=(v.co.x/.04,v.co.y/.09)
obj=bpy.data.objects.new('ReferenceHair',mesh);bpy.context.scene.collection.objects.link(obj)
for name in ('Basis','Bend'):
 key=obj.shape_key_add(name=name,from_mix=False)
 if name=='Bend':
  for v in key.data:v.co.z+=.002*v.co.y
obj.vertex_groups.new(name='Head').add(list(range(len(points))),1.,'REPLACE')
results=[]
for mode in ('surface','cage'):
 source=obj.copy();source.data=obj.data.copy();bpy.context.scene.collection.objects.link(source)
 objects,receipt=prepare_contour([source],'subdivision',[0],material_types={material.name:'hair'},hair_protection='authored',hair_normal_mode=mode)
 result=objects[0];normals,audit=prepare_hair_normals(result,{material.name},mode=mode)
 result.data.normals_split_custom_set(normals);result.data.update()
 results.append((result,receipt,audit))
a,b=[r[0].data for r in results]
assert np.array_equal(np.array([v.co[:] for v in a.vertices]),np.array([v.co[:] for v in b.vertices]))
for name in ('Basis','Bend'):
 assert np.array_equal(np.array([v.co[:] for v in a.shape_keys.key_blocks[name].data]),np.array([v.co[:] for v in b.shape_keys.key_blocks[name].data]))
assert [v.uv[:] for v in a.uv_layers.active.data]==[v.uv[:] for v in b.uv_layers.active.data]
assert [p.material_index for p in a.polygons]==[p.material_index for p in b.polygons]
assert results[1][1]['meshes'][0]['parts'][0]['hair_normal_reference']['weighted_weight']==50
assert results[1][2]['policy']=='weighted_cage_interpolation_v1'
assert all(abs(n.vector.length-1)<1e-4 for n in b.corner_normals)
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'HairCageFixtures.blend'))
atomic_json(OUT/'hair_cage_native.json',{'complete':True,'identical_geometry_shapes_uv_materials':True,'surface':results[0][2],'cage':results[1][2]})
print('HAIR_CAGE_NATIVE_READY',len(b.vertices),flush=True)
