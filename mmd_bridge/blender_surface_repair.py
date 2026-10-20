"""Opt-in repair candidates. Source objects, legacy exports and shader types stay intact."""
from array import array
from collections import Counter, defaultdict
import hashlib
import math

VERSION = 1
FACE_ID = '_mmdb_surface_face'
VERTEX_ID = '_mmdb_surface_vertex'


def _points(data):
    import numpy as np
    result=np.empty((len(data),3),dtype=np.float32);data.foreach_get('co',result.ravel());return result


def _triangle_key(points, ids):
    return tuple(sorted(tuple(round(float(v),7) for v in points[i]) for i in ids))


def attach_original(source, records, cfg, model_hash):
    """Exact duplicate triangles establish a surface domain, never a model name."""
    import numpy as np
    from .character_geometry_roles import geometry_override
    mesh=source.data;basis=_points(mesh.shape_keys.key_blocks[0].data if mesh.shape_keys else mesh.vertices)
    by_name={r['asset_name']:r for r in records}
    material_records={i:by_name[m.name] for i,m in enumerate(mesh.materials) if m and m.name in by_name}
    polygon_groups=defaultdict(list);lookup=defaultdict(list)
    for polygon in mesh.polygons:
        polygon_groups[polygon.material_index].append(polygon)
        if len(polygon.vertices)==3:lookup[_triangle_key(basis,polygon.vertices)].append(polygon)
    bindings=[];reviews=[]
    armature=source.find_armature()
    deform_groups={g.index for g in source.vertex_groups if not armature or g.name in armature.data.bones}
    scale=max(float(np.ptp(basis,axis=0).max()),.01)
    tolerance=max(1e-7,scale*1e-6)
    blocks=list(mesh.shape_keys.key_blocks) if mesh.shape_keys else []
    for slot,record in material_records.items():
        pbr=record.get('character_material',{}).get('pbr',{})
        override=geometry_override(cfg,model_hash,record['pmx_index'])
        if (pbr.get('type') in ('skin','face','hair','iris','sclera','eye_highlight','eye_shadow','lash','mouth','teeth')
                and override.get('host') is None):continue
        polygons=polygon_groups[slot]
        matches=defaultdict(dict)
        for polygon in polygons:
            if len(polygon.vertices)!=3:continue
            for other in lookup[_triangle_key(basis,polygon.vertices)]:
                if other.material_index!=slot:
                    matches[other.material_index][polygon.index]=other.index
        eligible={host:pairs for host,pairs in matches.items() if len(pairs)==len(polygons)
            and len(polygon_groups[host])>len(polygons)*1.1}
        specified=override.get('host')
        if specified is not None:
            host=next((i for i,r in material_records.items() if r['pmx_index']==specified),None)
            eligible={host:matches[host]} if host in matches and len(matches[host])==len(polygons) else {}
        # At least an actual transparent/cutout surface is required for automatic
        # overlay treatment. Opaque, overlapping mechanical surfaces are not decals.
        alpha=(pbr.get('alpha_mode') in ('masked','translucent') or pbr.get('params',{}).get('Opacity',1)<.999)
        if not eligible or (not alpha and specified is None):
            if specified is not None:reviews.append({'pmx_index':record['pmx_index'],'reason':'指定宿主没有完整共面三角面映射'})
            continue
        if len(eligible)!=1:
            reviews.append({'pmx_index':record['pmx_index'],'reason':'存在多个共面宿主，未自动投射'});continue
        host,pairs=next(iter(eligible.items()));vertices={};valid=True;reason=''
        for child_face,host_face in pairs.items():
            c=mesh.polygons[child_face];h=mesh.polygons[host_face]
            for i in c.vertices:
                nearest=min(h.vertices,key=lambda j:float(np.linalg.norm(basis[i]-basis[j])))
                if np.linalg.norm(basis[i]-basis[nearest])>tolerance or (i in vertices and vertices[i]!=nearest):
                    valid=False;reason='共面顶点映射不唯一';break
                vertices[i]=nearest
            if not valid:break
        if valid:
            host_vertices={i for polygon in polygon_groups[host] for i in polygon.vertices}
            if host_vertices.intersection(vertices):
                valid=False;reason='贴层与宿主共享原始顶点，保留原数据并待确认独立导出域'
        if valid:
            for i,j in vertices.items():
                a={g.group:g.weight for g in mesh.vertices[i].groups if g.weight>1e-7 and g.group in deform_groups}
                b={g.group:g.weight for g in mesh.vertices[j].groups if g.weight>1e-7 and g.group in deform_groups}
                if sum(abs(a.get(k,0)-b.get(k,0)) for k in a.keys()|b.keys())>1e-3:
                    valid=False;reason='贴片和宿主蒙皮不兼容';break
        if valid:
            source_ids=np.asarray(list(vertices),np.int32);host_ids=np.asarray([vertices[i] for i in source_ids],np.int32)
            for key in blocks:
                positions=_points(key.data)
                if np.linalg.norm(positions[source_ids]-positions[host_ids],axis=1).max(initial=0)>tolerance:
                    valid=False;reason='贴片包含与宿主独立的形态键运动';break
        if not valid:
            reviews.append({'pmx_index':record['pmx_index'],'reason':reason});continue
        bindings.append({'layer_slot':slot,'host_slot':host,'pmx_index':record['pmx_index'],
            'host_pmx_index':material_records[host]['pmx_index'],'face_pairs':pairs,'vertex_pairs':vertices,
            'offset_m':.0001,'method':'exact_triangles_weights_all_shape_keys','source_faces':len(polygons)})
    by_layer={b['layer_slot']:b for b in bindings};ordered=[];failed=set();done=set()
    def visit(slot,ancestors):
        if slot in done:return slot not in failed
        if slot in ancestors:return False
        binding=by_layer[slot];host=binding['host_slot']
        if host in by_layer and not visit(host,ancestors|{slot}):
            failed.add(slot);done.add(slot);return False
        ordered.append(binding);done.add(slot);return True
    for slot in by_layer:visit(slot,set())
    for slot in sorted(failed):
        reviews.append({'pmx_index':by_layer[slot]['pmx_index'],'reason':'贴层宿主存在循环或依赖失败，未应用绑定'})
    return ordered,reviews


