"""Native pixel/semantics regression plus read-only historical source planning."""
import sys
from pathlib import Path
import bpy
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_material_plan import source_material_plan
from mmd_bridge.blender_pbr_bake import native_pbr_channels
from mmd_bridge.core import atomic_json, file_hash, stable_id
from mmd_bridge.material_graph import constant_socket, output_domains

OUT=ROOT/'work/material_restoration/semantic_plan_v1'
OUT.mkdir(parents=True,exist_ok=True)
report={'complete':False,'checks':{},'source_plans':{},'records':[]}
atomic_json(OUT/'result.json',report)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene
scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=1

def fixture(name):
    bpy.ops.mesh.primitive_plane_add(size=2)
    obj=bpy.context.object
    obj['_mmd_uv_pinned']=True;obj['_mmd_geometry_identity']=name
    mat=bpy.data.materials.new(name);mat.use_nodes=True;obj.data.materials.append(mat)
    nodes,links=mat.node_tree.nodes,mat.node_tree.links
    return obj,mat,nodes,links,nodes.get('Principled BSDF'),nodes.get('Material Output')

def pixels(expr):
    assert expr['op']=='texture',expr
    image=bpy.data.images.load(expr['image']['path'],check_existing=False)
    image.colorspace_settings.name=expr['image']['color_space']
    values=np.array(image.pixels[:]).reshape(image.size[1],image.size[0],4)[3:-3,3:-3,:3]
    bpy.data.images.remove(image)
    return values

# An unconnected extracted output must be evaluated, never read as .default_value.
obj,mat,nodes,links,bsdf,terminal=fixture('VariableStaticPBR')
uv=nodes.new('ShaderNodeTexCoord');noise=nodes.new('ShaderNodeTexNoise')
noise.inputs['Scale'].default_value=3
links.new(uv.outputs['UV'],noise.inputs['Vector'])
for name in ('Base Color','Roughness','Metallic'):
    links.new(noise.outputs['Fac'],bsdf.inputs[name])
converter=Materials(OUT/'Scene','semantic-fixture',32,conversion_timeout=120)
converter.restore_scene_appearance=True;converter.isolated_baking=True
before_nodes=len(nodes)
record=converter.analyze(mat,obj,source_name='VariableStaticPBR')
assert record['automatic_route']['route']=='pbr_bake',record['automatic_route']
assert len(nodes)==before_nodes
for channel in ('base_color','roughness','metallic'):
    values=pixels(record['channels'][channel])
    assert float(np.ptp(values[:,:,0]))>.1,(channel,float(np.ptp(values[:,:,0])))
    assert channel not in record['basic_pbr_bake']['constant_channels']
report['records'].append(record)
report['checks']['variable_extraction_outputs_baked']=True

# Transmission degradation preserves the Checker cutout instead of a solid card.
obj,mat,nodes,links,bsdf,terminal=fixture('CutoutGlass')
glass=nodes.new('ShaderNodeBsdfGlass');glass.inputs['Color'].default_value=(.2,.6,.1,1)
transparent=nodes.new('ShaderNodeBsdfTransparent');mix=nodes.new('ShaderNodeMixShader')
uv=nodes.new('ShaderNodeTexCoord');checker=nodes.new('ShaderNodeTexChecker')
checker.inputs['Scale'].default_value=4
links.new(uv.outputs['UV'],checker.inputs['Vector'])
links.new(checker.outputs['Fac'],mix.inputs[0])
links.new(transparent.outputs[0],mix.inputs[1]);links.new(glass.outputs[0],mix.inputs[2])
links.new(mix.outputs[0],terminal.inputs['Surface'])
converter.force_bake[stable_id('semantic-fixture','CutoutGlass')]={
    'scope':'visual_transmission','opaque_fallback':True,'reason':'fixture appearance mismatch'}
count=len(nodes)
record=converter.analyze(mat,obj,source_name='CutoutGlass')
assert record['opaque_transmission_fallback']
alpha=pixels(record['channels']['opacity'])[:,:,0]
assert np.min(alpha)<.05 and np.max(alpha)>.95
assert .2<float(np.mean(alpha))<.8
assert len(nodes)==count and terminal.inputs['Surface'].links[0].from_node==mix
report['records'].append(record)
report['checks']['opaque_fallback_keeps_cutout']=True

# A static derivative is handled by a native normal adapter; live mix stays live.
obj,mat,nodes,links,bsdf,terminal=fixture('WetFresnel')
glossy=nodes.new('ShaderNodeBsdfGlossy');mix=nodes.new('ShaderNodeMixShader')
fresnel=nodes.new('ShaderNodeFresnel');noise=nodes.new('ShaderNodeTexNoise')
uv=nodes.new('ShaderNodeTexCoord');links.new(uv.outputs['UV'],noise.inputs['Vector'])
links.new(noise.outputs['Fac'],bsdf.inputs['Roughness'])
links.new(fresnel.outputs[0],mix.inputs[0]);links.new(bsdf.outputs[0],mix.inputs[1])
links.new(glossy.outputs[0],mix.inputs[2]);links.new(mix.outputs[0],terminal.inputs['Surface'])
plan=source_material_plan(mat)
assert plan['route']=='hybrid' and 'view' in plan['facts']['domains']
record=converter.analyze(mat,obj,source_name='WetFresnel')
assert record['surface_graph']['kind']=='mix' and not record.get('basic_pbr_bake')
assert 'fresnel' in str(converter.functions.expand(record['surface_graph']))
report['records'].append(record)
report['checks']['wet_surface_keeps_fresnel']=True

