"""Native Blender evidence for group sharing, pruning, colors and railway bark."""
import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bpy
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_appearance import expose_material_output
from mmd_bridge.blender_usd import _mesh_data,_write_chunk
from mmd_bridge.material_graph import analyze_material,constant_socket
from mmd_bridge.core import atomic_json

args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
out=Path(args[0] if args else 'work/material_functions_implementation/blender').resolve()
out.mkdir(parents=True,exist_ok=True)
railway='--railway' in args
if railway:
    bpy.ops.wm.open_mainfile(filepath=r'F:\MMD_Scene\Blender\SableFiSh\railway\railway.blend',load_ui=False,use_scripts=False)
    obj=bpy.data.objects['Cypress_5']
    names=['trunk.006','trunk.007']
    if '--leaves' in args:names=list(dict.fromkeys(m.name for m in obj.data.materials if m))
else:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_plane_add();obj=bpy.context.object
    layer=obj.data.color_attributes.new(name='TestColor',type='FLOAT_COLOR',domain='CORNER')
    layer.data.foreach_set('color',[.2,.4,.6,1]*len(layer.data))
    names=[]
    for i in range(2):
        mat=bpy.data.materials.new('UnrelatedPreset'+str(i));mat.use_nodes=True;tree=mat.node_tree;tree.nodes.clear()
        group=bpy.data.node_groups.new('DifferentName'+str(i),'ShaderNodeTree')
        for label,kind in [('Strength','NodeSocketFloat'),('Strength','NodeSocketFloat'),('Color','NodeSocketColor')]:
            group.interface.new_socket(name=label,in_out='INPUT',socket_type=kind)
        group.interface.new_socket(name='Result',in_out='OUTPUT',socket_type='NodeSocketShader')
        ins=group.nodes.new('NodeGroupInput');outs=group.nodes.new('NodeGroupOutput')
        bsdf=group.nodes.new('ShaderNodeBsdfPrincipled');mult=group.nodes.new('ShaderNodeMath');mult.operation='MULTIPLY'
        group.links.new(ins.outputs[0],mult.inputs[0]);group.links.new(ins.outputs[1],mult.inputs[1])
        group.links.new(mult.outputs[0],bsdf.inputs['Roughness'])
        group.links.new(ins.outputs[2],bsdf.inputs['Base Color'])
        group.links.new(bsdf.outputs[0],outs.inputs[0])
        call=tree.nodes.new('ShaderNodeGroup');call.node_tree=group
        call.inputs[0].default_value=.2+i*.1;call.inputs[1].default_value=.7
        color=tree.nodes.new('ShaderNodeVertexColor');color.layer_name='TestColor'
        tree.links.new(color.outputs['Color'],call.inputs[2])
        output=tree.nodes.new('ShaderNodeOutputMaterial');tree.links.new(call.outputs[0],output.inputs['Surface'])
        obj.data.materials.append(mat);names.append(mat.name)
converter=Materials(out,'railway' if railway else str(Path(__file__)),resolution=1024 if '--leaves' in args else 16)
converter.restore_scene_appearance=True
converter.isolated_baking=True
records=[];start=time.perf_counter()
for name in names:
    mat=bpy.data.materials[name].copy();expose_material_output(mat,bpy.context.scene.render.engine)
    # A supposedly closed noise branch must never enter a bake.
    def forbidden(*a,**k):raise AssertionError('Unexpected field bake in bark/fixture')
    if '--leaves' not in args:converter.procedural_field=forbidden
    record=converter.analyze(mat,obj,source_name=name)
    atomic_json(out/(name.replace('.','_')+'_debug.json'),record)
    assert not record.get('material_error') and not record.get('unsupported_closures'),{k:record.get(k) for k in ('name','material_error','unsupported_closures')}
    assert all(c.startswith('field_') for c in record['baked_channels']),{k:record.get(k) for k in ('name','baked_channels','unsupported_closures')}
    if '--leaves' not in args:assert not record.get('baked_fields'),record
    assert record.get('function_definitions'),record
    records.append(record)
geometry=_mesh_data(obj.data)
assert geometry.get('colors'),obj.data.color_attributes[:]
group={'id':'fixture','geometry':geometry,'materials':[records[0]['id']],
       'polygon_materials':[0]*len(obj.data.polygons),'collision':False,'instances':[]}
if '--leaves' in args:
    group.update(materials=[next(r['id'] for r in records if r['name']==m.name) for m in obj.data.materials],
                 polygon_materials=[p.material_index for p in obj.data.polygons])
_write_chunk(out/'colors.usdc',0,[group],1,False,'/Game/MMDBridgeValidation/FunctionsV2','fixture',definitions_only=True,
             material_names={r['id']:r['asset_name'] for r in records})
from pxr import Usd,UsdGeom
stage=Usd.Stage.CreateNew(str(out/'scene.usda'))
UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.SetStageMetersPerUnit(stage,1.)
mesh=UsdGeom.Xform.Define(stage,'/World/ColoredMesh')
mesh.GetPrim().GetReferences().AddReference('colors.usdc',group['prim_path'])
stage.GetRootLayer().Save()
report={'source':'railway' if railway else 'fixtures','materials':records,'functions':list(converter.functions.definitions.values()),
        'seconds':time.perf_counter()-start,'bake_stats':converter.bake_stats,'usd':str(out/'colors.usdc'),
        'vertices':len(obj.data.vertices),'colors':len(geometry['colors']['rgb'])}
atomic_json(out/'report.json',report)
print('FUNCTION_REGRESSION='+json.dumps({k:v for k,v in report.items() if k not in ('materials','functions')},ensure_ascii=False))
