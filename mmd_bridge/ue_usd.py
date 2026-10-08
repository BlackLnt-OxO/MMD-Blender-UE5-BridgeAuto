"""Unreal USD Stage deployment, persistent cache setup, and collision checks."""
from __future__ import annotations

import shutil
from pathlib import Path
import unreal as u

from .core import file_hash, read_json, atomic_json


def _enum(enum_type, *names):
    for name in names:
        if hasattr(enum_type, name):
            return getattr(enum_type, name)
    raise RuntimeError(f"UE 枚举 {enum_type.__name__} 缺少候选值: {names}")


def deploy_package(scene, project_dir, asset_id, content_root):
    usd = scene["usd"]
    target = (Path(project_dir) / "Content" / content_root.removeprefix("/Game/") /
              ("A_" + asset_id) / "Source" / "Scene")
    target.mkdir(parents=True, exist_ok=True)
    # Geometry and extracted textures are tool-owned. Clear only these exact
    # children so removed payloads cannot remain as stale data; Scene.usda is
    # deliberately excluded because it is the user's override layer.
    resolved_target = target.resolve()
    current_files={str(Path(item['relative'])).replace('\\','/') for item in usd['files']}
    for name in ("Geometry", "Textures", "Appearance", "Volumes"):
        generated = (target / name).resolve()
        if generated.parent != resolved_target:
            raise RuntimeError("USD 生成目录越出受管包范围")
        if generated.is_dir():
            for old in generated.rglob('*'):
                if not old.is_file() or old.suffix.lower() in ('.uasset','.umap'):
                    continue  # Preserve loaded UE directory markers and user assets.
                if not old.resolve().is_relative_to(resolved_target):
                    raise RuntimeError('USD 生成文件越出受管包范围')
                if old.relative_to(target).as_posix() not in current_files:
                    old.unlink()
    copied = []
    preserved_root = False
    for item in usd["files"]:
        source = Path(item["path"])
        if file_hash(source) != item["sha256"]:
            raise RuntimeError(f"USD 文件在导出后改变: {source}")
        relative = Path(item["relative"])
        destination = target / relative
        if relative.is_absolute() or not destination.resolve().is_relative_to(resolved_target):
            raise RuntimeError('USD 包的相对路径越出受管目录')
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
        if u.EditorAssetLibrary.get_metadata_tag(existing, "MMDBridge.Owner") != owner:
            raise RuntimeError(f"USD 缓存归属不匹配: {path}")
        return existing
    cache_type = getattr(u, "UsdAssetCache3", None)
    factory_type = (getattr(u, "UsdAssetCacheFactory", None) or
                    getattr(u, "UsdAssetCache3Factory", None))
    if factory_type is None:
        return None  # UE 5.8 exposes the Stage Actor but not this factory to Python.
    factory = factory_type()
    if cache_type is None:
        cache_type = factory.get_editor_property("supported_class")
    tools = u.AssetToolsHelpers.get_asset_tools()
    cache = tools.create_asset("USD_AssetCache", root + "/Scene", cache_type, factory)
    if cache is None:
        raise RuntimeError("无法创建持久化 UsdAssetCache3")
    directory = cache.get_editor_property("asset_directory")
    directory.set_editor_property("path", root + "/Scene/Generated")
    cache.set_editor_property("asset_directory", directory)
    u.EditorAssetLibrary.set_metadata_tag(cache, "MMDBridge.Owner", owner)
    u.EditorAssetLibrary.save_loaded_asset(cache)
    return cache


