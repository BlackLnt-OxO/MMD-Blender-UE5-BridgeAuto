"""Write per-object USD bindings without changing prototype/shared materials."""
import sys
from pathlib import Path
from pxr import Usd, UsdShade, Sdf
from .core import read_json, atomic_json, file_hash


def run(folder):
    folder = Path(folder)
    request = read_json(folder / 'build.json')
    root = Path(request['root_layer'])
    if file_hash(root) != request['root_hash']: raise ValueError('USD 根层已改变')
    stage = Usd.Stage.Open(str(root))
    layer = root.parent / 'MaterialSurfaces' / (folder.name + '.usda')
    layer.parent.mkdir(parents=True, exist_ok=True)
    overlay = Sdf.Layer.CreateNew(str(layer))
    stage.GetRootLayer().subLayerPaths.insert(0, str(layer))
    stage.SetEditTarget(overlay)
    for row in request['targets']:
        prim = stage.GetPrimAtPath(row['prim_path'])
        if not prim: raise ValueError('选中物体的 USD Prim 不存在')
        ancestor = prim
        while ancestor.IsInstanceProxy(): ancestor = ancestor.GetParent()
        if ancestor.IsInstance(): ancestor.SetInstanceable(False)
        prim = stage.GetPrimAtPath(row['prim_path'])
        if prim.IsInstance(): prim.SetInstanceable(False)
        meshes = [p for p in Usd.PrimRange(prim) if p.GetTypeName() == 'Mesh']
        matches = []
        for mesh in meshes:
            subsets = UsdShade.MaterialBindingAPI(mesh).GetMaterialBindSubsets()
            for bound in [s.GetPrim() for s in subsets] or [mesh]:
                material, _ = UsdShade.MaterialBindingAPI(bound).ComputeBoundMaterial('unreal')
                if not material: material, _ = UsdShade.MaterialBindingAPI(bound).ComputeBoundMaterial()
                shader = stage.GetPrimAtPath(str(material.GetPath()) + '/UnrealShader') if material else None
                value = shader.GetAttribute('info:unreal:sourceAsset').Get() if shader else None
                if value and value.path == row['original_asset']: matches.append(bound)
        if len(matches) != 1:
            raise ValueError('该材质槽无法唯一对应 USD 子集，保留原绑定：' + row['label'])
        material_path = '/World/Materials/SurfaceOverrides/M_' + row['candidate_id']
        material = UsdShade.Material.Define(stage, material_path)
        shader = UsdShade.Shader.Define(stage, material_path + '/UnrealShader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        shader.GetPrim().CreateAttribute('info:unreal:sourceAsset', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(row['candidate_asset']))
        material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out', Sdf.ValueTypeNames.Token))
        UsdShade.MaterialBindingAPI.Apply(matches[0]).Bind(material)
    overlay.Save()
    # The root is intentionally unsaved; UE commits only after validation.
    assert file_hash(root) == request['root_hash']
    atomic_json(folder / 'layer.json', {'path': str(layer), 'sha256': file_hash(layer),
                                      'relative': layer.relative_to(root.parent).as_posix()})


if __name__ == '__main__': run(sys.argv[-1])
