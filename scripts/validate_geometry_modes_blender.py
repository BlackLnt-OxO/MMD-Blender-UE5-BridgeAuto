"""Native geometry topology, normal, morph, UV and selection contract."""
from pathlib import Path
import sys, json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
import numpy as np
from mmd_bridge import blender_surface_repair as surface
from mmd_bridge.character_geometry import prepare_character_geometry, MODES

output=Path(sys.argv[sys.argv.index('--')+1]);output.mkdir(parents=True,exist_ok=True)
rows=[]
for mode,authored in [*((mode,False) for mode in MODES),('weld_weighted_except_hair',True),('weld_weighted_no_hair_weld',True)]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    points=[];faces=[]
    for slot in range(2):
        start=len(points)
        points.extend((x*.02+slot*.15,y*.02,.01 if x==2 and y==2 else 0) for y in range(5) for x in range(5))
        for y in range(4):
            for x in range(4):
                a=start+y*5+x;faces.extend([(a,a+1,a+6),(a,a+6,a+5)])
    mesh=bpy.data.meshes.new('PresetFixture');mesh.from_pydata(points,[],faces);mesh.update()
    for name in ('Body','Hair'):mesh.materials.append(bpy.data.materials.new(name))
    for p in mesh.polygons:p.material_index=int(p.index>=32);p.use_smooth=True
    if authored:
        mesh.normals_split_custom_set([(.08,.15,.985)]*len(mesh.loops))
    uv=mesh.uv_layers.new(name='UVMap')
    for p in mesh.polygons:
        for n,i in enumerate(p.loop_indices):uv.data[i].uv=((n==1)*.8,(n==2)*.8)
    obj=bpy.data.objects.new('PresetFixture',mesh);bpy.context.scene.collection.objects.link(obj)
    basis=obj.shape_key_add(name='Basis');key=obj.shape_key_add(name='Bend')
    for p in key.data:p.co.z+=p.co.x*.04
    group=obj.vertex_groups.new(name='head');group.add(list(range(len(points))),1.,'REPLACE')
    normal_before=[tuple(n.vector) for n in mesh.corner_normals]
    coordinates_before=[tuple(v.co) for v in mesh.vertices]
    records=[{'name':name,'asset_name':name,'pmx_index':i,'character_material':{'pbr':{'type':kind,'params':{},'alpha_mode':'opaque'}}}
             for i,(name,kind) in enumerate((('Body','skin'),('Hair','hair')))]
    cfg={'character_backend':'mmdbridge_fbx','character_material_profile':'character_pbr',
         'character_geometry_mode':mode,'model':'fixture_not_a_real_model.pmx'}
    import mmd_bridge.pmx_geometry
    saved=mmd_bridge.pmx_geometry.read_geometry
    mmd_bridge.pmx_geometry.read_geometry=lambda *a,**k:{i:{'humanoid_rig':True,'body_weight_ratio':1.} for i in (0,1)}
    try:results,receipt=prepare_character_geometry([obj],records,cfg)
    finally:mmd_bridge.pmx_geometry.read_geometry=saved
    result=results[0]
    assert set(result.data.shape_keys.key_blocks.keys())=={'Basis','Bend'}
    assert result.data.uv_layers.get('UVMap')
    assert all(v.groups for v in result.data.vertices)
    assert not any(m.type in ('WELD','WEIGHTED_NORMAL','SUBSURF') for m in result.modifiers)
    if mode=='original':
        assert result is obj
        assert [tuple(n.vector) for n in result.data.corner_normals]==normal_before
    if mode in ('original','weld_weighted','weld_weighted_except_hair','weld_weighted_no_hair_weld'):
        assert len(result.data.vertices)==len(points)
        assert [tuple(v.co) for v in result.data.vertices]==coordinates_before
        if mode=='weld_weighted_except_hair':
            hair_loops=[i for p in result.data.polygons if p.material_index==1 for i in p.loop_indices]
            error=float(np.max(np.abs(np.array([result.data.corner_normals[i].vector[:] for i in hair_loops])-np.array(normal_before)[hair_loops])))
            assert error<=1e-6,error
            if authored:assert error==0,error
            finish=receipt['normal_finish']
            assert finish['export_meshes'][0]['excluded_normal_max_error']<=1e-6
            hair_parts=[p for p in finish['parts'] if p.get('mode')=='original']
            assert len(hair_parts)==1 and hair_parts[0]['materials']==['Hair']
            assert not set(hair_parts[0]['modifier_order']) & {'WELD','WEIGHTED_NORMAL'}
        if mode=='weld_weighted_no_hair_weld':
            finish=receipt['normal_finish']
            hair_parts=[p for p in finish['parts'] if p.get('mode')=='weighted_only']
            assert len(hair_parts)==1 and hair_parts[0]['materials']==['Hair']
            assert hair_parts[0]['modifier_order']==['WEIGHTED_NORMAL','ARMATURE']
            assert hair_parts[0]['vertices_before']==hair_parts[0]['vertices_after']
            assert hair_parts[0]['faces_before']==hair_parts[0]['faces_after']
            body_parts=[p for p in finish['parts'] if p['materials']==['Body']]
            assert body_parts[0]['modifier_order']==['WELD','WEIGHTED_NORMAL','ARMATURE']
            if authored:
                hair_loops=[i for p in result.data.polygons if p.material_index==1 for i in p.loop_indices]
                changed=float(np.max(np.abs(np.array([result.data.corner_normals[i].vector[:] for i in hair_loops])-np.array(normal_before)[hair_loops])))
                assert changed>1e-5,'Weighted-only hair did not update authored normals'
    else:
        assert len(result.data.vertices)>len(points)
        targets=receipt['meshes'][0]['target_pmx_indices']
        assert targets==([0] if mode=='local_subdivision' else [0,1])
        if mode=='local_subdivision':
            indices={i for p in result.data.polygons if p.material_index==1 for i in p.vertices}
            expected=set(coordinates_before[25:])
            assert {tuple(result.data.vertices[i].co) for i in indices}==expected
    rows.append({'mode':mode,'authored_normals':authored,'complete':True,'counts':receipt['counts']})

