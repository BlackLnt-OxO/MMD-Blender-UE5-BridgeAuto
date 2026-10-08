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
                   recoverable_ue_asset_packages, recoverable_ue_generated_mesh,
                   scene_mesh_record_is_importable)
from . import process
from .ue_materials import Materials, prop
from .ue_usd import spawn_stage, configure_stage_collision


UE_CHECKPOINT_SCHEMA = 1
UE_MATERIAL_GC_BATCH = 4


def save_generated_level(level_editor, world, root, owner):
    """Save managed USD edits without an unattended editor opening a modal."""
    if (not world.get_path_name().startswith(root + '/Maps/') or
            u.EditorAssetLibrary.get_metadata_tag(world, 'MMDBridge.Owner') != owner):
        raise RuntimeError('拒绝自动保存非本任务关卡')
    settings_class = getattr(u, 'UsdProjectSettings', None)
    behavior = getattr(u, 'UsdSaveDialogBehavior', None)
    if settings_class is None or behavior is None:
        return level_editor.save_current_level()
    project = Path(u.Paths.convert_relative_path_to_full(u.Paths.project_dir())).resolve()
    managed = project/'Content'/root.removeprefix('/Game/')/'Source/Scene'
    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors():
        if not isinstance(actor, u.UsdStageActor):
            continue
        layer = Path(actor.get_editor_property('root_layer').file_path)
        if not layer.is_absolute():layer = project/layer
        if ('MMDBridge' not in [str(tag) for tag in actor.tags] or
                not layer.resolve().is_relative_to(managed.resolve())):
            return level_editor.save_current_level()  # Keep the user's dialog policy.
    settings = u.get_default_object(settings_class)
    name = 'show_save_layers_dialog_when_saving'
    previous = settings.get_editor_property(name)
    try:
        settings.set_editor_property(name, behavior.ALWAYS_SAVE)
        return level_editor.save_current_level()
    finally:
        settings.set_editor_property(name, previous)  # Never persist this override.


class GpuPause(Exception):
    pass


def check_gpu_budget(job, cfg, report):
    info = process.render_memory_info()
    peak=report.setdefault('metrics',{}).setdefault('memory_peak',{})
    for key,value in (info or {}).items():
        if key.endswith('_used_mb') and isinstance(value,(float,int)):
            peak[key]=max(peak.get(key,0),value)
    percent = process.gpu_budget_percent(info)
    pause_percent = int(cfg.get("ue_vram_pause_percent", 90))
    # Reserve one large texture import of headroom: UE can allocate over 1 GiB at once.
    if percent is None or percent < pause_percent - 5:
        return
    collect_import_garbage()
    info = process.render_memory_info()
    percent = process.gpu_budget_percent(info)
    if percent is None or percent < pause_percent - 5:
        return
    report["checks"]["gpu_pause"] = {"budget_percent": percent, "gpu": info}
    atomic_json(job.folder / "ue_report.json", report)
    job.status("ue_wait_vram", f"专用 / 共享显存或 DXGI 预算最高占用 {percent:.1f}%，已暂停导入并保留检查点",
               retry_after=time.time() + 10, pause_percent=pause_percent,
               resume_percent=int(cfg.get("ue_vram_resume_percent", 80)))
    raise GpuPause()


def scene_light_intensity(light, cfg):
    if light.get('version') == 1:
        from .scene_appearance import physical_light
        return physical_light(light,cfg.get('scene_light_scale',1))['intensity']
    luminance = sum(a * b for a, b in zip(light["color"], (.2126, .7152, .0722)))
    return max(0.0, light["energy"] * 683 * cfg.get("scene_light_scale", .15) * luminance)


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
    import gc
    gc.collect()  # Python wrappers may retain UWorld through reference cycles.
    collect = getattr(u.SystemLibrary, "collect_garbage", None)
    if callable(collect):
        collect()
    else:
        u.SystemLibrary.execute_console_command(None, "obj gc")


def material_compile_errors(log_path, start, asset_id):
    lines = import_log_lines(log_path, start)
    marker = ("A_" + asset_id).casefold()
    return [line for line in lines if "Failed to compile Material" in line and marker in line.casefold()]


