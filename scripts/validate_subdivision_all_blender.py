"""Native Blender geometry regressions, separate from real-character evidence."""
from pathlib import Path
import sys, json
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'vendor/python',ROOT/'vendor/blender_mmd_tools-4.5.13'):sys.path.insert(0,str(path))
import bpy
import numpy as np
from mmd_bridge.blender_contour import prepare_contour
from mmd_bridge.blender_mesh_finish import prepare_comparison

HERE=Path(sys.argv[sys.argv.index('--')+1]);HERE.mkdir(parents=True,exist_ok=True)
rows=[]
for level in (1,):
 for policy in ('native','protected'):
  bpy.ops.wm.read_factory_settings(use_empty=True)
  vertices=[];faces=[];slots=[]
  def piece(points,polygons,slot):
   start=len(vertices);vertices.extend(points);faces.extend(tuple(i+start for i in p) for p in polygons);slots.extend([slot]*len(polygons))
  piece([(-.1,0,0),(.1,0,0),(.03,.3,0),(0,.7,0),(-.03,.3,0)],[(0,1,2,4),(4,2,3)],0)
  tip=len(vertices)
  piece([(0,0,.3),(-.05,-.05,0),(.05,-.05,0),(.05,.05,0),(-.05,.05,0)],[(0,1,2),(0,2,3),(0,3,4),(0,4,1),(4,3,2,1)],1)
  piece([(x*.1+.5,y*.1,z*.1) for z in (0,1) for y in (0,1) for x in (0,1)],
        [(0,2,3,1),(4,5,7,6),(0,1,5,4),(2,6,7,3),(0,4,6,2),(1,3,7,5)],2)
  grid=len(vertices)
  piece([(x*.02,y*.02+.8,.04 if x==2 and y==2 else 0) for y in range(5) for x in range(5)],
        [(y*5+x,y*5+x+1,(y+1)*5+x+1,(y+1)*5+x) for y in range(4) for x in range(4)],3)
  mesh=bpy.data.meshes.new('Synthetic');mesh.from_pydata(vertices,[],faces)
  types={}
  for name,kind in [('HairCard','hair'),('ClosedTip','face'),('HardCorner','metal'),('SoftSurface','skin')]:
   mesh.materials.append(bpy.data.materials.new(name));types[name]=kind
  for p,slot in zip(mesh.polygons,slots):p.material_index=slot;p.use_smooth=True
  uv=mesh.uv_layers.new(name='UVMap')
  for loop in mesh.loops:uv.data[loop.index].uv=mesh.vertices[loop.vertex_index].co.xy
  obj=bpy.data.objects.new('Synthetic',mesh);bpy.context.scene.collection.objects.link(obj)
  for name,weight in [('BoneA',.3),('BoneB',.7)]:obj.vertex_groups.new(name=name).add(list(range(len(vertices))),weight,'REPLACE')
  obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Expression');key.data[tip].co.z+=.02
  extra=obj.shape_key_add(name='Relative');extra.relative_key=key;extra.data[grid+12].co.z+=.02
  key.value=.2;key.keyframe_insert('value',frame=1);key.value=.8;key.keyframe_insert('value',frame=2)
  output,receipt=prepare_contour([obj],'subdivision',all_materials=True,levels=level,boundary_policy=policy,material_types=types)
  result=output[0];reports=receipt['meshes'][0]['parts']
  by_material={r['materials'][0]:r for r in reports}
  assert len(result.data.shape_keys.key_blocks)==3
  assert result.data.shape_keys.key_blocks['Relative'].relative_key.name=='Expression'
  assert all(abs(sum(g.weight for g in v.groups)-1)<1e-5 for v in result.data.vertices)
  for frame,value in [(1,.2),(2,.8)]:
   bpy.context.scene.frame_set(frame);bpy.context.view_layer.update()
   assert abs(result.data.shape_keys.key_blocks['Expression'].value-value)<1e-5
  if policy=='protected':
   assert by_material['ClosedTip']['protection']['sources']['closed_tip']['vertices']
   assert by_material['HardCorner']['protection']['sources']['dihedral']['edges']
   assert by_material['SoftSurface']['max_original_vertex_displacement_m']>1e-4
   assert all(r['max_protected_error_m']<=1e-6 for r in reports)
  else:
   assert by_material['HairCard']['max_original_vertex_displacement_m']>1e-4
   assert by_material['ClosedTip']['max_original_vertex_displacement_m']>1e-4
  output,normal=prepare_comparison(output,.0001)
  bpy.context.scene.frame_set(1);bpy.context.view_layer.update()
  assert abs(output[0].data.shape_keys.key_blocks['Expression'].value-.2)<1e-5
  rows.append({'level':level,'policy':policy,'complete':True,'contour':receipt,'normal':normal})
  bpy.ops.wm.save_as_mainfile(filepath=str(HERE/f'{policy}_L{level}.blend'))
from mmd_bridge.character_geometry import prepare_character_geometry
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh=bpy.data.meshes.new('DefaultTargets')
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(2,0,0),(3,0,0),(2,1,0)],[],[(0,1,2),(3,4,5)])
records=[]
for name,kind in [('Body','skin'),('Lip','face')]:
 mesh.materials.append(bpy.data.materials.new(name));records.append({'asset_name':name,'character_material':{'pbr':{'type':kind}}})
mesh.polygons[1].material_index=1
obj=bpy.data.objects.new('DefaultTargets',mesh);bpy.context.scene.collection.objects.link(obj)
original_lip=[tuple(mesh.vertices[i].co) for i in (3,4,5)]
obj.shape_key_add(name='Basis');obj.shape_key_add(name='Smile')
output,receipt=prepare_character_geometry([obj],records,{'character_material_profile':'character_pbr',
 'character_backend':'mmdbridge_fbx','character_subdivision_enabled':True})
assert len(output[0].data.polygons)==4
assert receipt['contours'][0]['target_pmx_indices']==[0]
assert receipt['normal_finish']['weld_distance_m']==.0001
assert receipt['contours'][0]['meshes'][0]['untouched_material_fingerprints_verified']
(HERE/'default_geometry_regression.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
(HERE/'native_subdivision_regression.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print('SUBDIVISION_NATIVE_READY',len(rows),flush=True)
