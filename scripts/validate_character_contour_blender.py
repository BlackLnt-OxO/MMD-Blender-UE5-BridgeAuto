"""Run with Blender --background --factory-startup --python this_file [-- output_folder]."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_contour import prepare_contour,smoothing_mask,smooth_coordinates
from mmd_bridge.blender_mesh_finish import prepare_comparison
HERE=Path(sys.argv[sys.argv.index('--')+1]) if '--' in sys.argv else ROOT/'work/contour_blender_validation'
HERE.mkdir(parents=True,exist_ok=True);rows=[]
for mode in ('smooth','subdivision'):
 bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
 points=[(x*.02,y*.02,.01 if x==4 and y==4 else 0.) for y in range(9) for x in range(9)]
 points += [(0,0,.4),(.1,0,.4),(0,.1,.4)]
 faces=[(y*9+x,y*9+x+1,(y+1)*9+x+1,(y+1)*9+x) for y in range(8) for x in range(8)]+[(81,82,83)]
 mesh=bpy.data.meshes.new('Probe');mesh.from_pydata(points,[],faces)
 mesh.materials.append(bpy.data.materials.new('Untouched'));mesh.materials.append(bpy.data.materials.new('Body'))
 for p in mesh.polygons:p.material_index=1 if p.index<64 else 0;p.use_smooth=True
 uv=mesh.uv_layers.new(name='UVMap')
 for loop in mesh.loops:uv.data[loop.index].uv=mesh.vertices[loop.vertex_index].co.xy
 obj=bpy.data.objects.new('Probe',mesh);bpy.context.scene.collection.objects.link(obj)
 group=obj.vertex_groups.new(name='Bone');group.add(list(range(len(mesh.vertices))),1.,'REPLACE')
 root=bpy.data.objects.new('Controller',None);bpy.context.scene.collection.objects.link(root);root['smile']=.25
 basis=obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Smile');key.data[40].co.z+=.01
 extra=obj.shape_key_add(name='Relative');extra.relative_key=key;extra.data[40].co.z+=.02
 driver=key.driver_add('value').driver;driver.expression='amount';variable=driver.variables.new();variable.name='amount';variable.type='SINGLE_PROP';variable.targets[0].id=root;variable.targets[0].data_path='["smile"]'
 output,receipt=prepare_contour([obj],mode,[1]);result=output[0]
 if mode=='smooth':
  mask=smoothing_mask(84,[(1 if i<64 else 0,face) for i,face in enumerate(faces)],{1})
  edges=[tuple(edge.vertices) for edge in mesh.edges]
  expected=smooth_coordinates(points,edges,mask)
  for index,point in enumerate(expected):
   assert (result.data.vertices[index].co-basis.data[index].co).length<.1
   assert max(abs(a-b) for a,b in zip(result.data.vertices[index].co,point))<1e-7
   assert max(abs(a-b) for a,b in zip(result.data.shape_keys.key_blocks[0].data[index].co,point))<1e-7
 assert [k.name for k in result.data.shape_keys.key_blocks]==['Basis','Smile','Relative']
 assert result.data.shape_keys.key_blocks['Relative'].relative_key.name=='Smile'
 bpy.context.view_layer.update();assert abs(result.data.shape_keys.key_blocks['Smile'].value-.25)<1e-6
 assert all(abs(sum(g.weight for g in v.groups)-1.)<1e-5 for v in result.data.vertices)
 output,normal=prepare_comparison(output,.0001)
 assert output[0].data.shape_keys and len(output[0].data.shape_keys.key_blocks)==3
 assert len(output[0].data.vertices)==(84 if mode=='smooth' else 292)
 if mode=='subdivision':
  vertices=[v.co for v in output[0].data.vertices]
  for index in [i for i in range(81) if i%9 in (0,8) or i//9 in (0,8)]:
   assert min((p-basis.data[index].co).length for p in vertices)<1e-7
 rows.append({'mode':mode,'vertices':len(output[0].data.vertices),'contour':receipt,'normal':normal['modifier_order'],'complete':True})
(HERE/'blender_probe.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print('CONTOUR_BLENDER_PROBE_READY',len(rows),flush=True)
