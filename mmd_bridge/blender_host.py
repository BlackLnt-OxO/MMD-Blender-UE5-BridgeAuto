"""Blender worker. Run via scripts/blender_entry.py, not desktop Python."""
import math
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

from .core import Job, SCHEMA, MMD_VERSION, atomic_json, stable_id, desktop_path, validate_fbx, check_stop, Cancelled, file_hash, read_json
from .blender_materials import Materials
from .effective_inputs import read_effective_inputs, resolve_character_inputs

FBX_DEFAULTS = dict(use_selection=True, global_scale=1.0, apply_unit_scale=True,
    apply_scale_options="FBX_SCALE_UNITS", axis_forward="-Y", axis_up="Z",
    use_space_transform=True, bake_space_transform=False, add_leaf_bones=False,
    use_armature_deform_only=False, use_mesh_modifiers=False, mesh_smooth_type="FACE",
    bake_anim_use_all_bones=True, bake_anim_use_nla_strips=False,
    bake_anim_use_all_actions=False, bake_anim_force_startend_keying=True,
    bake_anim_step=1.0, bake_anim_simplify_factor=0.0, path_mode="RELATIVE")


def select(objects):
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def all_actions():
    return list(bpy.data.actions)


def shift_actions(actions, scale=1.0, offset=0):
    for action in actions:
        for curve in action.fcurves:
            for key in curve.keyframe_points:
                for point in (key.co, key.handle_left, key.handle_right):
                    point.x = point.x * scale + offset
            curve.update()


def enable_mmd():
    import mmd_tools
    actual=getattr(mmd_tools,'MMD_TOOLS_VERSION','.'.join(map(str,getattr(mmd_tools,'bl_info',{}).get('version',()))))
    if actual!=MMD_VERSION:
        raise RuntimeError(f'当前会话已载入 mmd_tools {actual}，此适配器需要 {MMD_VERSION}；请由桌面工具启动独立 Blender 会话')
    if not hasattr(bpy.types.Object, "mmd_type"):
        import addon_utils
        addon_utils.enable('mmd_tools',default_set=True,persistent=False)
    preferences=bpy.context.preferences.addons.get('mmd_tools')
    if preferences:
        preferences.preferences.shared_toon_folder=str(Path(mmd_tools.__file__).parent/'externals/MikuMikuDance')
    return mmd_tools


def export_options(kind):
    return {**FBX_DEFAULTS, "object_types": {"CAMERA"} if kind == "camera" else {"MESH", "ARMATURE"},
            "bake_anim": kind != "model"}


