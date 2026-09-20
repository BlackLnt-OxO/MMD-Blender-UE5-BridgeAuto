"""Validate an exported USD package with Blender's bundled USD runtime.

Run with:
  blender --background --python scripts/validate_usd.py -- <manifest.json>
"""
import json
import sys
from pathlib import Path

from pxr import Usd, UsdGeom, UsdPhysics


def main(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    scene = manifest["scene"]
    stage = Usd.Stage.Open(scene["usd"]["root_layer"], load=Usd.Stage.LoadAll)
    if stage is None:
        raise RuntimeError("无法打开 USD Stage")
    meshes = []
    missing_collision = []
    wrong_approximation = []
    instances = []
    for prim in stage.TraverseAll():
        if prim.IsInstance():
            instances.append(prim.GetPath().pathString)
        if not prim.IsA(UsdGeom.Mesh):
            continue
        meshes.append(prim.GetPath().pathString)
        if not prim.HasAPI(UsdPhysics.CollisionAPI):
            missing_collision.append(prim.GetPath().pathString)
        approximation = UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get()
        if approximation != "none":
            wrong_approximation.append({"prim": prim.GetPath().pathString,
                                        "approximation": approximation})
    result = {
        "meshes": len(meshes), "instances": len(instances),
        "missing_collision": missing_collision,
        "wrong_approximation": wrong_approximation,
        "expected_prototypes": scene["stats"]["prototypes"],
        "expected_instances": scene["stats"]["instances"],
    }
    print("MMDBRIDGE_USD_VALIDATION=" + json.dumps(result, ensure_ascii=False))
    if missing_collision or wrong_approximation:
        raise RuntimeError("USD 精确碰撞 API 验证失败")


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1])
