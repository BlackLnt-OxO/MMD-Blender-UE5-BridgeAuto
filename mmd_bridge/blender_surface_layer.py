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
    bindings = []
    for row in request['targets']:
        stage.SetEditTarget(stage.GetRootLayer())
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
                material, relation = UsdShade.MaterialBindingAPI(bound).ComputeBoundMaterial('unreal')
                if not material: material, relation = UsdShade.MaterialBindingAPI(bound).ComputeBoundMaterial()
                shader = stage.GetPrimAtPath(str(material.GetPath()) + '/UnrealShader') if material else None
                value = shader.GetAttribute('info:unreal:sourceAsset').Get() if shader else None
                if value and value.path == row['original_asset']: matches.append((bound, relation))
        if len(matches) != 1:
            raise ValueError('该材质槽无法唯一对应 USD 子集，保留原绑定：' + row['label'])
        bindings.append((row, *matches[0]))
    for index, (row, bound, relation) in enumerate(bindings):
        stage.SetEditTarget(overlay)
        # Each batch owns its USD materials so later switches cannot retarget older selections.
        material_path = '/World/Materials/SurfaceOverrides/' + folder.name + '/M_' + row['candidate_id']
        material = UsdShade.Material.Define(stage, material_path)
        shader = UsdShade.Shader.Define(stage, material_path + '/UnrealShader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        shader.GetPrim().CreateAttribute('info:unreal:sourceAsset', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(row['candidate_asset']))
        material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out', Sdf.ValueTypeNames.Token))
        stage.SetEditTarget(stage.GetRootLayer())
        owner = relation.GetPrim() if relation else bound
        name = 'MMDBridgeSurface_' + folder.name + '_' + str(index)
        collection = Usd.CollectionAPI.Apply(owner, name)
        collection.CreateExpansionRuleAttr().Set(Usd.Tokens.explicitOnly)
        collection.CreateIncludesRel().SetTargets([bound.GetPath()])
        api = UsdShade.MaterialBindingAPI.Apply(owner)
        order = owner.GetPropertyOrder()
        names = []
        for purpose in (UsdShade.Tokens.allPurpose, 'unreal'):
            if not api.Bind(collection, material, name, UsdShade.Tokens.strongerThanDescendants, purpose):
                raise ValueError('无法建立选中槽位的材质覆盖：' + row['label'])
            names.append(api.GetCollectionBindingRel(name, purpose).GetName())
        owner.SetPropertyOrder(names + [p for p in order if p not in names])
    for row, bound, _ in bindings:
        for purpose in (UsdShade.Tokens.allPurpose, 'unreal'):
            material, _ = UsdShade.MaterialBindingAPI(bound).ComputeBoundMaterial(purpose)
            shader = stage.GetPrimAtPath(str(material.GetPath()) + '/UnrealShader') if material else None
            value = shader.GetAttribute('info:unreal:sourceAsset').Get() if shader else None
            if not value or value.path != row['candidate_asset']:
                raise ValueError('候选覆盖未成为有效绑定，保留原绑定：' + row['label'])
    overlay.Save()
    stage.GetRootLayer().subLayerPaths[0] = layer.relative_to(root.parent).as_posix()
    planned = folder/'root_candidate.usda'
    planned.write_text(stage.GetRootLayer().ExportToString(), encoding='utf-8')
    # The live root remains untouched until UE checks the original bindings and commits.
    assert file_hash(root) == request['root_hash']
    atomic_json(folder / 'layer.json', {'path': str(layer), 'sha256': file_hash(layer),
                                      'relative': layer.relative_to(root.parent).as_posix(),
                                      'root_candidate_hash': file_hash(planned)})


if __name__ == '__main__': run(sys.argv[-1])
