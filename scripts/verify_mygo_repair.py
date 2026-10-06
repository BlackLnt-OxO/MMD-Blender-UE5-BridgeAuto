"""Check the fresh MyGO USD package with Blender's bundled USD runtime."""
import json
import sys
from array import array
from pathlib import Path

import bpy
from pxr import Usd, UsdGeom, UsdPhysics, UsdShade

manifest = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
scene = manifest["scene"]
stage = Usd.Stage.Open(scene["usd"]["root_layer"], load=Usd.Stage.LoadAll)
assert stage, "USD Stage 无法打开"
visible = list(stage.Traverse())
assert all("/__Prototypes" not in str(prim.GetPath()) for prim in visible), "原型作为可见场景几何导出"
assert len(scene["instances"]) == scene["stats"]["instances"]
assert all(stage.GetPrimAtPath(item["prim_path"]) for item in scene["instances"]), "实例清单引用了错误的 Stage 路径"
assert all(prim.HasAPI(UsdShade.MaterialBindingAPI) for prim in stage.TraverseAll()
           if prim.HasProperty("material:binding")), "材质绑定缺少 USD MaterialBindingAPI"
lamps = [item for item in scene["instances"] if item["name"] == "lamps"]
assert len(lamps) == 168, f"灯具实例数量错误: {len(lamps)}"
oversized = [item for item in lamps if max(item["bounds"]["size"][:2]) > 20]
assert not oversized, f"灯具实例仍有场景级边界: {len(oversized)}"
lamp_heads = [item for item in lamps if item.get("source_name") == "Cube.002"]
assert len(lamp_heads) == 24, f"灯头实例数量错误: {len(lamp_heads)}"
prototypes = {item["id"]: item for item in scene["prototypes"]}
assert all(prototypes[item["prototype_id"]]["vertices"] >= 20000 for item in lamp_heads), (
    "灯头仍使用单灯网格，未保留 Blender Array 生成的一圈灯")
ground_materials = [item for item in scene["materials"] if item.get("source_name") == "Patterned_Concrete_ugfnfifo"]
assert ground_materials, "地面材质缺失"
assert any({"water", "parking_line"} <= set(item.get("special_masks", {})) for item in ground_materials), (
    "地面水迹和停车线遮罩没有烘焙")
assert all(Path(mask["image"]["path"]).is_file() for item in ground_materials
           for mask in item.get("special_masks", {}).values()), "地面烘焙贴图缺失"
ground_masks = next(item["special_masks"] for item in ground_materials
                    if {"water", "parking_line"} <= set(item.get("special_masks", {})))
mask_white_ratio = {}
for semantic in ("water", "parking_line"):
    image = bpy.data.images.load(ground_masks[semantic]["image"]["path"], check_existing=False)
    pixels = array("f", [0.0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    values = pixels[0::image.channels]
    mask_white_ratio[semantic] = sum(value > .8 for value in values) / len(values)
    bpy.data.images.remove(image)
assert .2 < mask_white_ratio["water"] < .85, "水迹烘焙没有保留程序遮罩细节"
assert .005 < mask_white_ratio["parking_line"] < .2, "停车线烘焙没有保留线条"
missing_collision = [item["id"] for item in scene["prototypes"] if item["collision"]
                     and not stage.GetPrimAtPath(item["prim_path"] + "/Mesh").HasAPI(UsdPhysics.CollisionAPI)]
assert not missing_collision, f"碰撞网格缺少 USD CollisionAPI: {missing_collision[:5]}"
cameras = [prim for prim in stage.Traverse() if prim.IsA(UsdGeom.Camera)]
assert cameras and stage.GetEndTimeCode() >= 4108, "VMD 镜头或末帧缺失"
camera = UsdGeom.Camera(cameras[0])
focal = camera.GetFocalLengthAttr()
transform = UsdGeom.Xformable(cameras[0]).GetOrderedXformOps()[0]
for frame in (0, 2054, 4108):
    assert frame in focal.GetTimeSamples() and frame in transform.GetTimeSamples(), f"镜头缺少第 {frame} 帧"
    assert focal.Get(frame) > 0 and transform.Get(frame), f"镜头第 {frame} 帧无效"
print(json.dumps({"stage": scene["usd"]["root_layer"], "instances": len(scene["instances"]),
                  "lamps": len(lamps), "max_lamp_xy_m": max(max(item["bounds"]["size"][:2]) for item in lamps),
                  "lamp_head_vertices": prototypes[lamp_heads[0]["prototype_id"]]["vertices"],
                  "ground_mask_white_ratio": mask_white_ratio,
                  "cameras": len(cameras), "end_frame": stage.GetEndTimeCode(),
                  "camera_focal_mm": {frame: round(focal.Get(frame), 3) for frame in (0, 2054, 4108)},
                  "collision_prototypes": sum(item["collision"] for item in scene["prototypes"])}, ensure_ascii=False))
