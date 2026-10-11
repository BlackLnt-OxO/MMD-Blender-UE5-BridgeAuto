"""Reduce exported USD scatter hierarchy without changing geometry or materials.

Runs in Blender's USD Python environment, before UE translates the Stage.
Only static, positive-scale TRS instances with identical reference targets and
the same original parent may share a PointInstancer. Other parts stay editable.
"""
from collections import defaultdict
from pathlib import Path
import hashlib
import json
import shutil
import uuid
from .core import atomic_json, read_json, file_hash, ue_asset_folder


def compact_geometry(source, destination, minimum=16, protected_paths=()):
    from pxr import Gf, Sdf, Usd, UsdGeom, Vt
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError('Optimization must write a separate generated layer')
    if minimum < 2:
        raise ValueError('Batch threshold must be at least 2')
    protected_paths = tuple(str(path) for path in protected_paths)
    layer = Sdf.Layer.FindOrOpen(str(source))
    if not layer:
        raise ValueError('Cannot read geometry layer: ' + str(source))
    result = Sdf.Layer.CreateNew(str(destination))
    result.TransferContent(layer)
    candidates = defaultdict(list)
    specs = []
    result.Traverse(Sdf.Path.absoluteRootPath, lambda path: specs.append(result.GetPrimAtPath(path)))
    # Preserve anchored external references when writing outside the source package.
    with Sdf.ChangeBlock():
        for spec in specs:
            if spec is None:
                continue
            refs = list(spec.referenceList.GetAppliedItems())
            if not refs:
                continue
            anchored = [Sdf.Reference(ref.assetPath if source.parent == destination.parent else Sdf.ComputeAssetPathRelativeToLayer(layer, ref.assetPath),
                                     ref.primPath, ref.layerOffset, ref.customData) for ref in refs]
            spec.referenceList.ClearEditsAndMakeExplicit()
            spec.referenceList.explicitItems = anchored
            if not spec.instanceable or spec.typeName != 'Xform' or len(anchored) != 1 or len(spec.nameChildren):
                continue
            if dict(spec.customData) or dict(spec.variantSelections) or spec.inheritPathList.GetAppliedItems() or spec.specializesList.GetAppliedItems():
                continue
            if any(path == str(spec.path) or path.startswith(str(spec.path) + '/')
                   for path in protected_paths):
                continue  # User-authored overrides must keep their target prim.
            if set(spec.attributes.keys()) != {'xformOp:transform', 'xformOpOrder'} or len(spec.relationships):
                continue  # Preserve individual visibility, primvars and bindings.
            matrix = spec.attributes.get('xformOp:transform')
            order = spec.attributes.get('xformOpOrder')
            if matrix is None or result.ListTimeSamplesForPath(matrix.path) or order is None or list(order.default) != ['xformOp:transform']:
                continue
            value = matrix.default
            transform = Gf.Transform(value)
            scale = transform.GetScale()
            reconstructed = transform.GetMatrix()
            if min(scale) <= 0 or max(abs(value[r][c] - reconstructed[r][c]) for r in range(4) for c in range(4)) > 1e-7:
                continue  # Keep shear and reflected transforms in the original route.
            ref = anchored[0]
            key = (str(spec.path.GetParentPath()), ref.assetPath, str(ref.primPath),
                   ref.layerOffset.offset, ref.layerOffset.scale,
                   json.dumps(ref.customData, sort_keys=True, default=str))
            candidates[key].append((str(spec.path), transform, ref))
    stage = Usd.Stage.Open(result, load=Usd.Stage.LoadNone)
    mapping, batches = {}, []
    remove = []
    for key, items in sorted(candidates.items()):
        if len(items) < minimum:
            continue
        parent, asset_path, prototype_path = key[:3]
        digest = hashlib.sha256(repr(key[1:]).encode()).hexdigest()[:12]
        path = parent + '/Scatter_' + digest
        if stage.GetPrimAtPath(path):
            raise ValueError('Generated PointInstancer path already exists: ' + path)
        instancer = UsdGeom.PointInstancer.Define(stage, path)
        prototype = UsdGeom.Xform.Define(stage, path + '/Prototypes/P0')
        prototype.GetPrim().GetReferences().AddReference(items[0][2])
        # The original instance authored its matrix over the referenced root.
        # Put that matrix in the instance arrays, overriding the prototype root
        # with identity so the reference's root transform is not applied twice.
        prototype.MakeMatrixXform().Set(Gf.Matrix4d(1))
        instancer.CreatePrototypesRel().SetTargets([prototype.GetPath()])
        instancer.CreateProtoIndicesAttr().Set(Vt.IntArray([0] * len(items)))
        instancer.CreatePositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(t.GetTranslation()) for _, t, _ in items]))
        instancer.CreateScalesAttr().Set(Vt.Vec3fArray([Gf.Vec3f(t.GetScale()) for _, t, _ in items]))
        orientations = []
        for old, transform, _ in items:
            q = transform.GetRotation().GetQuat()
            orientations.append(Gf.Quath(q.GetReal(), Gf.Vec3h(q.GetImaginary())))
            mapping[old] = {'prim_path': path, 'instance_index': len(orientations) - 1}
        instancer.CreateOrientationsAttr().Set(Vt.QuathArray(orientations))
        instancer.GetPrim().SetCustomDataByKey('MMDBridge:sourceParent', parent)
        # Check float/half quantization before dropping any original transforms.
        computed = instancer.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default(),
                                                            UsdGeom.PointInstancer.ExcludeProtoXform)
        error = max(abs(a[r][c] - b.GetMatrix()[r][c]) for a, (_, b, _) in zip(computed, items)
                    for r in range(4) for c in range(4))
        if len(computed) != len(items) or error > .002:
            stage.RemovePrim(path)
            for old, _, _ in items:
                mapping.pop(old)
            continue
        batches.append({'prim_path': path, 'instances': len(items), 'transform_max_error': error,
                        'prototype_path': str(prototype.GetPath())})
        remove.extend(old for old, _, _ in items)
    # Avoid 40,000 individual USD composition rebuilds.
    with Sdf.ChangeBlock():
        for path in remove:
            spec = result.GetPrimAtPath(path)
            del spec.nameParent.nameChildren[spec.name]
    result.Save()
    return {'mapping': mapping, 'batches': batches, 'batched_instances': len(mapping),
            'removed_individual_prims': len(mapping), 'generated_batches': len(batches)}


