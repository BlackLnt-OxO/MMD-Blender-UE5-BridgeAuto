"""Compare per-part Weld/Weighted Normal shading with original morph topology."""
from array import array
import math
import bpy


def prepare_comparison(meshes, distance=.0001):
    """Transfer evaluated normals; preserve original vertices, UVs and morphs."""
    if not math.isfinite(distance) or not 0 < distance <= .01:
        raise ValueError('Weld distance must be in (0, 0.01] metres')
    editable=bpy.data.collections.new('MMDBridge_Editable_Weld_WeightedNormal')
    exported=bpy.data.collections.new('MMDBridge_Export_Weld_WeightedNormal')
    bpy.context.scene.collection.children.link(editable)
    bpy.context.scene.collection.children.link(exported)
    results=[];receipts=[];source_counts=[]
    for number,source in enumerate(meshes):
        clone=source.copy();clone.data=source.data.copy();clone.name=f'PBR_Finished_{number:03d}'
        exported.objects.link(clone)
        normals=[tuple(normal.vector) for normal in clone.data.corner_normals]
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
            keys=list(part.data.shape_keys.key_blocks) if part.data.shape_keys else []
            values=[key.value for key in keys]
            drivers=list(part.data.shape_keys.animation_data.drivers) if keys and part.data.shape_keys.animation_data else []
            muted=[driver.mute for driver in drivers]
            for driver in drivers:driver.mute=True
            for key in keys:key.value=0.
            modifiers=[(modifier,modifier.show_viewport,modifier.show_render) for modifier in part.modifiers]
            for modifier,_,_ in modifiers:modifier.show_viewport=modifier.show_render=False
            weld=part.modifiers.new('MMDBridge_Weld','WELD');weld.mode='ALL';weld.merge_threshold=distance
            weighted=part.modifiers.new('MMDBridge_WeightedNormal','WEIGHTED_NORMAL')
            weighted.mode='FACE_AREA';weighted.weight=50;weighted.keep_sharp=True
            part.modifiers.move(part.modifiers.find(weld.name),0);part.modifiers.move(part.modifiers.find(weighted.name),1)
            bpy.context.view_layer.update();depsgraph=bpy.context.evaluated_depsgraph_get()
            evaluated=part.evaluated_get(depsgraph);mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
            try:
                indices=mesh.attributes.get(identity)
                assert indices and len(indices.data)==len(mesh.loops)
                for key,normal in zip(indices.data,mesh.corner_normals):
                    index=key.value;assert 0<=index<len(normals)
                    normals[index]=tuple(normal.vector);transferred.add(index)
                receipts.append({'part':part.name,'materials':[m.name for m in mesh.materials if m],
                    'vertices_before':len(part.data.vertices),'vertices_after':len(mesh.vertices),
                    'faces_before':len(part.data.polygons),'faces_after':len(mesh.polygons),
                    'shape_keys_before':len(keys),'shape_keys_after':len(keys),
                    'weld_distance_m':distance,'weighted_mode':'FACE_AREA','weighted_weight':50,'keep_sharp':True,
                    'modifier_order':['WELD','WEIGHTED_NORMAL','ARMATURE']})
            finally:evaluated.to_mesh_clear()
            for modifier,viewport,render in modifiers:modifier.show_viewport=viewport;modifier.show_render=render
            for key,value in zip(keys,values):key.value=value
            for driver,mute in zip(drivers,muted):driver.mute=mute
            part.hide_render=True;part.hide_set(True)
        clone.data.normals_split_custom_set(normals);clone.data.update()
        assert clone.data.has_custom_normals
        assert len(clone.data.vertices)==source_counts[-1]['vertices']
        assert len(clone.data.polygons)==source_counts[-1]['faces']
        source_counts[-1].update(transferred_corners=len(transferred),total_corners=len(normals),
            retained_authored_corners=len(normals)-len(transferred))
        results.append(clone)
    return results,{'mode':'weld_weighted','experimental':True,'split_by':'original_pmx_material','parts':receipts,
        'weld_distance_m':distance,'modifier_order':['WELD','WEIGHTED_NORMAL'],
        'export_mesh_modifiers':False,'export_topology':'original_unchanged',
        'morph_transfer':'original_shape_data_unchanged','normal_transfer':'original_corner_identity',
        'export_meshes':source_counts}
