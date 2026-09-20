"""Read-only evaluated-scene inventory used before expensive material export."""
import bpy
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.material_policy import classify_surface

source, output = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
deps = bpy.context.evaluated_depsgraph_get()
records = []
for obj in bpy.context.scene.objects:
    if obj.type != "MESH" or obj.hide_render:
        continue
    evaluated = obj.evaluated_get(deps)
    mesh = evaluated.to_mesh()
    if not mesh.vertices:
        evaluated.to_mesh_clear()
        continue
    points = [evaluated.matrix_world @ v.co for v in mesh.vertices]
    size = [(max(p[i] for p in points) - min(p[i] for p in points)) * bpy.context.scene.unit_settings.scale_length for i in range(3)]
    uv = mesh.uv_layers[0] if mesh.uv_layers else None
    span = [max((p.uv[i] for p in uv.data), default=1) - min((p.uv[i] for p in uv.data), default=0) for i in range(2)] if uv else [1, 1]
    records.append({"name": obj.name, "vertices": len(mesh.vertices), "policy": classify_surface(size, span, planar=min(size)<0.02),
                    "materials": [mat.name if mat else None for mat in mesh.materials]})
    evaluated.to_mesh_clear()
Path(output).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