def _generated_assets(actor, scene):
    getter = getattr(actor, "get_generated_assets", None)
    component_getter = getattr(actor, "get_generated_component", None)
    if not callable(getter):
        return []
    paths = [item.get("prim_path") for item in scene.get("prototypes", [])
             if item.get("prim_path")]
    # Hidden definition prims may not have generated assets of their own. Query
    # visible references too, regardless of whether prototype paths exist.
    paths += [item.get("prim_path") for item in scene.get("instances", [])
              if item.get("prim_path")]
    paths = dict.fromkeys(paths)
    assets = {}
    by_path = {}
    for path in paths:
        try:
            values = getter(path)
            if isinstance(values, dict):
                values = values.values()
            meshes = [asset for asset in values or [] if isinstance(asset, u.StaticMesh)]
            if not meshes:
                values = getter(path + "/Mesh")
                if isinstance(values, dict):
                    values = values.values()
                meshes = [asset for asset in values or [] if isinstance(asset, u.StaticMesh)]
            if not meshes and callable(component_getter):
                component = component_getter(path + "/Mesh") or component_getter(path)
                if component is not None:
                    children = getattr(component, 'get_children_components', None)
                    components = [component] + (list(children(True)) if callable(children) else [])
                    for child in components:
                        if isinstance(child, u.StaticMeshComponent):
                            mesh = child.get_editor_property("static_mesh")
                            if isinstance(mesh, u.StaticMesh):
                                meshes.append(mesh)
        except Exception:
            continue
        by_path[path] = meshes
        for asset in meshes:
            assets[asset.get_path_name()] = asset
    return list(assets.values()), by_path


def save_stage_assets(actor, scene, root, owner):
    """Checkpoint only assets produced by this managed Stage transaction."""
    generated, _ = _generated_assets(actor, scene)
    # USD's unassigned meshes use an importer-created display-color material.
    # Persist the referenced asset too: saving only the mesh leaves a dangling
    # material package after the editor exits. Do not alter the material graph.
    for mesh in list(generated):
        if isinstance(mesh, u.StaticMesh):
            for slot in mesh.get_editor_property('static_materials'):
                material = slot.get_editor_property('material_interface')
                if material: generated.append(material)
    for volume in scene.get('volumes', []):
        path = volume.get('prim_path')
        if not path: continue
        assets = actor.get_generated_assets(path) or []
        if isinstance(assets, dict): assets = assets.values()
        generated.extend(assets)
        component = actor.get_generated_component(path)
        material = component.get_material(0) if component and hasattr(component, 'get_material') else None
        if material: generated.append(material)
    cache = u.load_asset(root + '/Scene/USD_AssetCache')
    saved = []
    visited = set()
    for asset in generated + ([cache] if cache else []):
        path = asset.get_path_name()
        if path in visited or not path.startswith(root + '/Scene/'):
            continue
        visited.add(path)
        previous = u.EditorAssetLibrary.get_metadata_tag(asset, 'MMDBridge.Owner')
        if previous and previous != owner:
            raise RuntimeError('USD 生成资源归属不对应: ' + path)
        u.EditorAssetLibrary.set_metadata_tag(asset, 'MMDBridge.Owner', owner)
        if not u.EditorAssetLibrary.save_loaded_asset(asset):
            raise RuntimeError('USD 生成资源检查点保存失败: ' + path)
        saved.append(path)
    return saved


