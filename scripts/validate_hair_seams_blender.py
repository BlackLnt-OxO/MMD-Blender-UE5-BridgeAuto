"""Native exact UV seam, morph/skin preservation and rejection fixtures."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_hair_seams import stitch_hair_seams
from mmd_bridge.blender_contour import _subdivide_part
from mmd_bridge.core import atomic_json
OUT=Path(sys.argv[sys.argv.index('--')+1]);OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
def fixture(name,mode='compatible'):
 points=[(0,0,0),(1,0,0),(1,1,0),(0,1,0),(1,0,0),(2,0,0),(2,1,0),(1,1,0)]
 if mode=='close_sheet':points[4]=(1,0,1e-8);points[7]=(1,1,1e-8)
 mesh=bpy.data.meshes.new(name);mesh.from_pydata(points,[],[(0,1,2,3),(4,5,6,7)])
 obj=bpy.data.objects.new(name,mesh);bpy.context.scene.collection.objects.link(obj)
 uv=mesh.uv_layers.new(name='UVMap')
 uv.data.foreach_set('uv',[0,0,1,0,1,1,0,1]*2)
 for p in mesh.polygons:p.use_smooth=True
 for name in ('Basis','Smile','Relative'):
  key=obj.shape_key_add(name=name,from_mix=False)
  if name!='Basis':
   for v in key.data:v.co.z=.1*v.co.y+(.02 if name=='Relative' else 0)
 obj.data.shape_keys.key_blocks['Relative'].relative_key=obj.data.shape_keys.key_blocks['Smile']
 if mode=='independent_morph':obj.data.shape_keys.key_blocks['Smile'].data[4].co.z+=.1
 obj.vertex_groups.new(name='Bone').add(list(range(8)),1.,'REPLACE')
 if mode=='independent_skin':
  obj.vertex_groups['Bone'].add([4],.7,'REPLACE');obj.vertex_groups.new(name='OtherBone').add([4],.3,'REPLACE')
 order=obj.vertex_groups.new(name='mmd_vertex_order')
 for i in range(8):order.add([i],i/8.,'REPLACE')
 if mode=='hard':
  next(e for e in mesh.edges if set(e.vertices)=={1,2}).use_edge_sharp=True
 if mode=='crease':
  attribute=mesh.attributes.new('crease_edge','FLOAT','EDGE')
  attribute.data[next(e.index for e in mesh.edges if set(e.vertices)=={1,2})].value=1.
 mesh.update();return obj
rows=[]
for mode in ('compatible','independent_morph','independent_skin','close_sheet','hard','crease','uv_morph'):
 obj=fixture('Seam_'+mode,mode);report=stitch_hair_seams(obj,{'vertex_count':8,'uv_vertices':{1}} if mode=='uv_morph' else None)
 assert report['changed']==(mode=='compatible'),(mode,report)
 assert len(obj.data.shape_keys.key_blocks)==3 and obj.data.shape_keys.key_blocks['Relative'].relative_key.name=='Smile'
 if mode=='compatible':
  assert len(obj.data.vertices)==6 and len(obj.data.polygons)==2 and report['exact_boundary_pairs']==1
  evaluated,keys,contour=_subdivide_part(obj,1,'protected','hair','authored')
  assert len(evaluated.vertices)==15 and len(evaluated.polygons)==8
  assert len(contour['protection']['sources']['boundary']['edges'])==6
  assert contour['max_protected_error_m']<=1e-6
 rows.append({'mode':mode,**report})
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'HairSeamFixtures.blend'))
atomic_json(OUT/'hair_seams_native.json',{'complete':True,'fixtures':rows})
print('HAIR_SEAMS_NATIVE_READY',len(rows),flush=True)
