"""UE editor worker. Legacy FBX import + deterministic scene/Sequencer assembly."""
import math
import os
import contextlib
import sys
import time
import traceback
from pathlib import Path
import unreal as u
from .core import (Job, read_json, atomic_json, file_hash, check_stop, Cancelled,
                   recoverable_ue_asset_packages, scene_mesh_record_is_importable)
from . import process
from .ue_materials import Materials, prop
from .ue_usd import spawn_stage


UE_CHECKPOINT_SCHEMA = 1
UE_MATERIAL_GC_BATCH = 4


class DeferredVRAM(RuntimeError):
    pass


def record_import_failure(job, report, stage, name, source, exc):
    """Persist one recoverable item failure and continue the remaining import."""
    failure = {
        "stage": str(stage), "name": str(name), "source": str(source or ""),
        "error_type": type(exc).__name__, "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    report.setdefault("failures", []).append(failure)
    report.setdefault("warnings", []).append(
        f"已跳过失败项 [{failure['stage']}] {failure['name']}: {failure['error']}")
    atomic_json(job.folder / "ue_report.json", report)
    job.status("ue_item_skipped",
               f"已跳过失败项 [{failure['stage']}] {failure['name']}: {failure['error']}")
    u.log_error(
        f"[MMDBridge][Skipped] {failure['stage']} / {failure['name']} / "
        f"{failure['source']}\n{failure['traceback']}")
    return failure


def record_import_skip(job, report, stage, name, source, reason):
    """Persist an intentional skip detected before an importer is invoked."""
    item = {"stage": str(stage), "name": str(name), "source": str(source or ""),
            "reason": str(reason)}
    report.setdefault("skipped_items", []).append(item)
    report.setdefault("warnings", []).append(
        f"已跳过 [{item['stage']}] {item['name']}: {item['reason']}")
    atomic_json(job.folder / "ue_report.json", report)
    job.status("ue_item_skipped", f"已跳过 [{item['stage']}] {item['name']}: {item['reason']}")
    u.log_warning(f"[MMDBridge][Skipped] {item['stage']} / {item['name']} / {item['reason']}")
    return item


def load_ue_checkpoint(job, data, manifest_path):
    """Load a durable resume point tied to this exact job and manifest."""
    digest = file_hash(manifest_path)
    path = job.folder / "ue_checkpoint.json"
    fresh = {
        "schema": UE_CHECKPOINT_SCHEMA,
        "job_id": data["id"],
        "manifest_sha256": digest,
        "completed": {"character": False, "materials": [], "scene_meshes": [], "assembly": False},
        "last_stage": None,
        "updated": time.time(),
    }
    if not path.exists():
        return fresh, False
    try:
        saved = read_json(path)
    except Exception:
        return fresh, False
    if (saved.get("schema") != UE_CHECKPOINT_SCHEMA or saved.get("job_id") != data["id"] or
            saved.get("manifest_sha256") != digest):
        return fresh, False
    completed = saved.setdefault("completed", {})
    completed.setdefault("character", False)
    completed.setdefault("materials", [])
    completed.setdefault("scene_meshes", [])
    completed.setdefault("assembly", False)
    return saved, bool(completed["character"] or completed["materials"] or
                       completed["scene_meshes"] or completed["assembly"])


def save_ue_checkpoint(job, checkpoint, stage, item=None):
    completed = checkpoint["completed"]
    if item is None:
        completed[stage] = True
    else:
        values = set(completed.setdefault(stage, []))
        values.add(str(item))
        completed[stage] = sorted(values)
    checkpoint["last_stage"] = stage
    checkpoint["updated"] = time.time()
    atomic_json(job.folder / "ue_checkpoint.json", checkpoint)


def collect_import_garbage():
    """Best-effort release of transient render assets between import batches."""
    collect = getattr(u.SystemLibrary, "collect_garbage", None)
    if callable(collect):
        collect()
    else:
        u.SystemLibrary.execute_console_command(None, "obj gc")


def ensure_import_vram(job, cfg, next_step):
    """Pause before the next asset at the high watermark; resume at the low one."""
    pause_percent = int(cfg.get("ue_vram_pause_percent", 90))
    resume_percent = int(cfg.get("ue_vram_resume_percent", 80))
    info = process.gpu_memory_info()
    if not info or not info.get("total_mb"):
        return info
    used_percent = info["used_mb"] * 100.0 / info["total_mb"]
    if used_percent < pause_percent:
        return info
    collect_import_garbage()
    refreshed = process.gpu_memory_info() or info
    refreshed_percent = refreshed["used_mb"] * 100.0 / refreshed["total_mb"]
    if refreshed_percent <= resume_percent:
        return refreshed
    message = (
        f"总 GPU 内存占用 {refreshed_percent:.1f}%，达到暂停阈值 {pause_percent}%；"
        f"已在“{next_step}”之前暂停、清理并保存检查点。降至 {resume_percent}% 后将自动恢复。"
    )
    job.status(
        "ue_wait_vram", message, used_vram_mb=refreshed["used_mb"],
        free_vram_mb=refreshed["free_mb"], total_vram_mb=refreshed["total_mb"],
        used_vram_percent=round(refreshed_percent, 1), pause_percent=pause_percent,
        resume_percent=resume_percent,
        retry_after=time.time()+15,
    )
    raise DeferredVRAM(message)


def maybe_collect_import_garbage(cfg):
    """Avoid expensive full GC while there is still a comfortable VRAM margin."""
    info = process.gpu_memory_info()
    pause_percent = int(cfg.get("ue_vram_pause_percent", 90))
    resume_percent = int(cfg.get("ue_vram_resume_percent", 80))
    # Below the resume watermark, favor throughput and skip explicit GC. Between
    # resume and pause, continue importing while collecting once per batch.
    soft_limit = resume_percent
    if (not info or not info.get("total_mb") or
            info["used_mb"] * 100.0 / info["total_mb"] >= soft_limit):
        collect_import_garbage()


def set_props(obj, **values):
    for key,value in values.items():
        prop(obj,key,value)
    return obj


def imported(task):
    return [u.load_asset(p) for p in task.get_editor_property("imported_object_paths")]


def import_fbx(path, destination, name, kind, skeleton=None):
    digest = file_hash(path)
    existing = u.load_asset(destination+'/'+name)
    owner = destination.split('/A_')[-1].split('/')[0]
    if existing and u.EditorAssetLibrary.get_metadata_tag(existing,'MMDBridge.Owner')==owner:
        reusable = kind=='static' or (kind=='animation' and existing.get_editor_property('skeleton')==skeleton)
        reusable = reusable or (kind=='model' and existing.get_editor_property('skeleton') is not None)
        if reusable and u.EditorAssetLibrary.get_metadata_tag(existing,'MMDBridge.FbxHash')==digest and u.EditorAssetLibrary.get_metadata_tag(existing,'MMDBridge.ImportProfile')=='legacy3':
            return [existing]
    options = u.FbxImportUI()
    set_props(options, automated_import_should_detect_type=False, import_materials=False, import_textures=False,
              create_physics_asset=False, import_as_skeletal=kind!="static", import_mesh=kind!="animation",
              import_animations=kind=="animation")
    prop(options,"mesh_type_to_import", {"model":u.FBXImportType.FBXIT_SKELETAL_MESH,
         "animation":u.FBXImportType.FBXIT_ANIMATION,"static":u.FBXImportType.FBXIT_STATIC_MESH}[kind])
    if skeleton:
        prop(options,"skeleton",skeleton)
    for data in (options.skeletal_mesh_import_data,options.anim_sequence_import_data,options.static_mesh_import_data):
        set_props(data,convert_scene=True,convert_scene_unit=True,force_front_x_axis=True,import_uniform_scale=1.0)
    set_props(options.skeletal_mesh_import_data,import_morph_targets=True,update_skeleton_reference_pose=False,
              use_t0_as_ref_pose=False,normal_import_method=u.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS)
    set_props(options.anim_sequence_import_data, import_custom_attribute=True,
              do_not_import_curve_with_zero=False, remove_redundant_keys=False,
              animation_length=u.FBXAnimationLengthImportType.FBXALIT_EXPORTED_TIME)
    set_props(options.static_mesh_import_data,combine_meshes=False,auto_generate_collision=False,generate_lightmap_u_vs=False,
              normal_import_method=u.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS)
    task = set_props(u.AssetImportTask(),filename=str(path),destination_path=destination,destination_name=name,
                     automated=True,replace_existing=True,save=True,options=options,factory=u.FbxFactory())
    u.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    assets = imported(task)
    if not assets:
        raise RuntimeError(f"FBX 未生成资源: {path}")
    owner = destination.split("/A_")[-1].split("/")[0]
    for asset in assets:
        if asset:
            u.EditorAssetLibrary.set_metadata_tag(asset,"MMDBridge.Owner",owner)
            u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.FbxHash',digest)
            u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.ImportProfile','legacy3')
            u.EditorAssetLibrary.save_loaded_asset(asset)
    return assets


class _CameraCutTee:
    """同时保留 UE Output Log 与独立会话日志，GUI 可实时读取后者。"""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            try:
                stream.write(value)
                stream.flush()
            except Exception:
                pass
        return len(value)

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


def run_camera_cut_request(command):
    """在 UE 编辑器内执行独立的 CameraCutAuto，并把完整输出落到会话目录。"""
    session = Path(command["session"]).resolve()
    mode = str(command.get("mode", "")).lower()
    session.mkdir(parents=True, exist_ok=True)
    job = Job(session)
    log_path = session / f"ue_{mode}.log"
    report_path = session / ("probe.json" if mode == "probe" else f"{mode}_latest.json")
    script_path = Path(__file__).with_name("ue_camera_cut_baker.py")
    config_path = session / f"camera_cut_config_{mode}.json"
    config = {
        "MODE": mode,
        "CUT_FRAMES": [int(frame) for frame in command.get("cut_frames", [])],
        "REPORT_DIR": str(session),
    }
    atomic_json(config_path, config)
    old_config = os.environ.get("CAMCUT_CONFIG")
    old_no_autorun = os.environ.get("CAMCUT_NO_AUTORUN")
    try:
        if not script_path.is_file():
            raise RuntimeError(f"UE 插件中缺少CameraCutAuto脚本：{script_path}。请在桌面工具中重新安装桥接插件。")
        job.status("camera_cut_running", f"CameraCutAuto：正在执行 {mode} 模式", mode=mode,
                   log=str(log_path))
        with log_path.open("a", encoding="utf-8", buffering=1) as handle:
            tee = _CameraCutTee(sys.stdout, handle)
            os.environ["CAMCUT_CONFIG"] = str(config_path)
            os.environ.pop("CAMCUT_NO_AUTORUN", None)
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                print(f"[MMDBridge] CameraCutAuto mode={mode}")
                namespace = {"__name__": "__main__", "__file__": str(script_path)}
                exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"),
                     namespace, namespace)
        reports = sorted(session.glob("*.json"), key=lambda path: path.stat().st_mtime)
        matching = [path for path in reports if path.name == "probe.json"
                    or path.name.startswith(mode + "_")]
        report = matching[-1] if matching else report_path
        if not report.exists():
            raise RuntimeError("CameraCutAuto 没有生成执行回执")
        payload = read_json(report)
        if payload.get("missing") or payload.get("verification", {}).get("pass") is False:
            raise RuntimeError("CameraCutAuto 验证失败，详见 " + str(report))
        if payload.get("skipped_existing_cuts"):
            job.status("camera_cut_existing", payload["message"], mode=mode, report=str(report))
            return
        job.status("camera_cut_complete", f"CameraCutAuto：{mode} 完成；报告：{report}",
                   mode=mode, report=str(report), log=str(log_path))
    except Exception:
        error = traceback.format_exc()
        try:
            with log_path.open("a", encoding="utf-8", buffering=1) as handle:
                handle.write(error)
        except Exception:
            pass
        job.status("camera_cut_failed", f"CameraCutAuto：{mode} 失败；详见 {log_path}",
                   mode=mode, log=str(log_path), error=error)
    finally:
        if old_config is None:
            os.environ.pop("CAMCUT_CONFIG", None)
        else:
            os.environ["CAMCUT_CONFIG"] = old_config
        if old_no_autorun is None:
            os.environ.pop("CAMCUT_NO_AUTORUN", None)
        else:
            os.environ["CAMCUT_NO_AUTORUN"] = old_no_autorun


