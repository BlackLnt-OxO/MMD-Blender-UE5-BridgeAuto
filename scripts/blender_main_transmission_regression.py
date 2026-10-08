"""Native main-policy Glass, continuous coverage and exclusion fixtures."""
import sys,json
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/material_restoration/main_transmission_native';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene;scene.render.engine='CYCLES'
bpy.ops.mesh.primitive_cube_add(size=.5);obj=bpy.context.object
source=OUT/'fixture.blend';bpy.ops.wm.save_as_mainfile(filepath=str(source))
converter=Materials(OUT/'Scene',str(source),64,workflow='legacy_main_v1');converter.restore_scene_appearance=True
records=[]
for name in ('GlassLamp','Repeated19Glass','DenseSmallGlass','LeafGlass','TaggedGlass','PrincipledTransmission','ContinuousAlpha','OpaqueControl'):
 material=bpy.data.materials.new(name);material.use_nodes=True;tree=material.node_tree;tree.nodes.clear()
 output=tree.nodes.new('ShaderNodeOutputMaterial')
 if 'Glass' in name:
  glass=tree.nodes.new('ShaderNodeBsdfGlass');transparent=tree.nodes.new('ShaderNodeBsdfTransparent');mix=tree.nodes.new('ShaderNodeMixShader')
  mix.inputs[0].default_value=.05;tree.links.new(transparent.outputs[0],mix.inputs[1]);tree.links.new(glass.outputs[0],mix.inputs[2]);shader=mix
 else:
  shader=tree.nodes.new('ShaderNodeBsdfPrincipled')
  if name=='PrincipledTransmission':shader.inputs['Transmission Weight'].default_value=1
  if name=='ContinuousAlpha':shader.inputs['Alpha'].default_value=.2
  if name=='OpaqueControl':shader.inputs['Coat Weight'].default_value=1
 tree.links.new(shader.outputs[0],output.inputs['Surface']);obj.data.materials.clear();obj.data.materials.append(material)
 if name=='TaggedGlass':
  material['mmd_bridge_foliage']=True
  material=material.copy();obj.data.materials[0]=material
  assert material['mmd_bridge_foliage']
 obj['_mmd_material_usage']=64 if name=='DenseSmallGlass' else 19 if name=='Repeated19Glass' else 1
 record=converter.analyze(material,obj,source_name=name,geometry_identity=name)
 if name in ('GlassLamp','Repeated19Glass','PrincipledTransmission'):
  assert record.get('surface_graph') and 'transmission' in record['closure_features'],record
  assert not record.get('opaque_transmission_fallback'),record
 if name=='GlassLamp':assert record['continuous_opacity'] and abs(record['channels']['opacity']['value']-.05)<1e-6
 if name=='ContinuousAlpha':assert record.get('surface_graph') and record['continuous_opacity'],record
 if name in ('DenseSmallGlass','LeafGlass','TaggedGlass'):
  assert not record['main_transmission_attempt']['eligible'] and record['opaque_transmission_fallback'],record
 if name=='OpaqueControl':assert not record.get('main_transmission_attempt') and not record.get('surface_graph'),record
 assert converter._main_transmission_repair is False
 records.append(record)
atomic_json(OUT/'records.json',records);atomic_json(OUT/'result.json',dict(complete=True,cases=len(records),opaque_controls_preserved=True,foliage_and_dense_props_excluded=True))
print('MAIN_TRANSMISSION_NATIVE_PASS',flush=True)