# A recoverable closure failure uses static fields and retains the live tree.
retry_obj=obj.copy();retry_obj.data=obj.data.copy();scene.collection.objects.link(retry_obj)
retry_mat=mat.copy();retry_obj.data.materials.clear();retry_obj.data.materials.append(retry_mat)
native_closure=converter.closure
calls=[0]
def fail_once(*args,**kwargs):
    calls[0]+=1
    if calls[0]==1:raise ValueError('fixture transient closure failure')
    return native_closure(*args,**kwargs)
converter.closure=fail_once
try:
    recovered=converter.analyze(retry_mat,retry_obj,source_name='WetLocalRecovery')
finally:converter.closure=native_closure
assert not recovered.get('basic_pbr_bake')
assert recovered['fallback_attempts'][-1]['route']=='local_fields'
assert recovered['fallback_attempts'][-1]['status']=='converted'
assert 'fresnel' in str(converter.functions.expand(recovered['surface_graph']))
report['records'].append(recovered)
report['checks']['local_failure_keeps_live_structure']=True

# Film transmission over a clipped card also keeps coverage when opaque.
obj,mat,nodes,links,bsdf,terminal=fixture('ThinLeaf')
thin=nodes.new('ShaderNodeBsdfTranslucent');transparent=nodes.new('ShaderNodeBsdfTransparent')
mix=nodes.new('ShaderNodeMixShader');mix.inputs[0].default_value=.3
links.new(transparent.outputs[0],mix.inputs[1]);links.new(thin.outputs[0],mix.inputs[2])
links.new(mix.outputs[0],terminal.inputs['Surface'])
with native_pbr_channels(mat,terminal.inputs['Surface'],opaque_fallback=True) as (channels,limits,normal):
    assert abs(constant_socket(channels['opacity'])-.3)<1e-6
report['checks']['thin_opaque_alpha_independent']=True

# A missing original Generated carrier blocks all routes before producing maps.
obj,mat,nodes,links,bsdf,terminal=fixture('BrokenGenerated')
noise=nodes.new('ShaderNodeTexNoise');links.new(noise.outputs['Fac'],bsdf.inputs['Base Color'])
obj['_mmd_generated_error']='fixture original coordinate mismatch'
written=converter.bake_stats['written']
try:converter.analyze(mat,obj,source_name='BrokenGenerated')
except ValueError as error:assert 'Generated' in str(error)
else:raise AssertionError('unreliable Generated unexpectedly converted')
assert converter.bake_stats['written']==written
assert not converter._active_record.get('basic_pbr_bake')
report['checks']['coordinate_error_blocks_bakes']=True

atomic_json(OUT/'records.json',report['records'])
# Plan historical sources from their effective graphs, without saving either .blend.
sources={
    'mygo':(Path(r"F:\MMD_Scene\Blender\SableFiSh\MyGO-ParkingLots\It'sMyGO!!!!!-ParkingLots.blend"),
        ['Patterned_Concrete_ugfnfifo','anno_y','riki_gold','tomori','uwb_pinkt']),
    'railway':(Path(r'F:\MMD_Scene\Blender\SableFiSh\railway\railway.blend'),
        ['Grass_smdrailr','Wild_Grass_vlkhcbxia','leaf','trunk'])}
for label,(path,names) in sources.items():
    digest=file_hash(path)
    bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False)
    plans={}
    for name in names:
        mat=bpy.data.materials.get(name)
        if mat is None:continue
        plan=source_material_plan(mat,bpy.context.scene.render.engine,
            policy={'category':'tiled' if name=='Patterned_Concrete_ugfnfifo' else 'small'})
        plans[name]=plan
        if name=='Patterned_Concrete_ugfnfifo':
            assert plan['route']=='hybrid' and 'layers' in plan['preserve'],plan
        if name=='anno_y':assert plan['route']=='nodes',plan
        if name in ('Grass_smdrailr','Wild_Grass_vlkhcbxia'):
            assert plan['facts']['coverage'],plan
            assert plan['route'] in ('nodes','hybrid'),plan
    assert digest==file_hash(path)
    report['source_plans'][label]={'sha256':digest,'plans':plans,'source_unchanged':True}
    atomic_json(OUT/'result.json',report)
report.update(complete=True,blender_version=bpy.app.version_string)
atomic_json(OUT/'result.json',report)
print('SEMANTIC_PLAN_NATIVE_PASS',list(report['checks']),flush=True)
