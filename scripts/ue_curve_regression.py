"""Read actual UE GPU pixels; no CPU transcription of the target expression."""
import sys,traceback
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json,file_hash
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/material_functions_implementation/curves';report={'checks':{},'warnings':[],'assets':{},'materials':[]}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    world=u.EditorLoadingAndSavingUtils.new_blank_map(False)
    converter=Materials('/Game/MMDBridgeValidation/FunctionsV2/Curves','CurveValidation',report)
    for record in read_json(OUT/'records.json'):
        parent=converter.create(record['asset_name'],u.Material,u.MaterialFactoryNew())
        converter.material=parent;u.MaterialEditingLibrary.delete_all_material_expressions(parent)
        parent.set_editor_property('shading_model',u.MaterialShadingModel.MSM_UNLIT)
        result=converter.expr(record['channels']['base_color'])
        assert u.MaterialEditingLibrary.connect_material_property(*result,u.MaterialProperty.MP_EMISSIVE_COLOR)
        errors=list(u.MaterialEditingLibrary.recompile_material(parent) or []);assert not errors,errors
        assert u.EditorAssetLibrary.save_loaded_asset(parent)
        rt=u.RenderingLibrary.create_render_target2d(world,16,16,u.TextureRenderTargetFormat.RTF_RGBA32F)
        u.RenderingLibrary.draw_material_to_render_target(world,rt,parent)
        got=u.RenderingLibrary.read_render_target_raw_pixel(world,rt,8,8,normalize=False)
        values=[got.r,got.g,got.b];expected=[max(0,v) for v in record['native_reference']]
        error=max(abs(a-b) for a,b in zip(values,expected))
        report['materials'].append(dict(name=record['name'],cycles=expected,ue=values,max_error=error))
        assert error<.003,(record['name'],values,expected,error)
        u.RenderingLibrary.release_render_target2d(rt)
    report.update(complete=True,source_sha256=file_hash(ROOT/'mmd_bridge/ue_materials.py'))
except Exception:report.update(complete=False,error=traceback.format_exc());u.log_error(report['error'])
atomic_json(OUT/'ue_result.json',report);print('CURVE_NATIVE_RESULT',report['complete'],flush=True)
