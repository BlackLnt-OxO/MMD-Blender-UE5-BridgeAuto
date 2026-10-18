"""Real Blender regression: modifier normals without changing morph topology."""
from pathlib import Path
from array import array
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_mesh_finish import prepare_comparison
from mmd_bridge.core import atomic_json
output=Path(sys.argv[sys.argv.index('--')+1]).resolve()
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
bpy.ops.object.armature_add();rig=bpy.context.object
mesh=bpy.data.meshes.new('RegressionMesh')
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(0,0,0),(0,-1,.4),(1,0,0),
                 (2,0,0),(3,0,0),(2,1,0)],[],[(0,1,2),(3,4,5),(6,7,8)])
obj=bpy.data.objects.new('Regression',mesh);bpy.context.scene.collection.objects.link(obj)
for name in ('Cloth','Skin'):mesh.materials.append(bpy.data.materials.new(name))
mesh.polygons[2].material_index=1
for polygon in mesh.polygons:polygon.use_smooth=True
uv=mesh.uv_layers.new(name='OriginalUV')
for index,loop in enumerate(uv.data):loop.uv=(index/10.,index/20.)
group=obj.vertex_groups.new(name=rig.data.bones[0].name);group.add(list(range(9)),1.,'REPLACE')
arm=obj.modifiers.new('OriginalArmature','ARMATURE');arm.object=rig
basis=obj.shape_key_add(name='Basis');blink=obj.shape_key_add(name='Blink')
blink.data[0].co.z=.2;blink.data[3].co.z=.4
smile=obj.shape_key_add(name='Smile');smile.relative_key=blink
smile.data[0].co.z=.4;smile.data[3].co.z=.6
def coords(data):
    value=array('f',[0.])*(len(data)*3);data.foreach_get('co',value);return value.tobytes()
original_shapes={key.name:coords(key.data) for key in mesh.shape_keys.key_blocks}
original_vertices=coords(mesh.vertices)
original_uv=[tuple(loop.uv) for loop in uv.data]
initial=tuple(mesh.corner_normals[0].vector)
finished,receipt=prepare_comparison([obj],.0001)
assert len(finished)==1
result=finished[0];data=result.data
assert len(data.vertices)==9 and len(data.polygons)==3
assert data.has_custom_normals and coords(data.vertices)==original_vertices
assert {key.name:coords(key.data) for key in data.shape_keys.key_blocks}==original_shapes
assert data.shape_keys.key_blocks['Smile'].relative_key==data.shape_keys.key_blocks['Blink']
assert [tuple(loop.uv) for loop in data.uv_layers['OriginalUV'].data]==original_uv
bone_index=result.vertex_groups[rig.data.bones[0].name].index
assert all(abs(next(group.weight for group in v.groups if group.group==bone_index)-1.)<1e-6 for v in data.vertices)
assert max(abs(a-b) for a,b in zip(initial,data.corner_normals[0].vector))>.01
print('NORMAL_SEAM',tuple(data.corner_normals[0].vector),tuple(data.corner_normals[3].vector),flush=True)
assert max(abs(a-b) for a,b in zip(data.corner_normals[0].vector,data.corner_normals[3].vector))<.001
assert receipt['export_topology']=='original_unchanged'
receipt.update(status='complete',checks=['original_topology','byte_identical_shape_coordinates',
    'relative_morphs','uv_seams','bone_weights','actual_normal_change','welded_seam_normal_agreement'])
atomic_json(output,receipt)
print('MESH_FINISH_BLENDER_REGRESSION_COMPLETE',flush=True)
