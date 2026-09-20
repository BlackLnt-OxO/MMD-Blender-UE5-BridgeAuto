"""Interactive upstream import. No PMX/VMD parser or character shader is owned here."""
from pathlib import Path
import unreal as u
from .core import atomic_json, read_json, check_stop


class UpstreamBlocked(RuntimeError):
    pass


class ImportCancelled(RuntimeError):
    pass


def probe_dependencies():
    version = u.SystemLibrary.get_engine_version()
    if not version.startswith("5.8."):
        raise UpstreamBlocked("MMD2Unreal 流程要求 UE 5.8.x")
    # Resolve reflected native classes, not just a .uproject enable flag.
    classes = {}
    for path in ("/Script/MMD2UnrealEditor", "/Script/KawaiiPhysics"):
        package = u.find_object(None, path)
        classes[path] = package is not None
    if not all(classes.values()):
        raise UpstreamBlocked("请安装并启用 UE 5.8 版 MMD2Unreal 和 KawaiiPhysics，然后重启 UE：" + str(classes))
    if not hasattr(u.AssetToolsHelpers.get_asset_tools(), "import_asset_tasks"):
        raise UpstreamBlocked("当前 UE 未提供交互 AssetImportTask 入口")
    return {"engine": version, "modules": classes, "entry": "AssetImportTask(automated=False)"}


def _assets(paths):
    return [asset for path in paths if (asset := u.load_asset(path)) is not None]


def import_sources(job):
    """One official modal dialog per source; persist each successful source separately."""
    data = job.data
    cfg = data["settings"]
    dependencies = probe_dependencies()
    job.verify_inputs()
    receipt_path = job.folder / "mmd2unreal_receipt.json"
    receipt = read_json(receipt_path) if receipt_path.exists() else {
        "job_id": data["id"], "inputs": data["inputs"], "sources": {}}
    if receipt["job_id"] != data["id"] or receipt["inputs"] != data["inputs"]:
        raise UpstreamBlocked("上游导入回执与输入不匹配")
    receipt["dependencies"] = dependencies
    # Separate from the bridge-owned root: never reparent upstream materials or
    # sweep/tag/delete upstream assets during scene refresh.
    root = cfg["content_root"] + "/Upstream_" + data["id"]
    for kind, expected_type in (("model", u.SkeletalMesh), ("motion", u.AnimSequence),
                                ("lipsync", u.AnimSequence), ("camera", u.LevelSequence)):
        check_stop(job)
        if not cfg.get(kind):
            continue
        saved = receipt["sources"].get(kind)
        if saved:
            loaded = _assets(saved["assets"])
            if len(loaded) != len(saved["assets"]) or not any(isinstance(a, expected_type) for a in loaded):
                raise UpstreamBlocked(f"{kind} 已导入资产缺失；请新建任务，避免重复覆盖")
            continue
        destination = root + "/" + kind.capitalize()
        if u.EditorAssetLibrary.list_assets(destination, recursive=True, include_folder=False):
            raise UpstreamBlocked(f"{kind} 目录存在未确认资产（可能上次导入中断）：{destination}；请新建任务")
        source_path = str(Path(cfg[kind]).resolve())
        message = f"正在导入 {kind}：{Path(source_path).name}\n文件：{source_path}\n请在 UE 的 MMD2Unreal 官方窗口确认；动作请选择刚导入的角色"
        job.status("waiting_user_import", message, source=kind, source_path=source_path)
        u.log(f"[MMDBridge / MMD2Unreal] {kind}: {source_path}")
        task = u.AssetImportTask()
        factory_class = getattr(u, "MMDPmxFactory" if kind == "model" else "MMDVmdFactory", None)
        if factory_class is None:
            raise UpstreamBlocked("MMD2Unreal 未暴露对应导入 Factory")
        task.set_editor_property("factory", factory_class())
        for key, value in {"filename": cfg[kind], "destination_path": destination,
                           "automated": False, "replace_existing": False, "save": True}.items():
            task.set_editor_property(key, value)
        u.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        paths = list(task.get_editor_property("imported_object_paths"))
        paths = sorted(set(map(str, paths)) | set(map(str, u.EditorAssetLibrary.list_assets(
            destination, recursive=True, include_folder=False))))
        imported = _assets(paths)
        if not imported:
            raise ImportCancelled(f"{kind} 导入窗口已结束，未生成资产；可能已取消。可恢复任务重试")
        expected = [a for a in imported if isinstance(a, expected_type)]
        if not expected:
            raise UpstreamBlocked(f"{kind} 未生成所需 {expected_type.__name__}，不能判定导入成功")
        for asset in imported:
            u.EditorAssetLibrary.save_loaded_asset(asset)
        receipt["sources"][kind] = {"source": cfg[kind], "assets": paths,
                                    "primary": [a.get_path_name() for a in expected]}
        atomic_json(receipt_path, receipt)
        job.status("upstream_imported", f"{kind} 导入完成，已验证生成资产", source=kind)
    check_stop(job)
    return receipt


def primary(receipt, kind):
    paths = receipt.get("sources", {}).get(kind, {}).get("primary", [])
    # Split expression files are handled independently when assembling tracks.
    return _assets(paths)


def prepare_camera(receipt, job):
    cameras = primary(receipt, "camera")
    if not cameras:
        return None
    if len(cameras) != 1:
        raise UpstreamBlocked("上游生成多个相机序列，无法唯一确定用于组合的序列")
    source = cameras[0]
    cfg = job.data["settings"]
    destination = cfg["content_root"] + "/Upstream_" + job.data["id"] + "/AssemblyCamera"
    copied = u.load_asset(destination)
    if copied is None:
        copied = u.EditorAssetLibrary.duplicate_asset(source.get_path_name(), destination)
        if copied is None:
            raise UpstreamBlocked("无法创建相机序列组合副本")
    u.LevelSequenceEditorBlueprintLibrary.open_level_sequence(copied)
    subsystem = u.get_editor_subsystem(u.LevelSequenceEditorSubsystem)
    # Imported possessables depend on their original world. Convert only the
    # assembly copy, before switching maps; the upstream source remains intact.
    for binding in list(copied.get_possessables()):
        if binding.get_parent().is_valid():
            continue
        if not subsystem.convert_to_spawnable(binding):
            raise UpstreamBlocked("相机绑定无法转为可独立加载的 Spawnable；已保留上游序列，请检查绑定")
    u.EditorAssetLibrary.save_loaded_asset(copied)
    return copied