# Every preset must offset a genuine Alpha layer; the no-modifier mode
# keeps all other positions and authored corner normals unchanged.
for mode in MODES:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    coords=[(x*.02,y*.02,(x*x+y*y)*.002) for y in range(3) for x in range(3)]
    faces=[]
    for y in range(2):
        for x in range(2):
            a=y*3+x;faces.extend([(a,a+1,a+4),(a,a+4,a+3)])
    coords.extend(coords[i] for i in (0,1,4,3));faces.extend([(9,10,11),(9,11,12)])
    mesh=bpy.data.meshes.new('LayerFixture');mesh.from_pydata(coords,[],faces);mesh.update()
    for name in ('Body','AnonymousOverlay'):mesh.materials.append(bpy.data.materials.new(name))
    for p in mesh.polygons:p.material_index=int(p.index>=8);p.use_smooth=True
    image=bpy.data.images.new('AlphaSource',width=2,height=2,alpha=True)
    image.pixels=[1.,.2,.4,1.,1.,.2,.4,0.,1.,.2,.4,0.,1.,.2,.4,1.]
    overlay_material=mesh.materials[1];overlay_material.use_nodes=True
    image_node=overlay_material.node_tree.nodes.new('ShaderNodeTexImage');image_node.image=image
    uv=mesh.uv_layers.new(name='UVMap')
    for p in mesh.polygons:
        for n,i in enumerate(p.loop_indices):uv.data[i].uv=((n==1)*.8,(n==2)*.8)
    obj=bpy.data.objects.new('LayerFixture',mesh);bpy.context.scene.collection.objects.link(obj)
    group=obj.vertex_groups.new(name='head');group.add(list(range(len(coords))),1.,'REPLACE')
    obj.shape_key_add(name='Basis');bend=obj.shape_key_add(name='Bend')
    for point in bend.data:point.co.z+=point.co.x*.1
    hidden=obj.shape_key_add(name='Hide')
    for point in hidden.data:point.co=(0.,0.,0.)
    before=surface._points(mesh.vertices).copy();normals=np.array([n.vector[:] for n in mesh.corner_normals])
    uv_before=np.array([d.uv[:] for d in mesh.uv_layers.active.data]);pixels_before=list(image.pixels)
    records=[{'name':name,'asset_name':name,'pmx_index':i,
              'character_material':{'pbr':{'type':kind,'alpha_mode':alpha,'params':{}}}}
             for i,(name,kind,alpha) in enumerate((('Body','skin','opaque'),('AnonymousOverlay','generic','masked')))]
    cfg={'character_backend':'mmdbridge_fbx','character_material_profile':'character_pbr',
         'character_geometry_mode':mode,'model':'fixture_not_a_real_model.pmx'}
    import mmd_bridge.pmx_geometry
    saved=mmd_bridge.pmx_geometry.read_geometry
    mmd_bridge.pmx_geometry.read_geometry=lambda *a,**k:{0:{'humanoid_rig':True,'body_weight_ratio':1.}}
    try:results,receipt=prepare_character_geometry([obj],records,cfg)
    finally:mmd_bridge.pmx_geometry.read_geometry=saved
    result=results[0]
    overlay=receipt.get('overlay_repair') or receipt
    attachment=overlay['meshes'][0]['attachments'][0]
    assert attachment['complete'] and attachment['spacing_world_m']==.0001
    assert np.array_equal(surface._points(mesh.vertices),before),'source geometry modified'
    assert np.array_equal(np.array([d.uv[:] for d in mesh.uv_layers.active.data]),uv_before)
    assert set(result.data.shape_keys.key_blocks.keys())=={'Basis','Bend','Hide'}
    assert not np.any(surface._points(result.data.shape_keys.key_blocks['Hide'].data))
    assert list(image.pixels)==pixels_before and result.data.materials[1]==overlay_material
    assert not any(m.type in ('WELD','WEIGHTED_NORMAL','SUBSURF') for m in result.modifiers)
    pairs=np.asarray(attachment['export_vertex_pairs'],np.int32)
    for name in ('Basis','Bend'):
        positions=surface._points(result.data.shape_keys.key_blocks[name].data)
        spacing=np.linalg.norm(positions[pairs[:,0]]-positions[pairs[:,1]],axis=1)
        assert float(np.abs(spacing-.0001).max())<1e-7
    if mode in ('original','weld_weighted','weld_weighted_except_hair','weld_weighted_no_hair_weld'):
        assert len(result.data.vertices)==13 and len(result.data.polygons)==10
        assert np.array_equal(surface._points(result.data.vertices)[:9],before[:9])
        if mode=='original':
            actual=np.array([n.vector[:] for n in result.data.corner_normals])
            host_loops=[i for p in result.data.polygons if p.material_index==0 for i in p.loop_indices]
            assert np.array_equal(actual[host_loops],normals[host_loops])
    rows.append({'mode':mode,'alpha_layer_complete':True,'complete':True,
                 'mapped_vertices':attachment['mapped_vertices'],'offset_m':attachment['spacing_world_m']})
(output/'native_geometry_modes.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
print('SIX_GEOMETRY_NATIVE_READY',len(rows),flush=True)