def remap_scene(scene, optimization):
    """Keep source names and instance indices in the manifest for validation."""
    mapping = optimization['mapping']
    for item in scene.get('instances', []):
        update = mapping.get(item['prim_path'])
        if update:
            item['source_prim_path'] = item['prim_path']
            item.update(update)
    for item in scene.get('prototypes', []):
        update = mapping.get(item.get('prim_path'))
        if update:
            item['prim_path'] = update['prim_path']
    for owner in scene.get('hierarchy', []):
        parts = []
        seen = set()
        for part in owner.get('parts', []):
            part = dict(part)
            update = mapping.get(part['prim_path'])
            if update:
                part['prim_path'] = update['prim_path']
                part['name'] = 'Scatter batch'
            if part['prim_path'] not in seen:
                seen.add(part['prim_path'])
                parts.append(part)
        owner['parts'] = parts
    scene['scatter_batches'] = optimization['batches']
    scene['import_optimization'] = {key: value for key, value in optimization.items() if key != 'mapping'}


def needs_import_compaction(job):
    cfg = job.data['settings']
    manifest = job.folder/'manifest.json'
    if not manifest.is_file():
        return False
    scene = read_json(manifest).get('scene', {})
    if scene.get('route') != 'usd_stage':
        return False
    prepared = scene.get('import_compaction', {})
    if prepared:
        if not cfg.get('optimize_usd_scatter', True):
            return True  # Restore the original manifest when the option is disabled.
        path = Path(prepared.get('geometry', ''))
        if prepared.get('version') == 2 and path.is_file() and file_hash(path) == prepared.get('geometry_sha256'):
            return False
        return True
    if not cfg.get('optimize_usd_scatter', True) or scene.get('scatter_batches'):
        return False
    return True