def create_asset(path, cls, factory, owner):
    asset = u.load_asset(path)
    if asset:
        if u.EditorAssetLibrary.get_metadata_tag(asset,"MMDBridge.Owner") != owner:
            raise RuntimeError(f"拒绝覆盖未受管理资源: {path}")
        return asset
    folder,name = path.rsplit("/",1)
    asset = u.AssetToolsHelpers.get_asset_tools().create_asset(name,folder,cls,factory)
    if not asset:
        raise RuntimeError(f"创建失败: {path}")
    u.EditorAssetLibrary.set_metadata_tag(asset,"MMDBridge.Owner",owner)
    return asset


def frame_number(n):
    return u.FrameNumber(int(n))


def bind_float(sequence, actor_binding, obj, name, path, samples):
    binding = sequence.add_possessable(obj)
    binding.set_parent(actor_binding)
    track = binding.add_track(u.MovieSceneFloatTrack)
    track.set_property_name_and_path(name,path)
    section = track.add_section()
    section.set_range(sequence.get_playback_start(),sequence.get_playback_end())
    channel = section.get_all_channels()[0]
    for frame,value in samples:
        channel.add_key(frame_number(frame),float(value),interpolation=u.MovieSceneKeyInterpolation.LINEAR)
    return binding


def scene_transform(matrix, units=1, aimed=False):
    # Blender -Y forward, +X right, +Z up -> UE +X forward, +Y right, +Z up.
    # Reflection applies to locations and to both bases of object transforms.
    def mul(a,b):
        return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    c = [[0,-1,0],[-1,0,0],[0,0,1]]
    rot = mul(mul(c,[r[:3] for r in matrix[:3]]),c)
    if aimed:
        rot = mul([r[:3] for r in matrix[:3]],[[0,1,0],[0,0,1],[-1,0,0]])
        rot = mul(c,rot)
    scales = [math.sqrt(sum(rot[i][j]**2 for i in range(3))) for j in range(3)]
    determinant = sum(rot[0][i]*(rot[1][(i+1)%3]*rot[2][(i+2)%3]-rot[1][(i+2)%3]*rot[2][(i+1)%3]) for i in range(3))
    if determinant<0:
        scales[0] *= -1
    loc = [-matrix[1][3]*100*units,-matrix[0][3]*100*units,matrix[2][3]*100*units]
    x,y,z = [u.Vector(*(rot[i][j]/(scales[j] or 1) for i in range(3))) for j in range(3)]
    rotation = u.MathLibrary.make_rotation_from_axes(x,y,z)
    return u.Transform(u.Vector(*loc),rotation,u.Vector(*(s*units for s in scales)))


