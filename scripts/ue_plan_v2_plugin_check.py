"""Verify the test project's installed UE plugin sees the new texture policy."""
import json
from pathlib import Path

from mmd_bridge.material_policy import texture_role
from mmd_bridge.ue_materials import Materials

assert texture_role({"color_space": "sRGB"}, data=True) == "mask"
assert texture_role({"color_space": "Non-Color"}) == "linear_color"
assert hasattr(Materials, "texture")
project = Path(__file__).resolve().parents[1]
output = project / "work" / "plan_v2_validation" / "ue_plugin_import.json"
output.write_text(json.dumps({"ok": True, "policy_module": texture_role.__code__.co_filename},
                             ensure_ascii=False, indent=2), encoding="utf-8")
