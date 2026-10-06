"""Native UE asset/connection/reload verification, without a visual claim."""
import sys, traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.ue_materials import Materials

OUT=ROOT/'work/material_restoration/semantic_plan_v1'
report={'complete':False,'warnings':[],'materials':[],'failures':[],
        'validation_scope':'native material assets, connections, saved reuse; no visual acceptance',
        'command_line':u.SystemLibrary.get_command_line()}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    builder=Materials('/Game/MMDBridgeValidation/SemanticPlanV1','SemanticPlanV1',report,
        {'restore_scene_appearance':True,'use_scene_master_material':False,'material_conversion_timeout':120})
    records=read_json(OUT/'records.json')
    real=OUT/'final_real_mygo/records.json'
    if real.is_file():records+=read_json(real)
    for record in records:
        instance=builder.build(record,OUT)
        parent=instance.get_editor_property('parent')
        if record['name']=='CutoutGlass':
            assert parent.get_editor_property('blend_mode')==u.BlendMode.BLEND_MASKED
            assert u.MaterialEditingLibrary.get_material_property_input_node(parent,u.MaterialProperty.MP_OPACITY_MASK)
        if record['name']=='WetFresnel':
            assert record['surface_graph']['kind']=='mix' and not record.get('basic_pbr_bake')
            assert u.MaterialEditingLibrary.get_material_property_input_node(parent,u.MaterialProperty.MP_FRONT_MATERIAL)
        if record['name']=='Patterned_Concrete_ugfnfifo':
            assert record['strategy']=='hybrid' and not record.get('basic_pbr_bake')
            assert record['surface_graph']['kind']=='mix'
        assert u.EditorAssetLibrary.save_loaded_asset(parent)
        builder.protect_asset(parent)
        assert builder.reuse(record),'saved native material did not reuse'
        report['materials'].append({'name':record['name'],'path':instance.get_path_name(),
            'parent':parent.get_path_name(),'blend_mode':str(parent.get_editor_property('blend_mode')),
            'strategy':record['strategy'],'saved_reuse':True})
        atomic_json(OUT/'ue_result.json',report)
    report['runtime_code_hashes']={name:file_hash(ROOT/'mmd_bridge'/name) for name in (
        'material_plan.py','material_policy.py','material_graph.py','material_contract.py','ue_materials.py')}
    report['complete']=True
except Exception:
    report['failures'].append(traceback.format_exc())
    u.log_error(report['failures'][-1])
atomic_json(OUT/'ue_result.json',report)
print('UE_SEMANTIC_PLAN_RESULT',report['complete'],len(report['materials']),flush=True)
