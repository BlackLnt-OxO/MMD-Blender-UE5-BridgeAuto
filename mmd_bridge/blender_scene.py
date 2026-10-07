"""Static scene geometry and material sidecar exporter; original .blend is never saved."""
import bpy
from pathlib import Path
from mathutils import Matrix
from .core import (atomic_json, read_json, stable_id, asset_name,
                   validate_fbx, SCHEMA, check_stop, scene_mesh_record_is_importable)
from .blender_host import FBX_DEFAULTS, select
from .blender_materials import Materials


def export_scene(job):
    data, cfg = job.data, job.data["settings"]
    # Schema v1 jobs predate the selector and remain resumable through the
    # original per-object FBX route.  New tasks default to a live USD Stage.
    if cfg.get("scene_format", "fbx_legacy" if data.get("schema") == 1 else "usd_stage") == "usd_stage":
        from .blender_usd import export_usd_scene
        return export_usd_scene(job)
    job.status("scene_export", "读取场景、提取打包贴图并转换材质")
    bpy.ops.wm.open_mainfile(filepath=cfg["scene"], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    scene.frame_set(cfg["scene_frame"])
    from .blender_usd import missing_external_files, render_visible_objects
    missing_external = missing_external_files()
    out = Path(cfg.get('character_output_dir') or cfg['output_override'] or Path(cfg['scene']).parent)
    out = out / ("Scene_" + data["id"])
    out.mkdir(parents=True, exist_ok=True)
    materials = Materials(out, cfg["scene"], cfg["bake_resolution"],
                          strategy=cfg.get("material_strategy", "auto"),
                          bake_fallback=cfg.get("material_bake_fallback", True),
                          complex_strategy=cfg.get('material_complex_strategy', 'pbr_bake'),
                          conversion_timeout=cfg.get('material_conversion_timeout', 120.0),
                          workflow=cfg.get('material_workflow', 'semantic_plan_v1'))
    units = scene.unit_settings.scale_length * cfg["scene_scale"]
    record = {"source": cfg["scene"], "reference_frame": cfg["scene_frame"], "objects": [], "meshes": [],
              "materials": [], "warnings": [], "skipped_items": [],
              "missing_external_files": missing_external, "meters_per_unit": units}
    record["warnings"].extend(
        f"场景引用的外部文件不存在：{path}；求值结果可能不完整" for path in missing_external)
    objects = list(scene.objects)
    render_visible = render_visible_objects(scene)
    identities = {o.name: stable_id(cfg["scene"], o.name) for o in objects}
    cache = {}
    deps = bpy.context.evaluated_depsgraph_get()
    for index, obj in enumerate(objects):
        check_stop(job)
        if obj.type in ("LIGHT", "CAMERA") and obj.name not in render_visible:
            continue
        if obj.type not in ("MESH", "CURVE", "SURFACE", "FONT", "EMPTY", "LIGHT", "CAMERA"):
            record["warnings"].append(f"对象类型未迁移: {obj.name} / {obj.type}")
            continue
        entry = {"id": identities[obj.name], "name": obj.name, "type": obj.type,
                 "matrix": [list(r) for r in obj.matrix_world],
                 "parent": identities.get(obj.parent.name) if obj.parent else None,
                 "visible": not obj.hide_render, "materials": []}
        if obj.type in ("MESH", "CURVE", "SURFACE", "FONT"):
            water = any(m.type == "OCEAN" for m in obj.modifiers)
            # Shared mesh instances remain shared unless modifiers/procedural material evaluation differs.
            mesh_key = stable_id(cfg["scene"], obj.data.name, obj.name if obj.modifiers else "")
            if mesh_key not in cache:
                try:
                    evaluated = obj.evaluated_get(deps)
                    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=deps)
                except Exception as exc:
                    cache[mesh_key] = None
                    record["warnings"].append(f"物体 {obj.name} 求值失败，已跳过：{type(exc).__name__}: {exc}")
                    record["skipped_items"].append({"stage": "scene_geometry", "name": obj.name,
                                                    "source": cfg["scene"], "reason": str(exc)})
                    record["objects"].append(entry)
                    continue
                mesh_obj = bpy.data.objects.new(asset_name("SM", obj.name, mesh_key), mesh)
                scene.collection.objects.link(mesh_obj)
                mesh_obj.matrix_world = Matrix.Identity(4)
                mesh.update()
                mesh_summary = {
                    "vertices": len(mesh.vertices), "polygons": len(mesh.polygons),
                    "degenerate": not any(poly.area > 1e-12 for poly in mesh.polygons),
                }
                if not scene_mesh_record_is_importable(mesh_summary):
                    cache[mesh_key] = None
                    record["warnings"].append(
                        f"空网格或全部退化，作为层级占位节点保留: {obj.name}")
                    record["skipped_items"].append({"stage": "scene_geometry", "name": obj.name,
                                                    "source": cfg["scene"], "reason": "空网格或全部退化"})
                    bpy.data.objects.remove(mesh_obj, do_unlink=True)
                    bpy.data.meshes.remove(mesh)
                    record["objects"].append(entry)
                    if index % 10 == 0:
                        job.status("scene_export", f"转换场景 {index+1}/{len(objects)}: {obj.name}")
                    continue
                # Duplicate materials: source names stay stable across instance traversal.
                source_mats=list(mesh.materials)
                for slot,mat in enumerate(source_mats):
                    if mat:
                        copy=mat.copy()
                        copy.name=mat.name+'_'+mesh_key[:8]
                        mesh.materials[slot]=copy
                for slot, mat in enumerate(source_mats):
                    if mat:
                        copy = mesh.materials[slot]
                        try:
                            rec = materials.analyze(copy, mesh_obj, water=water)
                        except Exception as exc:
                            rec = materials.fallback(copy, f"{type(exc).__name__}: {exc}", mat.name)
                        rec["source_name"] = mat.name
                        entry["materials"].append(rec["id"])
                    else:
                        entry["materials"].append(None)
                select([mesh_obj])
                target = out / (mesh_obj.name + ".fbx")
                scene.unit_settings.scale_length = 1.0
                bpy.ops.export_scene.fbx(filepath=str(target), **{**FBX_DEFAULTS, "object_types": {"MESH"}, "bake_anim": False})
                cache[mesh_key] = {"id": mesh_key, "name": mesh_obj.name, "file": validate_fbx(target),
                                   "materials": entry["materials"], **mesh_summary, "water": water}
                bpy.data.objects.remove(mesh_obj, do_unlink=True)
            if cache[mesh_key] is not None:
                entry["mesh"] = mesh_key
                entry["materials"] = cache[mesh_key]["materials"]
        elif obj.type == "LIGHT":
            entry["light"] = {"type": obj.data.type, "color": list(obj.data.color), "energy": obj.data.energy,
                              "size": getattr(obj.data, "size", 1), 'size_y':getattr(obj.data,'size_y',getattr(obj.data,'size',1)), "spot_size": getattr(obj.data, "spot_size", 1)}
        elif obj.type == "CAMERA":
            entry["camera"] = {"lens": obj.data.lens, "sensor_width": obj.data.sensor_width,
                               "sensor_height": obj.data.sensor_height, "projection": obj.data.type}
        record["objects"].append(entry)
        if index % 10 == 0:
            job.status("scene_export", f"转换场景 {index+1}/{len(objects)}: {obj.name}")
    record["meshes"] = [mesh for mesh in cache.values() if mesh is not None]
    record["materials"] = list(materials.records.values())
    record["warnings"] += materials.warnings
    if scene.world and scene.world.use_nodes:
        sky = next((n for n in scene.world.node_tree.nodes if n.type == "TEX_SKY"), None)
        bg = next((n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND"), None)
        record["world"] = {"sky": bool(sky), "strength": bg.inputs["Strength"].default_value if bg else 1,
                           "sun_elevation": getattr(sky, "sun_elevation", 0.5), "sun_rotation": getattr(sky, "sun_rotation", 0)}
        record["warnings"].append("Blender 世界光照映射为 UE 天空近似；曝光和强度可在生成关卡调整")
    manifest_file = job.folder / "manifest.json"
    manifest = read_json(manifest_file) if manifest_file.exists() else {
        "schema": SCHEMA, "job_id": data["id"], "asset_id": data["asset_id"], "fps": cfg["fps"],
        "frame_range": [cfg["start"], cfg["end"] or cfg["fps"]*10], "files": {}, "materials": [], "warnings": []}
    manifest["scene"] = record
    atomic_json(manifest_file, manifest)
    atomic_json(out / "manifest.json", manifest)
    for warning in dict.fromkeys(record["warnings"]):
        job.status("scene_warning", warning)
    job.status("scene_done", "场景导出完成", manifest=str(manifest_file))
