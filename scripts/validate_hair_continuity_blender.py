"""Native fixtures: coherent pre-triangle fields, close sheets and sharp fans."""
from pathlib import Path
import sys,math,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np
from mmd_bridge.blender_hair_normals import prepare_hair_normals,smooth_fans
from mmd_bridge.blender_surface_repair import triangulate_export

bpy.ops.wm.read_factory_settings(use_empty=True)
rows=[]
for label,double,sharp in [('CurvedStrip',False,False),('CloseOppositeSheets',True,False),('AuthoredHardEdge',False,True)]:
    points=[(math.sin(x*.2)*.02,y*.01,math.cos(x*.2)*.02) for y in range(6) for x in range(7)]
    faces=[(y*7+x,y*7+x+1,(y+1)*7+x+1,(y+1)*7+x) for y in range(5) for x in range(6)]
    front=len(faces);front_vertices=len(points)
    if double:
        points+=[(x,y,z-.00001) for x,y,z in points]
        faces+=[tuple(i+front_vertices for i in reversed(face)) for face in faces]
    mesh=bpy.data.meshes.new(label);mesh.from_pydata(points,[],faces)
    material=bpy.data.materials.new(label+'Hair');mesh.materials.append(material)
    for p in mesh.polygons:p.use_smooth=True
    if sharp:
        for edge in mesh.edges:
            a,b=edge.vertices
            if a%7==3 and b%7==3:edge.use_edge_sharp=True
    uv=mesh.uv_layers.new(name='UVMap')
    for p in mesh.polygons:
        for loop in p.loop_indices:
            v=mesh.loops[loop].vertex_index%front_vertices;x,y=v%7,v//7
            uv.data[loop].uv=((x/6 if not double or p.index<front else 1-x/6),y/5)
    obj=bpy.data.objects.new(label,mesh);bpy.context.scene.collection.objects.link(obj)
    obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Bend')
    for p in key.data:p.co.y+=p.co.z*.1
    before=np.array([p.co[:] for p in key.data]);uv_before=np.array([p.uv[:] for p in uv.data])
    normals,audit=prepare_hair_normals(obj,{material.name})
    assert audit['parts'][0]['proxy_after']['vertices']==len(points),'Nearby disconnected sheets were welded'
    assert not audit['parts'][0]['angular_threshold_used_for_selection']
    mesh.normals_split_custom_set(normals);mesh.update()
    actual=np.array([n.vector[:] for n in mesh.corner_normals])
    fans=smooth_fans([tuple(p.vertices) for p in mesh.polygons],[True]*len(faces),[e.key for e in mesh.edges if e.use_edge_sharp])
    max_spread=max(float(np.max(np.linalg.norm(actual[fan]-actual[fan[0]],axis=1))) for fan in fans)
    assert max_spread<1e-5,(label,max_spread)
    if double:
        front_normal=np.mean(actual[:front*4],axis=0);back_normal=np.mean(actual[front*4:],axis=0)
        assert float(front_normal@back_normal)<-.5
    mesh.calc_loop_triangles();loop_map=[i for t in mesh.loop_triangles for i in t.loops]
    expected=actual[loop_map].copy()
    out,tri=triangulate_export(obj)
    after=np.array([n.vector[:] for n in out.data.corner_normals])
    assert np.max(np.linalg.norm(expected-after,axis=1))<2e-5
    assert np.array_equal(before,np.array([p.co[:] for p in out.data.shape_keys.key_blocks['Bend'].data]))
    assert np.array_equal(uv_before[loop_map],np.array([p.uv[:] for p in out.data.uv_layers.active.data]))
    out.data.calc_tangents(uvmap='UVMap')
    tangent=np.array([p.tangent[:] for p in out.data.loops]);sign=np.array([p.bitangent_sign for p in out.data.loops])
    assert np.isfinite(tangent).all() and np.max(np.abs(np.sum(tangent*after,axis=1)))<1e-4
    rows.append({'fixture':label,'faces_before':len(faces),'triangles':len(out.data.polygons),'max_smooth_fan_spread':max_spread,
                 'normal_freeze_max_error':float(np.max(np.linalg.norm(expected-after,axis=1))),
                 'handedness':sorted(set(sign.tolist())),'shapes_uv_preserved':True,'audit':audit})
output=Path(sys.argv[sys.argv.index('--')+1]);output.mkdir(parents=True,exist_ok=True)
(output/'hair_native.json').write_text(json.dumps({'complete':True,'fixtures':rows},indent=2),encoding='utf8')
bpy.ops.wm.save_as_mainfile(filepath=str(output/'HairFixtures.blend'))
print('HAIR_CONTINUITY_NATIVE_READY',len(rows),flush=True)