def _annotate(mesh):
    for name,domain,items in ((FACE_ID,'FACE',mesh.polygons),(VERTEX_ID,'POINT',mesh.vertices)):
        attr=mesh.attributes.get(name) or mesh.attributes.new(name,'INT',domain)
        attr.data.foreach_set('value',array('i',range(len(items))))


def _surface_nodes(source, output, contour):
    """Each triangle's CC children retain source face and corner identities."""
    import bpy
    mesh=output.data;face_attr=mesh.attributes[FACE_ID]
    original_to_output={}
    if contour.get('mode')=='none':
        original_to_output={int(v.value):i for i,v in enumerate(mesh.attributes[VERTEX_ID].data)}
    else:
        for row in contour['meshes']:
            for part in row['parts']:
                original=bpy.data.objects[part['part']]
                ids=[v.value for v in original.data.attributes[VERTEX_ID].data]
                offset=part['assembled_vertex_offset']
                original_to_output.update({old:offset+new for old,new in zip(ids,part['original_vertex_output_mapping'])})
        # Non-target faces still contain their exact original vertices.
        selected=set(original_to_output)
        attr=mesh.attributes.get(VERTEX_ID)
        if attr:
            targets=set(contour['target_pmx_indices'])
            untouched={i for p in mesh.polygons if source.data.polygons[face_attr.data[p.index].value].material_index not in targets for i in p.vertices}
            for i in untouched:
                value=attr.data[i]
                if value.value not in selected and 0<=value.value<len(source.data.vertices):
                    original_to_output.setdefault(value.value,i)
    output_to_original={new:old for old,new in original_to_output.items()}
    children=defaultdict(list)
    for p in mesh.polygons:children[face_attr.data[p.index].value].append(p)
    result={}
    for parent,polygons in children.items():
        parent_vertices=set(source.data.polygons[parent].vertices)
        if len(polygons)==1 and len(polygons[0].vertices)==3:
            result[parent]={('v',output_to_original[i]):i for i in polygons[0].vertices if i in output_to_original}
            continue
        if len(polygons)!=3 or any(len(p.vertices)!=4 for p in polygons):continue
        corners={p.index:next((i for i in p.vertices if output_to_original.get(i) in parent_vertices),None) for p in polygons}
        if any(i is None for i in corners.values()):continue
        touched=defaultdict(set)
        for p in polygons:
            for i in p.vertices:touched[i].add(output_to_original[corners[p.index]])
        nodes={}
        for i,parents in touched.items():
            if i in output_to_original and output_to_original[i] in parent_vertices:key=('v',output_to_original[i])
            elif len(parents)==3:key=('f',parent)
            elif len(parents)==2:key=('e',*sorted(parents))
            else:continue
            nodes[key]=i
        if len(nodes)==7:result[parent]=nodes
    return result