def spawn_stage(scene, cfg, root, owner, actor_sub, tag, report, before_foliage=None):
    if not hasattr(u, "UsdStageActor"):
        raise RuntimeError("USDImporter 未加载，找不到 UsdStageActor；安装桥接后必须重启 UE")
    project_dir = Path(u.Paths.convert_relative_path_to_full(u.Paths.project_dir())).resolve()
    root_layer, copied, preserved = deploy_package(scene, project_dir, owner, cfg["content_root"])
    from .ue_materials import configure_baked_field_textures
    configure_baked_field_textures(scene, cfg, root, report)
    cache = _asset_cache(root, owner)
    actor = tag(actor_sub.spawn_actor_from_class(u.UsdStageActor, u.Vector()), "MMD USD Stage")
    if scene.get('render_look'):
        # Native Z-up USD maps (X,Y,Z) to (X,-Y,Z); ordinary bridge actors use (-Y,-X,Z).
        actor.set_actor_rotation(u.Rotator(pitch=0,yaw=-90,roll=0),False)

    # Prefer Blueprint-callable setters because they execute the Stage refresh
    # hooks that direct property writes can bypass.
    setter = getattr(actor, "set_usd_asset_cache", None)
    if cache is not None:
        if callable(setter):
            setter(cache)
        else:
            actor.set_editor_property("usd_asset_cache", cache)
    else:
        report.setdefault("warnings", []).append("UE Python 未暴露 USD 资源缓存工厂；Stage 使用 UE 内置缓存，重启后需验证重新生成")
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
    if scene.get('scatter_batches'):
        # Native PointInstancer translation produces shared instance components rather
        # than baking all instances into one enormous combined mesh.
        u.SystemLibrary.execute_console_command(None, 'USD.CollapseTopLevelPointInstancers 0')
    if u.SystemLibrary.is_unattended():
        actor.set_root_layer(str(root_layer))
    else:
        editor = getattr(u, "UsdStageEditorLibrary", None)
        if (editor is None or not editor.open_stage_editor() or
                not editor.set_attached_stage_actor(actor)):
            raise RuntimeError("无法打开 USD 阶段编辑器并关联场景 Actor")
        editor.file_open(str(root_layer))
        if editor.get_attached_stage_actor() != actor:
            raise RuntimeError("USD 阶段编辑器未关联当前场景 Actor")

    generated, by_path = _generated_assets(actor, scene)
    from .ue_shader_data import restore
    report.setdefault('checks',{})['shader_data']=restore(actor,[
        {'prim_path':i['prim_path'],**({'instance_index':i['instance_index']} if 'instance_index' in i else {}),**i['shader_data']}
        for i in scene.get('instances',[]) if i.get('shader_data')])
    static_meshes = [asset for asset in generated if isinstance(asset, u.StaticMesh)]
    if not static_meshes and scene.get('prototypes'):
        raise RuntimeError("USD Stage 未生成任何 Static Mesh；根层或资源缓存加载失败")
    volume_checks = []
    for volume in scene.get('volumes',[]):
        if not volume.get('prim_path'):
            continue
        component=actor.get_generated_component(volume['prim_path'])
        if not isinstance(component,u.HeterogeneousVolumeComponent):
            raise RuntimeError('USD 体积没有生成 HeterogeneousVolumeComponent: '+volume['prim_path'])
        material=component.get_material(0)
        if not material:
            raise RuntimeError('USD 体积未绑定可渲染材质: '+volume['prim_path'])
        component.get_owner().set_actor_label(volume['name'])
        assets=actor.get_generated_assets(volume['prim_path']) or []
        if isinstance(assets,dict):
            assets=assets.values()
        for asset in list(assets)+[material]:
            if asset.get_path_name().startswith(root+'/'):
                u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.Owner',owner)
                u.EditorAssetLibrary.save_loaded_asset(asset)
        volume_checks.append({'prim_path':volume['prim_path'],'component':component.get_path_name(),
                              'material':material.get_path_name()})
    package_path=root_layer.parent/'package.json'
    package_record=read_json(package_path)
    package_record['ue_volume_material_bindings']=volume_checks
    atomic_json(package_path,package_record)
    empty_material_slots = []
    for mesh in static_meshes:
        for index, slot in enumerate(mesh.get_editor_property("static_materials")):
            material = slot.get_editor_property("material_interface")
            if material is None or "WorldGridMaterial" in material.get_path_name():
                empty_material_slots.append(f"{mesh.get_name()}[{index}]")
    if empty_material_slots:
        raise RuntimeError("USD Stage 存在未绑定材质槽: " + ", ".join(empty_material_slots[:20]))
    from .ue_foliage import configure_distance_preservation
    from .ue_materials import Materials
    component_getter = getattr(actor, "get_generated_component", None)
    def foliage_components():
        if not callable(component_getter):
            return
        paths = dict.fromkeys(item['prim_path'] for item in
                              scene.get('hierarchy', []) + scene.get('instances', []))
        for path in paths:
            component = component_getter(path + '/Mesh') or component_getter(path)
            if component is not None:
                yield component
                if not isinstance(component, u.StaticMeshComponent):
                    yield from component.get_children_components(True)
    report.setdefault('checks', {})['foliage_distance'] = configure_distance_preservation(
        scene, report, root, static_meshes, before_foliage, foliage_components(),
        Materials(root, owner, report, cfg, before_foliage))
    labels = 0
    if scene.get("hierarchy") and not callable(component_getter):
        raise RuntimeError("UsdStageActor 无法取得生成组件，不能验证 Blender 大纲层级")
    if callable(component_getter):
        owners = {item["name"]: item["prim_path"] for item in scene.get("hierarchy", [])}
        for item in scene.get("hierarchy", []):
            component = component_getter(item["prim_path"])
            if component is None:
                raise RuntimeError(f"Blender 大纲 Actor 未生成: {item['prim_path']}")
            owner_actor = component.get_owner()
            if owner_actor is None or owner_actor == actor:
                raise RuntimeError(f"Blender 大纲层级缺少独立 Actor: {item['prim_path']}")
            if item["parent"]:
                parent_component = component_getter(owners[item["parent"]])
                if parent_component is None or owner_actor.get_attach_parent_actor() != parent_component.get_owner():
                    raise RuntimeError(f"Blender 大纲父子关系不匹配: {item['prim_path']}")
            owner_actor.set_actor_label(item["name"])
            labels += 1
        batch_counts = {item['prim_path']: item['instances'] for item in scene.get('scatter_batches', [])}
        checked_batches = set()
        for item in scene.get("instances", []):
            if item["prim_path"] == owners.get(item["name"]):
                continue
            component = component_getter(item["prim_path"])
            if component is None or component.get_owner() is None:
                raise RuntimeError(f"Blender 散件 Actor 未生成: {item['prim_path']}")
            if 'instance_index' in item:
                path = item['prim_path']
                if path in checked_batches:
                    continue
                meshes = [child for child in [component] + list(component.get_children_components(True))
                          if isinstance(child, u.InstancedStaticMeshComponent)]
                count = sum(child.get_instance_count() for child in meshes)
                if count != batch_counts[path]:
                    raise RuntimeError(f'USD 散布实例数量不匹配: {path}: {count}/{batch_counts[path]}')
                checked_batches.add(path)
                component.get_owner().set_actor_label(f'Scatter ({count} instances)')
                labels += 1
                continue
            parent_component = component_getter(owners[item["name"]])
            if (parent_component is None or
                    component.get_owner().get_attach_parent_actor() != parent_component.get_owner()):
                raise RuntimeError(f"Blender 散件未挂到原物体 Actor: {item['prim_path']}")
            component.get_owner().set_actor_label(item["label"])
            labels += 1
    legacy_meshes = []
    for prototype in scene.get("prototypes", []):
        if not prototype.get("disable_nanite"):
            continue
        meshes = by_path.get(prototype["prim_path"], [])
        if not meshes:
            raise RuntimeError(f"非 Nanite 网格未生成: {prototype['prim_path']}")
        for mesh in meshes:
            settings = mesh.get_editor_property("nanite_settings")
            triangles = mesh.get_num_triangles(0)
            if settings.get_editor_property("enabled") or triangles < prototype["triangles"] * 0.98:
                raise RuntimeError(f"非 Nanite 网格被简化: {prototype['prim_path']} "
                                   f"{triangles}/{prototype['triangles']} 三角形")
            legacy_meshes.append({"prim_path": prototype["prim_path"],
                                  "lod0_triangles": triangles, "source_triangles": prototype["triangles"]})
    result = {
        "route": "usd_stage", "actor": actor.get_path_name(), "root_layer": str(root_layer),
        "asset_cache": cache.get_path_name() if cache else None, "copied_files": copied,
        "user_root_preserved": preserved, "generated_assets": len(generated),
        "generated_static_meshes": len(static_meshes), "complex_collision_assets": 0,
        "verified_material_slots": sum(len(mesh.get_editor_property("static_materials")) for mesh in static_meshes),
        "fallback_collision": "none", "render_context": "unreal",
        "legacy_meshes": legacy_meshes,
        "blender_actor_labels": labels,
        "stats": scene.get("stats", {}),
        'volumes':volume_checks,
        'scatter_batches': len(scene.get('scatter_batches', [])),
        'batched_instances': sum(item['instances'] for item in scene.get('scatter_batches', [])),
    }
    report.setdefault("assets", {})["usd_stage"] = actor.get_path_name()
    report.setdefault("checks", {})["usd_stage"] = result
    return actor, result, by_path