def active_import_log(project):
    """Honor the native -abslog parameter, including paths with spaces."""
    parse=getattr(u.SystemLibrary,'parse_command_line',None)
    if callable(parse):
        _,_,parameters=parse(u.SystemLibrary.get_command_line())
        path=next((str(value) for key,value in parameters.items() if str(key).casefold()=='abslog'),None)
        if path:return Path(u.Paths.convert_relative_path_to_full(path)).resolve()
    return project.parent/'Saved/Logs'/(project.stem+'.log')


def import_log_lines(log_path, start):
    if not log_path.is_file():
        return []
    with log_path.open("rb") as stream:
        stream.seek(min(start, log_path.stat().st_size))
        return stream.read().decode("utf-8", errors="replace").splitlines()


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


def bind_usd_camera(sequence, samples, actor_sub, tag, cfg, report):
    """Make a persistent camera cut from the samples authored into this USD."""
    if not samples:
        raise RuntimeError("USD 镜头没有逐帧样本")
    camera = tag(actor_sub.spawn_actor_from_class(u.CineCameraActor, u.Vector()), "MMD USD Camera")
    binding = sequence.add_possessable(camera)
    track = binding.add_track(u.MovieScene3DTransformTrack)
    section = track.add_section()
    section.set_range(samples[0]["frame"], samples[-1]["frame"] + 1)
    channels = {str(ch.channel_name): ch for ch in section.get_all_channels()}
    needed = {"Location.X", "Location.Y", "Location.Z", "Rotation.X", "Rotation.Y", "Rotation.Z"}
    if not needed.issubset(channels):
        raise RuntimeError("UE 镜头 Transform 轨道缺少通道: " + ", ".join(sorted(needed - channels.keys())))
    component = camera.get_cine_camera_component()
    film = component.get_editor_property("filmback")
    film.sensor_width = float(samples[0]["sensor_width"])
    film.sensor_height = float(samples[0]["sensor_height"])
    component.set_editor_property("filmback", film)
    component.set_editor_property("current_focal_length", float(samples[0]["lens"]))
    first = scene_transform(samples[0]["matrix"], 1.0, True)
    camera.set_actor_transform(first, False, False)
    for sample in samples:
        transform = scene_transform(sample["matrix"], 1.0, True)
        position = transform.translation
        rotation = transform.rotation.rotator()
        values = {"Location.X": position.x, "Location.Y": position.y, "Location.Z": position.z,
                  "Rotation.X": rotation.roll, "Rotation.Y": rotation.pitch, "Rotation.Z": rotation.yaw}
        for name, value in values.items():
            channels[name].add_key(frame_number(sample["frame"]), float(value),
                                   interpolation=u.MovieSceneKeyInterpolation.LINEAR)
    for name in ("Scale.X", "Scale.Y", "Scale.Z"):
        if name in channels:
            channels[name].set_default(1.0)
    lens_binding = bind_float(sequence, binding, component, "CurrentFocalLength", "CurrentFocalLength",
                              [(sample["frame"], sample["lens"]) for sample in samples])
    cuts = sequence.add_track(u.MovieSceneCameraCutTrack)
    cut = cuts.add_section()
    cut.set_range(samples[0]["frame"], samples[-1]["frame"] + 1)
    camera_id = u.MovieSceneObjectBindingID()
    prop(camera_id, "guid", binding.get_id())
    cut.set_camera_binding_id(camera_id)
    report["checks"]["usd_camera_transforms"] = validate_camera(binding, samples)
    report["checks"]["usd_camera_samples"] = len(samples)
    report["assets"]["usd_camera"] = camera.get_path_name()
    return camera, lens_binding