def apply_attachments(source, output, contour, bindings):
    import numpy as np
    mesh=output.data;nodes=_surface_nodes(source,output,contour);results=[]
    for binding in bindings:
        pairs={};conflict=False
        for child,host in binding['face_pairs'].items():
            child_nodes=nodes.get(child);host_nodes=nodes.get(host)
            if not child_nodes or not host_nodes:
                conflict=True;break
            vertices=binding['vertex_pairs']
            for key,index in child_nodes.items():
                if key[0]=='v':target=('v',vertices[key[1]])
                elif key[0]=='e':target=('e',*sorted((vertices[key[1]],vertices[key[2]])))
                else:target=('f',host)
                if target not in host_nodes or (index in pairs and pairs[index]!=host_nodes[target]):
                    conflict=True;break
                pairs[index]=host_nodes[target]
            if conflict:break
        row={k:v for k,v in binding.items() if k not in ('face_pairs','vertex_pairs')}
        row['source_face_pairs']=[[int(a),int(b)] for a,b in binding['face_pairs'].items()]
        row['source_vertex_pairs']=[[int(a),int(b)] for a,b in binding['vertex_pairs'].items()]
        if conflict:
            row.update(complete=False,reason='细分域不一致或共享顶点映射冲突，未部分应用');results.append(row);continue
        ids=np.array(list(pairs),np.int32);host_ids=np.array([pairs[i] for i in ids],np.int32)
        host_polygons=[p for p in mesh.polygons if p.material_index==binding['host_slot']]
        # An area-weighted geometric normal, independent of old custom normals.
        triangles=[(p.vertices[0],p.vertices[i],p.vertices[i+1]) for p in host_polygons for i in range(1,len(p.vertices)-1)]
        triangles=np.asarray(triangles,np.int32)
        blocks=list(mesh.shape_keys.key_blocks) if mesh.shape_keys else []
        arrays=[mesh.vertices]+[key.data for key in blocks]
        cache={};prepared=[];collapsed=[]
        transform=np.asarray(output.matrix_world.to_3x3(),np.float64)
        inverse=np.linalg.inv(transform)
        for data_index,data in enumerate(arrays):
            positions=_points(data);digest=hashlib.sha256(positions.tobytes()).hexdigest()
            if digest not in cache:
                xyz=positions[triangles];face_normals=np.cross(xyz[:,1]-xyz[:,0],xyz[:,2]-xyz[:,0])
                normals=np.zeros_like(positions)
                for i in range(3):np.add.at(normals,triangles[:,i],face_normals)
                world_normals=normals[host_ids]@inverse
                length=np.linalg.norm(world_normals,axis=1);valid=length>=1e-12
                # Authored hide morphs collapse whole regions into a point.
                # Keep these collapsed instead of expanding a tiny visible shell.
                # An undefined Basis direction is a mapping failure, not guessed.
                if not valid.all() and data_index<=1:
                    row.update(complete=False,reason='Basis宿主包含无法确定朝向的顶点，未应用绑定')
                    conflict=True;break
                offset=np.zeros((len(ids),3),np.float64)
                offset[valid]=(world_normals[valid]/length[valid,None]*binding['offset_m'])@inverse.T
                cache[digest]=(positions[host_ids]+offset, int((~valid).sum()))
            target,collapse_count=cache[digest]
            positions[ids]=target;prepared.append((data,positions))
            if collapse_count:
                collapsed.append({'shape_key':blocks[data_index-1].name,'vertices':collapse_count})
        if conflict:
            results.append(row);continue
        for data,positions in prepared:data.foreach_set('co',positions.ravel())
        # Interpolated host weights keep layers together after skeletal deformation.
        groups={}
        armature=output.find_armature()
        deform_groups={g.index for g in output.vertex_groups if not armature or g.name in armature.data.bones}
        for layer,host in pairs.items():
            for g in mesh.vertices[host].groups:
                if g.group in deform_groups:groups.setdefault((g.group,float(g.weight)),[]).append(layer)
        for group in output.vertex_groups:
            if group.index in deform_groups:group.remove(ids.tolist())
        for (group,weight),vertices in groups.items():output.vertex_groups[group].add(vertices,weight,'REPLACE')
        row.update(complete=True,mapped_vertices=len(pairs),all_shape_keys=True,UV_unchanged=True,
                   collapsed_source_morphs=collapsed,spacing_world_m=binding['offset_m'],
                   export_vertex_pairs=[[int(a),int(b)] for a,b in pairs.items()])
        results.append(row)
    mesh.update();return results


