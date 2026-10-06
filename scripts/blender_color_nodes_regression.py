"""Extract real Blender graphs and native Cycles values for UE graph parity."""
import sys
from pathlib import Path
import bpy, numpy as np, OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/color_nodes_regression';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=2);obj=bpy.context.object
records=[]
cases=[('hsv_neutral','ShaderNodeHueSaturation',{'Hue':.5,'Saturation':1,'Value':1}),
       ('hsv_adjusted','ShaderNodeHueSaturation',{'Hue':.17,'Saturation':.3,'Value':1.3}),
       ('hsv_hdr','ShaderNodeHueSaturation',{'Hue':.45,'Saturation':.7,'Value':1.7}),
       ('bright','ShaderNodeBrightContrast',{'Bright':.2,'Contrast':.3}),
       ('gamma','ShaderNodeGamma',{'Gamma':2.2}),('gamma_zero','ShaderNodeGamma',{'Gamma':0}),
       ('separate_hsv','ShaderNodeSeparateColor',{}),('map_range','ShaderNodeMapRange',{}),
       ('less_than','ShaderNodeMath',{}),('divide_zero','ShaderNodeMath',{}),
       ('multiply_add','ShaderNodeMath',{})] + [
       ('mix_'+mode.lower(),'ShaderNodeMixRGB',{}) for mode in ('HUE','SATURATION','VALUE','COLOR')] + [
       ('mapping_'+mode.lower(),'ShaderNodeMapping',{}) for mode in ('POINT','TEXTURE','VECTOR','NORMAL','DYNAMIC')]
for name,kind,params in cases:
    mat=bpy.data.materials.new(name);mat.use_nodes=True
    tree=mat.node_tree;tree.nodes.clear()
    output=tree.nodes.new('ShaderNodeOutputMaterial');p=tree.nodes.new('ShaderNodeBsdfPrincipled')
    tree.links.new(p.outputs[0],output.inputs['Surface'])
    node=tree.nodes.new(kind)
    for key,val in params.items():node.inputs[key].default_value=val
    if node.inputs.get('Color'):node.inputs['Color'].default_value=(1.8,.35,.12,1) if name=='hsv_hdr' else (.2,.6,.1,1)
    if name=='separate_hsv':node.mode='HSV'
    if name=='map_range':
        node.inputs['Value'].default_value=.3;node.inputs['To Min'].default_value=2;node.inputs['To Max'].default_value=-1
    if name=='less_than':node.operation='LESS_THAN';node.inputs[0].default_value=.3;node.inputs[1].default_value=.7
    if name=='divide_zero':node.operation='DIVIDE';node.inputs[0].default_value=2;node.inputs[1].default_value=0
    if name=='multiply_add':
        node.operation='MULTIPLY_ADD';node.inputs[0].default_value=.7
        node.inputs[1].default_value=-.07;node.inputs[2].default_value=.02
    if name.startswith('mix_'):
        node.blend_type=name[4:].upper();node.inputs[0].default_value=.7
        node.inputs[1].default_value=(.2,.6,.1,1);node.inputs[2].default_value=(.9,.2,.1,1)
    source=node.outputs[0]
    if name.startswith('mapping_'):
        mode=name[8:].upper();node.vector_type='POINT' if mode=='DYNAMIC' else mode
        node.inputs['Vector'].default_value=(.4,-.2,.9)
        node.inputs['Scale'].default_value=(1.2,.8,1.4)
        if node.inputs.get('Location'):node.inputs['Location'].default_value=(.1,-.3,.2)
        node.inputs['Rotation'].default_value=(.21,-.47,.8)
        if mode=='DYNAMIC':
            combine=tree.nodes.new('ShaderNodeCombineXYZ')
            for s,v in zip(combine.inputs,(.21,-.47,.8)):s.default_value=v
            tree.links.new(combine.outputs[0],node.inputs['Rotation'])
        encode=tree.nodes.new('ShaderNodeMixRGB');encode.blend_type='ADD'
        encode.inputs[0].default_value=1;encode.inputs[2].default_value=(2,2,2,1)
        tree.links.new(source,encode.inputs[1]);source=encode.outputs[0]
    if name=='multiply_add':
        # UE clamps final emissive radiance to nonnegative values. Encode the
        # negative intermediate with an offset, testing that the math itself
        # retains its sign before it reaches the renderer.
        encode=tree.nodes.new('ShaderNodeMath');encode.operation='ADD'
        encode.inputs[1].default_value=1
        tree.links.new(source,encode.inputs[0]);source=encode.outputs[0]
    tree.links.new(source,p.inputs['Base Color'])
    obj.data.materials.clear();obj.data.materials.append(mat)
    # A real source stamp invalidates native reference pixels when the fixture
    # graph changes, just as a changed .blend invalidates production bakes.
    converter=Materials(OUT/name,str(Path(__file__).resolve()),resolution=16)
    record=converter.analyze(mat,obj=obj)
    assert not record['baked_channels'] and not record.get('material_error'),record
    # Reference uses Cycles' actual emitted output, independently of UE logic.
    texture=converter.bake(obj,mat,p.inputs['Base Color'],'emission','reference-'+name)
    inp=oiio.ImageInput.open(texture['image']['path']);a=inp.read_image(format=oiio.FLOAT);inp.close()
    record['native_reference']=a[4:12,4:12,:3].mean(axis=(0,1)).tolist()
    record['asset_name']='M_ColorNode_'+name
    records.append(record)
atomic_json(OUT/'records.json',records)
atomic_json(OUT/'blender_result.json',{'complete':True,'version':bpy.app.version_string,'count':len(records)})
print('BLENDER_COLOR_NODES_PASS',len(records),flush=True)