def import_job(folder):
    import_started=time.perf_counter()
    job = Job(folder)
    data = job.data
    cfg = data["settings"]
    engine_version = u.SystemLibrary.get_engine_version()
    upstream_route = (cfg.get("character_backend", "fbx_legacy") == "mmd2unreal" and
                      cfg.get("flow") not in ("scene", "camera"))
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
    u.EditorAssetLibrary.make_directory(cfg["content_root"])
    root = cfg["content_root"] + "/A_" + data["asset_id"]
    owner = data["asset_id"]
    report = {"job_id":data["id"],"engine_version":engine_version,"project":str(actual_project),"root":root,"assets":{},
              "warnings":list(manifest.get("warnings",[])),"failures":[],"skipped_items":[],
              "checks":{},"complete":False,
              "resume":{"used_checkpoint":resumed,"checkpoint":str(job.folder / "ue_checkpoint.json")}}
    ue_log = active_import_log(actual_project)
    report['checks']['import_log']=str(ue_log)
    ue_log_start = ue_log.stat().st_size if ue_log.is_file() else 0
    # A GPU/UE crash can save an imported object just before its owner tag is
    # written. Reclaim only names derived from this exact manifest; arbitrary
    # user assets in the managed root still block the import.
    recoverable = recoverable_ue_asset_packages(root, manifest, checkpoint)
    recovered = []
    for asset_path in u.EditorAssetLibrary.list_assets(root,recursive=True,include_folder=False):
        asset = u.load_asset(asset_path)
        if asset and asset.get_class().get_name() == 'DirectoryPlaceholder':
            continue  # Engine's DirectoryPlaceholder plugin owns these folder markers.
        current_owner = u.EditorAssetLibrary.get_metadata_tag(asset,"MMDBridge.Owner")
        if current_owner == owner:
            continue
        package_path = str(asset_path).split(".", 1)[0]
        usd_display_material = (package_path == root + "/Scene/Generated/MI_DisplayColor" and
                                isinstance(asset, u.MaterialInstanceConstant))
        if current_owner or (package_path not in recoverable and not
                             recoverable_ue_generated_mesh(package_path, root, isinstance(asset, u.StaticMesh)) and
                             not usd_display_material):
            raise RuntimeError(f"目标目录包含非本工具资产: {asset_path}")
        u.EditorAssetLibrary.set_metadata_tag(asset,"MMDBridge.Owner",owner)
        u.EditorAssetLibrary.save_loaded_asset(asset)
        recovered.append(package_path)
    asset = None  # Ownership scan can end on a UWorld; do not pin it across LoadLevel.
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
        world = None  # Release Python's strong UWorld reference before native map GC.
        collect_import_garbage()
        if u.EditorAssetLibrary.does_asset_exist(scratch_path):
            if not level_editor.load_level(scratch_path):
                raise RuntimeError("无法加载轻量导入关卡")
        elif not level_editor.new_level(scratch_path):
            raise RuntimeError("无法创建轻量导入关卡")
        world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        u.EditorAssetLibrary.set_metadata_tag(world,"MMDBridge.Owner",owner)
        save_generated_level(level_editor, world, root, owner)
        collect_import_garbage()

        job.status("ue_import", "恢复并验证骨架、Morph Targets 和动作" if resumed else "导入骨架、Morph Targets 和动作")
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
        concurrency=max(1,min(4,int(cfg.get('scene_build_concurrency',1))))
        values={name:concurrency for name in ('Editor.AsyncAssetCompilationMaxConcurrency',
                  'Editor.AsyncTextureCompilationMaxConcurrency','Editor.AsyncStaticMeshCompilationMaxConcurrency')}
        for name,value in values.items():u.SystemLibrary.execute_console_command(None,f'{name} {value}')
        report.setdefault('checks',{})['build_concurrency']={
            name:u.SystemLibrary.get_console_variable_int_value(name) for name in values}
        mats = Materials(root,owner,report,cfg,
                         lambda: check_gpu_budget(job, cfg, report))
        from . import ue_appearance
        ue_appearance.volume_materials(manifest.get('scene',{}),mats,cfg)
        material_records = manifest.get("materials",[]) + manifest.get("scene",{}).get("materials",[])
        completed_materials = set(checkpoint["completed"].get("materials", []))
        materials_started=time.perf_counter()
        for index,record in enumerate(material_records):
            check_stop(job)
            check_gpu_budget(job, cfg, report)
            from .material_contract import finalize_restoration
            finalize_restoration(record)
            if record['conversion_status'] in ('placeholder','failed'):
                if not any(f.get('stage') == 'source_material' and f.get('material_id') == record['id'] for f in report['failures']):
                    record_import_failure(job, report, 'source_material', record['name'],
                        ', '.join(record.get('source_objects', [])), RuntimeError(record.get('material_error') or
                            record.get('basic_pbr_bake', {}).get('error') or '源材质转换失败'))
                    report['failures'][-1]['material_id'] = record['id']
            if record["id"] in completed_materials and mats.reuse(record):
                job.status("ue_materials",f"恢复材质 {index+1}/{len(material_records)}: {record['name']}")
                if (index + 1) % UE_MATERIAL_GC_BATCH == 0:
                    collect_import_garbage()
                continue
            job.status("ue_materials",f"创建材质 {index+1}/{len(material_records)}: {record['name']}")
            try:
                mats.build(record,job.folder/"cache")
                save_ue_checkpoint(job, checkpoint, "materials", record["id"])
                completed_materials.add(record["id"])
            except GpuPause:
                saved=mats.checkpoint_interrupted_build()
                report.setdefault('checks',{}).setdefault('interrupted_material_builds',[]).extend(saved)
                raise
            except Exception as exc:
                record['conversion_status'] = 'failed'
                record['requires_visual_review'] = True
                from .material_contract import add_issue
                add_issue(record, 'ue_material_failed', str(exc), severity='error')
                record_import_failure(job, report, "material", record["name"],
                                      record.get("source_name", ""), exc)
                report['failures'][-1]['material_id'] = record['id']
                try:
                    fallback = {**record, "surface": "pbr", "water": False,
                                "pmx_index": None, "master_material_candidate": False,
                                "special_masks": {}, "channels": {"base_color": {
                                    "op": "constant", "value": [0.5, 0.5, 0.5, 1.0]}}}
                    fallback.pop('surface_graph',None)
                    mats.build(fallback, job.folder / "cache")
                    report["warnings"].append(f"UE 材质 {record['name']} 已使用灰色占位，需人工复核")
                except Exception as fallback_exc:
                    record_import_failure(job, report, "material_fallback", record["name"],
                                          record.get("source_name", ""), fallback_exc)
            finally:
                mats.release_batch()
            if (index + 1) % UE_MATERIAL_GC_BATCH == 0:
                collect_import_garbage()
        mats.release_batch()
        collect_import_garbage()
        report.setdefault('metrics',{})['materials_seconds']=time.perf_counter()-materials_started
        # Stage post-processing needs the material paths before opening USD.
        report['assets']['materials'] = mats.validate()
        if mesh and not upstream:
            try:
                mats.assign(mesh,manifest["materials"])
            except Exception as exc:
                record_import_failure(job, report, "character_material_assignment",
                                      mesh.get_name(), manifest["files"].get("model", {}).get("path", ""), exc)
        # Create a dedicated world asset; never replace or save the user's open level.
        level_path = root + "/Maps/MMD_Preview"
        world = None
        collect_import_garbage()
        # A new generated map is intentionally loaded. GUI warns to save user edits before launch.
        if u.EditorAssetLibrary.does_asset_exist(level_path):
            if not level_editor.load_level(level_path):
                raise RuntimeError("无法加载生成关卡")
        elif not level_editor.new_level(level_path):
            raise RuntimeError("无法创建生成关卡")
        world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        u.EditorAssetLibrary.set_metadata_tag(world,"MMDBridge.Owner",owner)
        save_generated_level(level_editor, world, root, owner)
        actor_sub = u.get_editor_subsystem(u.EditorActorSubsystem)
        for actor in actor_sub.get_all_level_actors():
            if "MMDBridge" in [str(t) for t in actor.tags]:
                actor_sub.destroy_actor(actor)
        job.status("ue_sequence","创建 Level Sequence")
        u.EditorAssetLibrary.make_directory(root + "/Sequences")
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
            report["skipped_items"] += scene.get("skipped_items", [])
            if scene.get("route") == "usd_stage":
                check_gpu_budget(job, cfg, report)
                report["checks"]["usd_resources_before_stage"] = {
                    "physical": process.physical_memory_info(), "gpu": process.gpu_memory_info()}
                job.status("ue_scene", "部署 USD 分层包并刷新 UsdStageActor")
                try:
                    def before_foliage():
                        check_stop(job)
                        check_gpu_budget(job, cfg, report)
                    stage_actor, stage_result, generated_by_path = spawn_stage(
                        scene, cfg, root, owner, actor_sub, tag, report, before_foliage)
                    report["checks"]["usd_resources_after_stage"] = {
                        "physical": process.physical_memory_info(), "gpu": process.gpu_memory_info()}
                    atomic_json(job.folder / "ue_report.json", report)
                    camera_record = scene.get("camera")
                    if camera_record:
                        camera_file = Path(camera_record["source"])
                        if (not camera_file.resolve().is_relative_to(job.folder.resolve()) or
                                file_hash(camera_file) != camera_record["sha256"]):
                            raise RuntimeError("USD 镜头采样不是本任务的已验证输入")
                        stage_sequence = stage_actor.get_level_sequence()
                        if not stage_sequence or not (stage_sequence.get_tracks() or
                                any(binding.get_tracks() for binding in stage_sequence.get_bindings())):
                            raise RuntimeError("USD Stage 关联序列没有动画轨道")
                        report["checks"]["usd_stage_sequence_tracks"] = (
                            len(stage_sequence.get_tracks()) +
                            sum(len(binding.get_tracks()) for binding in stage_sequence.get_bindings()))
                        bind_usd_camera(sequence, read_json(camera_file), actor_sub, tag, cfg, report)
                    if cfg.get("exact_collision", True):
                        def before_collision(index, total, mesh):
                            check_stop(job)
                            check_gpu_budget(job, cfg, report)
                            if index % UE_MATERIAL_GC_BATCH == 0:
                                collect_import_garbage()
                            job.status("ue_collision", f"验证 USD 网格碰撞 {index+1}/{total}: {mesh.get_name()}")
                        configure_stage_collision(stage_actor, scene, generated_by_path,
                                                  stage_result, before_collision)
                        report["checks"]["usd_resources_after_collision"] = {
                            "physical": process.physical_memory_info(), "gpu": process.gpu_memory_info()}
                        atomic_json(job.folder / "ue_report.json", report)
                except GpuPause:
                    raise
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
                    collect_import_garbage()
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
                    if light.get('version') == 1:
                        ue_appearance.configure_light(component,light,cfg,mats)
                    else:
                        component.set_light_color(u.LinearColor(*light["color"],1))
                        if light['type']!='SUN':
                            component.set_editor_property('intensity_units',u.LightUnits.LUMENS)
                        component.set_intensity(scene_light_intensity(light, cfg))
                        if light['type']=='SPOT':
                            component.set_editor_property('outer_cone_angle',math.degrees(light['spot_size']/2))
                        if light['type']=='AREA':
                            component.set_editor_property('source_width',light['size']*100*scene['meters_per_unit'])
                            component.set_editor_property('source_height',light.get('size_y',light['size'])*100*scene['meters_per_unit'])
                elif desc["type"] == "CAMERA":
                    actor = actor_sub.spawn_actor_from_class(u.CineCameraActor,u.Vector())
                    actor.get_cine_camera_component().set_editor_property('current_focal_length',desc["camera"]["lens"])
                    if scene.get('reference_camera'):
                        ue_appearance.configure_camera(actor.get_cine_camera_component(),desc['camera'],scene)
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
            if scene.get('render_look'):
                try:
                    ue_appearance.setup(scene,cfg,root,owner,actor_sub,tag,mats,report,sequence,actors)
                except Exception as exc:
                    record_import_failure(job,report,'scene_appearance','Source appearance',scene.get('source',''),exc)
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
            scene_lights = [desc['light'] for desc in scene['objects'] if desc['type'] == 'LIGHT']
            native_lights=[];missing_lights=[]
            for desc in scene['objects']:
                if desc['type']!='LIGHT':continue
                actor=actors.get(desc['id'])
                component=actor.get_component_by_class(u.LightComponent) if actor else None
                if component is None:missing_lights.append(desc['name']);continue
                native_lights.append(dict(name=desc['name'],actor=actor.get_path_name(),
                    hidden=bool(actor.get_editor_property('hidden')),snapshot=ue_appearance.light_snapshot(component,desc['light'],cfg)
                    if desc['light'].get('version')==1 else None))
            report['checks']['scene_lights_native']=dict(expected=len(scene_lights),
                actual=len(native_lights),missing=missing_lights,lights=native_lights)
            report['checks']['scene']={'objects':len(actors),'meshes':len(meshes),
                                       'usd_stage':scene.get('route') == 'usd_stage',
                                       'materials':len(scene['materials']),
                                       'lights':len(scene_lights),
                                       'light_scale':cfg.get('scene_light_scale', .15),
                                       'max_light_intensity':max((scene_light_intensity(light, cfg)
                                                                 for light in scene_lights), default=0)}
            report['warnings'].append(f"灯光按场景亮度倍率 {cfg.get('scene_light_scale', .15):g} 转换；跨渲染器曝光仍需视觉验收")
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
        if cfg.get("flow") == "camera" and manifest.get("camera_samples"):
            try:
                camera_file = Path(manifest["camera_samples"]).resolve()
                if not camera_file.is_relative_to(job.folder.resolve()):
                    raise RuntimeError("镜头采样文件不属于当前任务")
                bind_usd_camera(sequence, read_json(camera_file), actor_sub, tag, cfg, report)
            except Exception as exc:
                record_import_failure(job, report, "camera", "MMD_Camera",
                                      manifest["camera_samples"], exc)
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
        if not u.EditorAssetLibrary.save_loaded_asset(sequence):
            raise RuntimeError("正式 Level Sequence 未写入磁盘")
        report["assets"]["materials"] = mats.validate()
        report["assets"]["level"] = level_path
        from .ue_shader_data import asset_dependencies
        report['assets']['source_packages']=asset_dependencies(list(report['assets']['materials'].values())+[sequence.get_path_name()])
        # Each importer marks and saves its own assets; reloading the whole
        # directory here exhausted shared GPU memory during material saves.
        save_generated_level(level_editor, world, root, owner)
        if 'scene' in manifest and manifest['scene'].get('route') == 'usd_stage' and report.get('assets', {}).get('usd_stage'):
            from .ue_usd import save_stage_assets
            report['checks']['usd_saved_assets'] = save_stage_assets(stage_actor, manifest['scene'], root, owner)
        compile_errors = material_compile_errors(ue_log, ue_log_start, owner)
        report["checks"]["material_compile_errors"] = compile_errors
        for line in compile_errors:
            report["failures"].append({"stage": "material_compile", "name": "UE 材质编译",
                                       "source": str(ue_log), "error": line})
        log_errors = [line for line in import_log_lines(ue_log, ue_log_start)
                      if ": Error:" in line]
        report["checks"]["ue_log_errors"] = log_errors
        unsupported=[r['name'] for r in material_records if r.get('surface')=='unsupported']
        report['runtime_code_hashes']={name:file_hash(Path(__file__).parent/name) for name in (
            'ue_host.py','ue_reference.py','ue_materials.py','ue_appearance.py','ue_usd.py',
            'ue_foliage.py','ue_render_policy.py','material_graph.py','material_contract.py','ocio_cache.py','ue_shader_data.py','material_policy.py','material_plan.py','material_workflow.py','material_results.py','material_repair.py','ue_material_repair.py','process.py','core.py')}
        report['material_source_records']=[{k:r[k] for k in ('id','name','source_name','source_material_id',
            'source_aliases','source_objects','pmx_index','strategy','strategy_requested','basic_pbr_bake',
            'conversion_status','visual_status','issues','fallback_attempts','transmission_candidate','closure_features',
            'automatic_route','restoration_plan','material_usage_count','opaque_transmission_fallback','material_workflow','field_sampling') if k in r} for r in material_records]
        report['checks']['material_restoration'] = {status:sum(r.get('conversion_status') == status for r in material_records)
            for status in ('converted','approximated','placeholder','failed')}
        visual_review = [r['name'] for r in material_records if r.get('requires_visual_review')]
        foliage = report['checks'].get('foliage_distance', {})
        coverage_review = set(foliage.get('complex_masks_need_review', []))
        visual_review += [r['name'] + '（远距遮罩覆盖率）' for r in material_records
                          if r['id'] in coverage_review]
        visual_review += foliage.get('shared_texture_cutoffs_need_review', [])
        missing_external = manifest.get("scene", {}).get("missing_external_files", [])
        report['checks']['material_visual_review'] = visual_review
        report['checks']['unsupported_materials']=unsupported
        report['checks']['missing_external_files'] = missing_external
        failures = report.get("failures", [])
        skipped_items = report.get("skipped_items", [])
        appearance_review=report['checks'].get('appearance_visual_review',[])
        report['checks']['failed_items'] = len(failures)
        report['checks']['skipped_items'] = len(skipped_items)
        report["complete"] = not unsupported and not failures and not skipped_items and not visual_review and not appearance_review and not log_errors and not missing_external
        if not unsupported and not failures and not skipped_items and not appearance_review and not log_errors and not missing_external and manifest.get('scene', {}).get('usd'):
            from .bake_cache import cleanup_scene_bakes
            try:
                report['checks']['bake_cache_cleanup'] = cleanup_scene_bakes(manifest['scene'])
            except (OSError, ValueError) as exc:
                report['warnings'].append('场景缓存未清理，已保留文件：' + str(exc))
        save_ue_checkpoint(job, checkpoint, "assembly")
        atomic_json(job.folder/"ue_report.json",report)
        from .material_workflow import white_ledger
        white_ledger(job,manifest,report)
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
        if appearance_review:
            issue_lines.append('已导入但需外观复核：')
            issue_lines.extend(f"- [{item['stage']}] {item['name']}: {item['reason']}" for item in appearance_review)
        if log_errors:
            issue_lines.append(f"UE 日志有 {len(log_errors)} 条 Error；请先在 UE 检查再决定是否继续验收。")
        if missing_external:
            issue_lines.append("Blender 场景缺少外部文件：" + ", ".join(missing_external))
        for warning in dict.fromkeys(report["warnings"]):
            job.status("ue_warning", warning)
        if issue_lines:
            message = "UE 已尽可能完成导入，但结果需要人工复核。\n" + "\n".join(issue_lines)
        else:
            message = "UE 导入完成；请查看报告中的材质近似与未匹配轨道"
        job.status('needs_review' if issue_lines else "complete", message,
                   report=str(job.folder/"ue_report.json"), failures=len(failures))
        if not u.SystemLibrary.is_unattended():
            try:
                u.EditorAssetLibrary.sync_browser_to_objects([report["assets"].get("mesh",sequence.get_path_name())])
            except Exception as exc:
                u.log_warning(f"MMDBridge 无法在内容浏览器定位结果: {exc}")
    except GpuPause:
        # The map dirtied by this import is a genuine checkpoint, not an
        # unrelated user's map. Save it before the next bridge request's guard.
        paused_world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        if (paused_world and paused_world.get_path_name().split('.')[0].startswith(root+'/Maps/') and
                u.EditorAssetLibrary.get_metadata_tag(paused_world,'MMDBridge.Owner')==owner):
            save_generated_level(level_editor,paused_world,root,owner)
            paused_world=None
            world=None
            if u.EditorAssetLibrary.does_asset_exist(root+'/Maps/MMD_ImportScratch'):
                level_editor.load_level(root+'/Maps/MMD_ImportScratch')
            collect_import_garbage()
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
        report.setdefault('metrics',{})['elapsed_seconds']=time.perf_counter()-import_started
        atomic_json(job.folder/'ue_report.json',report)
        u.SystemLibrary.execute_console_command(None,cvar+" "+str(previous))


