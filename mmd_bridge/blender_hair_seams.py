"""Exact, deform-compatible UV seams for optional hair subdivision candidates.

This never proximity-welds sheets. Only two matching boundary edges with smooth
authored normals, split UVs and identical exported deformation data may join.
"""
from collections import defaultdict
import math
from functools import lru_cache

VERSION=1


@lru_cache(maxsize=4)
def uv_morph_evidence(path, source_sha256):
    """Optional source evidence, cached by the caller's verified model identity."""
    from pathlib import Path
    from .pmx_binary import ByteReader
    from .pmx_reader import _read_header,_read_vertices,_read_faces,_read_textures,_read_materials,_read_bones,_read_morphs
    reader=ByteReader(Path(path).read_bytes());reader.skip(4);header=_read_header(reader)
    for _ in range(4):reader.read_text()
    counts=_read_vertices(reader,header);_read_faces(reader,header);_read_textures(reader);_read_materials(reader,header);_read_bones(reader,header)
    return {'vertex_count':sum(counts.values()),'uv_vertices':frozenset(_read_morphs(reader,header,collect_uv_vertices=True))}


def seam_pairs(points, boundaries, signatures):
    """Return accepted boundary pairs; ambiguity or different morphs stay split."""
    if any(not math.isfinite(v) for p in points for v in p):raise ValueError('Nonfinite hair seam position')
    groups=defaultdict(list)
    for edge in boundaries:
        a,b=edge['vertices'];groups[tuple(sorted((tuple(points[a]),tuple(points[b]))))].append(edge)
    accepted=[];rejected=defaultdict(int)
    def same_normal(a,b):
        length_a=math.sqrt(sum(v*v for v in a));length_b=math.sqrt(sum(v*v for v in b))
        return length_a>1e-12 and length_b>1e-12 and sum(x*y for x,y in zip(a,b))/(length_a*length_b)>1.-1e-7
    for edges in groups.values():
        if len(edges)==1:continue
        if len(edges)!=2:rejected['ambiguous_edges']+=1;continue
        a,b=edges;x,y=a['vertices'];u,v=b['vertices']
        if x==v and y==u:continue
        if tuple(points[x])!=tuple(points[v]) or tuple(points[y])!=tuple(points[u]):rejected['winding']+=1;continue
        if a.get('protected') or b.get('protected'):rejected['authored_protection']+=1;continue
        if a.get('uv_morph') or b.get('uv_morph'):rejected['uv_morph']+=1;continue
        if any(not math.isfinite(value) for e in (a,b) for uv in e['uvs'] for value in uv):rejected['invalid_uv']+=1;continue
        if any(signatures[i]!=signatures[j] for i,j in ((x,v),(y,u))):rejected['deformation']+=1;continue
        if sum(i*j for i,j in zip(a['normal'],b['normal']))<math.cos(math.pi/4):rejected['face_angle']+=1;continue
        if not same_normal(a['normals'][0],b['normals'][1]) or not same_normal(a['normals'][1],b['normals'][0]):rejected['authored_normals']+=1;continue
        if a['uvs'][0]==b['uvs'][1] and a['uvs'][1]==b['uvs'][0]:rejected['not_a_uv_seam']+=1;continue
        accepted.append(((x,v),(y,u)))
    return accepted,dict(rejected)


