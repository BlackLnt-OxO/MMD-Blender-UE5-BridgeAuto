"""Independent Cycles ORCO comparisons for topology-preserving deformations."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy,numpy as np,OpenImageIO as oiio
from mmd_bridge.core import atomic_json
from mmd_bridge.scene_shader_data import ensure_generated_layers,pin_generated
from mmd_bridge.blender_materials import Materials
out=ROOT/'work/material_functions_implementation/generated/deform';out.mkdir(parents=True,exist_ok=True)
results=[]
for mode in ('DISPLACE','SIMPLE_DEFORM','COMBINED'):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8,radius=2)
    source=bpy.context.object;source.scale=(-1.3,.7,2.1)
    if mode in ('DISPLACE','COMBINED'):
        mod=source.modifiers.new('ArbitraryA','DISPLACE')
        mod.texture=bpy.data.textures.new('IndependentNoise',type='CLOUDS');mod.strength=.8
    if mode in ('SIMPLE_DEFORM','COMBINED'):
        mod=source.modifiers.new('ArbitraryB','SIMPLE_DEFORM');mod.deform_method='TWIST';mod.angle=.7
    material=bpy.data.materials.new('GeneratedReference');material.use_nodes=True;source.data.materials.append(material)
    tree=material.node_tree;shader=next(n for n in tree.nodes if n.type=='BSDF_PRINCIPLED')
    coordinates=tree.nodes.new('ShaderNodeTexCoord');tree.links.new(coordinates.outputs['Generated'],shader.inputs['Base Color'])
    converter=Materials(out,mode,resolution=128);converter.isolated_baking=True;converter.surface_policy={'category':'small'}
    def pixels(expression):
        image=oiio.ImageInput.open(expression['image']['path']);data=image.read_image(format=oiio.FLOAT);image.close();return data
    reference=pixels(converter.bake(source,material,shader.inputs['Base Color'],'field_reference',mode+'-native'))
    mesh=source.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.copy()
    target=bpy.data.objects.new('FrozenDeformed',mesh);bpy.context.scene.collection.objects.link(target);target.matrix_world=source.matrix_world
    indices=ensure_generated_layers(source,mesh);pin_generated(tree,mesh)
    actual=pixels(converter.bake(target,material,shader.inputs['Base Color'],'field_adapter',mode+'-carrier'))
    error=float(np.max(np.abs(reference-actual)));results.append(dict(mode=mode,max_error=error,indices=indices))
    assert error<1e-5,results[-1]
atomic_json(out/'result.json',dict(complete=True,comparisons=results))
print('DEFORM_GENERATED_PASS',results,flush=True)
