"""Live USD scene exporter built from Blender's evaluated dependency graph.

The source .blend is opened read-only for this process and is never saved.  All
geometry is taken from depsgraph.object_instances, so Geometry Nodes and other
instancers export their rendered result rather than their controller bounds.
"""
from __future__ import annotations

from array import array
import ctypes
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

import bpy
from mathutils import Vector

from .blender_materials import Materials
from .core import SCHEMA, atomic_json, file_hash, read_json, stable_id, check_stop


def _name(value):
    clean = re.sub(r"[^A-Za-z0-9_]", "_", str(value)).strip("_")
    return clean[:80] or "Item"


def _peak_working_set_mb(current=False):
    """Best-effort peak process working set for export comparison reports."""
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    try:
        from ctypes import wintypes
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        memory_info.restype = wintypes.BOOL
        ok = memory_info(current_process(), ctypes.byref(counters), counters.cb)
        size = counters.WorkingSetSize if current else counters.PeakWorkingSetSize
        return round(size / (1024 * 1024), 1) if ok else None
    except Exception:
        return None


def _matrix(value):
    # Blender stores translation in the last column while Gf.Matrix4d follows
    # the USD row-vector convention.  Transposing preserves the world transform.
    from pxr import Gf
    return Gf.Matrix4d(*[float(value[c][r]) for r in range(4) for c in range(4)])


def _bounds(points, matrix):
    if not points:
        return None
    low = Vector((math.inf, math.inf, math.inf))
    high = Vector((-math.inf, -math.inf, -math.inf))
    for point in points:
        world = matrix @ Vector(point)
        for axis in range(3):
            low[axis] = min(low[axis], world[axis])
            high[axis] = max(high[axis], world[axis])
    return {"min": list(low), "max": list(high),
            "size": [high[i] - low[i] for i in range(3)]}


def _hash_mesh(mesh, material_ids, collision):
    digest = hashlib.sha256()
    coords = array("f", [0.0]) * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", coords)
    digest.update(coords.tobytes())
    counts = array("i", (len(poly.vertices) for poly in mesh.polygons))
    indices = array("i", (vertex for poly in mesh.polygons for vertex in poly.vertices))
    digest.update(counts.tobytes())
    digest.update(indices.tobytes())
    digest.update(array("i", (poly.material_index for poly in mesh.polygons)).tobytes())
    digest.update(json.dumps(material_ids, ensure_ascii=False).encode("utf-8"))
    digest.update(str(bool(collision)).encode("ascii"))
    for layer in mesh.uv_layers:
        digest.update(layer.name.encode("utf-8", errors="replace"))
        values = array("f", [0.0]) * (len(layer.data) * 2)
        layer.data.foreach_get("uv", values)
        digest.update(values.tobytes())
    return digest.hexdigest()[:20]


def _spatial_material(material, seen=None):
    """Conservatively isolate evaluated instances with position-dependent shading."""
    if not material or not material.use_nodes:
        return False
    def visit(tree, seen):
        if tree.as_pointer() in seen:
            return False
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type in ("NEW_GEOMETRY", "OBJECT_INFO", "TEX_NOISE", "TEX_VORONOI"):
                return True
            if node.type == "TEX_COORD" and any(output.is_linked for output in node.outputs if output.name != "UV"):
                return True
            if node.type == "GROUP" and node.node_tree and visit(node.node_tree, seen):
                return True
        return False
    return visit(material.node_tree, set())


