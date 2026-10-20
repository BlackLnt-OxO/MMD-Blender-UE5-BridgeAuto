"""Opt-in contour experiments before the existing morph-safe normal finishing."""
from array import array
from collections import Counter
import hashlib
import math

VERSION = 2
SOFT_SURFACES = {'skin', 'face', 'cloth', 'satin', 'hosiery'}


def sharp_protection(coordinates, faces, *, sharp_edges=(), soft_surface=False,
                     edge_creases=(), vertex_creases=()):
    """Identify protection once on the authored Basis, before normal finishing."""
    points=[tuple(map(float,p)) for p in coordinates]
    if not all(math.isfinite(x) for p in points for x in p):
        raise ValueError('Non-finite contour coordinates')
    faces=[tuple(f) for f in faces];edge_faces={};incident=[set() for _ in points]
    angle_sums=[0.]*len(points);normals=[];invalid=set()
    def subtract(a,b):return tuple(x-y for x,y in zip(a,b))
    def dot(a,b):return sum(x*y for x,y in zip(a,b))
    def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    for number,face in enumerate(faces):
        normal=[0.,0.,0.]
        for a,b in zip(face,face[1:]+face[:1]):
            edge=tuple(sorted((a,b)));edge_faces.setdefault(edge,[]).append(number)
            value=cross(points[a],points[b])
            normal=[x+y for x,y in zip(normal,value)]
        length=math.sqrt(dot(normal,normal));bad=length<1e-14
        normals.append(tuple(x/length for x in normal) if not bad else (0.,0.,0.))
        for j,index in enumerate(face):
            incident[index].add(number)
            a=subtract(points[face[j-1]],points[index]);b=subtract(points[face[(j+1)%len(face)]],points[index])
            scale=math.sqrt(dot(a,a)*dot(b,b))
            if scale<1e-16:bad=True
            else:angle_sums[index]+=math.acos(max(-1.,min(1.,dot(a,b)/scale)))
        if bad:invalid.add(number)
    edge_values={tuple(sorted((a,b))):float(w) for a,b,w in edge_creases if w>0}
    vertex_values={int(i):float(w) for i,w in vertex_creases if w>0}
    sources={name:{'edges':[],'vertices':[]} for name in ('boundary','authored_sharp','authored_crease','dihedral','closed_tip')}
    sources['authored_crease']['edges']=[list(e) for e in sorted(edge_values)]
    sources['authored_crease']['vertices']=sorted(vertex_values)
    for edge,weight in edge_values.items():
        if weight>=1.:
            for index in edge:vertex_values[index]=1.
    def protect(edge,reason):
        edge_values[edge]=1.;sources[reason]['edges'].append(list(edge))
        for index in edge:vertex_values[index]=1.
    for edge,numbers in edge_faces.items():
        if len(numbers)==1:protect(edge,'boundary')
    for edge in sorted({tuple(sorted(e)) for e in sharp_edges}&set(edge_faces)):
        protect(edge,'authored_sharp')
    for edge,numbers in edge_faces.items():
        if soft_surface or len(numbers)!=2 or any(i in invalid for i in numbers):continue
        if dot(normals[numbers[0]],normals[numbers[1]])<=math.cos(math.pi/4)+1e-10:
            protect(edge,'dihedral')
    nonmanifold={v for edge,numbers in edge_faces.items() if len(numbers)!=2 for v in edge}
    invalid_vertices={v for i in invalid for v in faces[i]}
    ambiguous=[]
    for index,numbers in enumerate(incident):
        if index in nonmanifold or index in invalid_vertices or len(numbers)<3 or angle_sums[index]>math.pi+1e-10:continue
        neighbours={i:set() for i in numbers}
        for i in numbers:
            face=faces[i];j=face.index(index)
            for other in (face[j-1],face[(j+1)%len(face)]):
                neighbours[i].update(edge_faces[tuple(sorted((index,other)))])
        pending=[next(iter(numbers))];visited=set()
        while pending:
            i=pending.pop()
            if i not in visited:visited.add(i);pending.extend(neighbours[i]-visited)
        if visited!=numbers:ambiguous.append(index);continue
        vertex_values[index]=1.;sources['closed_tip']['vertices'].append(index)
    return {'edge_creases':[[*e,w] for e,w in sorted(edge_values.items())],
            'vertex_creases':[[i,w] for i,w in sorted(vertex_values.items())],
            'full_vertices':sorted(i for i,w in vertex_values.items() if w>=1.),'sources':sources,
            'issues':{'degenerate_faces':sorted(invalid),'nonmanifold_edges':[list(e) for e,n in edge_faces.items() if len(n)>2],
                      'ambiguous_vertex_fans':ambiguous},'soft_surface':bool(soft_surface),'dihedral_degrees':45.,'tip_angle_sum_degrees':180.}


