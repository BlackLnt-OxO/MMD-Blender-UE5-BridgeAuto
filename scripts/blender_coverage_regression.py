"""Native Blender coverage checks using anonymous materials, not asset names."""
import sys
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
from mmd_bridge.surface_coverage import constant

OUT=ROOT/'work/coverage_regression'
OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=2)
obj=bpy.context.object
image=bpy.data.images.new('Anonymous mask',width=2,height=2,alpha=True)
image.pixels=[.2,.8,.1,0, .2,.8,.1,1, .2,.8,.1,0, .2,.8,.1,1]
image.filepath_raw=str(OUT/'mask.png')
image.file_format='PNG'
image.save()
records=[]
checks=[]
for case in ('normal','swapped','nested','nested_shader_input','principled_alpha','diffuse','pure_transparent','unused','scattering','procedural','colored','add'):
    mat=bpy.data.materials.new('Anonymous '+case)
    mat.use_nodes=True
    tree=mat.node_tree
    tree.nodes.clear()
    output=tree.nodes.new('ShaderNodeOutputMaterial')
    base=tree.nodes.new('ShaderNodeBsdfPrincipled')
    base.inputs['Base Color'].default_value=(.2,.8,.1,1)
    transparent=tree.nodes.new('ShaderNodeBsdfTransparent')
    texture=tree.nodes.new('ShaderNodeTexImage'); texture.image=image
    mix=tree.nodes.new('ShaderNodeMixShader')
    tree.links.new(texture.outputs['Alpha'],mix.inputs[0])
    tree.links.new(transparent.outputs[0],mix.inputs[1])
    tree.links.new(base.outputs[0],mix.inputs[2])
    tree.links.new(mix.outputs[0],output.inputs['Surface'])
    if case=='swapped':
        tree.links.new(base.outputs[0],mix.inputs[1]); tree.links.new(transparent.outputs[0],mix.inputs[2])
    if case=='principled_alpha': base.inputs['Alpha'].default_value=.6
    if case=='diffuse':
        diffuse=tree.nodes.new('ShaderNodeBsdfDiffuse')
        diffuse.inputs['Color'].default_value=(.2,.8,.1,1)
        tree.links.new(diffuse.outputs[0],mix.inputs[2])
    if case=='unused': tree.links.new(base.outputs[0],output.inputs['Surface'])
    if case=='pure_transparent': tree.links.new(transparent.outputs[0],output.inputs['Surface'])
    if case=='colored': transparent.inputs['Color'].default_value=(.2,1,1,1)
    if case=='add':
        add=tree.nodes.new('ShaderNodeAddShader')
        tree.links.new(base.outputs[0],add.inputs[0]); tree.links.new(transparent.outputs[0],add.inputs[1])
        tree.links.new(add.outputs[0],output.inputs['Surface'])
    if case=='scattering':
        diffuse_transmission=tree.nodes.new('ShaderNodeBsdfTranslucent')
        inner=tree.nodes.new('ShaderNodeMixShader'); inner.inputs[0].default_value=.35
        tree.links.new(base.outputs[0],inner.inputs[1]); tree.links.new(diffuse_transmission.outputs[0],inner.inputs[2])
        tree.links.new(inner.outputs[0],mix.inputs[2])
    if case in ('nested','nested_shader_input'):
        group=bpy.data.node_groups.new('Anonymous wrapped '+case,'ShaderNodeTree')
        group.interface.new_socket(name='Mask',in_out='INPUT',socket_type='NodeSocketFloat')
        if case=='nested_shader_input':
            group.interface.new_socket(name='Solid',in_out='INPUT',socket_type='NodeSocketShader')
        group.interface.new_socket(name='Surface',in_out='OUTPUT',socket_type='NodeSocketShader')
        gi=group.nodes.new('NodeGroupInput'); go=group.nodes.new('NodeGroupOutput')
        tr=group.nodes.new('ShaderNodeBsdfTransparent'); p=group.nodes.new('ShaderNodeBsdfPrincipled')
        gm=group.nodes.new('ShaderNodeMixShader')
        group.links.new(gi.outputs['Mask'],gm.inputs[0]); group.links.new(tr.outputs[0],gm.inputs[1])
        group.links.new(gi.outputs['Solid'] if case=='nested_shader_input' else p.outputs[0],gm.inputs[2])
        group.links.new(gm.outputs[0],go.inputs['Surface'])
        gn=tree.nodes.new('ShaderNodeGroup'); gn.node_tree=group
        tree.links.new(texture.outputs['Alpha'],gn.inputs['Mask'])
        if case=='nested_shader_input': tree.links.new(base.outputs[0],gn.inputs['Solid'])
        reroute=tree.nodes.new('NodeReroute')
        tree.links.new(gn.outputs[0],reroute.inputs[0]); tree.links.new(reroute.outputs[0],output.inputs['Surface'])
    if case=='procedural':
        gamma=tree.nodes.new('ShaderNodeGamma'); gamma.inputs['Gamma'].default_value=2
        tree.links.new(texture.outputs['Alpha'],gamma.inputs['Color']); tree.links.new(gamma.outputs[0],mix.inputs[0])
    obj.data.materials.clear(); obj.data.materials.append(mat)
    obj.pop('_mmd_uv_pinned',None)
    converter=Materials(OUT/case,'anonymous-coverage-fixture',resolution=32,strategy='nodes' if case!='procedural' else 'auto')
    converter.restore_scene_appearance=True
    record=converter.analyze(mat,obj=obj)
    if case in ('colored','add'):
        assert record.get('coverage_error') and record['requires_visual_review'],record
    elif case=='unused':
        assert not record.get('coverage_source') and record['channels']['opacity']==constant(1),record
    else:
        assert record.get('coverage_source')=='shader_mix' and record['channels']['opacity']!=constant(1),record
        if case=='scattering':
            assert record['thin_surface_transmission'] and abs(record['channels']['transmission']['value']-.35)<1e-6,record
        if case=='diffuse':
            assert not record.get('material_error') and record['channels']['specular']==constant(0),record
        if case=='pure_transparent': assert record['channels']['opacity']==constant(0),record
        if case=='procedural': assert not record['baked_channels'],record
        record['asset_name']='M_Coverage_'+case
        record['surface_policy']={'size_m':[10,12,18]}
        records.append(record)
    checks.append({'case':case,'coverage':record.get('coverage_source'),'error':record.get('coverage_error')})