def _mesh_data(mesh):
    import numpy as np
    from pxr import Vt
    mesh.calc_loop_triangles()
    def vectors(collection, attribute, width, constructor):
        buffer = np.empty(len(collection) * width, dtype=np.float32)
        collection.foreach_get(attribute, buffer)
        return constructor.FromNumpy(buffer.reshape((-1, width)))
    def integers(collection, attribute):
        buffer = np.empty(len(collection), dtype=np.int32)
        collection.foreach_get(attribute, buffer)
        return Vt.IntArray.FromNumpy(buffer)
    points = vectors(mesh.vertices, 'co', 3, Vt.Vec3fArray)
    counts = integers(mesh.polygons, 'loop_total')
    indices = integers(mesh.loops, 'vertex_index')
    triangles = len(mesh.loop_triangles)
    if len(points) < 3 or not counts or triangles < 1:
        return None
    uvs = []
    for index, layer in enumerate(mesh.uv_layers):
        uvs.append({
            "name": "st" if index == 0 else "st" + str(index),
            "source_name": layer.name,
            "values": vectors(layer.data, 'uv', 2, Vt.Vec2fArray),
        })
    normals = []
    try:
        normals = vectors(mesh.corner_normals, 'vector', 3, Vt.Vec3fArray)
    except (AttributeError, RuntimeError):
        pass
    return {"points": points, "counts": counts, "indices": indices,
            "triangles": triangles, "uvs": uvs, "normals": normals}


