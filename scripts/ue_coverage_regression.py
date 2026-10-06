"""Native UE compile/save/reuse proof for the generic coverage converter."""
import sys, traceback
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json, read_json, file_hash
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/coverage_regression'
report={'checks':{},'warnings':[],'assets':{},'materials':[]}
try:
    converter=Materials('/Game/test/CoverageRegression_20261004','CoverageRegression_20261004',report,
        {'restore_scene_appearance':True,'use_scene_master_material':False})
    records=read_json(OUT/'records.json')
    source_path=OUT/'railway/source_result.json'
    if source_path.exists():
        source=read_json(source_path)
        assert source['complete'], source['errors']
        for i,record in enumerate(source['records']):
            record['asset_name']='M_SourceCoverage_'+str(i+1)
            records.append(record)
    for record in records:
        instance=converter.build(record,OUT)
        parent=instance.get_editor_property('parent')
        assert parent.get_editor_property('blend_mode')==u.BlendMode.BLEND_MASKED,parent.get_path_name()
        assert parent.get_editor_property('two_sided'),parent.get_path_name()
        mask=u.MaterialEditingLibrary.get_material_property_input_node(parent,u.MaterialProperty.MP_OPACITY_MASK)
        assert mask is not None,parent.get_path_name()
        errors=list(u.MaterialEditingLibrary.recompile_material(parent) or [])
        assert not errors, errors
        assert converter.reuse(record),record['name']
        # An old opaque parent carrying the same owner must not pass the cache check.
        parent.set_editor_property('blend_mode',u.BlendMode.BLEND_OPAQUE)
        assert not converter.reuse(record),record['name']
        parent.set_editor_property('blend_mode',u.BlendMode.BLEND_MASKED)
        assert u.EditorAssetLibrary.save_loaded_asset(parent)
        assert not list(u.MaterialEditingLibrary.recompile_material(parent) or [])
        report['materials'].append({'name':record['name'],'parent':parent.get_path_name(),
            'blend_mode':str(parent.get_editor_property('blend_mode')),
            'shading_model':str(parent.get_editor_property('shading_model')),
            'opacity_mask_node':mask.get_class().get_name(),'compile_errors':errors,'reuse_checked':True})
    report.update(complete=True,engine=u.SystemLibrary.get_engine_version(),
        material_code_sha256=file_hash(ROOT/'mmd_bridge/ue_materials.py'))
except Exception:
    report.update(complete=False,error=traceback.format_exc())
    u.log_error(report['error'])
atomic_json(OUT/'ue_result.json',report)
print('NATIVE_COVERAGE_RESULT',report['complete'],len(report['materials']),flush=True)