def smoothing_mask(vertex_count, faces, selected_materials, rings=2):
    """Keep each selected material's open border and neighbouring rings fixed."""
    movable=set()
    for material in selected_materials:
        selected=[vertices for index,vertices in faces if index==material]
        edges=Counter(tuple(sorted((a,b))) for vertices in selected
                      for a,b in zip(vertices,vertices[1:]+vertices[:1]))
        vertices={v for face in selected for v in face}
        pinned={v for edge,count in edges.items() if count!=2 for v in edge}
        neighbours={v:set() for v in vertices}
        for a,b in edges:
            neighbours[a].add(b);neighbours[b].add(a)
        for _ in range(rings):
            pinned |= {v for original in tuple(pinned) for v in neighbours[original]}
        movable |= vertices-pinned
    return [index in movable for index in range(vertex_count)]


def smooth_coordinates(coordinates, edges, mask, factor=.15, iterations=2):
    """Uniform neighbour smoothing with a fixed selection, without new vertices."""
    if not 0 <= factor <= 1 or iterations<0:
        raise ValueError('Invalid contour smoothing settings')
    neighbours=[[] for _ in coordinates]
    for a,b in edges:
        neighbours[a].append(b);neighbours[b].append(a)
    result=[tuple(p) for p in coordinates]
    movable=[i for i,enabled in enumerate(mask) if enabled and neighbours[i]]
    for _ in range(iterations):
        previous=result;result=list(previous)
        for i in movable:
            result[i]=tuple(previous[i][axis]*(1-factor)+factor*sum(previous[j][axis] for j in neighbours[i])/len(neighbours[i]) for axis in range(3))
    return result


def _coordinates(items):
    values=array('f',[0.])*(len(items)*3);items.foreach_get('co',values)
    return values


def _material_fingerprints(obj, indices):
    import numpy as np
    result={}
    for index in indices:
        polygons=[p for p in obj.data.polygons if p.material_index==index]
        loops=np.array([loop for p in polygons for loop in p.loop_indices],dtype=np.int64)
        vertices=np.array([obj.data.loops[int(loop)].vertex_index for loop in loops],dtype=np.int64)
        digest=hashlib.sha256(repr([(len(p.vertices),p.use_smooth) for p in polygons]).encode())
        digest.update(np.asarray(_coordinates(obj.data.vertices)).reshape(-1,3)[vertices].tobytes())
        for layer in obj.data.uv_layers:
            values=array('f',[0.])*(len(layer.data)*2);layer.data.foreach_get('uv',values)
            digest.update(np.asarray(values).reshape(-1,2)[loops].tobytes())
        if obj.data.shape_keys:
            for key in obj.data.shape_keys.key_blocks:
                digest.update((key.name+'|'+key.relative_key.name).encode())
                digest.update(np.asarray(_coordinates(key.data)).reshape(-1,3)[vertices].tobytes())
        # Group storage order is not semantic and can change when pieces join.
        digest.update(repr([sorted((g.group,g.weight) for g in obj.data.vertices[int(v)].groups) for v in vertices]).encode())
        result[index]=digest.hexdigest()
    return result


def _key_properties(source, target):
    for key in ('slider_min','slider_max','value','mute','interpolation','vertex_group'):
        setattr(target,key,getattr(source,key))


def _copy_rna(source,target,skip=(),remap=None):
    for prop in source.bl_rna.properties:
        key=prop.identifier
        if key in {'rna_type',*skip} or prop.is_readonly or prop.type=='COLLECTION':continue
        value=getattr(source,key)
        if remap and prop.type=='POINTER':value=remap.get(value,value)
        if getattr(prop,'is_array',False):value=list(value)
        setattr(target,key,value)


