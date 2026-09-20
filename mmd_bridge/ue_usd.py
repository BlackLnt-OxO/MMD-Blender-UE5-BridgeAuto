"""Unreal USD Stage deployment, persistent cache setup, and collision checks."""
from __future__ import annotations

import shutil
from pathlib import Path
import unreal as u

from .core import file_hash


def _enum(enum_type, *names):
    for name in names:
        if hasattr(enum_type, name):
            return getattr(enum_type, name)
    raise RuntimeError(f"UE 枚举 {enum_type.__name__} 缺少候选值: {names}")


def deploy_package(scene, project_dir, asset_id):
    usd = scene["usd"]
    target = Path(project_dir) / "MMDBridgeData" / ("A_" + asset_id) / "Scene"
    target.mkdir(parents=True, exist_ok=True)
    # Geometry and extracted textures are tool-owned. Clear only these exact
    # children so removed payloads cannot remain as stale data; Scene.usda is
    # deliberately excluded because it is the user's override layer.
    resolved_target = target.resolve()
    for name in ("Geometry", "Textures"):
        generated = (target / name).resolve()
        if generated.parent != resolved_target:
            raise RuntimeError("USD 生成目录越出受管包范围")
        if generated.is_dir():
            shutil.rmtree(generated)
    copied = []
    preserved_root = False
    for item in usd["files"]:
        source = Path(item["path"])
        if file_hash(source) != item["sha256"]:
            raise RuntimeError(f"USD 文件在导出后改变: {source}")
        relative = Path(item["relative"])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() == "Scene.usda" and destination.exists():
            preserved_root = True
            continue
        shutil.copy2(source, destination)
        copied.append(str(relative).replace("\\", "/"))
    root = target / "Scene.usda"
    if not root.is_file():
        raise RuntimeError(f"USD 用户根层未部署: {root}")
    return root, copied, preserved_root


def _asset_cache(root, owner):
    path = root + "/Scene/USD_AssetCache"
    existing = u.load_asset(path)
    if existing:
        return existing
    cache_type = getattr(u, "UsdAssetCache3", None)
    factory_type = (getattr(u, "UsdAssetCacheFactory", None) or
                    getattr(u, "UsdAssetCache3Factory", None))
    if cache_type is None or factory_type is None:
        raise RuntimeError("USDImporter 未暴露 UsdAssetCache3/Factory；请确认插件已启用并重启 UE")
    tools = u.AssetToolsHelpers.get_asset_tools()
    cache = tools.create_asset("USD_AssetCache", root + "/Scene", cache_type, factory_type())
    if cache is None:
        raise RuntimeError("无法创建持久化 UsdAssetCache3")
    u.EditorAssetLibrary.set_metadata_tag(cache, "MMDBridge.Owner", owner)
    u.EditorAssetLibrary.save_loaded_asset(cache)
    return cache


def _generated_assets(actor, scene):
    getter = getattr(actor, "get_generated_assets", None)
    if not callable(getter):
        return []
    paths = [item.get("prim_path") for item in scene.get("prototypes", [])
             if item.get("prim_path")]
    if not paths:
        # Schema v2 manifests written before prototype paths were recorded still
        # contain every instance path. UE resolves these to the shared mesh asset.
        paths = [item.get("prim_path") for item in scene.get("instances", [])
                 if item.get("prim_path")]
    assets = {}
    for path in paths:
        try:
            values = getter(path)
        except Exception:
            continue
        if isinstance(values, dict):
            values = values.values()
        for asset in values or []:
            assets[asset.get_path_name()] = asset
    return list(assets.values())


def spawn_stage(scene, cfg, root, owner, actor_sub, tag, report):
    if not hasattr(u, "UsdStageActor"):
        raise RuntimeError("USDImporter 未加载，找不到 UsdStageActor；安装桥接后必须重启 UE")
    project_dir = Path(u.Paths.convert_relative_path_to_full(u.Paths.project_dir())).resolve()
    root_layer, copied, preserved = deploy_package(scene, project_dir, owner)
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    u.SystemLibrary.execute_console_command(world, "USD.EnableCollision 1")
    actor = tag(actor_sub.spawn_actor_from_class(u.UsdStageActor, u.Vector()), "MMD USD Stage")
    cache = _asset_cache(root, owner)

    # Prefer Blueprint-callable setters because they execute the Stage refresh
    # hooks that direct property writes can bypass.
    setter = getattr(actor, "set_usd_asset_cache", None)
    if callable(setter):
        setter(cache)
    else:
        actor.set_editor_property("usd_asset_cache", cache)
    setter = getattr(actor, "set_share_assets_for_identical_prims", None)
    if callable(setter):
        setter(True)
    else:
        actor.set_editor_property("share_assets_for_identical_prims", True)
    collision_enum = getattr(u, "UsdCollisionType", None)
    if collision_enum:
        value = _enum(collision_enum, "NONE", "None")
        setter = getattr(actor, "set_fallback_collision_type", None)
        if callable(setter):
            setter(value)
        else:
            actor.set_editor_property("fallback_collision_type", value)
    load_enum = getattr(u, "UsdInitialLoadSet", None)
    if load_enum:
        value = _enum(load_enum, "LOAD_ALL", "LoadAll")
        setter = getattr(actor, "set_initial_load_set", None)
        if callable(setter):
            setter(value)
        else:
            actor.set_editor_property("initial_load_set", value)
    setter = getattr(actor, "set_render_context", None)
    if callable(setter):
        setter(u.Name("unreal"))
    else:
        actor.set_editor_property("render_context", u.Name("unreal"))
    setter = getattr(actor, "set_root_layer", None)
    if not callable(setter):
        raise RuntimeError("UsdStageActor 缺少 SetRootLayer，无法保证刷新通知")
    setter(str(root_layer))

    generated = _generated_assets(actor, scene)
    static_meshes = [asset for asset in generated if isinstance(asset, u.StaticMesh)]
    collision_assets = 0
    if cfg.get("exact_collision", True):
        trace_flag = _enum(u.CollisionTraceFlag, "CTF_USE_COMPLEX_AS_SIMPLE", "USE_COMPLEX_AS_SIMPLE")
        for mesh in static_meshes:
            body = mesh.get_editor_property("body_setup")
            if body:
                body.set_editor_property("collision_trace_flag", trace_flag)
                collision_assets += 1
                u.EditorAssetLibrary.save_loaded_asset(mesh)
        for component in actor.get_components_by_class(u.StaticMeshComponent):
            component.set_collision_enabled(_enum(u.CollisionEnabled, "QUERY_AND_PHYSICS", "QUERY_AND_PHYSICS"))
            component.set_collision_profile_name("BlockAll")

    result = {
        "route": "usd_stage", "actor": actor.get_path_name(), "root_layer": str(root_layer),
        "asset_cache": cache.get_path_name(), "copied_files": copied,
        "user_root_preserved": preserved, "generated_assets": len(generated),
        "generated_static_meshes": len(static_meshes), "complex_collision_assets": collision_assets,
        "fallback_collision": "none", "render_context": "unreal",
        "stats": scene.get("stats", {}),
    }
    report.setdefault("assets", {})["usd_stage"] = actor.get_path_name()
    report.setdefault("checks", {})["usd_stage"] = result
    return actor, result
