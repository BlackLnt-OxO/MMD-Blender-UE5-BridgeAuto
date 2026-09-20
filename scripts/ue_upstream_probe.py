"""Read-only capability probe in an isolated UE 5.8 test project."""
import json
import sys
import os
from pathlib import Path
import unreal

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.ue_mmd2unreal import probe_dependencies

report = {"engine": unreal.SystemLibrary.get_engine_version()}
try:
    report["dependencies"] = probe_dependencies()
    report["classes"] = [name for name in dir(unreal) if any(k in name.lower() for k in ("pmx", "vmd", "mmd", "kawaii"))]
    report["factory_help"] = {name: getattr(unreal, name).__doc__ for name in report["classes"] if "Factory" in name}
    material = unreal.new_object(unreal.Material)
    node = unreal.MaterialEditingLibrary.create_material_expression(material, unreal.MaterialExpressionMaterialFunctionCall)
    node.set_editor_property("material_function", unreal.load_asset("/Engine/Functions/Engine_MaterialFunctions01/Texturing/DeriveTangentBasis"))
    report["tangent_basis"] = {
        "inputs": list(unreal.MaterialEditingLibrary.get_material_expression_input_names(node)),
        "outputs": list(unreal.MaterialEditingLibrary.get_material_expression_output_names(node))}
    report["normal_enums"] = {"world": str(unreal.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_WORLD),
                              "tangent": str(unreal.MaterialVectorCoordTransform.TRANSFORM_TANGENT)}
    os.environ['CAMCUT_NO_AUTORUN'] = '1'
    from mmd_bridge.ue_camera_cut_baker import run_probe
    report['camera_cut'] = run_probe()
    report["passed"] = True
except Exception as exc:
    report.update(passed=False, error=str(exc))
(root / "work/upstream58/probe.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