def validate_animation(mesh, animation, manifest):
    options = u.AnimPoseEvaluationOptions(evaluate_curves=True, optional_skeletal_mesh=mesh, should_retarget=False)
    checks = []
    for sample in read_json(manifest['reference_samples']):
        pose = u.AnimPoseExtensions.get_anim_pose_at_time(animation,
            (sample['frame']-manifest['frame_range'][0])/manifest['fps'],options)
        available = {str(n) for n in u.AnimPoseExtensions.get_bone_names(pose)}
        missing = set(sample['bones'])-available
        if missing:
            raise RuntimeError('导入骨骼丢失: '+', '.join(sorted(missing)))
        worst = 0.0
        for name,matrix in sample['bones'].items():
            p = u.AnimPoseExtensions.get_bone_pose(pose,u.Name(name),u.AnimPoseSpaces.WORLD).translation
            expected = (-matrix[1][3]*100,-matrix[0][3]*100,matrix[2][3]*100)
            worst = max(worst,math.dist((p.x,p.y,p.z),expected))
        morph_error = max((abs(u.AnimPoseExtensions.get_curve_weight(pose,u.Name(name))-value)
                          for name,value in sample['morphs'].items() if name.startswith('m_')),default=0)
        checks.append({'frame':sample['frame'],'max_bone_error_cm':worst,'max_morph_error':morph_error})
        # UE animation compression introduces small errors; 1 mm / 1% thresholds.
        if worst > .1 or morph_error > .01:
            raise RuntimeError(f"第 {sample['frame']} 帧动画校验未通过: 骨骼 {worst:.5f} cm / 表情 {morph_error:.5f}")
    return checks