atomic_json(OUT/'records.json',records)
# MMD Diffuse/Toon groups must retain their dedicated base-texture route.
character=bpy.data.materials.new('Anonymous character diffuse')
character.use_nodes=True
tree=character.node_tree; tree.nodes.clear()
output=tree.nodes.new('ShaderNodeOutputMaterial')
group=bpy.data.node_groups.new('Anonymous character wrapper','ShaderNodeTree')
group.interface.new_socket(name='Shader',in_out='OUTPUT',socket_type='NodeSocketShader')
go=group.nodes.new('NodeGroupOutput'); diffuse=group.nodes.new('ShaderNodeBsdfDiffuse')
transform=group.nodes.new('ShaderNodeVectorTransform')
group.links.new(transform.outputs[0],diffuse.inputs['Color'])
group.links.new(diffuse.outputs[0],go.inputs[0])
gn=tree.nodes.new('ShaderNodeGroup');gn.node_tree=group
texture=tree.nodes.new('ShaderNodeTexImage');texture.image=image;texture.name='mmd_base_tex'
tree.links.new(gn.outputs[0],output.inputs['Surface'])
converter=Materials(OUT/'character','anonymous-character-fixture')
record=converter.analyze(character,pmx_index=0)
assert record['channels']['base_color']['op']=='texture' and record['channels']['opacity']['output']=='Alpha',record
checks.append({'case':'character_diffuse_preserves_texture_route','pmx_index':record['pmx_index']})
atomic_json(OUT/'blender_result.json',{'complete':True,'checks':checks})
print('BLENDER_COVERAGE_REGRESSION_PASS',len(checks),flush=True)