def prepare_import_compaction(job):
    """Optimize existing exports before UE launches, without rebaking/reexporting."""
    if not needs_import_compaction(job):
        job.status('scene_done', 'USD 散件优化已准备；等待 UE 导入')
        return
    manifest_path = job.folder/'manifest.json'
    manifest = read_json(manifest_path)
    backup = job.folder/'manifest_before_import_compaction.json'
    if manifest.get('scene', {}).get('import_compaction'):
        if not backup.is_file():
            raise ValueError('Original USD manifest is missing; refusing to reuse a damaged optimization')
        manifest = read_json(backup)
        if not job.data['settings'].get('optimize_usd_scatter', True):
            atomic_json(manifest_path,manifest)
            job.status('scene_done', 'USD 散件优化已关闭；恢复原有导入层级')
            return
    from pxr import Sdf
    scene = manifest['scene']
    geometry = next(item for item in scene['usd']['files'] if item['relative']=='Geometry.usda')
    source = Path(geometry['path']).resolve()
    if file_hash(source) != geometry['sha256']:
        raise ValueError('USD geometry changed since export; refusing cached optimization')
    if not backup.exists():
        shutil.copy2(manifest_path,backup)
    out = job.folder/'import_scene';out.mkdir(exist_ok=True)
    # Keep references anchored to the unchanged original package while computing
    # transforms. The result retains relative resources when deployed by UE.
    temporary = source.with_name('Geometry_ImportOptimized_'+uuid.uuid4().hex+'.usda')
    protected = set()
    cfg = job.data['settings']
    roots = [Path(scene['usd']['root_layer'])]
    if cfg.get('project'):
        target = (Path(cfg['project']).parent/'Content'/cfg['content_root'].removeprefix('/Game/')/
                  ue_asset_folder(job.data)/'Source/Scene/Scene.usda')
        roots.append(target)
    for root in roots:
        if not root.is_file():
            continue
        layer = Sdf.Layer.FindOrOpen(str(root))
        if layer:
            def protect(path):
                prim = layer.GetPrimAtPath(path)
                if prim and (len(prim.attributes) or len(prim.relationships) or prim.referenceList.GetAppliedItems() or prim.active is False):
                    protected.add(str(prim.path))
            layer.Traverse(Sdf.Path.absoluteRootPath,protect)
    try:
        optimization = compact_geometry(source,temporary,protected_paths=protected)
        destination = out/'Geometry.usda'
        shutil.copy2(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)
    remap_scene(scene,optimization)
    geometry.update(path=str(destination.resolve()),sha256=file_hash(destination))
    scene['import_compaction']={'version':2,'geometry':geometry['path'],
        'geometry_sha256':geometry['sha256'],'source_geometry_sha256':file_hash(source)}
    package_item = next(item for item in scene['usd']['files'] if item['relative']=='package.json')
    package = read_json(package_item['path'])
    labels = dict(package.get('actor_labels',{}))
    for old in optimization['mapping']:
        labels.pop(old,None)
    labels.update({item['prim_path']:f"Scatter ({item['instances']} instances)" for item in optimization['batches']})
    package.update(actor_labels=labels,scatter_batches=optimization['batches'],
                   shader_data=[{'prim_path':i['prim_path'],**({'instance_index':i['instance_index']} if 'instance_index' in i else {}),
                                 **i['shader_data']} for i in scene.get('instances',[]) if i.get('shader_data')],
                   import_optimization=scene['import_optimization'])
    package_path = out/'package.json';atomic_json(package_path,package)
    package_item.update(path=str(package_path.resolve()),sha256=file_hash(package_path))
    atomic_json(out/'optimization.json',scene['import_optimization'])
    atomic_json(manifest_path,manifest)
    job.status('scene_done',f"USD 散件优化完成：{optimization['batched_instances']} 个散件合为 {optimization['generated_batches']} 批次；等待 UE 导入")