def _write_material_layer(path, records, content_root, asset_id):
    from pxr import Sdf, Usd, UsdGeom, UsdShade
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Scope.Define(stage, "/World/Materials")
    for record in records:
        material = UsdShade.Material.Define(stage, "/World/Materials/M_" + record["id"])
        shader = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("UnrealShader"))
        asset = f"{content_root}/A_{asset_id}/Materials/MI_{record['id']}.MI_{record['id']}"
        shader.SetSourceAsset(Sdf.AssetPath(asset), "unreal")
        output = shader.CreateOutput("out", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput("unreal").ConnectToSource(output)
        material.GetPrim().SetCustomDataByKey("MMDBridge:strategy", record.get("strategy", "nodes"))
    stage.GetRootLayer().Save()


def _bind_material(prim, material_id):
    from pxr import Sdf
    prim.CreateRelationship("material:binding").SetTargets(
        [Sdf.Path("/World/Materials/M_" + material_id)])


def _write_chunk(path, number, groups, meters_per_unit, exact_collision, job=None):
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
    started = time.perf_counter()
    def progress(message, **fields):
        if job is not None:
            check_stop(job)
            elapsed = round(time.perf_counter() - started, 2)
            memory = _peak_working_set_mb(current=True)
            message += f"；分块耗时 {elapsed:.1f}s"
            if memory is not None:
                message += f"；进程内存 {memory:.0f} MB"
            job.status('scene_export', message, payload=number + 1,
                       payload_elapsed_seconds=elapsed, working_set_mb=memory, **fields)
    # Anonymous layer avoids exposing an incomplete payload as a finished file.
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    root_path = f"/World/Payload_{number:03d}"
    root = UsdGeom.Xform.Define(stage, root_path)
    prototypes = UsdGeom.Scope.Define(stage, root_path + "/__Prototypes")
    instances = UsdGeom.Scope.Define(stage, root_path + "/Instances")
    del prototypes, instances
    for group_index, group in enumerate(groups):
        progress(f"USD 分块 {number+1}：写入原型 {group_index+1}/{len(groups)}；"
                 f"{group['geometry']['triangles']} 三角形 / {len(group['instances'])} 实例",
                 prototype=group_index+1, prototypes=len(groups))
        proto_path = f"{root_path}/__Prototypes/P_{group['id']}"
        group["prim_path"] = proto_path
        proto = UsdGeom.Xform.Define(stage, proto_path)
        mesh = UsdGeom.Mesh.Define(stage, proto_path + "/Mesh")
        data = group["geometry"]
        mesh.CreatePointsAttr(data["points"])
        mesh.CreateFaceVertexCountsAttr(data["counts"])
        mesh.CreateFaceVertexIndicesAttr(data["indices"])
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        if data["normals"] and len(data["normals"]) == len(data["indices"]):
            mesh.CreateNormalsAttr(data["normals"])
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
        primvars = UsdGeom.PrimvarsAPI(mesh)
        for uv in data["uvs"]:
            var = primvars.CreatePrimvar(uv["name"], Sdf.ValueTypeNames.TexCoord2fArray,
                                         UsdGeom.Tokens.faceVarying)
            var.Set(uv["values"])
            var.GetAttr().SetCustomDataByKey("MMDBridge:sourceName", uv["source_name"])
        material_ids = group["materials"]
        polygon_materials = group["polygon_materials"]
        if len(material_ids) == 1 and material_ids[0]:
            _bind_material(mesh.GetPrim(), material_ids[0])
        elif material_ids:
            faces_by_slot = {}
            for face, slot in enumerate(polygon_materials):
                faces_by_slot.setdefault(slot, []).append(face)
            for slot, material_id in enumerate(material_ids):
                if not material_id:
                    continue
                faces = faces_by_slot.get(slot, [])
                if not faces:
                    continue
                subset = UsdGeom.Subset.Define(stage, mesh.GetPath().AppendChild(f"Material_{slot}"))
                subset.CreateElementTypeAttr(UsdGeom.Tokens.face)
                subset.CreateFamilyNameAttr("materialBind")
                subset.CreateIndicesAttr(faces)
                _bind_material(subset.GetPrim(), material_id)
            UsdGeom.Subset.SetFamilyType(mesh, "materialBind", UsdGeom.Tokens.partition)
        collision = bool(group["collision"] and exact_collision)
        if collision:
            UsdPhysics.CollisionAPI.Apply(mesh.GetPrim()).CreateCollisionEnabledAttr(True)
            UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr("none")
        mesh.GetPrim().SetCustomDataByKey("MMDBridge:topologyHash", group["id"])
        proto.GetPrim().SetInstanceable(False)
    layer = stage.GetRootLayer()
    # Never call Usd APIs inside ChangeBlock. Sdf batching prevents rebuilding
    # the stage's entire instance composition after every reference mutation.
    total = sum(len(group['instances']) for group in groups)
    written = 0
    with Sdf.ChangeBlock():
        for group in groups:
            for item in group['instances']:
                if written % 50 == 0:
                    progress(f"USD 分块 {number+1}：写入实例 {written}/{total}", instances_written=written)
                prim_path = f"{root_path}/Instances/I_{item['index']:07d}_{_name(item['name'])}"
                spec = Sdf.CreatePrimInLayer(layer, prim_path)
                spec.specifier = Sdf.SpecifierDef
                spec.typeName = 'Xform'
                spec.referenceList.prependedItems = [Sdf.Reference('', Sdf.Path(group['prim_path']))]
                spec.instanceable = True
                Sdf.AttributeSpec(spec, 'xformOp:transform', Sdf.ValueTypeNames.Matrix4d).default = _matrix(item['matrix'])
                Sdf.AttributeSpec(spec, 'xformOpOrder', Sdf.ValueTypeNames.TokenArray,
                                  Sdf.VariabilityUniform).default = ['xformOp:transform']
                spec.customData = {'MMDBridge': {'sourceObject': item['name'], 'sourceId': item['source_id']}}
                item['prim_path'] = prim_path
                written += 1
        progress(f"USD 分块 {number+1}：实例 {written}/{total}，正在提交 USD 组合")
    progress(f"USD 分块 {number+1}：正在保存文件（{written} 实例）")
    temporary = Path(str(path) + '.partial.usdc')
    if not layer.Export(str(temporary)):
        raise RuntimeError(f'USD 保存失败: {temporary}')
    if job is not None:
        check_stop(job)
    os.replace(temporary, path)
    return root_path


def _write_geometry_index(path, chunks, meters_per_unit):
    from pxr import Sdf, Usd, UsdGeom
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Scope.Define(stage, "/World/Chunks")
    for chunk in chunks:
        prim = UsdGeom.Xform.Define(stage, f"/World/Chunks/Chunk_{chunk['index']:03d}")
        prim.GetPrim().GetPayloads().AddPayload(
            Sdf.Payload("./Geometry/" + Path(chunk["path"]).name, chunk["root_prim"]))
    stage.GetRootLayer().Save()


def _write_user_root(path, meters_per_unit):
    from pxr import Sdf, Usd, UsdGeom
    if path.exists():
        return False
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    stage.GetRootLayer().subLayerPaths = ["./Geometry.usda", "./ToolMaterials.usda"]
    stage.GetRootLayer().customLayerData = {
        "MMDBridge": "User override root. Re-export replaces only tool-owned sublayers."
    }
    stage.GetRootLayer().Save()
    return True


def export_usd_scene(job):
    started = time.perf_counter()
    try:
        from pxr import Usd  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("当前 Blender 缺少 USD Python 模块，无法安全回退到代理网格") from exc

    data, cfg = job.data, job.data["settings"]
    job.status("scene_export", "读取 Blender 最终求值几何并生成 USD Payload")
    bpy.ops.wm.open_mainfile(filepath=cfg["scene"], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    frame = int(cfg.get("scene_frame", 1))
    scene.frame_set(frame)
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    export_root = Path(cfg.get("output_override") or Path(cfg["scene"]).parent) / ("Scene_" + data["id"])
    package = export_root / "Scene"
    geometry_folder = package / "Geometry"
    geometry_folder.mkdir(parents=True, exist_ok=True)
    materials = Materials(package, cfg["scene"], int(cfg.get("bake_resolution", 0)),
                          strategy=cfg.get("material_strategy", "auto"))
    meters_per_unit = float(scene.unit_settings.scale_length) * float(cfg.get("scene_scale", 1.0))
    materials.meters_per_unit = meters_per_unit
    exact_collision = bool(cfg.get("exact_collision", True))
    budget = int(cfg.get("usd_payload_triangle_budget", 1_000_000))
    warnings = []
    authored = getattr(bpy.data, "version", None)
    if authored and tuple(authored) > tuple(bpy.app.version):
        warnings.append(
            f"源文件由 Blender {'.'.join(map(str, authored))} 保存；当前使用 "
            f"{'.'.join(map(str, bpy.app.version))} 读取，已继续但需检查版本差异")

    groups = {}
    # DepsgraphObjectInstance wrappers are ephemeral RNA views.  Materializing
    # them with list() invalidates earlier entries while Blender advances the
    # iterator, so consume the iterator directly.
    instances = deps.object_instances
    try:
        total = len(instances)
    except TypeError:
        total = sum(1 for _ in instances)
        deps.update()
        instances = deps.object_instances
    lights_and_cameras = []
    for original in scene.objects:
        if original.hide_render or original.type not in ("LIGHT", "CAMERA"):
            continue
        entry = {"id": stable_id(cfg["scene"], original.name), "name": original.name,
                 "type": original.type, "matrix": [list(row) for row in original.matrix_world],
                 "visible": True}
        if original.type == "LIGHT":
            entry["light"] = {"type": original.data.type, "color": list(original.data.color),
                              "energy": original.data.energy, "size": getattr(original.data, "size", 1),
                              "size_y": getattr(original.data, "size_y", getattr(original.data, "size", 1)),
                              "spot_size": getattr(original.data, "spot_size", 1)}
        else:
            entry["camera"] = {"lens": original.data.lens, "sensor_width": original.data.sensor_width,
                               "sensor_height": original.data.sensor_height, "projection": original.data.type}
        lights_and_cameras.append(entry)

    # Pass 1 freezes evaluated meshes and transforms without linking any helper
    # object to the scene.  Linking while iterating object_instances mutates the
    # dependency graph and can recursively enumerate the export helpers.
    frozen = {}
    for index, instance in enumerate(instances):
        check_stop(job)
        if index % 50 == 0:
            job.status("scene_export", f"冻结场景最终求值结果 {index+1}/{total}")
        evaluated = instance.object
        source = getattr(evaluated, "original", evaluated)
        owner = getattr(getattr(instance, "parent", None), "original", None) or source
        if evaluated.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META", "CURVES"):
            continue
        if owner.hide_render or (not instance.is_instance and source.hide_render):
            continue
        collision = (exact_collision and
                     str(source.get("mmd_bridge_collision", "exact")).lower() != "none" and
                     str(owner.get("mmd_bridge_collision", "exact")).lower() != "none")
        pointer = evaluated.data.as_pointer() if getattr(evaluated, "data", None) else evaluated.as_pointer()
        surface_class = str(owner.get("mmd_bridge_surface_class", source.get("mmd_bridge_surface_class", "auto")))
        material_override = owner.get("mmd_bridge_material_strategy", source.get("mmd_bridge_material_strategy"))
        spatial = any(_spatial_material(slot.material) for slot in evaluated.material_slots)
        transform_key = tuple(float(v) for row in instance.matrix_world for v in row) if spatial else tuple(instance.matrix_world.to_scale())
        source_key = (pointer,
                      tuple(slot.material.name if slot.material else "" for slot in evaluated.material_slots),
                      collision, surface_class, material_override, transform_key)
        item = frozen.get(source_key)
        if item is None:
            try:
                mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=deps)
            except Exception as exc:
                raise RuntimeError(f"无法取得最终求值几何，禁止退回代理网格: {source.name}") from exc
            mesh.update()
            if len(mesh.vertices) < 3 or not mesh.polygons:
                bpy.data.meshes.remove(mesh)
                frozen[source_key] = False
                continue
            item = {"mesh": mesh, "source_name": source.name,
                    "surface_class": surface_class, "material_override": material_override,
                    "spatial": spatial,
                    "source_materials": list(mesh.materials),
                    "collision": collision,
                    "instances": [], "source_types": set()}
            frozen[source_key] = item
        if item is False:
            continue
        matrix = instance.matrix_world.copy()
        item["instances"].append({"name": owner.name,
                                  "source_id": stable_id(cfg["scene"], owner.name),
                                  "matrix_object": matrix})
        item["source_types"].add("INSTANCE" if instance.is_instance else evaluated.type)

    exported = 0
    usable = [item for item in frozen.values() if item is not False]
    world_low = [math.inf] * 3
    world_high = [-math.inf] * 3
    for item in usable:
        points = [v.co for v in item["mesh"].vertices]
        for instance in item["instances"]:
            bounds = _bounds(points, instance["matrix_object"])
            for axis in range(3):
                world_low[axis] = min(world_low[axis], bounds["min"][axis])
                world_high[axis] = max(world_high[axis], bounds["max"][axis])
            for material in item["source_materials"]:
                if material:
                    low, high = materials.material_world_bounds.setdefault(material.name, ([math.inf]*3, [-math.inf]*3))
                    for axis in range(3):
                        low[axis] = min(low[axis], bounds["min"][axis])
                        high[axis] = max(high[axis], bounds["max"][axis])
    materials.world_bounds = (world_low, world_high)
    for frozen_index, item in enumerate(usable):
        check_stop(job)
        mesh = item["mesh"]
        temp = bpy.data.objects.new("MMDBridge_Evaluated_" + _name(item["source_name"]), mesh)
        scene.collection.objects.link(temp)
        temp.matrix_world = item["instances"][0]["matrix_object"]
        temp["mmd_bridge_surface_class"] = item["surface_class"]
        if item["material_override"] is not None:
            temp["mmd_bridge_material_strategy"] = item["material_override"]
        try:
            material_ids = []
            geometry_identity = _hash_mesh(
                mesh, [mat.name if mat else None for mat in item["source_materials"]], item["collision"])
            if item["spatial"]:
                geometry_identity = stable_id(geometry_identity, str([list(row) for row in temp.matrix_world]),
                                              json.dumps({mat.name: materials.material_world_bounds.get(mat.name)
                                                          for mat in item["source_materials"] if mat}, sort_keys=True))
            copies = [material.copy() if material else None for material in item["source_materials"]]
            for slot, copy in enumerate(copies):
                mesh.materials[slot] = copy
            for slot, source_material in enumerate(item["source_materials"]):
                if source_material is None:
                    material_ids.append(None)
                    continue
                copy = copies[slot]
                rec = materials.analyze(copy, temp, water=False,
                                        source_name=source_material.name,
                                        geometry_identity=geometry_identity)
                rec["source_name"] = source_material.name
                material_ids.append(rec["id"])
            # Material baking may add MMD_BakeUV, so serialize geometry only
            # after analysis rather than freezing a pre-bake attribute layout.
            geometry = _mesh_data(mesh)
            if geometry is None:
                raise RuntimeError(f"最终求值网格在材质分析后变为空: {item['source_name']}")
            topology = _hash_mesh(mesh, material_ids, item["collision"])
            candidate = {"id": topology, "geometry": geometry, "materials": material_ids,
                         "polygon_materials": [int(poly.material_index) for poly in mesh.polygons],
                         "collision": item["collision"], "instances": [],
                         "source_types": set(item["source_types"])}
            cached = groups.setdefault(topology, candidate)
            for source_instance in item["instances"]:
                matrix = source_instance.pop("matrix_object")
                source_instance.update(index=exported, matrix=[list(row) for row in matrix],
                                       bounds=_bounds(cached["geometry"]["points"], matrix))
                cached["instances"].append(source_instance)
                exported += 1
        finally:
            bpy.data.objects.remove(temp, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if frozen_index % 10 == 0:
            job.status("scene_export", f"分析最终网格 {frozen_index+1}/{len(usable)}；实例 {exported}")

    if not groups:
        raise RuntimeError("场景没有可导出的最终求值网格；未生成代理 USD")

    # Keep all instances of one prototype in the same payload.  This guarantees
    # one topology/collision cook for large repeated sets such as parking blocks.
    bins = []
    for group in sorted(groups.values(), key=lambda item: item["geometry"]["triangles"], reverse=True):
        cost = group["geometry"]["triangles"]
        target = next((item for item in bins if item["triangles"] + cost <= budget), None)
        if target is None:
            target = {"groups": [], "triangles": 0}
            bins.append(target)
        target["groups"].append(group)
        target["triangles"] += cost
    chunks = []
    for number, payload in enumerate(bins):
        check_stop(job)
        target = geometry_folder / f"Payload_{number:03d}.usdc"
        chunk_started = time.perf_counter()
        root_prim = _write_chunk(target, number, payload["groups"], meters_per_unit, exact_collision, job)
        chunks.append({"index": number, "path": str(target), "root_prim": root_prim,
                       "prototype_count": len(payload["groups"]),
                       "instance_count": sum(len(group["instances"]) for group in payload["groups"]),
                       "triangles": payload["triangles"], "sha256": file_hash(target),
                       "write_seconds": round(time.perf_counter() - chunk_started, 3),
                       "working_set_mb": _peak_working_set_mb(current=True)})
        job.status("scene_export", f"写入 USD Payload {number+1}/{len(bins)}")
        for group in payload['groups']:
            group['vertex_count'] = len(group['geometry']['points'])
            group['geometry'] = {'triangles': group['geometry']['triangles']}
            group['polygon_materials'] = []

    # A refresh may produce fewer chunks or switch a channel from baked back to
    # translated nodes.  Remove only obsolete tool-generated payloads/textures;
    # Scene.usda is intentionally never touched here so user overrides survive.
    current_payloads = {Path(chunk["path"]).resolve() for chunk in chunks}
    for old_payload in geometry_folder.glob("Payload_*.usdc"):
        if old_payload.resolve() not in current_payloads:
            old_payload.unlink()
    current_textures = {Path(item["path"]).resolve() for item in materials.images.values()}
    for old_texture in (package / "Textures").iterdir():
        if old_texture.is_file() and old_texture.resolve() not in current_textures:
            old_texture.unlink()

    records = list(materials.records.values())
    material_layer = package / "ToolMaterials.usda"
    geometry_layer = package / "Geometry.usda"
    root_layer = package / "Scene.usda"
    _write_material_layer(material_layer, records, cfg["content_root"], data["asset_id"])
    _write_geometry_index(geometry_layer, chunks, meters_per_unit)
    root_created = _write_user_root(root_layer, meters_per_unit)
    package_json = package / "package.json"
    record = {
        "route": "usd_stage", "source": cfg["scene"], "reference_frame": frame,
        "blender_version": ".".join(map(str, bpy.app.version)),
        "source_blender_version": ".".join(map(str, authored)) if authored else None,
        "meters_per_unit": meters_per_unit, "objects": lights_and_cameras,
        "materials": records, "warnings": warnings + materials.warnings,
        "usd": {"root_layer": str(root_layer), "geometry_layer": str(geometry_layer),
                "material_layer": str(material_layer), "payloads": chunks,
                "root_created": root_created},
        "stats": {"prototypes": len(groups), "instances": exported,
                  "triangles_unique": sum(group["geometry"]["triangles"] for group in groups.values()),
                  "triangles_instanced": sum(group["geometry"]["triangles"] * len(group["instances"])
                                             for group in groups.values()),
                  "payloads": len(chunks),
                  "instance_reuse_ratio": round(exported / max(1, len(groups)), 3)},
        "prototypes": [{"id": group["id"], "triangles": group["geometry"]["triangles"],
                        "vertices": group["vertex_count"],
                        "prim_path": group.get("prim_path"),
                        "material_slots": group["materials"], "collision": group["collision"],
                        "instances": len(group["instances"]),
                        "source_types": sorted(group["source_types"])} for group in groups.values()],
        "instances": [item for group in groups.values() for item in group["instances"]],
    }
    atomic_json(package_json, {"schema": SCHEMA, "asset_id": data["asset_id"],
                               "root": "Scene.usda", "generated_layers": ["Geometry.usda", "ToolMaterials.usda"],
                               "payloads": ["Geometry/" + Path(chunk["path"]).name for chunk in chunks],
                               "stats": record["stats"]})
    record["usd"]["files"] = [
        {"path": str(path), "relative": str(path.relative_to(package)).replace("\\", "/"),
         "sha256": file_hash(path), "size": path.stat().st_size}
        for path in sorted((item for item in package.rglob("*") if item.is_file()), key=lambda item: str(item))
    ]
    record["metrics"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_system_working_set_mb": _peak_working_set_mb(),
        "package_file_count": len(record["usd"]["files"]),
        "package_bytes": sum(item["size"] for item in record["usd"]["files"]),
        "ue_assets_requested": 2 + len(records),  # Stage actor/cache plus material assets
        "instance_reuse_ratio": record["stats"]["instance_reuse_ratio"],
    }
    manifest_path = job.folder / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {
        "schema": SCHEMA, "job_id": data["id"], "asset_id": data["asset_id"], "fps": cfg["fps"],
        "frame_range": [cfg["start"], cfg["end"] or cfg["fps"] * 10],
        "files": {}, "materials": [], "warnings": []}
    manifest["schema"] = SCHEMA
    manifest["scene"] = record
    atomic_json(manifest_path, manifest)
    atomic_json(export_root / "manifest.json", manifest)
    job.status("scene_done", f"USD 场景导出完成：{len(groups)} 原型 / {exported} 实例 / {len(chunks)} Payload",
               manifest=str(manifest_path), root_layer=str(root_layer))
