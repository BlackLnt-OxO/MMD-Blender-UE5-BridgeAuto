"""Independent Cycles pixels for curve lookup, extrapolation and ramp edges."""
import sys
from pathlib import Path
import bpy, OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/material_functions_implementation/curves';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.mesh.primitive_plane_add()
obj=bpy.context.object;records=[]
for kind in ('curve','cardinal'):
    for x in (-.2,0.,.27,.5,1.,1.2):
        mat=bpy.data.materials.new(kind+str(x));mat.use_nodes=True
        tree=mat.node_tree;tree.nodes.clear()
        output=tree.nodes.new('ShaderNodeOutputMaterial');p=tree.nodes.new('ShaderNodeBsdfPrincipled')
        tree.links.new(p.outputs[0],output.inputs['Surface'])
        node=tree.nodes.new('ShaderNodeRGBCurve' if kind=='curve' else 'ShaderNodeValToRGB')
        if kind=='curve':
            node.mapping.initialize()
            node.mapping.curves[0].points.new(.4,.23)
            node.mapping.curves[3].points.new(.6,.75);node.mapping.update()
            node.inputs['Color'].default_value=(x,x*.7,x*.5,1)
            node.inputs['Fac'].default_value=.8
            node.mapping.black_level=(.02,.01,.03);node.mapping.white_level=(.9,.95,.85)
        else:
            node.color_ramp.interpolation='CARDINAL'
            node.color_ramp.elements[0].color=(.1,.2,.3,.5)
            node.color_ramp.elements[1].color=(.8,.4,.2,1)
            e=node.color_ramp.elements.new(.45);e.color=(.4,.9,.1,.8)
            node.inputs[0].default_value=x
        tree.links.new(node.outputs[0],p.inputs['Base Color'])
        obj.data.materials.clear();obj.data.materials.append(mat)
        converter=Materials(OUT/('case_'+str(len(records))),str(Path(__file__)),resolution=16)
        converter.surface_policy={}
        # Keep the operator under test; emulate its use inside a parameterized group.
        converter._function_frames.append({'bindings':{}})
        expr=converter.expr(p.inputs['Base Color']);converter._function_frames.clear()
        reference=converter.bake(obj,mat,p.inputs['Base Color'],'emission',mat.name)
        image=oiio.ImageInput.open(reference['image']['path']);a=image.read_image(format=oiio.FLOAT);image.close()
        records.append({'name':mat.name,'asset_name':'M_Curve_'+str(len(records)),
                        'channels':{'base_color':expr},'native_reference':a[4:12,4:12,:3].mean(axis=(0,1)).tolist()})
atomic_json(OUT/'records.json',records)
print('CURVE_REFERENCE_PASS',len(records),flush=True)
