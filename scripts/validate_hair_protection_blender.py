"""Compare only automatic interior creases; keep authored edges and boundaries."""
from pathlib import Path
import sys,math,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np
from mmd_bridge.blender_contour import prepare_contour
bpy.ops.wm.read_factory_settings(use_empty=True);rows=[]
for policy in ('legacy','authored'):
 points=[(math.sin((x-2)*math.pi/3)*.02,y*.01,math.cos((x-2)*math.pi/3)*.02) for y in range(5) for x in range(5)]
 faces=[]
 for y in range(4):
  for x in range(4):
   a=y*5+x;faces.extend([(a,a+1,a+6),(a,a+6,a+5)])
 mesh=bpy.data.meshes.new('Hair_'+policy);mesh.from_pydata(points,[],faces);mat=bpy.data.materials.new('Hair_'+policy);mesh.materials.append(mat)
 for p in mesh.polygons:p.use_smooth=True
 for edge in mesh.edges:
  if edge.key==tuple(sorted((7,12))):edge.use_edge_sharp=True
 uv=mesh.uv_layers.new(name='UVMap')
 for loop in mesh.loops:uv.data[loop.index].uv=(loop.vertex_index%5/4,loop.vertex_index//5/4)
 obj=bpy.data.objects.new('Hair_'+policy,mesh);bpy.context.scene.collection.objects.link(obj)
 obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Bend')
 for p in key.data:p.co.y+=p.co.z*.1
 output,receipt=prepare_contour([obj],'subdivision',[0],material_types={mat.name:'hair'},hair_protection=policy)
 part=receipt['meshes'][0]['parts'][0];protection=part['protection']
 assert protection['sources']['boundary']['edges'] and protection['sources']['authored_sharp']['edges']
 assert part['max_protected_error_m']<=1e-6
 assert len(output[0].data.shape_keys.key_blocks)==2
 if policy=='authored':assert not protection['sources']['dihedral']['edges']
 else:assert protection['sources']['dihedral']['edges']
 rows.append({'policy':policy,'protected_points':len(protection['full_vertices']),'source_counts':{k:len(v['edges']) for k,v in protection['sources'].items()},
              'max_protected_error_m':part['max_protected_error_m'],'vertices':len(output[0].data.vertices),'faces':len(output[0].data.polygons),'shape_keys':2})
assert rows[0]['protected_points']>rows[1]['protected_points'] and rows[0]['vertices']==rows[1]['vertices']
folder=Path(sys.argv[sys.argv.index('--')+1]);folder.mkdir(exist_ok=True,parents=True)
(folder/'hair_protection_native.json').write_text(json.dumps({'complete':True,'rows':rows},indent=2),encoding='utf8')
bpy.ops.wm.save_as_mainfile(filepath=str(folder/'HairProtectionFixtures.blend'))
print('HAIR_PROTECTION_NATIVE_READY',rows,flush=True)
