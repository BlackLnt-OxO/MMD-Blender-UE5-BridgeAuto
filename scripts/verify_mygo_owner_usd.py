"""Verify the fresh owner-grouped MyGO USD without opening Unreal."""
import json
import sys
from pathlib import Path

from pxr import Usd, UsdGeom, UsdPhysics, UsdShade

manifest = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
scene = manifest["scene"]
stage = Usd.Stage.Open(scene["usd"]["root_layer"], load=Usd.Stage.LoadAll)
assert stage, "USD Stage 无法打开"
owners = {item["name"]: item for item in scene["hierarchy"] if item["mesh"]}
assert len(owners) == len(scene["instances"]) == scene["stats"]["prototypes"]
meshes = [prim for prim in stage.Traverse() if prim.IsA(UsdGeom.Mesh)]
assert len(meshes) == len(owners), f"网格数量与 Blender 物体不一致: {len(meshes)} / {len(owners)}"
assert not any("/__Prototypes" in str(prim.GetPath()) for prim in stage.Traverse())
riki = next(item for item in scene["hierarchy"] if item["name"] == "riki")
assert not riki["mesh"]
for name in ("Cube.049", "Cube.050", "Cube.051", "Cube.052", "Cube.053",
             "Cube.054", "Cube.055", "Cube.056", "Cube.057", "Cylinder.123"):
    item = owners[name]
    assert item["parent"] == "riki" and item["prim_path"].startswith(riki["prim_path"] + "/")
    assert stage.GetPrimAtPath(item["prim_path"] + "/Mesh").IsA(UsdGeom.Mesh), name
assert len({item["name"] for item in scene["instances"]}) == len(scene["instances"])
assert len([item for item in scene["instances"] if item["name"] == "lamps"]) == 1
assert len([item for item in scene["instances"] if item["name"] == "parking block"]) == 1
cube = next(item for item in scene["prototypes"] if item["prim_path"] == owners["Cube.053"]["prim_path"])
assert len(cube["material_slots"]) > 1, "Cube.053 的多个材质槽丢失"
missing_collision = [item["id"] for item in scene["prototypes"] if item["collision"]
                     and not stage.GetPrimAtPath(item["prim_path"] + "/Mesh").HasAPI(UsdPhysics.CollisionAPI)]
assert not missing_collision, f"碰撞设置缺失: {missing_collision[:5]}"
assert any(prim.IsA(UsdGeom.Camera) for prim in stage.Traverse()) and stage.GetEndTimeCode() >= 4108
assert all(stage.GetPrimAtPath(item["prim_path"] + "/Mesh") for item in scene["instances"])
print(json.dumps({"objects": len(owners), "meshes": len(meshes),
                  "riki_children": sum(item["parent"] == "riki" for item in owners.values()),
                  "cube_053_material_slots": len(cube["material_slots"]),
                  "lamps_triangles": next(item["triangles"] for item in scene["prototypes"]
                                           if item["prim_path"] == owners["lamps"]["prim_path"]),
                  "camera_end_frame": stage.GetEndTimeCode()}, ensure_ascii=False))
