"""Native surface-domain and shape-key fixture; no downloaded assets."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np
from mmd_bridge.blender_surface_repair import attach_original,_annotate,apply_attachments,triangulate_export,_points
from mmd_bridge.blender_contour import prepare_contour
from mmd_bridge.blender_mesh_finish import prepare_comparison

rows=[]
for subdivide in (False,True):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    coords=[(x*.02,y*.02,(x*x+y*y)*.002) for y in range(3) for x in range(3)]
    faces=[]
    for y in range(2):
        for x in range(2):
            a=y*3+x;faces.extend([(a,a+1,a+4),(a,a+4,a+3)])
    host_indices=(0,1,4,3);coords.extend(coords[i] for i in host_indices)
    faces.extend([(9,10,11),(9,11,12)])
    mesh=bpy.data.meshes.new('SurfaceFixture');mesh.from_pydata(coords,[],faces);mesh.update()
    for name in ('Host','Overlay'):mesh.materials.append(bpy.data.materials.new(name))
    for p in mesh.polygons:p.material_index=int(p.index>=8);p.use_smooth=True
    uv=mesh.uv_layers.new(name='UVMap')
    for p in mesh.polygons:
        for n,i in enumerate(p.loop_indices):uv.data[i].uv=((n==1)*.8,(n==2)*.8)
    obj=bpy.data.objects.new('SurfaceFixture',mesh);bpy.context.scene.collection.objects.link(obj)
    group=obj.vertex_groups.new(name='SharedBone');group.add(list(range(len(coords))),1.,'REPLACE')
    armature=bpy.data.armatures.new('FixtureRig');rig=bpy.data.objects.new('FixtureRig',armature)
    bpy.context.scene.collection.objects.link(rig);bpy.context.view_layer.objects.active=rig;rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT');bone=armature.edit_bones.new('SharedBone');bone.tail.z=.1
    bpy.ops.object.mode_set(mode='OBJECT');modifier=obj.modifiers.new('Armature','ARMATURE');modifier.object=rig
    helper=obj.vertex_groups.new(name='MMD_Helper');helper.add(list(range(9)),.2,'REPLACE');helper.add(list(range(9,13)),.8,'REPLACE')
    basis=obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Bend')
    for p in key.data:p.co.z+=p.co.x*.1
    hidden=obj.shape_key_add(name='Authored_Hide')
    for p in hidden.data:p.co=(0.,0.,0.)
    original=_points(basis.data).copy()
    records=[{'asset_name':'Host','name':'Host','pmx_index':0,'character_material':{'pbr':{'type':'cloth','alpha_mode':'opaque','params':{}}}},
             {'asset_name':'Overlay','name':'无名覆盖','pmx_index':1,'character_material':{'pbr':{'type':'generic','alpha_mode':'masked','params':{}}}}]
    bindings,reviews=attach_original(obj,records,{},'test');assert len(bindings)==1 and not reviews
    key.data[9].co.z+=.005
    rejected,issues=attach_original(obj,records,{},'test')
    assert not rejected and '独立' in issues[0]['reason']
    key.data[9].co.z-=.005
    _annotate(mesh)
    if subdivide:
        result,contour=prepare_contour([obj],'subdivision',[0,1],boundary_policy='protected',material_types={'Host':'cloth','Overlay':'cloth'})
        result=result[0]
    else:result=obj;contour={'mode':'none'}
    applied=apply_attachments(obj,result,contour,bindings);assert all(r['complete'] for r in applied),applied
    before=_points(result.data.shape_keys.key_blocks[0].data)
    result,tri=triangulate_export(result)
    assert np.array_equal(before,_points(result.data.shape_keys.key_blocks[0].data))
    assert [k.name for k in result.data.shape_keys.key_blocks]==['Basis','Bend','Authored_Hide']
    assert np.abs(_points(result.data.shape_keys.key_blocks['Authored_Hide'].data)).max()==0
    assert applied[0]['collapsed_source_morphs']
    assert all(len(p.vertices)==3 for p in result.data.polygons)
    result,normal=prepare_comparison([result],.0001,safe_normals=subdivide,repair_materials={'Host','Overlay'})
    assert np.array_equal(before,_points(result[0].data.shape_keys.key_blocks[0].data))
    assert all(np.isfinite(n.vector[:]).all() for n in result[0].data.corner_normals)
    rows.append({'subdivision':subdivide,'attachments':applied,'triangulation':tri,'normal':normal,'complete':True})
# A material overlay that shares host vertices cannot be offset independently.
# Refuse it explicitly; never move the host or silently claim a repaired layer.
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh=bpy.data.meshes.new('SharedDomain');mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],[],[(0,1,2),(0,2,3),(0,1,2)])
for name in ('Host','Overlay'):mesh.materials.append(bpy.data.materials.new(name))
mesh.polygons[2].material_index=1
obj=bpy.data.objects.new('SharedDomain',mesh);bpy.context.scene.collection.objects.link(obj)
bindings,reviews=attach_original(obj,records,{},'test')
assert not bindings and '共享' in reviews[0]['reason']
rows.append({'shared_vertex_overlay_refused':True,'complete':True})
# Coincident front/back hair faces retain opposite geometric orientations.
# Safe transfer must not unify them just because the weld proxy merges positions.
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh=bpy.data.meshes.new('DoubleSidedHair')
mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)]*2,[],[(0,1,2),(0,2,3),(6,5,4),(7,6,4)])
mesh.materials.append(bpy.data.materials.new('Hair'))
for p in mesh.polygons:p.use_smooth=True
obj=bpy.data.objects.new('DoubleSidedHair',mesh);bpy.context.scene.collection.objects.link(obj)
finished,receipt=prepare_comparison([obj],.0001,safe_normals=True,repair_materials={'Hair'})
out=finished[0].data;directions=[float(n.vector.z) for n in out.corner_normals]
assert min(directions[:6])>.99 and max(directions[6:])<-.99,directions
rows.append({'opposite_hair_faces_preserved':True,'complete':True})
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh=bpy.data.meshes.new('HardTip');mesh.from_pydata([(-.005,-.005,0),(.005,-.005,0),(.005,.005,0),(-.005,.005,0),(0,0,.04)],[],
 [(3,2,1,0),(0,1,4),(1,2,4),(2,3,4),(3,0,4)])
mesh.materials.append(bpy.data.materials.new('Hard'))
obj=bpy.data.objects.new('HardTip',mesh);bpy.context.scene.collection.objects.link(obj)
obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Shift')
for point in key.data:point.co.x+=.001
finished,contour=prepare_contour([obj],'subdivision',[0],boundary_policy='protected',material_types={'Hard':'metal'})
part=contour['meshes'][0]['parts'][0]
assert 4 in part['protection']['sources']['closed_tip']['vertices']
assert part['protection']['sources']['dihedral']['edges'] and part['max_protected_error_m']<=1e-6
rows.append({'closed_tip_and_hard_edges_preserved':True,'complete':True})
from mmd_bridge.blender_surface_repair import refresh_final_directions
manual=[{'character_material':{'pbr':{'type':'hair','textures':{'direction':{'kind':'manual','path':'user_direction.png'}}}}}]
before=json.dumps(manual,sort_keys=True)
assert refresh_final_directions([],manual)==[] and json.dumps(manual,sort_keys=True)==before
rows.append({'manual_direction_map_unchanged':True,'complete':True})
print('SURFACE_REPAIR_NATIVE_FIXTURE',json.dumps(rows),flush=True)