def _copy_animation(source,target,remap):
    if not source.animation_data:return
    old=source.animation_data;new=target.animation_data_create()
    if old.action:
        new.action=old.action
        slot=getattr(old,'action_slot',None)
        if slot:new.action_slot=slot
    assert not old.nla_tracks,'NLA key animation needs a separate export path'
    for original in old.drivers:
        curve=target.driver_add(original.data_path)
        if isinstance(curve,list):curve=curve[original.array_index]
        _copy_rna(original,curve,{'data_path','array_index','driver','group'})
        _copy_rna(original.driver,curve.driver)
        for variable in list(curve.driver.variables):curve.driver.variables.remove(variable)
        for variable in original.driver.variables:
            new_variable=curve.driver.variables.new();new_variable.type=variable.type;new_variable.name=variable.name
            for a,b in zip(variable.targets,new_variable.targets):_copy_rna(a,b,remap=remap)
        for modifier in list(curve.modifiers):curve.modifiers.remove(modifier)
        for modifier in original.modifiers:
            _copy_rna(modifier,curve.modifiers.new(modifier.type))


def _subdivide_part(part, levels=1, boundary_policy='protected', surface_type='generic', hair_protection='legacy', hair_seams=None, hair_normal_mode='surface'):
    import bpy
    import numpy as np
    seam_report=None
    if surface_type=='hair' and hair_seams:
        from .blender_hair_seams import stitch_hair_seams
        seam_report=stitch_hair_seams(part,hair_seams.get('evidence'))
    keys=list(part.data.shape_keys.key_blocks) if part.data.shape_keys else []
    basis=_coordinates(keys[0].data if keys else part.data.vertices)
    points=np.asarray(basis).reshape(-1,3)
    faces=[tuple(p.vertices) for p in part.data.polygons]
    def authored(name,domain):
        attr=part.data.attributes.get(name)
        if not attr:return []
        if domain=='EDGE':return [[*part.data.edges[i].vertices,v.value] for i,v in enumerate(attr.data) if v.value>0]
        return [[i,v.value] for i,v in enumerate(attr.data) if v.value>0]
    original_edges=authored('crease_edge','EDGE');original_vertices=authored('crease_vert','POINT')
    protection=sharp_protection(points,faces,sharp_edges=[tuple(e.vertices) for e in part.data.edges if e.use_edge_sharp],
        soft_surface=surface_type in SOFT_SURFACES or (surface_type=='hair' and hair_protection=='authored'),
        edge_creases=original_edges,vertex_creases=original_vertices)
    if surface_type=='hair':protection['hair_protection_policy']=hair_protection
    cage_normals=None
    if surface_type=='hair' and hair_normal_mode=='cage':
        from .blender_hair_normals import coarse_weighted_reference
        cage_normals=coarse_weighted_reference(part)
    if boundary_policy=='protected':
        crease=part.data.attributes.get('crease_edge') or part.data.attributes.new('crease_edge','FLOAT','EDGE')
        lookup={tuple(sorted(e.vertices)):e.index for e in part.data.edges}
        for a,b,w in protection['edge_creases']:crease.data[lookup[(a,b)]].value=w
        corners=part.data.attributes.get('crease_vert') or part.data.attributes.new('crease_vert','FLOAT','POINT')
        for i,w in protection['vertex_creases']:corners.data[i].value=w
        pinned=np.asarray(protection['full_vertices'],dtype=np.int64)
    else:
        # Native groups keep authored information but add no tool-generated constraints.
        pinned=np.empty(0,dtype=np.int64)
    proxy=part.copy();proxy.data=part.data.copy();proxy.name='Contour_Evaluation'
    bpy.context.scene.collection.objects.link(proxy)
    if proxy.data.shape_keys:proxy.shape_key_clear()
    proxy.data.vertices.foreach_set('co',basis)
    for modifier in list(proxy.modifiers):proxy.modifiers.remove(modifier)
    if cage_normals is not None:proxy.data.normals_split_custom_set(cage_normals);proxy.data.update()
    token_name='_mmdb_contour_identity'
    assert not proxy.data.attributes.get(token_name)
    tokens=np.random.default_rng(104729).random((len(points),3),dtype=np.float32)
    token=proxy.data.attributes.new(token_name,'FLOAT_VECTOR','POINT');token.data.foreach_set('vector',tokens.ravel())
    modifier=proxy.modifiers.new('MMDBridge_Contour_Subdivision','SUBSURF')
    modifier.subdivision_type='CATMULL_CLARK';modifier.levels=modifier.render_levels=levels
    modifier.uv_smooth='PRESERVE_BOUNDARIES'
    if cage_normals is not None and not hasattr(modifier,'use_custom_normals'):raise ValueError('Blender does not support cage normal interpolation')
    if hasattr(modifier,'use_custom_normals'):modifier.use_custom_normals=cage_normals is not None
    def evaluate(values,kind='CATMULL_CLARK'):
        modifier.subdivision_type=kind
        proxy.data.vertices.foreach_set('co',values);proxy.data.update();bpy.context.view_layer.update()
        graph=bpy.context.evaluated_depsgraph_get()
        return bpy.data.meshes.new_from_object(proxy.evaluated_get(graph),preserve_all_data_layers=True,depsgraph=graph)
    def topology(data):
        values=np.empty(len(data.loops),dtype=np.int32);data.loops.foreach_get('vertex_index',values)
        counts=np.empty(len(data.polygons),dtype=np.int32);data.polygons.foreach_get('loop_total',counts)
        return hashlib.sha256(values.tobytes()+counts.tobytes()).hexdigest()
    output=evaluate(basis);signature=topology(output)
    linear=evaluate(basis,'SIMPLE');assert signature==topology(linear)
    result_tokens=np.empty(len(linear.vertices)*3,dtype=np.float32)
    linear.attributes[token_name].data.foreach_get('vector',result_tokens)
    dtype=np.dtype((np.void,12))
    _,source_ids,output_ids=np.intersect1d(tokens.view(dtype).ravel(),result_tokens.reshape(-1,3).view(dtype).ravel(),return_indices=True)
    mapping=np.full(len(points),-1,dtype=np.int64);mapping[source_ids]=output_ids
    assert np.all(mapping>=0),'Original vertex mapping was lost during subdivision'
    scale=np.asarray(part.matrix_world.to_3x3(),dtype=np.float64)
    maximum_pin=0.
    def verify_pins(values,processed):
        nonlocal maximum_pin
        if not len(pinned):return
        expected=np.asarray(values).reshape(-1,3)[pinned]
        actual=np.asarray(processed).reshape(-1,3)[mapping[pinned]]
        error=np.linalg.norm((actual-expected)@scale.T,axis=1).max(initial=0)
        maximum_pin=max(maximum_pin,float(error))
        assert error<=1e-6,('Protected vertex moved over 0.001 mm',part.name,error)
    out_basis=_coordinates(output.vertices);verify_pins(basis,out_basis)
    delta=(np.asarray(out_basis).reshape(-1,3)-np.asarray(_coordinates(linear.vertices)).reshape(-1,3))@scale.T
    displacement=np.linalg.norm(delta,axis=1)
    original_delta=(np.asarray(out_basis).reshape(-1,3)[mapping]-points)@scale.T
    report={'vertices_before':len(part.data.vertices),'vertices_after':len(output.vertices),
        'faces_before':len(part.data.polygons),'faces_after':len(output.polygons),
        'max_displacement_from_linear_m':float(displacement.max(initial=0)),
        'mean_original_vertex_displacement_m':float(np.linalg.norm(original_delta,axis=1).mean()),
        'max_original_vertex_displacement_m':float(np.linalg.norm(original_delta,axis=1).max(initial=0)),
        'level':levels,'boundary_policy':boundary_policy,'surface_type':surface_type,
        'protection':protection if boundary_policy=='protected' else {'authored_edge_creases':original_edges,'authored_vertex_creases':original_vertices},
        'protected_vertex_coordinates':[[int(i),*map(float,points[i])] for i in pinned],
        'original_vertex_output_mapping':mapping.tolist(),
        'open_boundary_vertices':len({v for e in protection['sources']['boundary']['edges'] for v in e})}
    if seam_report is not None:report['hair_seams']=seam_report
    if cage_normals is not None:report['hair_normal_reference']={'mode':'coarse_weld_weighted_interpolation','corners':len(cage_normals),'weighted_mode':'FACE_AREA','weighted_weight':50,'weld_distance_m':.0001}
    bpy.data.meshes.remove(linear)
    cache={hashlib.sha256(basis).hexdigest():out_basis};shape_coordinates={}
    for key in keys:
        values=_coordinates(key.data);digest=hashlib.sha256(values).hexdigest()
        if digest not in cache:
            evaluated=evaluate(values);assert signature==topology(evaluated),key.name
            cache[digest]=_coordinates(evaluated.vertices);bpy.data.meshes.remove(evaluated)
        verify_pins(values,cache[digest]);shape_coordinates[key.name]=cache[digest]
    report.update(unique_key_evaluations=len(cache),max_protected_error_m=maximum_pin,
                  shape_key_names=[key.name for key in keys])
    output.attributes.remove(output.attributes[token_name])
    proxy_data=proxy.data;bpy.data.objects.remove(proxy,do_unlink=True);bpy.data.meshes.remove(proxy_data)
    # Keep editable inputs with the actual protection attributes and modifier order.
    editable=bpy.data.collections.get('MMDBridge_Editable_Contour')
    if editable is None:
        editable=bpy.data.collections.new('MMDBridge_Editable_Contour');bpy.context.scene.collection.children.link(editable)
    for collection in list(part.users_collection):collection.objects.unlink(part)
    editable.objects.link(part)
    preview=part.modifiers.new('MMDBridge_Contour_Subdivision','SUBSURF')
    preview.subdivision_type='CATMULL_CLARK';preview.levels=preview.render_levels=levels
    preview.uv_smooth='PRESERVE_BOUNDARIES'
    if cage_normals is not None:
        part.data.normals_split_custom_set(cage_normals);part.data.update()
    if hasattr(preview,'use_custom_normals'):preview.use_custom_normals=cage_normals is not None
    part.modifiers.move(part.modifiers.find(preview.name),0)
    return output,shape_coordinates,report


