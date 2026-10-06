"""Read saved standee materials without changing scene assets."""
import sys
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
import copy
from mmd_bridge.ue_materials import Materials
from mmd_bridge.core import read_json,atomic_json
OUT=ROOT/'work/material_restoration/full_scene'
job=Path(read_json(OUT/'job_path.json')['job'])
manifest=read_json(job/'manifest.json');report=read_json(job/'ue_report.json')
rows=[]
probe={'warnings':[],'failures':[]};converter=Materials('/Game/MMDBridgeValidation/MyGOClosureProbe','MyGOClosureProbe',probe,{'restore_scene_appearance':True,'material_conversion_timeout':120})
overrides={}
def front_only(value):
    if isinstance(value,dict):
        if value.get('op')=='backfacing':return {'op':'constant','value':0}
        return {k:front_only(v) for k,v in value.items()}
    if isinstance(value,list):return [front_only(v) for v in value]
    return value
for record in manifest['scene']['materials']:
    if record['name'] not in ('tomori','anno','neko','riki','soyo'):continue
    asset=u.load_asset(report['assets']['materials'][record['id']])
    diagnostic=copy.deepcopy(record);diagnostic['surface_graph']=front_only(diagnostic['surface_graph'])
    if record['name']=='tomori':
        # Opaque diagnostic control only: test the same imported texture and UVs.
        diagnostic['surface_graph']=diagnostic['surface_graph']['b']['a']
        diagnostic['closure_features']=[]
    elif record['name']=='anno':
        original_node=converter.node
        def blended_node(kind,**values):
            if kind=='SubstrateHorizontalMixing':values['use_parameter_blending']=True
            return original_node(kind,**values)
        converter.node=blended_node
    variant=converter.build(diagnostic,job)
    if record['name']=='anno':converter.node=original_node
    overrides[asset.get_path_name()]=variant.get_path_name()
    rows.append(dict(name=record['name'],id=record['id'],path=asset.get_path_name(),
        vectors=[dict(name=str(p.parameter_info.name),value=str(p.parameter_value)) for p in asset.vector_parameter_values],
        scalars=[dict(name=str(p.parameter_info.name),value=p.parameter_value) for p in asset.scalar_parameter_values],
        textures=[dict(name=str(p.parameter_info.name),value=p.parameter_value.get_path_name() if p.parameter_value else None) for p in asset.texture_parameter_values],
        parent=asset.parent.get_path_name(),blend=str(asset.parent.blend_mode)))
atomic_json(OUT/'visual_diagnostics.json',rows)
atomic_json(OUT/'front_probe_overrides.json',overrides)
print('VISUAL_DIAGNOSTICS',len(rows),flush=True)
