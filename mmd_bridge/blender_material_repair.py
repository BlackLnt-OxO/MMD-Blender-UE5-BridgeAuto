"""Evaluate only requested source objects; author UV-only USD overrides."""
import copy
from pathlib import Path
import bpy
import numpy as np
from mathutils import Matrix
from pxr import Usd, UsdGeom, UsdShade, Sdf, Vt
from .core import read_json, atomic_json, check_stop, file_hash, asset_name, stable_id, Cancelled
from .material_repair import verify_request
from .blender_materials import Materials
from .blender_render import with_render_depsgraph
from .blender_usd import _prepare_generated_coordinates
from .material_uv import used_channels, relocate_record


def export(job, folder):
    folder = Path(folder)
    request = verify_request(job, folder)
    cfg = job.data['settings']
    result = read_json(folder/'export.json') if (folder/'export.json').is_file() else {'experimental': True, 'records': {}, 'failures': [], 'layers': {}}
    result['complete'] = False
    bpy.ops.wm.open_mainfile(filepath=cfg['scene'], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    scene.frame_set(cfg.get('scene_frame', 1))
    from .blender_appearance import expose_material_output
    for material in bpy.data.materials:
        expose_material_output(material, scene.render.engine)
    names = {i.get('source_name', i['name']) for t in request['targets'] for p in t['placements'] for i in p['instances']}
    meshes = {}
    def capture(deps):
        for name in names:
            source = bpy.data.objects.get(name)
            if source is not None and source.type == 'MESH':
                evaluated = source.evaluated_get(deps)
                mesh = evaluated.data.copy()
                item = {'mesh': mesh, 'source_materials': list(mesh.materials)}
                try: _prepare_generated_coordinates(item, source, scene.render.engine, {})
                except ValueError as exc: mesh['_mmd_generated_error'] = str(exc)
                meshes[name] = mesh
    with_render_depsgraph(scene, capture)
    stage = Usd.Stage.Open(request['root_layer'])
    if not stage: raise ValueError('无法读取原场景 USD')
    # Earlier candidates may already add coordinates to a shared mesh. Compose
    # them in a session layer so later targets allocate distinct UV channels.
    # The original root remains read-only until the native transaction commits.
    stage.GetSessionLayer().subLayerPaths = [v['path'] for identity,v in result['layers'].items() if identity in result['records']]
    converter = Materials(folder / 'Source', cfg['scene'], cfg.get('bake_resolution', 1024),
                          conversion_timeout=cfg.get('material_conversion_timeout', 120), workflow='semantic_plan_v1')
    converter.restore_scene_appearance = True
    converter.isolated_baking = True
    converter.meters_per_unit = cfg.get('scene_scale', 1) * scene.unit_settings.scale_length
    checkpoint_path = job.folder / 'scene_checkpoint.json'
    if checkpoint_path.is_file():
        checkpoint = read_json(checkpoint_path)
        converter.material_world_bounds = checkpoint.get('material_world_bounds', {})
        converter.world_bounds = checkpoint.get('world_bounds')
    converter.progress = job.status
    scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'; scene.cycles.samples = 1
    for index, target in enumerate(request['targets']):
        check_stop(job)
        identity = target['id']
        result['failures'] = [f for f in result['failures'] if f.get('material_id') != identity]
        if identity in result['records']: continue
        job.status('material_repair_export', f'实验性补修 {index+1}/{len(request["targets"])}: {target["source_name"]}')
        try:
            placements = target['placements']
            # A material record can only share a repair when its geometry and UV layout agree.
            prototype = placements[0]['prototype']
            path = prototype['prim_path']
            prim = stage.GetPrimAtPath(path)
            if not prim.IsA(UsdGeom.Mesh): path += '/Mesh'; prim = stage.GetPrimAtPath(path)
            usd_mesh = UsdGeom.Mesh(prim)
            points = np.asarray(usd_mesh.GetPointsAttr().Get(), dtype=np.float32)
            counts = np.asarray(usd_mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int32)
            indices = np.asarray(usd_mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int32)
            mesh = None; instance = None
            for candidate in placements[0]['instances']:
                raw = meshes.get(candidate.get('source_name', candidate['name']))
                if raw is None or len(raw.vertices) != len(points) or len(raw.loops) != len(indices): continue
                a = np.empty(len(points)*3, dtype=np.float32); raw.vertices.foreach_get('co', a)
                b = np.empty(len(indices), dtype=np.int32); raw.loops.foreach_get('vertex_index', b)
                c = np.empty(len(counts), dtype=np.int32); raw.polygons.foreach_get('loop_total', c)
                if np.allclose(a.reshape((-1,3)), points, atol=1e-6) and np.array_equal(b,indices) and np.array_equal(c,counts):
                    mesh = raw.copy(); instance = candidate; break
            if mesh is None: raise ValueError('目标原型与当前源求值几何不对应，禁止猜测 UV／坐标')
            aliases = set(target.get('source_aliases', [])) | {target['source_name']}
            slot = next((i for i,m in enumerate(mesh.materials) if m and m.name in aliases), None)
            if slot is None: raise ValueError('目标槽位没有对应源材质／别名')
            source_mat = mesh.materials[slot]
            obj = bpy.data.objects.new('Repair_' + identity, mesh)
            obj.matrix_world = Matrix(instance.get('source_matrix', instance['matrix']))
            obj['_mmd_source_name'] = instance.get('source_name', instance['name'])
            obj['_mmd_object_random'] = instance.get('shader_data', {}).get('object_random', 0)
            obj['_mmd_geometry_identity'] = prototype['id']
            if mesh.get('_mmd_generated_error'): obj['_mmd_generated_error'] = mesh['_mmd_generated_error']
            material = source_mat.copy(); mesh.materials[slot] = material
            from .scene_shader_data import pin_generated
            pin_generated(material.node_tree, mesh)
            try:
                rec = converter.analyze(material, obj, source_name=target['source_name'], geometry_identity=prototype['id'])
                if rec['conversion_status'] in ('placeholder','failed'): raise ValueError(rec.get('material_error','补修仍为占位'))
                existing = sorted([v for v in UsdGeom.PrimvarsAPI(prim).GetPrimvars() if v.GetPrimvarName().startswith('st')],
                                  key=lambda v: 0 if v.GetPrimvarName() == 'st' else int(v.GetPrimvarName()[2:]))
                by_name = {v.GetAttr().GetCustomDataByKey('MMDBridge:sourceName'): i for i,v in enumerate(existing)}
                mapping = {}; additions = []
                for old in used_channels([rec], converter.functions, include_base=False):
                    layer = mesh.uv_layers[old]
                    values = np.empty(len(indices)*2, dtype=np.float32); layer.data.foreach_get('uv', values)
                    values = values.reshape((-1,2))
                    mapped = None
                    if layer.name in by_name:
                        candidate = by_name[layer.name]
                        prior = np.asarray(existing[candidate].ComputeFlattened(),dtype=np.float32)
                        if values.shape == prior.shape and np.allclose(values, prior, atol=1e-6): mapped = candidate
                    if mapped is None:
                        # Preserve the original channel used by good materials.
                        # A verified source UV may be added for this repair only.
                        mapped = len(existing) + len(additions); additions.append((mapped, layer))
                    mapping[old] = mapped
                if max(mapping.values(), default=0) >= 4: raise ValueError('补修需要超过 UE 四个 UV 通道，保留原占位')
                rec = relocate_record(rec, mapping, converter.functions)
                rec['function_definitions'] = converter.functions.reachable({'channels':rec['channels'],'surface_graph':rec.get('surface_graph')})
                rec.update(id=identity, asset_name=asset_name('M', target['source_name']+'_Repair', identity),
                           repair_batch=request['batch'], visual_status='needs_review', requires_visual_review=True)
                layer_path = folder / (identity + '.usda')
                overlay = Usd.Stage.CreateNew(str(layer_path))
                paths = {i['prim_path'] for placement in placements for i in placement['instances']}
                shader_paths = set()
                for proto_path in paths:
                    original = stage.GetPrimAtPath(proto_path)
                    if not original.IsA(UsdGeom.Mesh): proto_path += '/Mesh'; original=stage.GetPrimAtPath(proto_path)
                    other = UsdGeom.Mesh(original)
                    if list(other.GetFaceVertexIndicesAttr().Get()) != indices.tolist() or not np.allclose(other.GetPointsAttr().Get(),points):
                        raise ValueError('共享记录的不同原型不能安全共享补修')
                    # References carry their own material scopes. Override the
                    # shader actually bound to this mesh/subset, not an unused
                    # global look with the same name.
                    for bound_prim in [original] + list(original.GetChildren()):
                        bound, _ = UsdShade.MaterialBindingAPI(bound_prim).ComputeBoundMaterial()
                        if not bound or bound.GetPrim().GetName() != 'M_'+identity: continue
                        connected = bound.GetSurfaceOutput('unreal').GetConnectedSource()
                        if not connected: raise ValueError('目标材质没有 Unreal 着色器连接')
                        shader_paths.add(str(connected[0].GetPrim().GetPath()))
                    over = overlay.OverridePrim(proto_path)
                    ancestor = original
                    while ancestor and not ancestor.IsPseudoRoot():
                        if ancestor.IsInstance(): overlay.OverridePrim(ancestor.GetPath()).SetInstanceable(False)
                        ancestor = ancestor.GetParent()
                    for mapped, layer in additions:
                        values = np.empty(len(indices)*2, dtype=np.float32); layer.data.foreach_get('uv', values)
                        var=UsdGeom.PrimvarsAPI(over).CreatePrimvar('st' if mapped==0 else 'st'+str(mapped), Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
                        var.Set(Vt.Vec2fArray.FromNumpy(values.reshape((-1,2))))
                        var.GetAttr().SetCustomDataByKey('MMDBridge:sourceName', layer.name)
                if not shader_paths: raise ValueError('目标槽位没有可验证的 USD 材质绑定')
                for shader_path in shader_paths:
                    shader = UsdShade.Shader(overlay.OverridePrim(shader_path))
                    shader.SetSourceAsset(Sdf.AssetPath('__MATERIAL_'+identity+'__'), 'unreal')
                overlay.GetRootLayer().Save()
                result['records'][identity] = rec
                result['layers'][identity] = {'path':str(layer_path), 'sha256':file_hash(layer_path),
                                             'added_uv_channels':[mapped for mapped, _ in additions]}
                stage.GetSessionLayer().subLayerPaths = list(stage.GetSessionLayer().subLayerPaths) + [str(layer_path)]
            finally:
                bpy.data.objects.remove(obj, do_unlink=True)
        except Cancelled: raise
        except Exception as exc:
            result['failures'].append({'material_id':identity,'error':str(exc)})
        atomic_json(folder / 'export.json', result)
    result['complete']=True; atomic_json(folder/'export.json',result)
    job.status('material_repair_ready', '白模补修候选已生成，等待 UE 验证和局部绑定')