def triangulate_export(source):
    """Freeze face diagonals without adding vertices or losing shape/UV data."""
    import bpy
    import numpy as np
    from .blender_contour import _copy_animation, _key_properties
    old=source.data
    if all(len(p.vertices)==3 for p in old.polygons):return source,{'changed':False,'triangles':len(old.polygons)}
    old.calc_loop_triangles();triangles=list(old.loop_triangles)
    mesh=bpy.data.meshes.new(old.name+'_FinalTriangles')
    mesh.from_pydata([v.co[:] for v in old.vertices],[],[t.vertices[:] for t in triangles])
    for material in old.materials:mesh.materials.append(material)
    for p,t in zip(mesh.polygons,triangles):p.material_index=t.material_index;p.use_smooth=old.polygons[t.polygon_index].use_smooth
    corner_ids=[i for t in triangles for i in t.loops]
    for uv in old.uv_layers:
        values=np.empty((len(uv.data),2),np.float32);uv.data.foreach_get('uv',values.ravel())
        mesh.uv_layers.new(name=uv.name).data.foreach_set('uv',values[corner_ids].ravel())
    sharp={tuple(sorted(e.vertices)) for e in old.edges if e.use_edge_sharp}
    for edge in mesh.edges:edge.use_edge_sharp=tuple(sorted(edge.vertices)) in sharp
    for name,domain,indices in ((FACE_ID,'FACE',[t.polygon_index for t in triangles]),(VERTEX_ID,'POINT',range(len(old.vertices)))):
        attr=old.attributes.get(name)
        if attr:mesh.attributes.new(name,'INT',domain).data.foreach_set('value',array('i',[attr.data[i].value for i in indices]))
    normals=np.empty((len(old.corner_normals),3),np.float32);old.corner_normals.foreach_get('vector',normals.ravel())
    mesh.normals_split_custom_set(normals[corner_ids]);mesh.update()
    result=source.copy();result.data=mesh;result.name=source.name+'_FinalTriangles'
    bpy.context.scene.collection.objects.link(result)
    result.vertex_groups.clear()
    for group in source.vertex_groups:
        copied=result.vertex_groups.new(name=group.name);copied.lock_weight=group.lock_weight
    batches={}
    for v in old.vertices:
        for g in v.groups:batches.setdefault((g.group,float(g.weight)),[]).append(v.index)
    for (group,weight),ids in batches.items():result.vertex_groups[group].add(ids,weight,'REPLACE')
    if old.shape_keys:
        for key in old.shape_keys.key_blocks:
            new=result.shape_key_add(name=key.name,from_mix=False);new.data.foreach_set('co',_points(key.data).ravel())
        target=result.data.shape_keys;target.use_relative=old.shape_keys.use_relative
        for key in old.shape_keys.key_blocks:
            new=target.key_blocks[key.name];_key_properties(key,new);new.relative_key=target.key_blocks[key.relative_key.name]
        _copy_animation(old.shape_keys,target,{old.shape_keys:target,source:result})
    source.hide_render=True;source.hide_set(True)
    return result,{'changed':True,'vertices':len(mesh.vertices),'faces_before':len(old.polygons),'triangles':len(mesh.polygons),'shape_keys_preserved':len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0}


def refresh_final_directions(meshes, records):
    """Rebake automatic flow fields on the final triangles; keep manual inputs."""
    from pathlib import Path
    from types import SimpleNamespace
    from .blender_character_pbr import hair_flow
    from .character_profiles import fingerprint
    updates=[]
    for record in records:
        pbr=record.get('character_material',{}).get('pbr',{})
        binding=pbr.get('textures',{}).get('direction',{})
        if pbr.get('type')!='hair' or binding.get('kind')!='generated':continue
        before=binding.get('sha256')
        flow,evidence=hair_flow(SimpleNamespace(meshes=meshes),record,Path(binding['path']).parent)
        record['textures']['direction']=flow
        pbr['textures']['direction'].update(path=flow['path'],sha256=flow['sha256'])
        pbr['direction_source']={**evidence,'geometry':'final_export_triangles','surface_repair_version':VERSION}
        pbr['signature']=fingerprint({k:v for k,v in pbr.items() if k!='signature'})
        updates.append({'pmx_index':record['pmx_index'],'before_sha256':before,
                        'after_sha256':flow['sha256'],'manual_inputs_unchanged':True,**pbr['direction_source']})
    return updates


