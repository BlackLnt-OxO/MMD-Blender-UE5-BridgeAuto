"""Opt-in, topology-aware hair normals frozen before final triangulation.

No model identities or UV-distance welding. A smooth fan always consumes one
evaluated normal field; the legacy per-corner angular switch is audit-only here.
"""
from array import array
from collections import defaultdict
import math

VERSION = 1
POLICY = 'continuous_fans_v1'


def smooth_fans(faces, smooth, sharp_edges=()):
    """Corner-index fans separated by authored sharp/nonmanifold/flat edges."""
    offset=0;parent=[];edge_faces=defaultdict(list);corners=[]
    for index,face in enumerate(faces):
        ids=list(range(offset,offset+len(face)));parent.extend(ids);offset+=len(face)
        corners.append(dict(zip(face,ids)))
        for a,b in zip(face,face[1:]+face[:1]):edge_faces[tuple(sorted((a,b)))].append(index)
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    sharp={tuple(sorted(edge)) for edge in sharp_edges}
    for edge,polygons in edge_faces.items():
        if edge in sharp or len(polygons)!=2 or not all(smooth[p] for p in polygons):continue
        a,b=polygons
        # Opposite duplicate faces are a two-sided domain, not a smooth seam.
        fa,fb=faces[a],faces[b]
        if set(fa)==set(fb):continue
        for vertex in edge:
            x,y=find(corners[a][vertex]),find(corners[b][vertex]);parent[y]=x
    result=defaultdict(list)
    for i in range(offset):result[find(i)].append(i)
    return list(result.values())


def _unit(value):
    if not all(math.isfinite(v) for v in value):return None
    length=math.sqrt(sum(v*v for v in value))
    return tuple(v/length for v in value) if length>1e-12 else None


def coherent_field(fans, proxy, reference, weights):
    """Use a single valid field for the entire fan, never an angular corner mix."""
    normals=[None]*len(reference);fallback=[];angular_audit=0;spread=0.
    for number,fan in enumerate(fans):
        invalid=any(proxy.get(i) is None or _unit(proxy[i]) is None for i in fan)
        field=reference if invalid else proxy
        if invalid:fallback.append(number)
        values=[_unit(field[i]) for i in fan]
        if any(v is None for v in values):
            raise ValueError('Hair normal fan has no valid geometric reference: '+str(number))
        total=tuple(sum(value[axis]*max(weights[i],1e-20) for i,value in zip(fan,values)) for axis in range(3))
        normal=_unit(total)
        if normal is None:raise ValueError('Hair normal fan cancels across incompatible surfaces: '+str(number))
        for i in fan:
            normals[i]=normal
            old=_unit(proxy[i]) if i in proxy else None;actual=_unit(reference[i])
            if old and actual and sum(x*y for x,y in zip(old,actual))<math.cos(math.radians(15)):angular_audit+=1
        spread=max(spread,max(1.-sum(x*y for x,y in zip(normal,v)) for v in values))
    return normals,{'smooth_fans':len(fans),'whole_fan_fallbacks':fallback,
                    'angular_15deg_audit_corners':angular_audit,'angular_threshold_used_for_selection':False,
                    'input_fan_max_one_minus_dot':spread}


def interpolated_field(fans, field, reference):
    """Preserve interpolated cage directions; invalid fans fall back together."""
    result=[None]*len(field);fallback=[]
    for number,fan in enumerate(fans):
        valid=all(_unit(field[i]) is not None for i in fan)
        if not valid:fallback.append(number)
        for i in fan:
            result[i]=_unit(field[i] if valid else reference[i])
            if result[i] is None:raise ValueError('Invalid interpolated hair fan: '+str(number))
    return result,{'smooth_fans':len(fans),'whole_fan_fallbacks':fallback,'angular_threshold_used_for_selection':False}


def coarse_weighted_reference(part, distance=.0001):
    """Use the accepted coarse weld/weighted recipe as a shading guide only."""
    import bpy
    from .blender_mesh_finish import prepare_comparison
    before=set(bpy.data.objects);collections=set(bpy.data.collections)
    try:
        finished,_=prepare_comparison([part],distance)
        return [tuple(n.vector) for n in finished[0].data.corner_normals]
    finally:
        for obj in set(bpy.data.objects)-before:
            data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if data and data.users==0:bpy.data.meshes.remove(data)
        for collection in set(bpy.data.collections)-collections:
            if not collection.objects:bpy.data.collections.remove(collection)