def stitch_hair_seams(part, evidence=None):
    """Bake verified seam joins on an independent contour input, retaining keys."""
    import bpy,bmesh,numpy as np
    from .blender_contour import _coordinates,_copy_animation,_key_properties
    mesh=part.data;keys=list(mesh.shape_keys.key_blocks) if mesh.shape_keys else []
    if not mesh.uv_layers.active:return {'version':VERSION,'changed':False,'reason':'no_uv_layer','source':part.name}
    positions=np.asarray(_coordinates(keys[0].data if keys else mesh.vertices)).reshape(-1,3)
    shapes=np.stack([np.asarray(_coordinates(k.data)).reshape(-1,3) for k in keys],axis=1) if keys else positions[:,None,:]
    if not np.isfinite(shapes).all():raise ValueError('Nonfinite hair shape coordinates')
    identity_groups={g.index for g in part.vertex_groups if g.name=='mmd_vertex_order'}
    original_groups=[dict((g.group,float(g.weight)) for g in v.groups) for v in mesh.vertices]
    uv_morphs=(evidence or {}).get('uv_vertices',())
    affected=set()
    if uv_morphs:
        if len(identity_groups)!=1:return {'version':VERSION,'changed':False,'needs_review':'UV morph source identity is unavailable','source':part.name}
        identity=next(iter(identity_groups));count=evidence['vertex_count']
        for i,groups in enumerate(original_groups):
            if identity not in groups or abs(groups[identity]*count-round(groups[identity]*count))>.1:
                return {'version':VERSION,'changed':False,'needs_review':'UV morph vertex identity is ambiguous','source':part.name}
            if round(groups[identity]*count) in uv_morphs:affected.add(i)
    def attribute_value(item):
        for name in ('vector','color','value'):
            if hasattr(item,name):
                value=getattr(item,name)
                return tuple(value) if hasattr(value,'__iter__') and not isinstance(value,(str,bytes)) else value
        raise ValueError('Unsupported hair seam point attribute: '+str(type(item)))
    point_attributes={a.name:[attribute_value(v) for v in a.data] for a in mesh.attributes
                      if a.domain=='POINT' and a.name not in ('position','_mmdb_surface_vertex')}
    signatures=[(shapes[i].tobytes(),tuple(sorted((g.group,float(g.weight)) for g in v.groups if g.group not in identity_groups)),
                 tuple((name,values[i]) for name,values in point_attributes.items())) for i,v in enumerate(mesh.vertices)]
    edge_faces=defaultdict(list)
    for p in mesh.polygons:
        loops=list(p.loop_indices)
        for a,b in zip(loops,loops[1:]+loops[:1]):
            x,y=mesh.loops[a].vertex_index,mesh.loops[b].vertex_index
            edge_faces[tuple(sorted((x,y)))].append((x,y,a,b,p))
    crease=mesh.attributes.get('crease_edge');corner=mesh.attributes.get('crease_vert');boundaries=[]
    for edge in mesh.edges:
        faces=edge_faces[tuple(sorted(edge.vertices))]
        if len(faces)!=1:continue
        x,y,a,b,p=faces[0]
        boundaries.append({'vertices':(x,y),'normal':tuple(p.normal),
            'normals':(tuple(mesh.corner_normals[a].vector),tuple(mesh.corner_normals[b].vector)),
            'uvs':(tuple(mesh.uv_layers.active.data[a].uv),tuple(mesh.uv_layers.active.data[b].uv)),
            'uv_morph':x in affected or y in affected,
            'protected':edge.use_edge_sharp or bool(crease and crease.data[edge.index].value>0) or bool(corner and (corner.data[x].value>0 or corner.data[y].value>0))})
    pairs,rejected=seam_pairs(positions,boundaries,signatures)
    report={'version':VERSION,'source_vertices':len(mesh.vertices),'exact_boundary_pairs':len(pairs),'rejected':rejected,
            'position_tolerance_m':0.,'all_shape_keys_checked':len(keys),'all_nonidentity_vertex_groups_checked':True,
            'identity_groups_retained_via_representative_and_backup':[part.vertex_groups[i].name for i in identity_groups],'source':part.name}
    if not pairs:return dict(report,changed=False,vertices_after=len(mesh.vertices))
    parent=list(range(len(mesh.vertices)))
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for pair in pairs:
        for a,b in pair:
            a,b=find(a),find(b);parent[max(a,b)]=min(a,b)
    representatives=[find(i) for i in range(len(parent))]
    for i,rep in enumerate(representatives):assert signatures[i]==signatures[rep],'Incompatible seam union'
    temporary=part.copy();temporary.data=mesh.copy();bpy.context.scene.collection.objects.link(temporary)
    if temporary.data.shape_keys:temporary.shape_key_clear()
    temporary.data.vertices.foreach_set('co',positions.ravel())
    vertex_id='_mmdb_seam_vertex';face_id='_mmdb_seam_face'
    assert not temporary.data.attributes.get(vertex_id) and not temporary.data.attributes.get(face_id)
    temporary.data.attributes.new(vertex_id,'INT','POINT').data.foreach_set('value',np.arange(len(mesh.vertices),dtype=np.int32))
    temporary.data.attributes.new(face_id,'INT','FACE').data.foreach_set('value',np.arange(len(mesh.polygons),dtype=np.int32))
    bm=bmesh.new()
    try:
        bm.from_mesh(temporary.data);bm.verts.ensure_lookup_table()
        bmesh.ops.weld_verts(bm,targetmap={bm.verts[i]:bm.verts[rep] for i,rep in enumerate(representatives) if i!=rep})
        bm.to_mesh(temporary.data)
    finally:bm.free()
    data=temporary.data;data.update()
    origins=[v.value for v in data.attributes[vertex_id].data]
    assert set(origins)==set(representatives) and len(origins)==len(set(origins))
    assert [v.value for v in data.attributes[face_id].data]==list(range(len(mesh.polygons))),'Seam weld changed face order or removed faces'
    mapping={old:i for i,old in enumerate(origins)};collapsed=[mapping[rep] for rep in representatives]
    for old,new in zip(mesh.polygons,data.polygons):
        assert tuple(collapsed[i] for i in old.vertices)==tuple(new.vertices),'Seam weld changed polygon winding'
    for layer in mesh.uv_layers:
        assert [tuple(v.uv) for v in layer.data]==[tuple(v.uv) for v in data.uv_layers[layer.name].data],'Seam weld altered UV corners'
    for index,old in enumerate(origins):
        assert tuple(sorted((g.group,float(g.weight)) for g in data.vertices[index].groups if g.group not in identity_groups))==signatures[old][1]
        for name,values in point_attributes.items():
            assert data.attributes.get(name) and attribute_value(data.attributes[name].data[index])==values[old],('Seam weld altered point data',name)
    for group in identity_groups:
        temporary.vertex_groups[group].remove(list(range(len(data.vertices))))
        for index,old in enumerate(origins):
            if group in original_groups[old]:temporary.vertex_groups[group].add([index],original_groups[old][group],'REPLACE')
    for number,key in enumerate(keys):
        new=temporary.shape_key_add(name=key.name,from_mix=False);new.data.foreach_set('co',shapes[origins,number,:].ravel())
    if keys:
        target=data.shape_keys;target.use_relative=mesh.shape_keys.use_relative;target.eval_time=mesh.shape_keys.eval_time
        for key in keys:
            new=target.key_blocks[key.name];_key_properties(key,new);new.relative_key=target.key_blocks[key.relative_key.name]
        _copy_animation(mesh.shape_keys,target,{mesh.shape_keys:target,part:part})
    # Retain the exact input for review; the user's full original was never edited.
    backup=part.copy();backup.name=part.name+'_BeforeVerifiedSeams';bpy.context.scene.collection.objects.link(backup)
    backup.hide_render=True;backup.hide_set(True)
    data.attributes.remove(data.attributes[vertex_id]);data.attributes.remove(data.attributes[face_id])
    part.data=data;bpy.data.objects.remove(temporary,do_unlink=True)
    report.update(changed=True,vertices_after=len(data.vertices),removed_duplicate_vertices=len(mesh.vertices)-len(data.vertices),
                  source_to_welded_vertex=collapsed,UV_corners_preserved=True,material_faces_preserved=True,
                  morph_relative_keys_preserved=True,input_backup=backup.name)
    return report