def prepare_surface_candidate(meshes, records, cfg, policy):
    import bpy
    from .pmx_geometry import read_geometry
    from .character_geometry_roles import purpose,geometry_override
    from .blender_contour import prepare_contour
    from .blender_mesh_finish import prepare_comparison
    evidence=read_geometry(cfg['model']);model_hash=policy['source_model_sha256'];scope=policy['candidate']['scope']
    if scope not in ('none','local','all'):raise ValueError('未知修复候选范围')
    results=[];rows=[];all_roles=[]
    by_name={r['asset_name']:r for r in records}
    types={name:r['character_material']['pbr']['type'] for name,r in by_name.items()}
    for original in meshes:
        # Candidate code never annotates or edits the user's input copy.
        source=original.copy();source.data=original.data.copy();source.name=original.name+'_SurfaceInput'
        bpy.context.scene.collection.objects.link(source);_annotate(source.data)
        material_names=[m.name if m else '' for m in source.data.materials]
        bindings,reviews=attach_original(source,records,cfg,model_hash)
        print('SURFACE_BINDINGS',original.name,[{k:v for k,v in b.items() if k not in ('face_pairs','vertex_pairs')} for b in bindings],reviews,flush=True)
        role_by_slot={};slot_by_pmx={r['pmx_index']:i for i,m in enumerate(source.data.materials) if m and (r:=by_name.get(m.name))}
        for slot,mat in enumerate(source.data.materials):
            if mat is None or mat.name not in by_name:continue
            record=by_name[mat.name];measurement=dict(evidence.get(record['pmx_index'],{}))
            binding=next((b for b in bindings if b['layer_slot']==slot),None)
            if binding:measurement['attached_host']=binding['host_pmx_index']
            role=purpose(record,measurement,geometry_override(cfg,model_hash,record['pmx_index']))
            role_by_slot[slot]=role;record['geometry_purpose']=role
        targets={i for i,r in role_by_slot.items() if r['local_selected']}
        # An attached layer follows the selected host, including mixed-purpose
        # host slots; no material names or hard-coded sample IDs are used.
        for b in bindings:
            override=geometry_override(cfg,model_hash,b['pmx_index'])
            if b['host_slot'] in targets and override.get('local')!='exclude':targets.add(b['layer_slot'])
            elif b['host_slot'] not in targets:targets.discard(b['layer_slot'])
        if scope=='all':targets={p.material_index for p in source.data.polygons}
        if scope=='none':targets=set()
        selected=sorted(targets)
        if selected:
            processed,contour=prepare_contour([source],'subdivision',selected,levels=1,boundary_policy='protected',material_types=types)
            result=processed[0]
        else:result=source;contour={'mode':'none','meshes':[]}
        attachments=apply_attachments(source,result,contour,bindings)
        result,triangulation=triangulate_export(result)
        finished,normal=prepare_comparison([result],.0001,safe_normals=bool(selected),
            repair_materials={source.data.materials[i].name for i in selected})
        results.extend(finished);original.hide_render=True;original.hide_set(True)
        role_rows=[{'pmx_index':by_name[material_names[i]]['pmx_index'],
                    'name':by_name[material_names[i]]['name'],**role} for i,role in role_by_slot.items()]
        all_roles.extend(role_rows)
        rows.append({'source':original.name,'scope':scope,'target_pmx_indices':[by_name[material_names[i]]['pmx_index'] for i in selected],
            'contour':contour,'attachments':attachments,'attachment_reviews':reviews,'triangulation':triangulation,'normal_finish':normal,
            'needs_review':[r for r in role_rows if r['needs_review']]})
    direction_updates=refresh_final_directions(results,records) if scope=='all' else []
    return results,{'geometry_policy':policy,'candidate_version':VERSION,'scope':scope,'meshes':rows,'geometry_roles':all_roles,
        'final_direction_maps':direction_updates,
        'complete':all(a['complete'] for r in rows for a in r['attachments']) and not any(r['attachment_reviews'] for r in rows),
        'appearance_status':'not_reviewed','legacy_assets_modified':False}