class Character:
    def __init__(self, job):
        self.job = job
        self.data = job.data
        # 方案 §4 / §37：下游不再自己判断"用原 PMX 还是 ProcessedPMX"，
        # 也不再用源 VMD —— 统一读 <job>/effective.json。
        # 这里用副本覆盖，避免回写 job.json；没有 effective.json 时行为与以前完全一致。
        self.cfg = dict(self.data["settings"])
        effective_model, effective_motion, effective_note = resolve_character_inputs(
            self.cfg, job.folder)
        self.cfg["model"] = effective_model
        self.cfg["motion"] = effective_motion
        effective = read_effective_inputs(job.folder)
        if "effective_lipsync" in effective:
            self.cfg["lipsync"] = effective["effective_lipsync"]
        self.mode1_fbx = self.cfg.get("character_backend") == "mmdbridge_fbx"
        if self.mode1_fbx and not effective:
            raise RuntimeError("MMDBridge 任务缺少有效输入回执，不能直接导入原始 VMD")
        if self.mode1_fbx and not Path(self.cfg["model"]).resolve().is_relative_to((job.folder / "input").resolve()):
            raise RuntimeError("MMDBridge 输入模型不是本任务修复后的 PMX")
        if self.mode1_fbx and effective.get("expected_bone_count", 0):
            proof_path = job.folder / "mmd_full" / "attempt.json"
            proof = read_json(proof_path) if proof_path.is_file() else {}
            vmd = proof.get("vmd", {})
            baked = Path(self.cfg["motion"]).resolve()
            if (not proof.get("validated") or not baked.is_relative_to((job.folder / "mmd_full" / "vmd").resolve())
                    or Path(vmd.get("path", "")).resolve() != baked
                    or vmd.get("sha256") != file_hash(baked)):
                raise RuntimeError("MMDBridge 全段烘焙回执与 Blender 输入 VMD 不一致")
        elif self.mode1_fbx:
            if (effective.get("mmm_skipped_reason") != "zero_physics_bones"
                    or not Path(self.cfg["motion"]).resolve().is_relative_to((job.folder / "input").resolve())):
                raise RuntimeError("MMDBridge 任务缺少物理烘焙或零物理骨回执")
        if self.mode1_fbx:
            print(f"MMDBridge 原始模型: {effective['source_model']}\n"
                  f"MMDBridge 原始动作: {effective['source_vmd']}\n"
                  f"MMDBridge 输入模型: {self.cfg['model']}\n"
                  f"MMDBridge 输入动作: {self.cfg['motion']}", flush=True)
        self.effective_note = effective_note
        self.out = Path(self.cfg["character_output_dir"]) if self.cfg.get("character_output_dir") else Path(self.cfg["output_override"]) if self.cfg["output_override"] else (
            Path(bpy.data.filepath).parent if bpy.data.filepath else desktop_path())
        self.out.mkdir(parents=True, exist_ok=True)
        self.prefix = "MMD_" + self.data["asset_id"][:10] + "_" + self.data["id"]
        self.bundle = self.out / (self.prefix + "_Data")
        self.bundle.mkdir(exist_ok=True)
        self.materials = Materials(self.bundle, self.cfg["model"])
        self.manifest = {"schema": SCHEMA, "job_id": self.data["id"], "asset_id": self.data["asset_id"],
            "blender_version": bpy.app.version_string, "mmd_tools_version": MMD_VERSION,
            "units": {"blender_meters": 1.0, "ue_centimeters": 100.0, "fbx_forward": "-Y", "fbx_up": "Z"},
            "fps": self.cfg["fps"], "warnings": [], "files": {}, "bones": [], "morphs": [], "materials": []}
        self.index = 0
        self.kinds = ["model", "animation"] + (["camera"] if self.cfg["camera"] and not self.mode1_fbx else [])
        self.armature = None
        self.meshes = []
        self.camera = None
        self.manual = False
        self.finished = False
        self.last_command = None

    def prepare(self):
        checkpoint = self.job.folder / 'character_checkpoint.json'
        if checkpoint.is_file():
            saved = read_json(checkpoint)
            library = self.job.folder / 'Character.blend'
            if file_hash(library) != saved['sha256']:
                raise ValueError('角色还原点损坏，请新建任务')
            enable_mmd()
            bpy.ops.wm.open_mainfile(filepath=str(library), load_ui=False, use_scripts=False)
            self.armature = bpy.data.objects[saved['armature']]
            self.meshes = [bpy.data.objects[name] for name in saved['meshes']]
            self.camera = bpy.data.objects.get(saved.get('camera', ''))
            self.manifest = read_json(self.job.folder / 'manifest.json')
            self.index = 0
            self.job.status('preparing', '恢复角色采样还原点；已完成的 FBX 将复用')
            return
        if bpy.app.version[:2] != (4, 3):
            raise RuntimeError("当前 Blender 执行适配器仅验收 4.3.x；所选版本可发现但尚未通过兼容验收")
        self.job.status("preparing", "正在导入 PMX、动作和口型")
        check_stop(self.job)
        existing_actions=set(all_actions())
        enable_mmd()
        scene = bpy.context.scene
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
        scene.render.fps, scene.render.fps_base = 30, 1.0
        scene.frame_set(0)
        before = set(bpy.data.objects)
        result = bpy.ops.mmd_tools.import_model(filepath=self.cfg["model"], scale=self.cfg["scale"],
            types={"MESH", "ARMATURE", "MORPHS", "DISPLAY"}, rename_bones=False,
            clean_model=False, remove_doubles=False, save_log=False, log_level="WARNING")
        if "FINISHED" not in result:
            raise RuntimeError("PMX 导入未完成")
        created = set(bpy.data.objects) - before
        root = next(o for o in created if getattr(o, "mmd_type", "") == "ROOT")
        self.armature = next(o for o in created if o.type == "ARMATURE")
        self.meshes = [o for o in created if o.type == "MESH" and o.find_armature() == self.armature]
        select([root])
        bpy.ops.mmd_tools.import_vmd(filepath=self.cfg["motion"], scale=self.cfg["scale"], margin=0,
            bone_mapper="PMX", create_new_action=True, use_nla=False, update_scene_settings=False,
            save_log=False, log_level="WARNING")
        shift_actions(set(all_actions())-existing_actions, 1.0, self.cfg["motion_offset"] * 30 / self.cfg["fps"])
        if self.cfg["lipsync"]:
            # Isolate new lip actions then merge by channel: replace complete matching curves.
            previous = {}
            for obj in self.meshes:
                keys = obj.data.shape_keys
                if keys:
                    previous[keys] = keys.animation_data.action if keys.animation_data else None
                    if keys.animation_data:
                        keys.animation_data.action = None
            select(self.meshes)
            bpy.ops.mmd_tools.import_vmd(filepath=self.cfg["lipsync"], scale=self.cfg["scale"], margin=0,
                create_new_action=False, update_scene_settings=False, save_log=False, log_level="WARNING")
            for keys, old in previous.items():
                new = keys.animation_data.action if keys.animation_data else None
                if new:
                    shift_actions([new], 1.0, self.cfg["lipsync_offset"] * 30 / self.cfg["fps"])
                    if old:
                        paths = {(f.data_path, f.array_index) for f in new.fcurves}
                        for curve in old.fcurves:
                            if (curve.data_path, curve.array_index) not in paths:
                                dest = new.fcurves.new(curve.data_path, index=curve.array_index)
                                dest.keyframe_points.add(len(curve.keyframe_points))
                                for a, b in zip(curve.keyframe_points, dest.keyframe_points):
                                    b.co, b.interpolation = a.co, a.interpolation
                                    b.handle_left, b.handle_right = a.handle_left, a.handle_right
                elif old:
                    keys.animation_data_create().action = old
        if self.cfg["camera"]:
            camera_data = bpy.data.cameras.new("MMD_SourceCamera")
            self.camera = bpy.data.objects.new("MMD_SourceCamera", camera_data)
            scene.collection.objects.link(self.camera)
            select([self.camera])
            before_actions = set(bpy.data.actions)
            bpy.ops.mmd_tools.import_vmd(filepath=self.cfg["camera"], scale=self.cfg["scale"], margin=0,
                update_scene_settings=False, save_log=False, log_level="WARNING")
            shift_actions(set(bpy.data.actions)-before_actions, 1.0, self.cfg["camera_offset"] * 30 / self.cfg["fps"])
        shift_actions(set(all_actions())-existing_actions, self.cfg["fps"] / 30)
        scene.render.fps = self.cfg["fps"]
        scene.render.resolution_x, scene.render.resolution_y = self.cfg["width"], self.cfg["height"]
        maximum = max(v["max_frame"] * self.cfg["fps"] / 30 + self.cfg.get(k+"_offset", 0)
                      for k, v in self.data["vmds"].items())
        scene.frame_start = self.cfg["start"]
        scene.frame_end = self.cfg["end"] or math.ceil(maximum)
        self.manifest["frame_range"] = [scene.frame_start, scene.frame_end]
        self.manifest["resolution"] = [self.cfg["width"], self.cfg["height"]]
        if scene.rigidbody_world:
            scene.rigidbody_world.enabled = False
        source_bones = {b.mmd_bone.name_j for b in self.armature.pose.bones}
        source_morphs = {k.name for o in self.meshes if o.data.shape_keys for k in o.data.shape_keys.key_blocks}
        for role, info in self.data["vmds"].items():
            for kind, available in (("bone", source_bones), ("morph", source_morphs)):
                missing = sorted(set(info["names"].get(kind, [])) - available)
                if missing:
                    self.manifest["warnings"].append(f"{role} 未匹配 {kind}: {', '.join(missing)}")
        for collection in ("bone_morphs", "material_morphs", "uv_morphs", "group_morphs"):
            morphs = getattr(root.mmd_root, collection, [])
            if morphs:
                self.manifest["warnings"].append(f"{collection}: {len(morphs)} 项不作为独立 UE Morph Target 导出")
        # Preserve readable MMD names in metadata; export stable ASCII names through FBX.
        for i, bone in enumerate(list(self.armature.data.bones)):
            original = bone.name
            exported = f"b_{i:04d}_{stable_id(original)[:8]}"
            bone.name = exported
            self.manifest["bones"].append({"original": original, "exported": exported})
        self.armature.name = "MMD_Rig"
        for obj in self.meshes:
            if obj.data.shape_keys:
                for i, key in enumerate(list(obj.data.shape_keys.key_blocks)[1:]):
                    old = key.name
                    key.name = "m_" + stable_id(old)
                    self.manifest["morphs"].append({"original": old, "exported": key.name})
            for i, mat in enumerate(obj.data.materials):
                if mat:
                    self.materials.analyze(mat, pmx_index=i)
        self.manifest["materials"] = list(self.materials.records.values())
        self.manifest["warnings"] += self.materials.warnings
        # Duplicate the hierarchy: constraint evaluation stays on the source rig.
        source_arm = self.armature
        source_meshes = self.meshes
        export_arm = source_arm.copy()
        export_arm.data = source_arm.data.copy()
        export_arm.name = "MMD_ExportRig"
        export_arm.parent = None
        export_arm.matrix_world = source_arm.matrix_world.copy()
        export_arm.animation_data_clear()
        scene.collection.objects.link(export_arm)
        for bone in export_arm.pose.bones:
            for constraint in list(bone.constraints):
                bone.constraints.remove(constraint)
        export_meshes = []
        for source in source_meshes:
            obj = source.copy()
            obj.data = source.data.copy()
            scene.collection.objects.link(obj)
            obj.parent = export_arm
            obj.matrix_world = source.matrix_world.copy()
            obj.animation_data_clear()
            if obj.data.shape_keys:
                obj.data.shape_keys.animation_data_clear()
            for mod in list(obj.modifiers):
                if mod.type == "ARMATURE":
                    mod.object = export_arm
                else:
                    obj.modifiers.remove(mod)
            export_meshes.append(obj)
        # Ensure one skeletal root for UE without changing source bindings.
        roots = [b for b in export_arm.data.bones if b.parent is None]
        select([export_arm])
        bpy.ops.object.mode_set(mode="EDIT")
        for bone in export_arm.data.edit_bones:
            # Evaluated IK may move a connected control bone away from its parent tail.
            # FBX bones do not encode Blender's connected-head constraint.
            bone.use_connect = False
        if len(roots) > 1:
            root_bone = export_arm.data.edit_bones.new("MMD_Root")
            root_bone.head, root_bone.tail = (0, 0, 0), (0, 0, 0.1)
            for b in export_arm.data.edit_bones:
                if b != root_bone and b.parent is None:
                    b.parent = root_bone
        bpy.ops.object.mode_set(mode="OBJECT")
        for bone in export_arm.data.bones:
            # FBX/UE use ordinary parent inheritance. Re-solve all sampled local poses
            # against that convention (MMD IK controls often disable inheritance).
            bone.use_inherit_rotation = True
            bone.inherit_scale = "FULL"
            bone.use_local_location = True
        samples = []
        frame_samples = {scene.frame_start, (scene.frame_start+scene.frame_end)//2, scene.frame_end}
        camera_samples = []
        camera_export = None
        if self.camera:
            camera_export = bpy.data.objects.new("MMD_Camera", self.camera.data.copy())
            camera_export.data.animation_data_clear()
            scene.collection.objects.link(camera_export)
        self.job.status("baking_animation", "采样 IK、骨骼、表情与镜头（不含物理）")
        frames = np.arange(scene.frame_start, scene.frame_end+1, dtype=np.float32)
        targets = [b for b in export_arm.pose.bones if b.name in source_arm.pose.bones]
        poses = np.empty((len(targets), len(frames), 10), dtype=np.float32)
        morph_pairs = [(source, target) for source,target in zip(source_meshes,export_meshes) if source.data.shape_keys]
        morph_values = [np.empty((len(target.data.shape_keys.key_blocks)-1,len(frames)), dtype=np.float32) for source,target in morph_pairs]
        camera_values = np.empty((len(frames),7), dtype=np.float32) if camera_export else None
        previous_euler = None
        for frame_index, frame in enumerate(range(scene.frame_start, scene.frame_end+1)):
            scene.frame_set(frame)
            deps = bpy.context.evaluated_depsgraph_get()
            evaluated = source_arm.evaluated_get(deps)
            for bone_index, target in enumerate(targets):
                src = evaluated.pose.bones[target.name]
                # Solve local basis against the evaluated SOURCE parent, not the partially
                # updated target pose (which compounds errors down long MMD bone chains).
                basis = target.bone.convert_local_to_pose(
                    src.matrix, target.bone.matrix_local,
                    parent_matrix=src.parent.matrix if src.parent else (target.parent.bone.matrix_local if target.parent else Matrix.Identity(4)),
                    parent_matrix_local=target.parent.bone.matrix_local if target.parent else Matrix.Identity(4),
                    invert=True)
                loc,quat,scale = basis.decompose()
                if frame_index and np.dot(poses[bone_index,frame_index-1,3:7],quat) < 0:
                    quat.negate()
                poses[bone_index,frame_index] = (*loc,*quat,*scale)
            for pair_index,(source,target) in enumerate(morph_pairs):
                evaluated_mesh = source.evaluated_get(deps)
                for key_index,key in enumerate(list(target.data.shape_keys.key_blocks)[1:]):
                    morph_values[pair_index][key_index,frame_index] = evaluated_mesh.data.shape_keys.key_blocks[key.name].value
            if camera_export:
                camera = self.camera.evaluated_get(deps)
                transform = camera.matrix_world.copy()
                transform.translation = transform.translation * self.cfg["camera_scale"] + Vector(self.cfg["camera_offset_xyz"])
                euler = transform.to_euler('XYZ', previous_euler) if previous_euler else transform.to_euler('XYZ')
                previous_euler = euler.copy()
                camera_values[frame_index] = (*transform.translation,*euler,camera.data.lens)
                camera_samples.append({"frame": frame, "matrix": [list(r) for r in transform],
                    "lens": camera.data.lens, "sensor_width": camera.data.sensor_width,
                    "sensor_height": camera.data.sensor_height, "sensor_fit": camera.data.sensor_fit,
                    "projection": camera.data.type, "ortho_scale": camera.data.ortho_scale})
            if frame in frame_samples:
                samples.append({"frame": frame, "bones": {b.name: [list(r) for r in b.matrix] for b in evaluated.pose.bones},
                    "morphs": {k.name: k.value for o in source_meshes if o.data.shape_keys for k in o.data.shape_keys.key_blocks}})
            if frame % 100 == 0:
                self.job.status("baking_animation", f"采样动画 {frame}/{scene.frame_end}")
                check_stop(self.job)
        # Bulk curves avoid quadratic key insertion and reevaluating the growing export rig.
        def curve(id_data,path,index,values):
            animation = id_data.animation_data_create()
            if not animation.action:
                animation.action = bpy.data.actions.new(id_data.name + '_Sampled')
            fcurve = animation.action.fcurves.new(path,index=index)
            fcurve.keyframe_points.add(len(frames))
            fcurve.keyframe_points.foreach_set('co',np.column_stack((frames,values)).ravel())
            for key in fcurve.keyframe_points:
                key.interpolation = 'LINEAR'
            fcurve.update()
        for bone_index,target in enumerate(targets):
            target.rotation_mode = 'QUATERNION'
            for prop_name,offset,count in (('location',0,3),('rotation_quaternion',3,4),('scale',7,3)):
                for component in range(count):
                    curve(export_arm,target.path_from_id(prop_name),component,poses[bone_index,:,offset+component])
        for pair_index,(source,target) in enumerate(morph_pairs):
            for key_index,key in enumerate(list(target.data.shape_keys.key_blocks)[1:]):
                curve(target.data.shape_keys,key.path_from_id('value'),0,morph_values[pair_index][key_index])
        if camera_export:
            camera_export.rotation_mode = 'XYZ'
            for component in range(3):
                curve(camera_export,'location',component,camera_values[:,component])
                curve(camera_export,'rotation_euler',component,camera_values[:,3+component])
            curve(camera_export.data,'lens',0,camera_values[:,6])
        self.armature, self.meshes, self.camera = export_arm, export_meshes, camera_export
        atomic_json(self.bundle / "reference_samples.json", samples)
        self.manifest["reference_samples"] = str(self.bundle / "reference_samples.json")
        if camera_samples:
            camera_path = self.job.folder / "camera_samples.json" if self.mode1_fbx else self.bundle / "camera.json"
            atomic_json(camera_path, camera_samples)
            self.manifest["camera_samples"] = str(camera_path)
        for obj in created:
            obj.hide_set(True)
        self.set_kind("model")
        self.save_manifest()
        library = self.job.folder / 'Character.blend'
        temporary = self.job.folder / 'Character.partial.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(temporary), check_existing=False, copy=True)
        temporary.replace(library)
        atomic_json(self.job.folder / 'character_checkpoint.json', {
            'sha256': file_hash(library), 'armature': self.armature.name,
            'meshes': [mesh.name for mesh in self.meshes], 'camera': self.camera.name if self.camera else ''})

    def set_kind(self, kind):
        self.armature.data.pose_position = "REST" if kind == "model" else "POSE"
        # Shape keys must be zero for the reference mesh; preserve the animation action.
        for mesh in self.meshes:
            keys = mesh.data.shape_keys
            if keys and keys.animation_data:
                for curve in keys.animation_data.action.fcurves:
                    curve.mute = kind == "model"
            if keys and kind == "model":
                for key in keys.key_blocks:
                    key.value = 0
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        select([self.camera] if kind == "camera" else [self.armature] + self.meshes)

    def path_for(self, kind):
        return self.out / (self.prefix + "_" + {"model": "Model", "animation": "Animated", "camera": "Camera"}[kind] + ".fbx")

    def record(self, kind, path, options):
        from .blender_validate import inspect_export
        validation = inspect_export(path,kind,self.manifest,self.armature)
        self.manifest["files"][kind] = {**validate_fbx(path), 'validation':validation, "options": {k: sorted(v) if isinstance(v, set) else v for k, v in options.items() if isinstance(v,(str,int,float,bool,set,list,tuple)) or v is None}}
        self.save_manifest()

    def save_manifest(self):
        atomic_json(self.job.folder / "manifest.json", self.manifest)
        atomic_json(self.bundle / "manifest.json", self.manifest)

    def export_all(self):
        for kind in self.kinds[self.index:]:
            check_stop(self.job)
            saved = self.manifest['files'].get(kind, {})
            if saved.get('path') and Path(saved['path']).is_file() and file_hash(saved['path']) == saved.get('sha256'):
                self.job.status('exporting', f'复用已完成 {kind} FBX')
                self.index += 1
                continue
            self.job.status("exporting", f"导出 {kind}")
            self.set_kind(kind)
            opts = export_options(kind)
            bpy.ops.export_scene.fbx(filepath=str(self.path_for(kind)), **opts)
            self.record(kind, self.path_for(kind), opts)
            self.index += 1
        self.complete()

    def complete(self):
        check_stop(self.job)
        self.armature.data.pose_position = "POSE"
        if self.mode1_fbx:
            import subprocess
            script = Path(__file__).resolve().parents[1] / "scripts" / "validate_blender_fbx.py"
            result = subprocess.run([bpy.app.binary_path, "-b", "--python", str(script), "--", str(self.job.folder)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            (self.job.folder / "blender_roundtrip.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError(f"双 FBX 回读失败，退出码 {result.returncode}；详见 blender_roundtrip.log")
            check_stop(self.job)
        self.finished = True
        if self.mode1_fbx:
            self.job.status("character_done", "MMDBridge 角色双 FBX 导出完成，等待 UE 导入",
                            manifest=str(self.job.folder / "manifest.json"))
        else:
            self.job.status("character_done", "角色导出完成", manifest=str(self.job.folder / "manifest.json"))


ACTIVE = None


def export_camera(job):
    """Sample a VMD camera without loading a PMX or a Blender scene."""
    cfg = job.data["settings"]
    job.status("camera_export", "独立导入 VMD 镜头并逐帧采样")
    enable_mmd()
    scene = bpy.context.scene
    camera = bpy.data.objects.new("MMD_SourceCamera", bpy.data.cameras.new("MMD_SourceCamera"))
    scene.collection.objects.link(camera)
    select([camera])
    before = set(bpy.data.actions)
    bpy.ops.mmd_tools.import_vmd(filepath=cfg["camera"], scale=cfg["scale"], margin=0,
                                 update_scene_settings=False, save_log=False, log_level="WARNING")
    shift_actions(set(bpy.data.actions) - before, cfg["fps"] / 30,
                  cfg["camera_offset"])
    end = cfg["end"] or math.ceil(job.data["vmds"]["camera"]["max_frame"] * cfg["fps"] / 30)
    progress_path = job.folder / 'camera_checkpoint.json'
    samples = read_json(progress_path)['samples'] if progress_path.exists() else []
    if samples:
        job.status('camera_export', f'恢复镜头采样还原点 {samples[-1]["frame"]}/{end}')
    for frame in range(samples[-1]['frame'] + 1 if samples else cfg["start"], end + 1):
        check_stop(job)
        scene.frame_set(frame)
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        matrix = evaluated.matrix_world.copy()
        matrix.translation = matrix.translation * cfg["camera_scale"] + Vector(cfg["camera_offset_xyz"])
        data = evaluated.data
        samples.append({"frame": frame, "matrix": [list(row) for row in matrix],
                        "lens": data.lens, "sensor_width": data.sensor_width,
                        "sensor_height": data.sensor_height, "sensor_fit": data.sensor_fit,
                        "projection": data.type, "ortho_scale": data.ortho_scale})
        if frame % 100 == 0:
            atomic_json(progress_path, {'samples': samples})
            job.status("camera_export", f"采样镜头 {frame}/{end}")
    if not samples:
        raise RuntimeError("镜头没有可导出的帧")
    atomic_json(progress_path, {'samples': samples})
    camera_path = job.folder / "camera_samples.json"
    atomic_json(camera_path, samples)
    atomic_json(job.folder / "manifest.json", {
        "schema": SCHEMA, "job_id": job.data["id"], "asset_id": job.data["asset_id"],
        "fps": cfg["fps"], "frame_range": [cfg["start"], end], "files": {},
        "materials": [], "warnings": [], "camera_samples": str(camera_path)})
    job.status("scene_done", f"镜头采样完成：{len(samples)} 帧；等待 UE 导入")


def main(args):
    global ACTIVE
    job = Job(args[0])
    mode = args[1] if len(args) > 1 else "character"
    try:
        if mode == 'reference':
            from .scene_reference import render_blender
            render_blender(job.folder)
            return
        if mode == "camera":
            export_camera(job)
            return
        if mode == "scene":
            from .blender_scene import export_scene
            export_scene(job)
            return
        if mode == 'scene_import':
            from .usd_scene_optimizer import prepare_import_compaction
            prepare_import_compaction(job)
            return
        ACTIVE = Character(job)
        ACTIVE.prepare()
        if bpy.app.background or ACTIVE.cfg["unattended"]:
            ACTIVE.export_all()
        else:
            from .blender_ui import register, open_export
            register()
            bpy.app.timers.register(open_export, first_interval=0.5)
    except Cancelled as exc:
        job.status('stopped',str(exc))
    except Exception:
        job.status("failed", traceback.format_exc())
        traceback.print_exc()
        if bpy.app.background:
            raise