TICK = None
LAST = None
CAMERA_LAST = None
LAST_TIME = 0
LABEL_TIME = 0
BUSY = False


def restore_scene_actor_labels():
    if not hasattr(u, "UsdStageActor"):
        return
    editor = u.get_editor_subsystem(u.UnrealEditorSubsystem)
    if editor is None or editor.get_editor_world() is None:
        return
    project_dir = Path(u.Paths.convert_relative_path_to_full(u.Paths.project_dir()))
    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors():
        if not isinstance(actor, u.UsdStageActor):
            continue
        parts = actor.get_path_name().split("/")
        asset_index = next((index for index, part in enumerate(parts)
                            if part.startswith("A_")), None)
        if asset_index is None:
            continue
        asset_id = parts[asset_index]
        package = (project_dir / "Content" / Path(*parts[parts.index("Game") + 1:asset_index + 1]) /
                   "Source" / "Scene" / "package.json") if "Game" in parts else Path()
        if not package.is_file():
            package = project_dir / "MMDBridgeData" / asset_id / "Scene" / "package.json"
        if not package.is_file():
            continue
        labels = read_json(package).get("actor_labels", {})
        probe = next(((path, label) for path, label in labels.items()
                      if path.rsplit("/", 1)[-1] != label), None)
        if probe is None:
            continue
        component = actor.get_generated_component(probe[0])
        if component is None or component.get_owner() is None:
            continue
        if component.get_owner().get_actor_label() == probe[1]:
            continue
        generated = {path: actor.get_generated_component(path) for path in labels}
        if any(component is None or component.get_owner() is None for component in generated.values()):
            continue
        for path, label in labels.items():
            generated[path].get_owner().set_actor_label(label, False)


