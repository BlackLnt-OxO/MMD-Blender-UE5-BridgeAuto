"""Check a scene export before sending its Stage to Unreal."""
import json
import sys
from pathlib import Path

from pxr import Usd, UsdGeom, UsdShade


def main(path):
    scene = json.loads(Path(path).read_text(encoding="utf-8"))["scene"]
    stage = Usd.Stage.Open(scene["usd"]["root_layer"], load=Usd.Stage.LoadAll)
    assert stage, "USD Stage did not open"
    hierarchy = {item["name"]: item for item in scene["hierarchy"]}
    assert sum(len(item["parts"]) for item in hierarchy.values()) == len(scene["instances"])
    labels = json.loads((Path(scene["usd"]["root_layer"]).parent / "package.json").read_text(
        encoding="utf-8"))["actor_labels"]
    cache = UsdGeom.XformCache()
    for item in scene["hierarchy"]:
        prim = stage.GetPrimAtPath(item["prim_path"])
        assert prim and prim.GetMetadata("displayName") == item["name"], item["name"]
        assert labels[item["prim_path"]] == item["name"]
        if item["parent"]:
            assert item["prim_path"].startswith(hierarchy[item["parent"]]["prim_path"] + "/")
    for item in scene["instances"]:
        prim = stage.GetPrimAtPath(item["prim_path"])
        mesh = stage.GetPrimAtPath(item["prim_path"] + "/Mesh")
        assert prim and mesh.IsA(UsdGeom.Mesh), item["prim_path"]
        assert labels[item["prim_path"]] == item["label"]
        materials = [UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial()[0]]
        materials += [UsdShade.MaterialBindingAPI(child).ComputeBoundMaterial()[0]
                      for child in mesh.GetChildren() if child.IsA(UsdGeom.Subset)]
        assert any(materials), item["prim_path"]
        actual = cache.GetLocalToWorldTransform(prim).ExtractTranslation()
        expected = [item["matrix"][axis][3] for axis in range(3)]
        assert max(abs(actual[axis] - expected[axis]) for axis in range(3)) < 1e-3, item["prim_path"]
    assert not any("__Prototypes" in str(prim.GetPath()) for prim in stage.Traverse())
    print(json.dumps({"outliner_objects": len(hierarchy), "pieces": len(scene["instances"]),
                      "prototypes": len(scene["prototypes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1])
