"""Build actual UE materials and verify transmission modes and connections."""
import sys,time,uuid
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/material_restoration/main_transmission_native'
owner='TransmissionValidation_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:4]
report={'checks':{},'warnings':[]};builder=Materials('/Game/'+owner,owner,report,{'thin_surface_scattering':'legacy_diffusion'})
results=[]
for record in read_json(OUT/'records.json'):
 instance=builder.build(record,OUT);asset=instance.get_path_name();parent=instance.get_editor_property('parent')
 blend=parent.get_editor_property('blend_mode')
 if record['name'] in ('GlassLamp','Repeated19Glass','PrincipledTransmission','ContinuousAlpha'):
  assert blend==u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE,(record['name'],blend)
  assert builder.lib.get_material_property_input_node(parent,u.MaterialProperty.MP_OPACITY)
  assert not builder.lib.get_material_property_input_node(parent,u.MaterialProperty.MP_OPACITY_MASK)
 if record['name']=='OpaqueControl':assert blend==u.BlendMode.BLEND_OPAQUE
 results.append(dict(name=record['name'],asset=asset,parent=parent.get_path_name(),blend_mode=str(blend)))
atomic_json(OUT/'ue_result.json',dict(complete=True,materials=results,report=report))
print('MAIN_TRANSMISSION_UE_COMPILE_PASS',flush=True)