def configure_stage_collision(actor, scene, by_path, result, before_each):
    prototypes = {item["id"]: item for item in scene.get("prototypes", [])}
    if any("prototype_id" not in item for item in scene.get("instances", [])):
        raise RuntimeError("USD 清单缺少实例与碰撞原型对应关系；请重新导出场景")
    instance_paths = {}
    for item in scene["instances"]:
        instance_paths.setdefault(item["prototype_id"], []).append(item["prim_path"])
    collision_meshes = {}
    for proto_id, prototype in prototypes.items():
        if not prototype["collision"]:
            continue
        paths = [prototype["prim_path"]] + instance_paths.get(proto_id, [])
        assets = [asset for path in paths for asset in by_path.get(path, [])]
        if not assets:
            raise RuntimeError(f"碰撞原型没有生成静态网格: {proto_id}")
        for mesh in assets:
            collision_meshes[mesh.get_path_name()] = mesh
    trace_flag = _enum(u.CollisionTraceFlag, "CTF_USE_COMPLEX_AS_SIMPLE", "USE_COMPLEX_AS_SIMPLE")
    meshes = list(collision_meshes.values())
    for index, mesh in enumerate(meshes):
        before_each(index, len(meshes), mesh)
        body = mesh.get_editor_property("body_setup")
        if not body:
            raise RuntimeError(f"静态网格缺少碰撞 BodySetup: {mesh.get_path_name()}")
        if (body.get_editor_property("never_needs_cooked_collision_data") or
                body.get_editor_property("collision_trace_flag") != trace_flag):
            body.set_editor_property("never_needs_cooked_collision_data", False)
            body.set_editor_property("collision_trace_flag", trace_flag)
            if not u.EditorAssetLibrary.save_loaded_asset(mesh):
                raise RuntimeError(f"碰撞网格保存失败: {mesh.get_path_name()}")
        result["complex_collision_assets"] = index + 1
    enabled = _enum(u.CollisionEnabled, "QUERY_AND_PHYSICS", "QUERY_AND_PHYSICS")
    disabled = _enum(u.CollisionEnabled, "NO_COLLISION", "NO_COLLISION")
    getter = getattr(actor, "get_generated_component", None)
    if not callable(getter):
        raise RuntimeError("UsdStageActor 缺少 GetGeneratedComponent，无法验证碰撞组件")
    covered = set()
    component_count = 0
    checked_components = set()
    for item in scene["instances"]:
        path = item["prim_path"]
        component = getter(path + "/Mesh") or getter(path)
        if 'instance_index' in item and component is not None:
            components = [child for child in [component] + list(component.get_children_components(True))
                          if isinstance(child, u.StaticMeshComponent)]
            if len(components) != 1:
                raise RuntimeError(f'USD 散布批次静态网格不唯一: {path}')
            component = components[0]
            if path in checked_components:
                continue
            checked_components.add(path)
        if not isinstance(component, u.StaticMeshComponent):
            raise RuntimeError(f"USD 实例缺少静态网格组件: {path}")
        mesh = component.get_editor_property("static_mesh")
        mesh_path = mesh.get_path_name() if mesh else None
        collidable = prototypes[item["prototype_id"]]["collision"]
        if collidable and mesh_path not in collision_meshes:
            raise RuntimeError(f"USD 碰撞实例网格不匹配: {path}")
        component.set_collision_enabled(enabled if collidable else disabled)
        if collidable:
            component.set_collision_profile_name("BlockAll")
            covered.add(mesh_path)
            component_count += 1
    if covered != set(collision_meshes):
        raise RuntimeError(f"USD 碰撞组件缺失: {len(collision_meshes) - len(covered)} 个网格未挂到场景")
    result["collision_components"] = component_count
    return result