def _assemble(source,parts):
    """Join evaluated pieces using bulk arrays, including every morph and weight."""
    import bpy
    import numpy as np
    coordinates=array('f');edges=array('i');loops=array('i');starts=array('i');counts=array('i')
    material_indices=array('i');smooth=array('b');sharp=array('b');seams=array('b');normals=array('f')
    uvs={uv.name:array('f') for uv in source.data.uv_layers}
    creases=array('f');corners=array('f');offset=loop_offset=0
    lookup={material.name:index for index,material in enumerate(source.data.materials) if material}
    def values(items,prop,width=1,kind='i'):
        data=array(kind,[0])*(len(items)*width);items.foreach_get(prop,data);return data
    for part,data,_ in parts:
        coordinates.extend(_coordinates(data.vertices))
        edges.extend(int(v)+offset for v in values(data.edges,'vertices',2))
        loops.extend(int(v)+offset for v in values(data.loops,'vertex_index'))
        starts.extend(int(v)+loop_offset for v in values(data.polygons,'loop_start'))
        counts.extend(values(data.polygons,'loop_total'))
        material_indices.extend(lookup[part.data.materials[i].name] for i in values(data.polygons,'material_index'))
        smooth.extend(values(data.polygons,'use_smooth',kind='b'))
        sharp.extend(values(data.edges,'use_edge_sharp',kind='b'));seams.extend(values(data.edges,'use_seam',kind='b'))
        normals.extend(values(data.corner_normals,'vector',3,'f'))
        for name,buffer in uvs.items():
            layer=data.uv_layers.get(name);assert layer is not None,(part.name,name)
            buffer.extend(values(layer.data,'uv',2,'f'))
        for name,items,buffer in [('crease_edge',data.edges,creases),('crease_vert',data.vertices,corners)]:
            attr=data.attributes.get(name)
            buffer.extend(values(attr.data,'value',kind='f') if attr else array('f',[0.])*len(items))
        offset+=len(data.vertices);loop_offset+=len(data.loops)
    mesh=bpy.data.meshes.new(source.data.name+'_ContourSubdivision')
    mesh.vertices.add(len(coordinates)//3);mesh.vertices.foreach_set('co',coordinates)
    mesh.edges.add(len(edges)//2);mesh.edges.foreach_set('vertices',edges)
    mesh.loops.add(len(loops));mesh.loops.foreach_set('vertex_index',loops)
    mesh.polygons.add(len(counts));mesh.polygons.foreach_set('loop_start',starts);mesh.polygons.foreach_set('loop_total',counts)
    for material in source.data.materials:mesh.materials.append(material)
    mesh.polygons.foreach_set('material_index',material_indices);mesh.polygons.foreach_set('use_smooth',smooth)
    mesh.update(calc_edges=True)
    mesh.edges.foreach_set('use_edge_sharp',sharp);mesh.edges.foreach_set('use_seam',seams)
    for name,buffer in uvs.items():mesh.uv_layers.new(name=name).data.foreach_set('uv',buffer)
    for name,domain,buffer in [('crease_edge','EDGE',creases),('crease_vert','POINT',corners)]:
        mesh.attributes.new(name,'FLOAT',domain).data.foreach_set('value',buffer)
    mesh.normals_split_custom_set(np.asarray(normals).reshape(-1,3))
    # Candidate-only source-domain identities; absent on every legacy path.
    for name,domain in (('_mmdb_surface_face','FACE'),('_mmdb_surface_vertex','POINT')):
        if not source.data.attributes.get(name):continue
        buffer=array('i')
        for _,data,_ in parts:
            attr=data.attributes.get(name)
            assert attr is not None,('Lost surface identity',name)
            buffer.extend(values(attr.data,'value'))
        mesh.attributes.new(name,'INT',domain).data.foreach_set('value',buffer)
    result=source.copy();result.data=mesh;result.name=source.name+'_ContourSubdivision'
    bpy.context.scene.collection.objects.link(result)
    result.vertex_groups.clear()
    for group in source.vertex_groups:
        copied=result.vertex_groups.new(name=group.name);copied.lock_weight=group.lock_weight
    offset=0
    for part,data,_ in parts:
        batches={}
        for vertex in data.vertices:
            for membership in vertex.groups:
                batches.setdefault((membership.group,float(membership.weight)),[]).append(vertex.index+offset)
        for (group,weight),indices in batches.items():result.vertex_groups[group].add(indices,weight,'REPLACE')
        offset+=len(data.vertices)
    if source.data.shape_keys:
        original=source.data.shape_keys
        for old in original.key_blocks:
            buffer=array('f')
            for part,_,key_coordinates in parts:
                points=key_coordinates.get(old.name)
                if points is None:points=_coordinates(part.data.shape_keys.key_blocks[old.name].data)
                buffer.extend(points)
            key=result.shape_key_add(name=old.name,from_mix=False);key.data.foreach_set('co',buffer)
        target=result.data.shape_keys;target.use_relative=original.use_relative;target.eval_time=original.eval_time
        for old in original.key_blocks:
            key=target.key_blocks[old.name];_key_properties(old,key);key.relative_key=target.key_blocks[old.relative_key.name]
        _copy_animation(original,target,{original:target,source:result})
        assert [k.name for k in target.key_blocks]==[k.name for k in original.key_blocks]
    result.data.update()
    return result


def prepare_contour(meshes, mode='none', material_indices=(), *, levels=1, boundary_policy='protected', material_types=None, all_materials=False, hair_protection='legacy', hair_seams=None, hair_normal_mode='surface'):
    """Return independent geometry copies; normal finishing must run afterwards."""
    if mode not in ('none','smooth','subdivision'):raise ValueError('Unknown contour mode')
    if mode=='none':return meshes,{'mode':'none'}
    if not material_indices and not all_materials:raise ValueError('Contour smoothing requires original material indices')
    if type(levels) is not int or levels not in (1,2):raise ValueError('Subdivision level must be 1 or 2')
    if boundary_policy not in ('native','protected'):raise ValueError('Invalid boundary protection policy')
    if hair_protection not in ('legacy','authored'):raise ValueError('Invalid hair protection policy')
    if hair_normal_mode not in ('surface','cage'):raise ValueError('Invalid hair normal mode')
    material_types=material_types or {}
    import bpy
    import numpy as np
    results=[];reports=[];found=set()
    for source in meshes:
        available=set(range(len(source.data.materials))) if all_materials else {i for i in material_indices if i<len(source.data.materials)}
        used={p.material_index for p in source.data.polygons}&available
        if not used:results.append(source);continue
        found|=used
        if mode=='smooth':
            result=source.copy();result.data=source.data.copy();result.name=source.name+'_ContourSmooth'
            bpy.context.scene.collection.objects.link(result)
            faces=[(p.material_index,tuple(p.vertices)) for p in result.data.polygons]
            mask=smoothing_mask(len(result.data.vertices),faces,used)
            edges=[tuple(edge.vertices) for edge in result.data.edges]
            keys=list(result.data.shape_keys.key_blocks) if result.data.shape_keys else []
            cache={};maximum=0.
            for data in [result.data.vertices,*[key.data for key in keys]]:
                before=_coordinates(data);digest=hashlib.sha256(before).hexdigest()
                if digest not in cache:
                    points=np.asarray(before).reshape(-1,3)
                    after=np.asarray(smooth_coordinates(points,edges,mask),dtype=np.float32)
                    delta=np.linalg.norm(after-points,axis=1);maximum=max(maximum,float(delta.max(initial=0)))
                    assert np.array_equal(after[np.logical_not(mask)],points[np.logical_not(mask)])
                    cache[digest]=array('f',after.ravel())
                data.foreach_set('co',cache[digest])
            result.data.update()
            reports.append({'mesh':source.name,'vertices_before':len(source.data.vertices),'vertices_after':len(result.data.vertices),
                'faces_before':len(source.data.polygons),'faces_after':len(result.data.polygons),'factor':.15,'iterations':2,
                'pinned_boundary_rings':2,'movable_vertices':sum(mask),'max_displacement_m':maximum,'unique_key_evaluations':len(cache)})
        else:
            untouched=set(range(len(source.data.materials)))-used
            original_fingerprints=_material_fingerprints(source,untouched)
            clone=source.copy();clone.data=source.data.copy();clone.name=source.name+'_ContourParts'
            bpy.context.scene.collection.objects.link(clone)
            bpy.ops.object.select_all(action='DESELECT');clone.select_set(True);bpy.context.view_layer.objects.active=clone
            bpy.ops.object.mode_set(mode='EDIT');bpy.ops.mesh.select_all(action='SELECT');bpy.ops.mesh.separate(type='MATERIAL');bpy.ops.object.mode_set(mode='OBJECT')
            pieces=[part for part in bpy.context.selected_objects if part.type=='MESH'];assembled=[];part_reports=[]
            assembled_offset=0
            target_names={source.data.materials[i].name for i in used}
            for part in pieces:
                names={part.data.materials[p.material_index].name for p in part.data.polygons}
                if names&target_names:
                    kind=material_types.get(next(iter(names)),'generic')
                    seam_settings=hair_seams if hair_seams and names<=hair_seams['materials'] else None
                    data,keys,report=_subdivide_part(part,levels,boundary_policy,kind,hair_protection,seam_settings,hair_normal_mode)
                    report['assembled_vertex_offset']=assembled_offset
                    part_reports.append(dict(report,part=part.name,materials=sorted(names)))
                    print('CONTOUR_PART_READY',sorted(names),report['vertices_before'],report['vertices_after'],flush=True)
                    assembled.append((part,data,keys))
                else:assembled.append((part,part.data,{}))
                assembled_offset+=len(assembled[-1][1].vertices)
            result=_assemble(source,assembled)
            assert _material_fingerprints(result,untouched)==original_fingerprints,'Subdivision changed an untargeted surface'
            reports.append({'mesh':source.name,'vertices_before':len(source.data.vertices),'vertices_after':len(result.data.vertices),
                'faces_before':len(source.data.polygons),'faces_after':len(result.data.polygons),'parts':part_reports,
                'untouched_materials':[m.name for m in source.data.materials if m and m.name not in target_names],
                'shape_keys_preserved':len(result.data.shape_keys.key_blocks) if result.data.shape_keys else 0})
            reports[-1]['untouched_material_fingerprints_verified']=original_fingerprints
            for part in pieces:part.hide_render=True;part.hide_set(True)
        source.hide_render=True;source.hide_set(True)
        results.append(result)
    if not all_materials and found!=set(material_indices):raise ValueError('Contour target material indices were not found')
    return results,{'mode':mode,'experimental':True,'target_pmx_indices':sorted(material_indices),'scope':'all' if all_materials else 'selected',
                   'geometry_version':VERSION,'level':levels if mode=='subdivision' else 0,'boundary_policy':boundary_policy,'meshes':reports,
                   'export_geometry':'processed_coordinates_and_topology','next_modifiers':['WELD','WEIGHTED_NORMAL','ARMATURE']}
