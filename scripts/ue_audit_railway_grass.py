"""Read saved grass blend overrides and material input connections."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json
out=ROOT/'work/material_functions_implementation/grass_shadow'
result={'complete':False,'read_only':True,'materials':[]}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
    scene=read_json(job.folder/'manifest.json')['scene'];report=read_json(job.folder/'ue_report.json')
    for record in scene['materials']:
        if 'grass' not in record['name'].lower():continue
        instance=u.load_asset(report['assets']['materials'][record['id']]);parent=instance.get_editor_property('parent')
        overrides=instance.get_editor_property('base_property_overrides')
        item={'name':record['name'],'instance':instance.get_path_name(),'parent':parent.get_path_name(),
            'parent_blend_mode':str(parent.get_editor_property('blend_mode')),
            'parent_shading_model':str(parent.get_editor_property('shading_model')),
            'override_blend_mode':overrides.get_editor_property('override_blend_mode'),
            'instance_blend_mode':str(overrides.get_editor_property('blend_mode'))}
        for name in ('MP_OPACITY_MASK','MP_OPACITY','MP_SUBSURFACE_COLOR'):
            source=u.MaterialEditingLibrary.get_material_property_input_node(parent,getattr(u.MaterialProperty,name))
            item[name]=source.get_path_name() if source else None
        result['materials'].append(item)
    result['complete']=True
except Exception:
    result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(out/'saved_grass_audit.json',result)
print('GRASS_AUDIT',result,flush=True)
