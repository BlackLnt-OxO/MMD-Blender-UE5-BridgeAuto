"""Compile native UE graphs and read their GPU-rendered linear output."""
import sys,traceback
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/color_nodes_regression';report={'checks':{},'warnings':[],'assets':{},'materials':[]}
try:
    world=u.EditorLoadingAndSavingUtils.new_blank_map(False)
    converter=Materials('/Game/test/ColorNodesRegression_20261004','ColorNodesRegression_20261004',report)
    for record in read_json(OUT/'records.json'):
        atomic_json(OUT/'ue_progress.json',{'name':record['name'],'stage':'build'})
        parent=converter.create(record['asset_name'],u.Material,u.MaterialFactoryNew())
        converter.material=parent
        u.MaterialEditingLibrary.delete_all_material_expressions(parent)
        parent.set_editor_property('shading_model',u.MaterialShadingModel.MSM_UNLIT)
        result=converter.expr(record['channels']['base_color'])
        assert u.MaterialEditingLibrary.connect_material_property(result[0],result[1],u.MaterialProperty.MP_EMISSIVE_COLOR)
        errors=list(u.MaterialEditingLibrary.recompile_material(parent) or [])
        assert not errors,errors
        assert u.EditorAssetLibrary.save_loaded_asset(parent)
        atomic_json(OUT/'ue_progress.json',{'name':record['name'],'stage':'render'})
        rt=u.RenderingLibrary.create_render_target2d(world,16,16,u.TextureRenderTargetFormat.RTF_RGBA32F)
        u.RenderingLibrary.draw_material_to_render_target(world,rt,parent)
        got=u.RenderingLibrary.read_render_target_raw_pixel(world,rt,8,8,normalize=False)
        values=[got.r,got.g,got.b];expected=record['native_reference']
        error=max(abs(a-b) for a,b in zip(values,expected))
        report['materials'].append({'name':record['name'],'native_cycles':expected,'native_ue':values,
            'max_error':error,'compile_errors':errors,'parent':parent.get_path_name()})
        assert error<.003,(record['name'],values,expected)
        u.RenderingLibrary.release_render_target2d(rt)
        atomic_json(OUT/'ue_progress.json',{'name':record['name'],'stage':'verified','max_error':error})
    report['complete']=True
except Exception:
    report.update(complete=False,error=traceback.format_exc());u.log_error(report['error'])
atomic_json(OUT/'ue_result.json',report)
print('UE_COLOR_NODES_RESULT',report['complete'],flush=True)
u.SystemLibrary.quit_editor()
