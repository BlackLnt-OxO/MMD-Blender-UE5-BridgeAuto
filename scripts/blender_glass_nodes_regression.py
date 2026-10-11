import sys,json,runpy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/glass_nodes_20261011/fixtures';OUT.mkdir(parents=True,exist_ok=True)
checks=runpy.run_path(str(ROOT/'tests/test_glass_geometry.py'))
for name,test in checks.items():
    if name.startswith('test_'):test()
bpy.context.scene.render.engine='CYCLES'
bpy.ops.mesh.primitive_plane_add(size=2)
obj=bpy.context.object

def material(name):
    result=bpy.data.materials.new(name);result.use_nodes=True
    result.node_tree.nodes.clear()
    return result,result.node_tree.nodes.new('ShaderNodeOutputMaterial')
def group(material,node):
    tree=bpy.data.node_groups.new('Nested_'+node.bl_idname,'ShaderNodeTree')
    tree.interface.new_socket(name='Shader',in_out='OUTPUT',socket_type='NodeSocketShader')
    output=tree.nodes.new('NodeGroupOutput')
    copy=tree.nodes.new(node.bl_idname)
    for socket in node.inputs:
        if hasattr(socket,'default_value') and copy.inputs.get(socket.name):
            copy.inputs[socket.name].default_value=socket.default_value
    tree.links.new(copy.outputs[0],output.inputs[0])
    wrapped=material.node_tree.nodes.new('ShaderNodeGroup');wrapped.node_tree=tree
    return wrapped
results=[]
for name in ('group_holes','reroute_holes','partial_alpha','texture_alpha','colored_transparent','colored_mix','inactive_glass','group_refraction','group_glass','bound_group','principled_transmission','add_glass','thin_scattering'):
    mat,output=material(name);nodes=mat.node_tree.nodes;links=mat.node_tree.links
    if name in ('group_holes','reroute_holes'):
        base=nodes.new('ShaderNodeBsdfDiffuse')
        hole=nodes.new('ShaderNodeBsdfTransparent')
        if name=='group_holes':hole=group(mat,hole)
        else:
            reroute=nodes.new('NodeReroute');links.new(hole.outputs[0],reroute.inputs[0]);hole=reroute
        mix=nodes.new('ShaderNodeMixShader');mix.inputs[0].default_value=.25
        links.new(base.outputs[0],mix.inputs[1]);links.new(hole.outputs[0],mix.inputs[2]);root=mix
    elif name=='partial_alpha':
        a=nodes.new('ShaderNodeBsdfPrincipled');a.inputs['Alpha'].default_value=.2
        b=nodes.new('ShaderNodeBsdfPrincipled');b.inputs['Alpha'].default_value=.8
        root=nodes.new('ShaderNodeMixShader');root.inputs[0].default_value=.5
        links.new(a.outputs[0],root.inputs[1]);links.new(b.outputs[0],root.inputs[2])
    elif name=='texture_alpha':
        root=nodes.new('ShaderNodeBsdfPrincipled')
        image=bpy.data.images.new('CoverageTexture',width=4,height=4,alpha=True)
        image.pixels=[value for i in range(16) for value in (1,1,1,float(i%2))]
        texture=nodes.new('ShaderNodeTexImage');texture.image=image
        links.new(texture.outputs['Alpha'],root.inputs['Alpha'])
    elif name=='colored_transparent':
        root=nodes.new('ShaderNodeBsdfTransparent');root.inputs['Color'].default_value=(.2,.5,1,1)
    elif name in ('colored_mix','inactive_glass'):
        a=nodes.new('ShaderNodeBsdfDiffuse')
        b=nodes.new('ShaderNodeBsdfTransparent' if name=='colored_mix' else 'ShaderNodeBsdfGlass')
        if name=='colored_mix':b.inputs['Color'].default_value=(.2,.5,1,1)
        root=nodes.new('ShaderNodeMixShader');root.inputs[0].default_value=.3 if name=='colored_mix' else 0
        links.new(a.outputs[0],root.inputs[1]);links.new(b.outputs[0],root.inputs[2])
    elif name in ('group_refraction','group_glass'):
        node=nodes.new('ShaderNodeBsdfRefraction' if name=='group_refraction' else 'ShaderNodeBsdfGlass')
        node.inputs['IOR'].default_value=1.45;node.inputs['Roughness'].default_value=.4
        root=group(mat,node)
    elif name=='bound_group':
        root=group(mat,nodes.new('ShaderNodeBsdfGlass'))
        tree=root.node_tree
        tree.interface.new_socket(name='IOR',in_out='INPUT',socket_type='NodeSocketFloat')
        inp=tree.nodes.new('NodeGroupInput')
        leaf=next(n for n in tree.nodes if n.type=='BSDF_GLASS')
        tree.links.new(inp.outputs['IOR'],leaf.inputs['IOR'])
        root.inputs['IOR'].default_value=1.7
    elif name=='principled_transmission':
        root=nodes.new('ShaderNodeBsdfPrincipled')
        weight=nodes.new('ShaderNodeMath');weight.operation='MULTIPLY'
        weight.inputs[0].default_value=.5;weight.inputs[1].default_value=1
        links.new(weight.outputs[0],root.inputs['Transmission Weight'])
    elif name=='add_glass':
        root=nodes.new('ShaderNodeAddShader')
        links.new(nodes.new('ShaderNodeBsdfDiffuse').outputs[0],root.inputs[0])
        links.new(nodes.new('ShaderNodeBsdfGlass').outputs[0],root.inputs[1])
    else:root=group(mat,nodes.new('ShaderNodeBsdfTranslucent'))
    links.new(root.outputs[0],output.inputs['Surface'])
    obj.data.materials.clear();obj.data.materials.append(mat)
    converter=Materials(OUT/name,'fixture.blend',resolution=64,workflow='legacy_main_v1')
    converter.restore_scene_appearance=True
    record=converter.analyze(mat,obj)
    if name in ('group_holes','reroute_holes'):
        assert record['channels']['opacity']['value']==.75,record
        assert 'transmission' not in record.get('closure_features',[]),record
    if name=='partial_alpha':assert abs(record['channels']['opacity']['value']-.5)<1e-6,record
    if name=='colored_transparent':assert 'straight_transmission' in record['closure_features'],record
    if name=='colored_mix':assert 'straight_transmission' in record['closure_features'],record
    if name=='inactive_glass':assert 'transmission' not in record['closure_features'],record
    if name in ('group_glass','group_refraction','principled_transmission','add_glass','bound_group'):
        assert 'transmission' in record['closure_features'],record
    if name=='bound_group':
        expanded=converter.functions.expand(record['surface_graph'])
        assert abs(expanded['channels']['ior']['value']-1.7)<1e-6,record
    if name=='texture_alpha':
        assert record['channels']['opacity']['op']!='constant',record
        assert 'transmission' not in record.get('closure_features',[]),record
    if name=='thin_scattering':assert record.get('thin_surface_transmission'),record
    results.append(record)
atomic_json(OUT/'records.json',results)
print('BLENDER_NODE_FIXTURES_VALIDATED',len(results),flush=True)