def prepare_hair_normals(source, material_names, distance=.0001, *, mode='surface'):
    """Evaluate Weld(CONNECTED)+Weighted Normal on pre-triangle hair domains.

    Only shading data is returned. Render topology, UVs, skin and every morph
    remain on the original export copy. Disconnected sheets cannot weld through
    proximity. Missing proxy corners switch the whole fan to the actual surface.
    """
    if not math.isfinite(distance) or not 0 < distance <= .01:
        raise ValueError('Weld distance must be in (0, 0.01] metres')
    if mode not in ('surface','cage'):raise ValueError('Unknown hair normal mode')
    import bpy
    mesh=source.data
    result=[tuple(n.vector) for n in mesh.corner_normals];rows=[]
    for slot,material in enumerate(mesh.materials):
        if not material or material.name not in material_names:continue
        polygons=[p for p in mesh.polygons if p.material_index==slot]
        if not polygons:continue
        vertices=sorted({v for p in polygons for v in p.vertices});mapping={v:i for i,v in enumerate(vertices)}
        faces=[tuple(mapping[v] for v in p.vertices) for p in polygons]
        coordinates=mesh.shape_keys.key_blocks[0].data if mesh.shape_keys else mesh.vertices
        data=bpy.data.meshes.new('MMDBridge_HairNormalDomain');data.from_pydata([coordinates[i].co[:] for i in vertices],[],faces)
        sharp={tuple(sorted(mapping[v] for v in e.vertices)) for e in mesh.edges
               if e.use_edge_sharp and all(v in mapping for v in e.vertices)}
        for p,original in zip(data.polygons,polygons):p.use_smooth=original.use_smooth
        for e in data.edges:e.use_edge_sharp=tuple(sorted(e.vertices)) in sharp
        identity='_mmdb_hair_corner';data.attributes.new(identity,'INT','CORNER').data.foreach_set('value',array('i',range(len(data.loops))))
        obj=bpy.data.objects.new('MMDBridge_HairNormalDomain',data);bpy.context.scene.collection.objects.link(obj)
        def evaluate(weld):
            if weld:
                modifier=obj.modifiers.new('MMDBridge_ConnectedWeld','WELD');modifier.mode='CONNECTED';modifier.merge_threshold=distance
            weight=obj.modifiers.new('MMDBridge_HairWeightedNormal','WEIGHTED_NORMAL');weight.mode='FACE_AREA';weight.weight=50;weight.keep_sharp=True
            bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get();evaluated=obj.evaluated_get(graph)
            output=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
            try:
                values={key.value:tuple(normal.vector) for key,normal in zip(output.attributes[identity].data,output.corner_normals)}
                counts={'vertices':len(output.vertices),'faces':len(output.polygons)}
            finally:evaluated.to_mesh_clear()
            for modifier in list(obj.modifiers):obj.modifiers.remove(modifier)
            return values,counts
        try:
            reference,_=evaluate(False);proxy,counts=evaluate(True)
            fans=smooth_fans(faces,[p.use_smooth for p in polygons],sharp)
            weights=[p.area for p in data.polygons for _ in p.loop_indices]
            indices=[i for p in polygons for i in p.loop_indices]
            if mode=='cage':
                field=[tuple(mesh.corner_normals[i].vector) for i in indices]
                normals,audit=interpolated_field(fans,field,reference)
            else:normals,audit=coherent_field(fans,proxy,reference,weights)
            for index,normal in zip(indices,normals):result[index]=normal
            rows.append({'material':material.name,'slot':slot,'vertices_before':len(vertices),'faces_before':len(faces),
                         'proxy_after':counts,'corners':len(indices),'weld_mode':'CONNECTED','weld_distance_m':distance,
                         'weighted_mode':'FACE_AREA','weighted_weight':50,'keep_sharp':True,**audit})
        finally:bpy.data.objects.remove(obj,do_unlink=True);bpy.data.meshes.remove(data)
    return result,{'version':VERSION,'policy':POLICY if mode=='surface' else 'weighted_cage_interpolation_v1',
                   'generation_surface':'subdivision_polygons_before_final_triangulation' if mode=='surface' else 'coarse_weighted_field_interpolated_by_subdivision',
                   'cross_sheet_proximity_weld':False,'parts':rows}