def validate_camera(binding,samples):
    tracks=[t for t in binding.get_tracks() if isinstance(t,u.MovieScene3DTransformTrack)]
    if len(tracks)!=1 or not tracks[0].get_sections():
        raise RuntimeError('镜头缺少唯一运动轨道')
    channels={str(c.channel_name):c for c in tracks[0].get_sections()[0].get_all_channels()}
    values={}
    for name,ch in channels.items():
        # FBX centimetre conversion can place a 100x scale on camera actors;
        # scale is irrelevant to projection and must not resize child components.
        if name.startswith('Scale.'):
            for key in ch.get_keys():
                ch.remove_key(key)
            ch.set_default(1.0)
        values[name]={k.get_time().frame_number.value:k.get_value() for k in ch.get_keys()}
    checks=[]
    for sample in (samples[0],samples[len(samples)//2],samples[-1]):
        frame=sample['frame']
        def value(name):
            if frame in values[name]:
                return values[name][frame]
            if not values[name] and channels[name].has_default():
                return channels[name].get_default()
            raise RuntimeError(f'镜头 {name} 缺少第 {frame} 帧，可能存在时间偏移')
        actual=tuple(value('Location.'+a) for a in 'XYZ')
        m=sample['matrix']
        position_error=math.dist(actual,(-m[1][3]*100,-m[0][3]*100,m[2][3]*100))
        rotation=u.Rotator(pitch=value('Rotation.Y'),yaw=value('Rotation.Z'),roll=value('Rotation.X'))
        forward=u.MathLibrary.get_forward_vector(rotation)
        up=u.MathLibrary.get_up_vector(rotation)
        expected_forward=(m[1][2],m[0][2],-m[2][2])
        expected_up=(-m[1][1],-m[0][1],m[2][1])
        axis_error=max(math.dist((forward.x,forward.y,forward.z),expected_forward),math.dist((up.x,up.y,up.z),expected_up))
        checks.append({'frame':frame,'position_error_cm':position_error,'axis_error':axis_error})
        if position_error>.1 or axis_error>.001:
            raise RuntimeError(f'镜头第 {frame} 帧坐标校验失败: {position_error:.4f} cm / {axis_error:.5f}')
    return checks


def import_job(folder):
    job = Job(folder)
    data = job.data
    cfg = data["settings"]
    engine_version = u.SystemLibrary.get_engine_version()
    upstream_route = cfg.get("character_backend", "fbx_legacy") == "mmd2unreal"
    if not engine_version.startswith("5.8.") and (upstream_route or not engine_version.startswith("5.7.")):
        raise RuntimeError("新流程要求 UE 5.8.x；历史 FBX 任务兼容 UE 5.7.x")
    manifest_path = job.folder / "manifest.json"
    manifest = read_json(manifest_path)
    checkpoint, resumed = load_ue_checkpoint(job, data, manifest_path)
    check_stop(job)
    if manifest.get("schema") not in (1, 2) or manifest["job_id"] != data["id"]:
        raise RuntimeError("清单版本或任务 ID 不匹配")
    expected_project = Path(cfg["project"]).resolve() if cfg["project"] else None
    actual_project = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
    if expected_project and str(expected_project).casefold()!=str(actual_project).casefold():
        raise RuntimeError("当前 UE 项目与任务目标不同，已阻止导入")
    upstream = None
    camera_sequence = None
    if upstream_route and any(cfg.get(k) for k in ("model", "motion", "lipsync", "camera")):
        from .ue_mmd2unreal import import_sources, prepare_camera, UpstreamBlocked, ImportCancelled
        try:
            upstream = import_sources(job)
            camera_sequence = prepare_camera(upstream, job)
        except (UpstreamBlocked, ImportCancelled) as exc:
            state = "upstream_blocked" if isinstance(exc, UpstreamBlocked) else "cancelled"
            atomic_json(job.folder / "ue_report.json", {"complete": False, "state": state, "error": str(exc)})
            job.status(state, str(exc))
            return
        except Cancelled as exc:
            job.status("stopped", str(exc))
            return
    for f in manifest["files"].values():
        if file_hash(f["path"]) != f["sha256"]:
            raise RuntimeError("FBX 在导出后改变，必须重新关联并验证")
    root = cfg["content_root"] + "/A_" + data["asset_id"]
    owner = data["asset_id"]
    report = {"job_id":data["id"],"engine_version":engine_version,"project":str(actual_project),"root":root,"assets":{},
              "warnings":list(manifest.get("warnings",[])),"failures":[],"skipped_items":[],
              "checks":{},"complete":False,
              "resume":{"used_checkpoint":resumed,"checkpoint":str(job.folder / "ue_checkpoint.json")}}
    # A GPU/UE crash can save an imported object just before its owner tag is
    # written. Reclaim only names derived from this exact manifest; arbitrary
    # user assets in the managed root still block the import.
    recoverable = recoverable_ue_asset_packages(root, manifest, checkpoint)
    recovered = []
    for asset_path in u.EditorAssetLibrary.list_assets(root,recursive=True,include_folder=False):
        asset = u.load_asset(asset_path)
        current_owner = u.EditorAssetLibrary.get_metadata_tag(asset,"MMDBridge.Owner")
        if current_owner == owner:
            continue
        package_path = str(asset_path).split(".", 1)[0]
        if current_owner or package_path not in recoverable:
            raise RuntimeError(f"目标目录包含非本工具资产: {asset_path}")
        u.EditorAssetLibrary.set_metadata_tag(asset,"MMDBridge.Owner",owner)
        u.EditorAssetLibrary.save_loaded_asset(asset)
        recovered.append(package_path)
    if recovered:
        report["warnings"].append(f"从中断导入中恢复 {len(recovered)} 个未完成标记的资源")
    cvar = "Interchange.FeatureFlags.Import.FBX"
    previous = u.SystemLibrary.get_console_variable_int_value(cvar)
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    u.SystemLibrary.execute_console_command(world,cvar+" 0")
    try:
        # Import in an empty managed map so the user's current scene does not keep
        # unrelated textures and render assets resident in GPU memory.
        level_editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
        scratch_path = root + "/Maps/MMD_ImportScratch"
        job.status("ue_prepare", "切换到轻量导入关卡并释放当前场景渲染资源")
        if u.EditorAssetLibrary.does_asset_exist(scratch_path):
            if not level_editor.load_level(scratch_path):
                raise RuntimeError("无法加载轻量导入关卡")
        elif not level_editor.new_level(scratch_path):
            raise RuntimeError("无法创建轻量导入关卡")
        world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        u.EditorAssetLibrary.set_metadata_tag(world,"MMDBridge.Owner",owner)
        level_editor.save_current_level()
        collect_import_garbage()

        job.status("ue_import", "恢复并验证骨架、Morph Targets 和动作" if resumed else "导入骨架、Morph Targets 和动作")
        ensure_import_vram(job, cfg, "角色与动画导入")
        mesh = animation = None
        if upstream:
            from .ue_mmd2unreal import primary
            models = primary(upstream, "model")
            if len(models) > 1:
                raise RuntimeError("PMX 生成多个骨骼网格，无法唯一确定主角色")
            mesh = models[0] if models else None
            motions = primary(upstream, "motion")
            animation = next((a for a in motions if not a.get_name().endswith("_Expression")), None)
            if mesh:
                skeleton = mesh.get_editor_property("skeleton")
                if not skeleton or (animation and animation.get_editor_property("skeleton") != skeleton):
                    raise RuntimeError("上游角色与动作骨架不匹配，请在官方导入窗口选择对应角色")
                report["assets"].update(mesh=mesh.get_path_name(), skeleton=skeleton.get_path_name())
                if animation:
                    report["assets"]["animation"] = animation.get_path_name()
            report["upstream"] = upstream
            report["checks"]["character_materials"] = "preserved_from_mmd2unreal"
        def import_character_assets():
            assets = import_fbx(manifest["files"]["model"]["path"],root+"/Character","SK_Character","model")
            imported_mesh = next((a for a in assets if isinstance(a,u.SkeletalMesh)),None)
            if imported_mesh is None:
                raise RuntimeError("模型 FBX 没有生成骨骼网格体")
            skeleton = imported_mesh.get_editor_property("skeleton")
            if skeleton is None:
                raise RuntimeError("骨骼网格缺少 Skeleton。请为此任务选择新的内容目录，避免复用中断前不完整的资产。")
            for a in (imported_mesh,skeleton):
                u.EditorAssetLibrary.set_metadata_tag(a,"MMDBridge.Owner",owner)
            # The import task can report only the mesh. Persist its separately-created
            # skeleton before any later stage, otherwise a failed first run leaves a
            # mesh/animation referencing an unsaved package after editor restart.
            u.EditorAssetLibrary.save_loaded_asset(skeleton)
            u.EditorAssetLibrary.save_loaded_asset(imported_mesh)
            assets = import_fbx(manifest["files"]["animation"]["path"],root+"/Character","AN_Motion","animation",skeleton)
            imported_animation = next((a for a in assets if isinstance(a,u.AnimSequence)),None)
            if imported_animation is None:
                raise RuntimeError("动画 FBX 未生成动画序列")
            morphs = [m.get_name() for m in imported_mesh.get_editor_property("morph_targets")]
            expected = [m["exported"] for m in manifest["morphs"]]
            missing = [name for name in expected if not any(m==name or m.endswith("_"+name) for m in morphs)]
            if missing:
                raise RuntimeError("Morph Targets 丢失: " + ", ".join(missing))
            curves = [str(n) for n in u.AnimationLibrary.get_animation_curve_names(imported_animation,u.RawCurveTrackTypes.RCT_FLOAT)]
            if expected and not curves:
                raise RuntimeError("Morph 存在，但动画没有表情曲线")
            if imported_animation.get_editor_property("skeleton") != skeleton:
                raise RuntimeError("动画与模型未使用相同的 Skeleton，已阻止错误结果通过验收")
            report["checks"].update(morph_count=len(morphs),morph_names=morphs,curve_names=curves,
                skeleton_matches=imported_animation.get_editor_property("skeleton")==skeleton,
                animation_seconds=imported_animation.get_play_length())
            report['checks']['animation_samples'] = validate_animation(imported_mesh,imported_animation,manifest)
            report["assets"].update(mesh=imported_mesh.get_path_name(),skeleton=skeleton.get_path_name(),animation=imported_animation.get_path_name())
            return imported_mesh, imported_animation
        if "model" in manifest["files"]:
            try:
                mesh, animation = import_character_assets()
            except Exception as exc:
                record_import_failure(job, report, "character", "MMD Character",
                                      manifest["files"]["model"]["path"], exc)
                mesh = animation = None
        save_ue_checkpoint(job, checkpoint, "character")
        mats = Materials(root,owner,report,cfg)
        material_records = manifest.get("materials",[]) + manifest.get("scene",{}).get("materials",[])
        completed_materials = set(checkpoint["completed"].get("materials", []))
        for index,record in enumerate(material_records):
            check_stop(job)
            if record["id"] in completed_materials and mats.reuse(record):
                job.status("ue_materials",f"恢复材质 {index+1}/{len(material_records)}: {record['name']}")
                continue
            ensure_import_vram(job, cfg, f"材质 {index+1}/{len(material_records)}: {record['name']}")
            job.status("ue_materials",f"创建材质 {index+1}/{len(material_records)}: {record['name']}")
            try:
                mats.build(record,job.folder/"cache")
                save_ue_checkpoint(job, checkpoint, "materials", record["id"])
                completed_materials.add(record["id"])
            except Exception as exc:
                record_import_failure(job, report, "material", record["name"],
                                      record.get("source_name", ""), exc)
            finally:
                mats.release_batch()
            if (index + 1) % UE_MATERIAL_GC_BATCH == 0:
                maybe_collect_import_garbage(cfg)
        mats.release_batch()
        collect_import_garbage()
        if mesh and not upstream:
            try:
                mats.assign(mesh,manifest["materials"])
            except Exception as exc:
                record_import_failure(job, report, "character_material_assignment",
                                      mesh.get_name(), manifest["files"].get("model", {}).get("path", ""), exc)
        # Create a dedicated world asset; never replace or save the user's open level.
        level_path = root + "/Maps/MMD_Preview"
        # A new generated map is intentionally loaded. GUI warns to save user edits before launch.
        if u.EditorAssetLibrary.does_asset_exist(level_path):
            if not level_editor.load_level(level_path):
                raise RuntimeError("无法加载生成关卡")
        elif not level_editor.new_level(level_path):
            raise RuntimeError("无法创建生成关卡")
        world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        u.EditorAssetLibrary.set_metadata_tag(world,"MMDBridge.Owner",owner)
        level_editor.save_current_level()
        actor_sub = u.get_editor_subsystem(u.EditorActorSubsystem)
        for actor in actor_sub.get_all_level_actors():
            if "MMDBridge" in [str(t) for t in actor.tags]:
                actor_sub.destroy_actor(actor)
        job.status("ue_sequence","创建 Level Sequence")
        sequence = create_asset(root+"/Sequences/LS_MMD",u.LevelSequence,u.LevelSequenceFactoryNew(),owner)
        for binding in list(sequence.get_bindings()):
            binding.remove()
        for track in list(sequence.get_tracks()):
            sequence.remove_track(track)
        start,end = manifest["frame_range"]
        sequence.set_display_rate(u.FrameRate(manifest["fps"],1))
        sequence.set_tick_resolution_directly(u.FrameRate(24000,1))
        sequence.set_playback_start(start)
        sequence.set_playback_end(end+1)
        def tag(actor,label):
            actor.tags = [u.Name("MMDBridge")]
            actor.set_actor_label(label)
            return actor
        if mesh:
            actor = tag(actor_sub.spawn_actor_from_class(u.SkeletalMeshActor,u.Vector()),"MMD Character")
            component = actor.skeletal_mesh_component
            component.set_skeletal_mesh_asset(mesh)
            binding = sequence.add_possessable(actor)
            if animation:
                track = binding.add_track(u.MovieSceneSkeletalAnimationTrack)
                section = track.add_section()
                section.set_range(start + cfg.get("motion_offset", 0), end+1)
                params = section.get_editor_property("params")
                params.animation = animation
                prop(section,"params",params)
            if upstream:
                expressions = primary(upstream, "lipsync") + [a for a in primary(upstream, "motion") if a != animation]
                for expression in expressions:
                    # Do not silently let a non-additive expression animation
                    # replace body motion; retain the asset and report it.
                    additive = str(expression.get_editor_property("additive_anim_type"))
                    if expression.get_editor_property("skeleton") != mesh.get_editor_property("skeleton") or "NONE" in additive.upper():
                        record_import_failure(job, report, "expression", expression.get_name(), "",
                                              RuntimeError("独立表情不是同骨架叠加动画，已保留上游资产，需在 UE 中配置混合"))
                        continue
                    track = binding.add_track(u.MovieSceneSkeletalAnimationTrack)
                    section = track.add_section()
                    section.set_range(start + cfg.get("lipsync_offset", 0), end+1)
                    params = section.get_editor_property("params")
                    params.animation = expression
                    prop(section, "params", params)
        if camera_sequence:
            track = sequence.add_track(u.MovieSceneSubTrack)
            section = track.add_section()
            section.set_sequence(camera_sequence)
            camera_rate = camera_sequence.get_display_rate()
            source_start = camera_sequence.get_playback_start() * manifest["fps"] * camera_rate.denominator / camera_rate.numerator
            source_end = camera_sequence.get_playback_end() * manifest["fps"] * camera_rate.denominator / camera_rate.numerator
            offset = cfg.get("camera_offset", 0)
            section.set_range(round(source_start) + offset, round(source_end) + offset)
            report["assets"]["camera_sequence"] = camera_sequence.get_path_name()
        water_actors = []
        if "scene" in manifest:
            scene = manifest["scene"]
            report["warnings"] += scene["warnings"]
            if scene.get("route") == "usd_stage":
                job.status("ue_scene", "部署 USD 分层包并刷新 UsdStageActor")
                try:
                    spawn_stage(scene, cfg, root, owner, actor_sub, tag, report)
                except Exception as exc:
                    record_import_failure(job, report, "usd_stage", "MMD USD Stage",
                                          scene.get("usd", {}).get("root_layer", ""), exc)
                # Lights and cameras remain ordinary UE actors.  Geometry is live
                # on the Stage and must never fall through to legacy FBX import.
                scene["meshes"] = []
            meshes = {}
            completed_scene_meshes = set(checkpoint["completed"].get("scene_meshes", []))
            for index,desc in enumerate(scene["meshes"]):
                check_stop(job)
                if not scene_mesh_record_is_importable(desc):
                    record_import_skip(
                        job, report, "scene_mesh", desc["name"], desc["file"]["path"],
                        f"无可导入几何体（顶点 {desc.get('vertices', 0)}）；作为层级占位节点保留")
                    save_ue_checkpoint(job, checkpoint, "scene_meshes", desc["id"])
                    completed_scene_meshes.add(desc["id"])
                    continue
                ensure_import_vram(job, cfg, f"场景网格 {index+1}/{len(scene['meshes'])}: {desc['name']}")
                verb = "恢复" if desc["id"] in completed_scene_meshes else "导入"
                job.status("ue_scene",f"{verb}场景网格 {index+1}/{len(scene['meshes'])}")
                try:
                    assets = import_fbx(desc["file"]["path"],root+"/Scene",desc["name"],"static")
                    static = next((a for a in assets if isinstance(a,u.StaticMesh)), None)
                    if static is None:
                        raise RuntimeError("FBX 未生成 Static Mesh")
                    meshes[desc["id"]] = static.get_path_name()
                    try:
                        mats.assign(static,desc["materials"])
                    except Exception as exc:
                        record_import_failure(job, report, "scene_material_assignment",
                                              desc["name"], desc["file"]["path"], exc)
                    save_ue_checkpoint(job, checkpoint, "scene_meshes", desc["id"])
                    completed_scene_meshes.add(desc["id"])
                except Exception as exc:
                    record_import_failure(job, report, "scene_mesh", desc["name"],
                                          desc["file"]["path"], exc)
                if (index + 1) % UE_MATERIAL_GC_BATCH == 0:
                    maybe_collect_import_garbage(cfg)
            def spawn_scene_actor(desc):
                if desc.get("mesh") in meshes:
                    actor = actor_sub.spawn_actor_from_class(u.StaticMeshActor,u.Vector())
                    actor.static_mesh_component.set_static_mesh(u.load_asset(meshes[desc["mesh"]]))
                    for i,mat_id in enumerate(desc["materials"]):
                        if mat_id in mats.water:
                            water_actors.append((actor,i))
                elif desc["type"] == "LIGHT":
                    light = desc["light"]
                    cls = {"POINT":u.PointLight,"SUN":u.DirectionalLight,"SPOT":u.SpotLight,"AREA":u.RectLight}[light["type"]]
                    actor = actor_sub.spawn_actor_from_class(cls,u.Vector())
                    component = actor.get_component_by_class(u.LightComponent)
                    component.set_light_color(u.LinearColor(*light["color"],1))
                    efficacy=683*sum(a*b for a,b in zip(light['color'],(.2126,.7152,.0722)))
                    if light['type']!='SUN':
                        component.set_editor_property('intensity_units',u.LightUnits.LUMENS)
                    component.set_intensity(light["energy"]*efficacy)
                    if light['type']=='SPOT':
                        component.set_editor_property('outer_cone_angle',math.degrees(light['spot_size']/2))
                    if light['type']=='AREA':
                        component.set_editor_property('source_width',light['size']*100*scene['meters_per_unit'])
                        component.set_editor_property('source_height',light.get('size_y',light['size'])*100*scene['meters_per_unit'])
                elif desc["type"] == "CAMERA":
                    actor = actor_sub.spawn_actor_from_class(u.CineCameraActor,u.Vector())
                    actor.get_cine_camera_component().set_editor_property('current_focal_length',desc["camera"]["lens"])
                else:
                    actor = actor_sub.spawn_actor_from_class(u.TargetPoint,u.Vector())
                tag(actor,desc["name"])
                actor.set_actor_transform(scene_transform(desc["matrix"],scene["meters_per_unit"],desc['type'] in ('CAMERA','LIGHT')),False,False)
                actor.set_actor_hidden_in_game(not desc["visible"])
                actor.set_is_temporarily_hidden_in_editor(not desc['visible'])
                return actor
            actors = {}
            for desc in scene["objects"]:
                try:
                    actors[desc["id"]] = spawn_scene_actor(desc)
                except Exception as exc:
                    record_import_failure(job, report, "scene_actor", desc["name"],
                                          scene.get("source", ""), exc)
            for desc in scene["objects"]:
                if desc["id"] not in actors or desc.get("parent") not in actors:
                    continue
                try:
                    if actors[desc["parent"]].get_editor_property('root_component'):
                        actors[desc["id"]].attach_to_actor(actors[desc["parent"]],u.Name("None"),
                            u.AttachmentRule.KEEP_WORLD,u.AttachmentRule.KEEP_WORLD,u.AttachmentRule.KEEP_WORLD,False)
                except Exception as exc:
                    record_import_failure(job, report, "scene_parenting", desc["name"],
                                          scene.get("source", ""), exc)
            if scene.get("world",{}).get("sky"):
                try:
                    tag(actor_sub.spawn_actor_from_class(u.SkyAtmosphere,u.Vector()),"MMD Sky")
                    sun = tag(actor_sub.spawn_actor_from_class(u.DirectionalLight,u.Vector()),"MMD Sun")
                    sun.set_actor_rotation(u.Rotator(-math.degrees(scene["world"]["sun_elevation"]),math.degrees(scene["world"]["sun_rotation"]),0),False)
                    sky = tag(actor_sub.spawn_actor_from_class(u.SkyLight,u.Vector()),"MMD Ambient")
                    sky.light_component.set_intensity(scene["world"]["strength"])
                except Exception as exc:
                    record_import_failure(job, report, "scene_world", "MMD Sky",
                                          scene.get("source", ""), exc)
            report['checks']['scene']={'objects':len(actors),'meshes':len(meshes),
                                       'usd_stage':scene.get('route') == 'usd_stage',
                                       'materials':len(scene['materials'])}
            report['warnings'].append('灯光采用线性 RGB 的光视效率近似转换为流明/照度；跨渲染器曝光仍需调整')
        else:
            tag(actor_sub.spawn_actor_from_class(u.DirectionalLight,u.Vector(0,0,300)),"Preview Light")
            tag(actor_sub.spawn_actor_from_class(u.SkyLight,u.Vector()),"Preview Ambient")
        for actor,index in water_actors:
            try:
                actor_binding = sequence.add_possessable(actor)
                binding = sequence.add_possessable(actor.static_mesh_component)
                binding.set_parent(actor_binding)
                track = binding.add_track(u.MovieSceneComponentMaterialTrack)
                track.set_material_index(index)
                section = track.add_section()
                section.set_range(start,end+1)
                tick = sequence.get_tick_resolution().numerator / manifest["fps"]
                parameter=u.MaterialParameterInfo(name='SequenceTime',association=u.MaterialParameterAssociation.GLOBAL_PARAMETER,index=-1)
                section.add_scalar_parameter_key(parameter,frame_number(start*tick),0.0,'','',u.MovieSceneKeyInterpolation.LINEAR)
                section.add_scalar_parameter_key(parameter,frame_number((end+1)*tick),(end+1-start)/manifest["fps"],'','',u.MovieSceneKeyInterpolation.LINEAR)
            except Exception as exc:
                record_import_failure(job, report, "water_track", actor.get_actor_label(), "", exc)
        report['checks']['water_tracks'] = len(water_actors)
        def import_sequence_camera():
            samples = read_json(manifest["camera_samples"])
            mixed_projection=any(s['projection']!='PERSP' for s in samples)
            camera = tag(actor_sub.spawn_actor_from_class(u.CameraActor if mixed_projection else u.CineCameraActor,u.Vector()),"MMD_Camera")
            binding = sequence.add_possessable(camera)
            settings = set_props(u.MovieSceneUserImportFBXSettings(),match_by_name_only=False,create_cameras=False,
                reduce_keys=False,force_front_x_axis=True,convert_scene_unit=True,import_uniform_scale=1.0)
            if not u.SequencerTools.import_level_sequence_fbx(world,sequence,[binding],settings,manifest["files"]["camera"]["path"]):
                raise RuntimeError("镜头 FBX 导入失败")
            report['checks']['camera_transforms'] = validate_camera(binding,samples)
            component = camera.get_component_by_class(u.CameraComponent) if mixed_projection else camera.get_cine_camera_component()
            aspect=cfg['width']/cfg['height']
            vertical=samples[0]['sensor_fit']=='VERTICAL' or (samples[0]['sensor_fit']=='AUTO' and aspect<1)
            # Replace imported lens keys; geometry FBX alone does not preserve filmback fit.
            for b in sequence.get_bindings():
                for t in list(b.get_tracks()):
                    if isinstance(t,u.MovieSceneFloatTrack) and str(t.get_property_name()) in ("CurrentFocalLength",'FieldOfView'):
                        b.remove_track(t)
            if mixed_projection:
                component.set_editor_property('aspect_ratio',aspect)
                component.set_editor_property('constrain_aspect_ratio',True)
                fov=[(s['frame'],math.degrees(2*math.atan((s['sensor_height']*aspect if vertical else s['sensor_width'])/(2*s['lens'])))) for s in samples]
                bind_float(sequence,binding,component,'FieldOfView','FieldOfView',fov)
                cb=bind_float(sequence,binding,component,'OrthoWidth','OrthoWidth',[(s['frame'],s['ortho_scale']*100*(aspect if vertical else 1)) for s in samples])
                track=cb.add_track(u.MovieSceneByteTrack)
                track.set_property_name_and_path('ProjectionMode','ProjectionMode')
                section=track.add_section()
                section.set_range(start,end+1)
                channel=section.get_all_channels()[0]
                for sample in samples:
                    channel.add_key(frame_number(sample['frame']),1 if sample['projection']=='ORTHO' else 0)
            else:
                film = component.get_editor_property("filmback")
                film.sensor_height = samples[0]["sensor_height"] if vertical else samples[0]['sensor_width']/aspect
                film.sensor_width = film.sensor_height * aspect
                component.set_editor_property("filmback",film)
                bind_float(sequence,binding,component,"CurrentFocalLength","CurrentFocalLength",[(s["frame"],s["lens"]) for s in samples])
            cuts = sequence.add_track(u.MovieSceneCameraCutTrack)
            cut = cuts.add_section()
            cut.set_range(start,end+1)
            camera_id = u.MovieSceneObjectBindingID()
            prop(camera_id,"guid",binding.get_id())
            cut.set_camera_binding_id(camera_id)
            report["checks"]["camera_samples"] = len(samples)
        if "camera" in manifest["files"]:
            try:
                import_sequence_camera()
            except Exception as exc:
                record_import_failure(job, report, "camera", "MMD_Camera",
                                      manifest["files"]["camera"]["path"], exc)
        try:
            sequence_actor = tag(actor_sub.spawn_actor_from_class(u.LevelSequenceActor,u.Vector()),"MMD Sequence")
            sequence_actor.set_sequence(sequence)
        except Exception as exc:
            record_import_failure(job, report, "sequence_actor", "MMD Sequence", "", exc)
        report["assets"]["sequence"] = sequence.get_path_name()
        report["assets"]["materials"] = mats.validate()
        report["assets"]["level"] = level_path
        # Mark every asset generated by these importers, including skeletons and mesh build data.
        for asset_path in u.EditorAssetLibrary.list_assets(root,recursive=True,include_folder=False):
            asset = u.load_asset(asset_path)
            if asset:
                try:
                    u.EditorAssetLibrary.set_metadata_tag(asset,"MMDBridge.Owner",owner)
                    u.EditorAssetLibrary.save_loaded_asset(asset)
                except Exception as exc:
                    record_import_failure(job, report, "asset_save", asset_path, "", exc)
        level_editor.save_current_level()
        unsupported=[r['name'] for r in material_records if r.get('surface')=='unsupported']
        visual_review = [r['name'] for r in material_records if r.get('requires_visual_review')]
        report['checks']['material_visual_review'] = visual_review
        report['checks']['unsupported_materials']=unsupported
        failures = report.get("failures", [])
        skipped_items = report.get("skipped_items", [])
        report['checks']['failed_items'] = len(failures)
        report['checks']['skipped_items'] = len(skipped_items)
        report["complete"] = not unsupported and not failures and not skipped_items and not visual_review
        save_ue_checkpoint(job, checkpoint, "assembly")
        atomic_json(job.folder/"ue_report.json",report)
        issue_lines = []
        if failures:
            issue_lines.append(f"已跳过 {len(failures)} 个失败项：")
            issue_lines.extend(
                f"- [{item['stage']}] {item['name']}: {item['error']}" for item in failures)
        if skipped_items:
            issue_lines.append(f"已主动跳过 {len(skipped_items)} 个不可导入项：")
            issue_lines.extend(
                f"- [{item['stage']}] {item['name']}: {item['reason']}" for item in skipped_items)
        if unsupported:
            issue_lines.append("未还原材质：" + ", ".join(unsupported))
        if visual_review:
            issue_lines.append("需近景/远景及接缝视觉验收：" + ", ".join(visual_review))
        if issue_lines:
            message = "UE 已尽可能完成导入，但结果需要人工复核。\n" + "\n".join(issue_lines)
        else:
            message = "UE 导入完成；请查看报告中的材质近似与未匹配轨道"
        job.status('needs_review' if issue_lines else "complete", message,
                   report=str(job.folder/"ue_report.json"), failures=len(failures))
        try:
            u.EditorAssetLibrary.sync_browser_to_objects([report["assets"].get("mesh",sequence.get_path_name())])
        except Exception as exc:
            u.log_warning(f"MMDBridge 无法在内容浏览器定位结果: {exc}")
    except DeferredVRAM as exc:
        report["deferred"] = str(exc)
        atomic_json(job.folder/"ue_report.json",report)
    except Cancelled as exc:
        report["cancelled"] = True
        atomic_json(job.folder/"ue_report.json",report)
        job.status("stopped",str(exc))
    except Exception:
        report["error"] = traceback.format_exc()
        atomic_json(job.folder/"ue_report.json",report)
        job.status("failed",report["error"])
        raise
    finally:
        u.SystemLibrary.execute_console_command(None,cvar+" "+str(previous))


TICK = None
LAST = None
CAMERA_LAST = None
LAST_TIME = 0
BUSY = False


def start_bridge(mailbox, project):
    global TICK,LAST,CAMERA_LAST
    box = Path(mailbox)
    box.mkdir(parents=True,exist_ok=True)
    if TICK is not None:
        u.unregister_slate_post_tick_callback(TICK)
    receipt=box/'response.json'
    saved_receipt = read_json(receipt) if receipt.exists() else {}
    terminal_states = {"complete", "needs_review", "failed", "stopped", "cancelled", "upstream_blocked"}
    # A crash/restart must replay a request whose receipt only captured an
    # intermediate state such as awaiting_ue or ue_wait_vram.
    LAST = saved_receipt.get('id') if saved_receipt.get('state') in terminal_states else None
    camera_receipt = box/'camera_cut_response.json'
    CAMERA_LAST=read_json(camera_receipt).get('id') if camera_receipt.exists() else None
    def tick(delta):
        global LAST,CAMERA_LAST,LAST_TIME,BUSY
        if BUSY:
            return
        if time.monotonic()-LAST_TIME < 1:
            return
        LAST_TIME = time.monotonic()
        try:
            if u.AssetRegistryHelpers.get_asset_registry().is_loading_assets():
                return
            actual = u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())
            atomic_json(box/"heartbeat.json",{"project":actual,"pid":os.getpid(),"time":time.time(),"engine":u.SystemLibrary.get_engine_version()})
            camera_request = box/"camera_cut_request.json"
            if camera_request.exists():
                command = read_json(camera_request)
                if command["id"] != CAMERA_LAST:
                    CAMERA_LAST = command["id"]
                    BUSY = True
                    try:
                        run_camera_cut_request(command)
                    finally:
                        BUSY = False
                    camera_status_path = Path(command["session"]) / "status.json"
                    camera_status = (
                        read_json(camera_status_path)
                        if camera_status_path.exists()
                        else {"state": "camera_cut_failed"}
                    )
                    atomic_json(box/"camera_cut_response.json", {
                        "id": CAMERA_LAST,
                        "state": camera_status.get("state", "camera_cut_failed"),
                        "time": time.time(),
                    })
                    return
            request = box/"request.json"
            if request.exists():
                command = read_json(request)
                if command["id"] != LAST:
                    LAST = command["id"]
                    # Do not change levels while unsaved user content is present.
                    dirty = u.EditorLoadingAndSavingUtils.get_dirty_map_packages()
                    if dirty:
                        Job(command["job"]).status("ue_wait_save","请先保存 UE 当前关卡，再点击重试连接")
                        atomic_json(receipt,{'id':LAST,'state':'ue_wait_save'})
                        return
                    BUSY = True
                    try:
                        try:
                            import_job(command["job"])
                        except Exception:
                            # Validation before import_job's inner transaction must
                            # also reach the GUI instead of leaving awaiting_ue.
                            error = traceback.format_exc()
                            Job(command["job"]).status("failed", error)
                            u.log_error(error)
                    finally:
                        BUSY = False
                        status_path=Job(command['job']).folder/'status.json'
                        state=read_json(status_path).get('state') if status_path.exists() else 'failed'
                        atomic_json(receipt,{'id':LAST,'state':state,'time':time.time()})
        except Exception:
            u.log_error(traceback.format_exc())
    TICK = u.register_slate_post_tick_callback(tick)
