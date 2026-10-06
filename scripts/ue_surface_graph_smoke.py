"""Native UE material compilation and saved reload check, after Blender smoke."""
import sys,traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge import ue_materials

out=ROOT/'work/surface_graph_smoke'
report={'warnings':[]}
try:
    converter=ue_materials.Materials('/Game/TEST_ON_928/ClosureSmoke','ClosureSmoke',report,
        {'restore_scene_appearance':True,'use_scene_master_material':False})
    for record in read_json(out/'records.json'):
        instance=converter.build(record,out)
        material=instance.get_editor_property('parent')
        front=u.MaterialEditingLibrary.get_material_property_input_node(material,u.MaterialProperty.MP_FRONT_MATERIAL)
        assert isinstance(front,u.MaterialExpressionSubstrateHorizontalMixing if record['surface_graph']['kind']=='mix' else u.MaterialExpressionSubstrateAdd)
        assert not front.get_editor_property('use_parameter_blending')
        report.setdefault('materials',[]).append({'path':material.get_path_name(),'root':type(front).__name__,
            'saved':True})
        assert u.EditorAssetLibrary.save_loaded_asset(material)
        assert converter.reuse(record)
    report.update(complete=True,materials_code=ue_materials.__file__,materials_code_sha256=file_hash(ue_materials.__file__))
except Exception:
    report.update(complete=False,error=traceback.format_exc())
    u.log_error(report['error'])
atomic_json(out/'ue_result.json',report)
