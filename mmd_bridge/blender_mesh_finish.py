"""Compare per-part Weld/Weighted Normal shading with original morph topology."""
from array import array
import math
import bpy


def prepare_comparison(meshes, distance=.0001, *, safe_normals=False, repair_materials=(), excluded_materials=(), weighted_only_materials=()):
    """Transfer evaluated normals; preserve original vertices, UVs and morphs."""
    if not math.isfinite(distance) or not 0 < distance <= .01:
        raise ValueError('Weld distance must be in (0, 0.01] metres')
    editable=bpy.data.collections.new('MMDBridge_Editable_Weld_WeightedNormal')
    exported=bpy.data.collections.new('MMDBridge_Export_Weld_WeightedNormal')
    bpy.context.scene.collection.children.link(editable)
    bpy.context.scene.collection.children.link(exported)
    results=[];receipts=[];source_counts=[]
    excluded_materials = set(excluded_materials)
    weighted_only_materials = set(weighted_only_materials)
    if excluded_materials & weighted_only_materials:
        raise ValueError('A material cannot be both excluded and weighted-normal only')
    for number,source in enumerate(meshes):
        clone=source.copy();clone.data=source.data.copy();clone.name=f'PBR_Finished_{number:03d}'
        exported.objects.link(clone)
        normals=[tuple(normal.vector) for normal in (source.data if excluded_materials else clone.data).corner_normals]
        excluded_corners = {index for polygon in source.data.polygons
            if source.data.materials[polygon.material_index].name in excluded_materials
            for index in polygon.loop_indices}
        # Blender re-encodes the entire custom-normal layer, even for untouched
        # values. Preserve the authored integer representation for exclusions.
        authored = source.data.attributes.get('custom_normal')
        authored_encoded = None
        if excluded_corners:
            authored_encoded = array('i', [0]) * (len(source.data.loops) * 2)
            if authored:
                if authored.data_type != 'INT16_2D':
                    raise ValueError('当前 Blender 法线编码不支持安全保留排除部件，已停止导出')
                authored.data.foreach_get('value', authored_encoded)
        excluded_edges = {source.data.loops[index].edge_index for index in excluded_corners}
        authored_sharp = {index:source.data.edges[index].use_edge_sharp for index in excluded_edges}
        actual_normals=None;safe_replaced=0
        if safe_normals:
            # An independent weighted-normal evaluation on the actual export
            # surface supplies missing/unsafe corners from the weld proxy.
            actual=clone.copy();actual.data=clone.data.copy();actual.name='Surface_Normal_Reference'
            bpy.context.scene.collection.objects.link(actual)
            if actual.data.shape_keys:
                coordinates=array('f',[0.])*(len(actual.data.vertices)*3)
                actual.data.shape_keys.key_blocks[0].data.foreach_get('co',coordinates)
                actual.shape_key_clear();actual.data.vertices.foreach_set('co',coordinates)
            for modifier in list(actual.modifiers):actual.modifiers.remove(modifier)
            # Zeros request Blender's geometric smooth normals. Only the
            # selected candidate surfaces consume this reference below.
            actual.data.normals_split_custom_set([(0.,0.,0.)]*len(actual.data.loops))
            weight=actual.modifiers.new('MMDBridge_ActualSurfaceWeightedNormal','WEIGHTED_NORMAL')
            weight.mode='FACE_AREA';weight.weight=50;weight.keep_sharp=True
            bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get()
            evaluated=actual.evaluated_get(graph);reference=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
            actual_normals=[tuple(n.vector) for n in reference.corner_normals]
            evaluated.to_mesh_clear();actual_data=actual.data;bpy.data.objects.remove(actual,do_unlink=True);bpy.data.meshes.remove(actual_data)
        identity='_mmdb_original_corner'
        assert not source.data.attributes.get(identity)
        attribute=source.data.attributes.new(name=identity,type='INT',domain='CORNER')
        attribute.data.foreach_set('value',array('i',range(len(source.data.loops))))
        source_counts.append({'vertices':len(clone.data.vertices),'faces':len(clone.data.polygons),
            'shape_keys':len(clone.data.shape_keys.key_blocks) if clone.data.shape_keys else 0})
        order={material.name:index for index,material in enumerate(source.data.materials) if material}
        bpy.ops.object.select_all(action='DESELECT');source.select_set(True)
        bpy.context.view_layer.objects.active=source
        bpy.ops.object.mode_set(mode='EDIT');bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.separate(type='MATERIAL');bpy.ops.object.mode_set(mode='OBJECT')
        parts=sorted((obj for obj in bpy.context.selected_objects if obj.type=='MESH'),
            key=lambda obj:min(order.get(material.name,len(order)) for material in obj.data.materials if material))
        transferred=set()
        for part in parts:
            for collection in list(part.users_collection):collection.objects.unlink(part)
            editable.objects.link(part)
            used_materials = {polygon.material_index for polygon in part.data.polygons}
            names = [material.name for index, material in enumerate(part.data.materials)
                     if material and index in used_materials]
            if names and all(name in excluded_materials for name in names):
                receipts.append({'part':part.name, 'materials':names, 'mode':'original',
                    'reason':'explicit_material_exclusion', 'modifier_order':[modifier.type for modifier in part.modifiers],
                    'authored_normals_retained':True})
                part.hide_render=True;part.hide_set(True)
                continue
            keys=list(part.data.shape_keys.key_blocks) if part.data.shape_keys else []
            values=[key.value for key in keys]
            drivers=list(part.data.shape_keys.animation_data.drivers) if keys and part.data.shape_keys.animation_data else []
            action=part.data.shape_keys.animation_data.action if keys and part.data.shape_keys.animation_data else None
            if action:
                from mmd_tools.compat.action_compat import get_action_fcurves
                action_curves=list(get_action_fcurves(action))
            else:action_curves=[]
            action_muted=[curve.mute for curve in action_curves]
            for curve in action_curves:curve.mute=True
            muted=[driver.mute for driver in drivers]
            for driver in drivers:driver.mute=True
            for key in keys:key.value=0.
            modifiers=[(modifier,modifier.show_viewport,modifier.show_render) for modifier in part.modifiers]
            for modifier,_,_ in modifiers:modifier.show_viewport=modifier.show_render=False
            weighted_only = bool(names) and all(name in weighted_only_materials for name in names)
            weld = None
            if not weighted_only:
                weld=part.modifiers.new('MMDBridge_Weld','WELD');weld.mode='ALL';weld.merge_threshold=distance
            weighted=part.modifiers.new('MMDBridge_WeightedNormal','WEIGHTED_NORMAL')
            weighted.mode='FACE_AREA';weighted.weight=50;weighted.keep_sharp=True
            if weld:part.modifiers.move(part.modifiers.find(weld.name),0)
            part.modifiers.move(part.modifiers.find(weighted.name),0 if weighted_only else 1)
            bpy.context.view_layer.update();depsgraph=bpy.context.evaluated_depsgraph_get()
            evaluated=part.evaluated_get(depsgraph);mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
            try:
                indices=mesh.attributes.get(identity)
                assert indices and len(indices.data)==len(mesh.loops)
                for key,normal in zip(indices.data,mesh.corner_normals):
                    index=key.value;assert 0<=index<len(normals)
                    if index in excluded_corners:
                        continue
                    normals[index]=tuple(normal.vector);transferred.add(index)
                if weighted_only:
                    assert len(mesh.vertices)==len(part.data.vertices) and len(mesh.polygons)==len(part.data.polygons), 'Weighted-only part changed topology'
                receipts.append({'part':part.name,'materials':[m.name for m in mesh.materials if m],
                    **({'mode':'weighted_only'} if weighted_only else {}),
                    'vertices_before':len(part.data.vertices),'vertices_after':len(mesh.vertices),
                    'faces_before':len(part.data.polygons),'faces_after':len(mesh.polygons),
                    'shape_keys_before':len(keys),'shape_keys_after':len(keys),
                    'weld_distance_m':None if weighted_only else distance,'weighted_mode':'FACE_AREA','weighted_weight':50,'keep_sharp':True,
                    'modifier_order':(['WEIGHTED_NORMAL'] if weighted_only else ['WELD','WEIGHTED_NORMAL'])+['ARMATURE']})
            finally:evaluated.to_mesh_clear()
            for modifier,viewport,render in modifiers:modifier.show_viewport=viewport;modifier.show_render=render
            for key,value in zip(keys,values):key.value=value
            for driver,mute in zip(drivers,muted):driver.mute=mute
            for curve,mute in zip(action_curves,action_muted):curve.mute=mute
            part.hide_render=True;part.hide_set(True)
        if actual_normals is not None:
            # Never transfer a strongly different normal across nearby sheets
            # or leave a deleted proxy corner with an unrelated authored value.
            limit=math.cos(math.radians(15))
            for polygon in clone.data.polygons:
                name=clone.data.materials[polygon.material_index].name
                if name not in repair_materials:continue
                for index in polygon.loop_indices:
                    a=normals[index];b=actual_normals[index]
                    dot=sum(x*y for x,y in zip(a,b))
                    if index not in transferred or not all(math.isfinite(v) for v in a) or dot<limit:
                        normals[index]=b;safe_replaced+=1
        clone.data.normals_split_custom_set(normals)
        if authored_encoded is not None:
            encoded = array('i', [0]) * (len(clone.data.loops) * 2)
            target = clone.data.attributes.get('custom_normal')
            target.data.foreach_get('value', encoded)
            for index in excluded_corners:
                encoded[index*2:index*2+2] = authored_encoded[index*2:index*2+2]
            target.data.foreach_set('value', encoded)
            for index, sharp in authored_sharp.items():
                clone.data.edges[index].use_edge_sharp = sharp
        clone.data.update()
        if excluded_corners:
            actual = clone.data.corner_normals
            error = max(abs(actual[index].vector[axis]-normals[index][axis])
                        for index in excluded_corners for axis in range(3))
            if error > 1e-6:
                raise ValueError(f'排除部件的原法线保留校验失败（最大误差 {error:.8g}），已停止导出')
            source_counts[-1].update(excluded_corners=len(excluded_corners),excluded_normal_max_error=error)
        assert clone.data.has_custom_normals
        assert len(clone.data.vertices)==source_counts[-1]['vertices']
        assert len(clone.data.polygons)==source_counts[-1]['faces']
        source_counts[-1].update(transferred_corners=len(transferred),total_corners=len(normals),
            retained_authored_corners=len(normals)-len(transferred),safe_actual_surface_corners=safe_replaced)
        results.append(clone)
    return results,{'mode':'weld_weighted','experimental':True,'split_by':'original_pmx_material','parts':receipts,
        'weld_distance_m':distance,'modifier_order':['WELD','WEIGHTED_NORMAL'],
        'export_mesh_modifiers':False,'export_topology':'original_unchanged',
        'morph_transfer':'original_shape_data_unchanged','normal_transfer':'original_corner_identity',
        'export_meshes':source_counts,'safe_actual_surface_normals':bool(safe_normals),
        **({'excluded_materials':sorted(excluded_materials)} if excluded_materials else {}),
        **({'weighted_only_materials':sorted(weighted_only_materials)} if weighted_only_materials else {})}
