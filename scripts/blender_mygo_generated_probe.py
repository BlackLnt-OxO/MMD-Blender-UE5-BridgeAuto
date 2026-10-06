"""Native Cycles oracle for GN Generated; source blend is never saved."""
import sys
from pathlib import Path
import bpy
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json
SOURCE=Path(r"F:\MMD_Scene\Blender\SableFiSh\MyGO-ParkingLots\It'sMyGO!!!!!-ParkingLots.blend")
OUT=ROOT/'work/material_restoration/generated_probe';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=str(SOURCE),load_ui=False,use_scripts=False)
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=1
scene.render.bake.margin=0
rows=[]
for name in ('Cube.049','Cube.050','Cube.051'):
    obj=bpy.data.objects[name]
    bpy.ops.object.select_all(action='DESELECT');obj.hide_set(False);obj.hide_render=False
    obj.select_set(True);bpy.context.view_layer.objects.active=obj
    mesh=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.copy()
    coord=np.asarray([v.undeformed_co[:] for v in mesh.vertices],dtype=np.float32)
    center=np.asarray(mesh.texspace_location);size=np.asarray(mesh.texspace_size)
    generated=(coord-center)/(2*size)+.5
    # Candidate uses the exact evaluated mesh texture space, not source-host vertex indices.
    indices=np.asarray([l.vertex_index for l in mesh.loops])
    xy=mesh.uv_layers.new(name='MMDBridge_Generated_XY');zw=mesh.uv_layers.new(name='MMDBridge_Generated_ZW')
    xy.data.foreach_set('uv',generated[indices,:2].astype(np.float32).ravel())
    packed=np.zeros((len(indices),2),dtype=np.float32);packed[:,0]=generated[indices,2]
    zw.data.foreach_set('uv',packed.ravel())
    mats=set(m for m in mesh.materials if m)
    obj.data=obj.data.copy()
    for mat in mats:
        if mat.name not in obj.data.materials:obj.data.materials.append(mat)
    saved=[];images=[]
    try:
        for mat in mats:
            tree=mat.node_tree;output=tree.get_output_node('CYCLES')
            old=output.inputs['Surface'].links[0].from_socket
            emit=tree.nodes.new('ShaderNodeEmission');tex=tree.nodes.new('ShaderNodeTexCoord')
            tree.links.new(tex.outputs['Generated'],emit.inputs['Color']);tree.links.new(emit.outputs[0],output.inputs['Surface'])
            image=bpy.data.images.new('Probe_'+name,width=64,height=64,float_buffer=True)
            target=tree.nodes.new('ShaderNodeTexImage');target.image=image;tree.nodes.active=target
            saved.append((tree,output,old,emit,tex,target));images.append(image)
        bpy.ops.object.bake(type='EMIT',uv_layer='UVMap',margin=0)
        reference=[np.asarray(image.pixels[:],dtype=np.float32) for image in images]
        frozen=bpy.data.objects.new('ProbeFrozen',mesh);scene.collection.objects.link(frozen)
        frozen.matrix_world=obj.matrix_world;obj.select_set(False);frozen.select_set(True)
        bpy.context.view_layer.objects.active=frozen
        from mmd_bridge.scene_shader_data import pin_generated
        for tree,*_ in saved:pin_generated(tree,mesh)
        bpy.ops.object.bake(type='EMIT',uv_layer='UVMap',margin=0)
        errors=[float(np.max(np.abs(before-np.asarray(image.pixels[:],dtype=np.float32))))
                for before,image in zip(reference,images)]
        rows.append(dict(object=name,vertices=len(mesh.vertices),center=center.tolist(),size=size.tolist(),errors=errors))
        assert max(errors)<1e-4,rows[-1]
        bpy.data.objects.remove(frozen,do_unlink=True)
    finally:
        for tree,output,old,emit,tex,target in saved:
            tree.links.new(old,output.inputs['Surface'])
            for node in (emit,tex,target):tree.nodes.remove(node)
        for image in images:bpy.data.images.remove(image)
atomic_json(OUT/'result.json',dict(complete=True,rows=rows))
print('MYGO_GENERATED_PASS',rows,flush=True)