def start_bridge(mailbox, project):
    global TICK,LAST,CAMERA_LAST
    # The Python plugin also starts in -game MRQ processes. Editor commands and
    # label restoration must not run there or consume the editor's mailbox.
    if hasattr(u, 'UnrealEditorSubsystem') and u.get_editor_subsystem(u.UnrealEditorSubsystem) is None:
        return
    box = Path(mailbox)
    box.mkdir(parents=True,exist_ok=True)
    if TICK is not None:
        u.unregister_slate_post_tick_callback(TICK)
    receipt=box/'response.json'
    saved_receipt = read_json(receipt) if receipt.exists() else {}
    terminal_states = {"complete", "needs_review", "failed", "stopped", "cancelled", "upstream_blocked", "ue_wait_vram", "ue_paused_gpu_crash", "ue_paused_resource_limit"}
    # A crash/restart must replay a request whose receipt only captured an
    # intermediate state such as awaiting_ue or ue_wait_vram.
    LAST = saved_receipt.get('id') if saved_receipt.get('state') in terminal_states else None
    camera_receipt = box/'camera_cut_response.json'
    CAMERA_LAST=read_json(camera_receipt).get('id') if camera_receipt.exists() else None
    module_dir = Path(__file__).parent
    code_hashes = {name: file_hash(module_dir / name) for name in ('ue_host.py','ue_reference.py','ue_materials.py','ue_appearance.py','ue_usd.py',
        'ue_foliage.py','ue_render_policy.py','material_graph.py','material_contract.py','ocio_cache.py','ue_shader_data.py','material_policy.py','material_plan.py','material_workflow.py','material_results.py','material_repair.py','ue_material_repair.py','process.py','core.py')}
    def tick(delta):
        global LAST,CAMERA_LAST,LAST_TIME,LABEL_TIME,BUSY
        if BUSY:
            return
        if hasattr(u, 'UnrealEditorSubsystem') and u.get_editor_subsystem(u.UnrealEditorSubsystem) is None:
            return
        if time.monotonic()-LAST_TIME < 1:
            return
        LAST_TIME = time.monotonic()
        try:
            if u.AssetRegistryHelpers.get_asset_registry().is_loading_assets():
                return
            actual = u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())
            atomic_json(box/"heartbeat.json",{"project":actual,"pid":os.getpid(),"time":time.time(),"engine":u.SystemLibrary.get_engine_version(),
                                            'code_hashes':code_hashes})
            if time.monotonic() - LABEL_TIME >= 5:
                LABEL_TIME = time.monotonic()
                restore_scene_actor_labels()
                if hasattr(u,'UsdStageActor'):
                    from .ue_appearance import restore_viewport_look
                    restore_viewport_look(u.get_editor_subsystem(u.EditorActorSubsystem))
            camera_request = box/"camera_cut_request.json"
            render_request = box / 'render_request.json'
            if render_request.exists():
                command = read_json(render_request)
                render_receipt = box / 'render_response.json'
                received = read_json(render_receipt) if render_receipt.exists() else {}
                if command['id'] != received.get('id'):
                    BUSY = True
                    atomic_json(render_receipt, {'id': command['id'], 'state': 'ue_render'})
                    def rendered():
                        global BUSY
                        BUSY = False
                        status = read_json(Job(command['job']).folder / 'status.json')
                        atomic_json(render_receipt, {'id': command['id'], 'state': status.get('state'), 'time': time.time()})
                    try:
                        from .ue_reference import start
                        if (Job(command['job']).folder / 'stop').exists():
                            raise ValueError('任务已停止；请从 GUI 重新发起渲染')
                        start(command['job'], rendered, pipeline=command.get('pipeline'))
                    except Exception:
                        Job(command['job']).status('failed', traceback.format_exc())
                        rendered()
                    return
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
                    paused = read_json(Path(command["job"]) / "status.json").get("state")
                    if paused in {"ue_paused_gpu_crash", "ue_paused_resource_limit", "stopped"} or (Path(command['job']) / 'stop').exists():
                        paused = 'stopped' if (Path(command['job']) / 'stop').exists() else paused
                        atomic_json(receipt, {"id": LAST, "state": paused, "time": time.time()})
                        return
                    # Do not change levels while unsaved user content is present.
                    dirty = u.EditorLoadingAndSavingUtils.get_dirty_map_packages()
                    if dirty:
                        Job(command["job"]).status("ue_wait_save","请先保存 UE 当前关卡，再点击重试连接")
                        atomic_json(receipt,{'id':LAST,'state':'ue_wait_save'})
                        return
                    BUSY = True
                    try:
                        try:
                            if command.get('action') == 'material_repair':
                                from .ue_material_repair import run
                                run(command['job'],command['batch'],undo=command.get('repair_action')=='undo')
                            else:
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
