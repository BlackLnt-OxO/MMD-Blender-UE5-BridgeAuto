from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = 2
SUPPORTED_SCHEMAS = (1, 2)
MMD_VERSION = "4.5.13"


def resource_root() -> Path:
    # Never add PyInstaller's native-module directory to Blender's sys.path.
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "bridge_resources"
    return Path(__file__).resolve().parents[1]


def atomic_json(path, value, *, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                         separators=(',',':') if compact else None, allow_nan=False)
    for number in range(1, 10000):
        tmp = path.with_name(f"{path.name}.pending_{number:02d}")
        try:
            with tmp.open("x", encoding="utf-8") as stream:
                stream.write(payload)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError(f"临时文件编号已用尽: {path}")
    try:
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(*parts):
    return hashlib.sha256("\0".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def recoverable_ue_asset_packages(root, manifest, checkpoint):
    """Deterministic UE packages that an interrupted import may safely reclaim."""
    packages = {
        root + "/Character/SK_Character",
        root + "/Character/SK_Character_Skeleton",
        root + "/Character/AN_Motion",
        root + "/Maps/MMD_ImportScratch",
        root + "/Maps/MMD_Preview",
        root + "/Sequences/LS_MMD",
        root + "/Scene/USD_AssetCache",
    }
    appearance=manifest.get('scene',{})
    if appearance.get('render_look'):
        packages.update(root+'/Materials/'+name for name in ('OCIO_Blender','MRQ_SourceLook','M_Environment'))
        packages.add(root+'/Sequences/MRQ_SourceLook')
    if appearance.get('environment',{}).get('surface'):
        packages.add(root+'/Textures/T_Environment_D')
    for volume in appearance.get('volumes',[]):
        if volume.get('id'):
            packages.add(root+'/Materials/M_Volume_'+volume['id'])
    for mesh in manifest.get("scene", {}).get("meshes", []):
        if mesh.get("name"):
            packages.add(root + "/Scene/" + mesh["name"])

    image_names = set()

    def visit(value):
        if isinstance(value, dict):
            image = value.get("image")
            if isinstance(image, dict) and image.get("id"):
                image_names.add(str(image.get("asset_name", "T_" + image["id"])))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(manifest.get("materials", []))
    visit(manifest.get("scene", {}).get("materials", []))
    for image_name in image_names:
        for suffix in ("C", "N", "D"):
            packages.add(f"{root}/Textures/{image_name}_{suffix}")

    # Only unfinished materials are safe to rebuild and reclaim. A completed
    # material unexpectedly missing its owner marker remains a hard conflict.
    completed = set(checkpoint.get("completed", {}).get("materials", []))
    records = manifest.get("materials", []) + manifest.get("scene", {}).get("materials", [])
    for record in records:
        identity = str(record.get("id", ""))
        if identity and identity not in completed:
            if record.get("asset_name"):
                packages.add(root + "/Materials/" + record["asset_name"])
            packages.add(root + "/Materials/MI_" + record.get("asset_name", identity))
    return packages


def recoverable_ue_generated_mesh(package_path, root, is_static_mesh):
    return is_static_mesh and package_path.startswith(root + "/Scene/Generated/")


def scene_mesh_record_is_importable(record):
    """Whether an exported scene mesh contains geometry UE can create."""
    try:
        vertices = int(record.get("vertices", 0))
        polygons = record.get("polygons")
        return vertices >= 3 and (polygons is None or int(polygons) > 0) and not record.get("degenerate", False)
    except (TypeError, ValueError):
        return False


def asset_name(prefix, name, identity):
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")[:36] or "Asset"
    base = f"{prefix}_{clean}"
    key = (base, identity)
    if key not in _ASSET_NAMES:
        _ASSET_NAMES[key] = f"{base}_{1 + sum(k[0] == base for k in _ASSET_NAMES):02d}"
    return _ASSET_NAMES[key]


_ASSET_NAMES = {}


def desktop_path():
    if os.name == "nt":
        buf = ctypes.create_unicode_buffer(32768)
        # CSIDL_DESKTOPDIRECTORY follows redirected/OneDrive desktop locations.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
            return Path(buf.value)
    return Path.home() / "Desktop"


def idle_seconds():
    if os.name != "nt":
        return 0.0
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0  # failure must never trigger an unattended export
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def inspect_vmd(path):
    """Bounds-checked VMD 1/2 reader; names and timing, no DCC required."""
    data = Path(path).read_bytes()
    if len(data) < 34 or not data.startswith(b"Vocaloid Motion Data"):
        raise ValueError(f"不是有效 VMD 文件: {path}")
    offset = 50 if b"0002" in data[:30] else 40
    result = {"path": str(path), "tracks": {}, "max_frame": 0, "names": {}}
    sections = [("bone", 111, 15), ("morph", 23, 15), ("camera", 61, 0),
                ("light", 28, 0), ("shadow", 9, 0)]
    for index, (kind, size, name_size) in enumerate(sections):
        if offset == len(data) and index >= 2:
            result["tracks"][kind] = 0
            continue
        if offset + 4 > len(data):
            raise ValueError(f"VMD {kind} 计数截断: {path}")
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if count > (len(data) - offset) // size:
            raise ValueError(f"VMD {kind} 数据截断: {path}")
        names = set()
        for i in range(count):
            pos = offset + i * size
            frame = struct.unpack_from("<I", data, pos + name_size)[0]
            result["max_frame"] = max(frame, result["max_frame"])
            if name_size:
                names.add(data[pos:pos+name_size].split(b"\0")[0].decode("cp932", errors="replace"))
        offset += count * size
        result["tracks"][kind] = count
        result["names"][kind] = sorted(names)
    if offset < len(data):
        if len(data) - offset < 4:
            raise ValueError("VMD IK 计数截断")
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        for _ in range(count):
            if offset + 9 > len(data):
                raise ValueError("VMD IK 数据截断")
            frame, visible, n = struct.unpack_from("<IBI", data, offset)
            offset += 9
            if n > (len(data) - offset) // 21:
                raise ValueError("VMD IK 列表截断")
            offset += n * 21
            result["max_frame"] = max(frame, result["max_frame"])
    if offset != len(data):
        raise ValueError("VMD 含未知尾部数据，不能安全判定")
    return result


@dataclass
class Settings:
    flow: str = "auto"
    #: 角色主通路。默认 ``fbx_legacy``（方案 §31）：
    #: PMX → PhysicsBakeFix → MMM → baked.vmd → Blender/mmd_tools → FBX → UE。
    #: ``mmd2unreal`` 保留为 Legacy，不再作为默认，也不再新增功能。
    character_backend: str = "fbx_legacy"
    blender: str = ""
    character_blender: str = ""  # Full flow can use 4.3 for characters and 5.2 for scenes.
    unreal: str = ""
    #: MikuMikuMoving 安装目录。MMM 是免安装软件、不写注册表，
    #: 因此以手动指定为主（方案 §8），仅辅以受限的自动检测。
    mmm_path: str = ""
    mmdbridge_path: str = ""
    character_output_dir: str = ""
    model: str = ""
    motion: str = ""
    lipsync: str = ""
    camera: str = ""
    scene: str = ""
    scene_object_filter: list[str] = field(default_factory=list)  # Optional targeted USD export
    reference_hidden_objects: list[str] = field(default_factory=list)  # Preview worlds only
    project: str = ""
    working_blend: str = ""
    output_override: str = ""  # CLI/automated acceptance only
    content_root: str = "/Game/Scenes"
    scale: float = 0.08
    fps: int = 30
    start: int = 0
    end: int = 0
    motion_offset: int = 0
    lipsync_offset: int = 0
    camera_offset: int = 0
    camera_scale: float = 1.0
    camera_offset_xyz: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    width: int = 1920
    height: int = 1080
    scene_scale: float = 1.0
    scene_light_scale: float = 1.0
    restore_scene_appearance: bool = True
    scene_viewport_ocio: bool = True
    scene_render_pipeline: str = 'lumen'
    ue_render_memory_profile: str = 'balanced'
    foliage_distance_scale: float = 2.0
    thin_surface_scattering: str = 'legacy_diffusion'
    fog_density_scale: float = 1.0
    fog_occlusion_quality: str = 'native_balanced'
    render_vram_guard_enabled: bool = False
    scene_exposure_offset: float = 0.0
    volume_resolution: int = 256
    world_volume_distance: float = 100.0
    scene_frame: int = 1
    scene_format: str = "usd_stage"
    scene_import_profile: str = 'faithful'
    optimize_usd_scatter: bool = True
    material_strategy: str = "auto"
    material_workflow: str = 'legacy_main_v1'
    material_bake_fallback: bool = True
    material_complex_strategy: str = 'pbr_bake'
    material_conversion_timeout: float = 120.0
    scene_master_material: str = "/Game/SelfMakeMaterial/Material/M_00"
    character_master_material: str = "/Game/SelfMakeMaterial/Material/M_00"
    eye_master_material: str = "/Game/SelfMakeMaterial/Material/M_00_Eye"
    use_scene_master_material: bool = False
    bake_resolution: int = 2048
    usd_payload_triangle_budget: int = 1_000_000
    exact_collision: bool = True
    collision_min_scene_ratio: float = 0.005
    ue_vram_pause_percent: int = 90
    ue_vram_resume_percent: int = 80
    unattended: bool = False

    def validate(self):
        errors = []
        if self.scene_import_profile not in ('faithful', 'render_optimized'):
            errors.append('未知场景导入策略')
        if self.ue_render_memory_profile not in ('balanced', 'low', 'quality', 'compatible'):
            errors.append('未知渲染显存策略')
        if self.thin_surface_scattering not in ('legacy_diffusion','two_sided_wrap'):
            errors.append('未知叶片透光方案')
        if self.fog_occlusion_quality not in ('native_balanced','native_quality','native_detail','legacy','detail','vsm_finer','vsm_nocache'):
            errors.append('未知细雾光遮挡方案')
        if self.flow not in ("auto", "character", "scene", "camera"):
            errors.append("未知导入流程")
        if self.flow == "scene" and any((self.model, self.motion, self.lipsync, self.camera)):
            errors.append("独立场景流程只能包含 Blender 场景")
        if self.flow == "camera" and any((self.model, self.motion, self.lipsync, self.scene)):
            errors.append("独立镜头流程只能包含镜头 VMD")
        if self.flow == "character" and (self.scene or self.camera):
            errors.append("独立角色流程不能同时包含场景或镜头")
        if self.character_backend not in ("mmd2unreal", "fbx_legacy", "mmdbridge_fbx"):
            errors.append("未知角色通路")
        legacy = self.character_backend in ("fbx_legacy", "mmdbridge_fbx")
        if not self.model and not self.scene and not self.camera:
            errors.append("至少选择 PMX 角色、VMD 相机或 Blender 场景")
        if legacy and bool(self.model) != bool(self.motion):
            errors.append("角色流程必须同时选择模型和动作")
        if not self.model and (self.motion or self.lipsync or (legacy and self.camera and self.flow != "camera")):
            errors.append("角色动作和口型需要 PMX 模型")
        for key in ("blender", "character_blender", "unreal", "model", "motion", "lipsync", "camera", "scene", "working_blend", "project"):
            if key == "working_blend" and self.character_backend == "mmdbridge_fbx":
                continue
            value = getattr(self, key)
            if value and not Path(value).is_file():
                errors.append(f"{key} 文件不存在: {value}")
        if not self.blender and (self.scene or legacy):
            errors.append("请选择 Blender")
        if self.character_backend == "mmdbridge_fbx" and self.flow not in ("scene", "camera"):
            if not self.model or not self.motion:
                errors.append("MMDBridge FBX 流程需要 PMX 模型和动作 VMD")
            if self.camera and not self.scene:
                errors.append("新镜头通路需同时选择 Blender 场景，镜头将与场景写入同一个 USD")
            if self.fps != 30 or self.start != 0 or self.end != 0 or self.motion_offset or self.lipsync_offset:
                errors.append("MMDBridge Mode 1 当前要求 30fps、起点 0、自动结束帧、动作与口型偏移 0")
            runtime = Path(self.mmdbridge_path)
            for name in ("MikuMikuDance_CHS.exe", "d3d9.dll",
                         "mmdbridge_scripts/mmdbridge_vmd_mode1.py"):
                if not (runtime / name).is_file():
                    errors.append(f"MMDBridge 目录缺少 {name}")
        if self.character_output_dir and not Path(self.character_output_dir).is_dir():
            errors.append("临时中转目录不存在")
        for key in ("scale", "scene_scale", "camera_scale"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                errors.append(f"{key} 必须为有限正数")
        if not math.isfinite(self.scene_light_scale) or not 0 <= self.scene_light_scale <= 5:
            errors.append("scene_light_scale 必须在 0 到 5 之间")
        if not math.isfinite(self.fog_density_scale) or not 0 <= self.fog_density_scale <= 100:
            errors.append('fog_density_scale 必须在 0 到 100 之间')
        if not math.isfinite(self.scene_exposure_offset) or not -20 <= self.scene_exposure_offset <= 20:
            errors.append('scene_exposure_offset 必须在 -20 到 20 EV 之间')
        if not math.isfinite(self.foliage_distance_scale) or not 1 <= self.foliage_distance_scale <= 4:
            errors.append('foliage_distance_scale 必须在 1 到 4 之间')
        if not isinstance(self.volume_resolution,int) or not 8 <= self.volume_resolution <= 512:
            errors.append('volume_resolution 必须是 8 到 512 的整数')
        if not math.isfinite(self.world_volume_distance) or not 1 <= self.world_volume_distance <= 10000:
            errors.append('world_volume_distance 必须在 1 到 10000 米之间')
        if not 1 <= self.fps <= 240 or min(self.width, self.height) <= 0:
            errors.append("帧率或画幅无效")
        if self.end and self.end < self.start:
            errors.append("结束帧不得小于开始帧")
        if self.scene_format not in ("fbx_legacy", "usd_stage"):
            errors.append("场景通路必须为 fbx_legacy 或 usd_stage")
        if self.scene_render_pipeline not in ('lumen', 'path_tracing'):
            errors.append('场景渲染管线必须为 lumen 或 path_tracing')
        if self.material_strategy not in ("auto", "nodes", "hybrid", "bake"):
            errors.append("材质策略必须为 auto/nodes/hybrid/bake")
        if self.material_workflow not in ('legacy_main_v1', 'semantic_plan_v1'):
            errors.append('材质工作流必须为 legacy_main_v1/semantic_plan_v1')
        if self.material_complex_strategy not in ('nodes', 'pbr_bake'):
            errors.append('复杂材质策略必须为 nodes/pbr_bake')
        if not math.isfinite(self.material_conversion_timeout) or self.material_conversion_timeout < 0:
            errors.append('材质转换时间预算必须为非负秒数（0=不限时）')
        for label, path in (("场景", self.scene_master_material),
                            ("角色", self.character_master_material), ("眼睛", self.eye_master_material)):
            if path and not re.fullmatch(r"/Game(?:/[\w]+)+", path):
                errors.append(f"{label}母材质须位于 /Game 下")
        if self.bake_resolution not in (0, 1024, 2048, 4096, 8192):
            errors.append("烘焙分辨率必须为自动/1K/2K/4K/8K")
        if not 10_000 <= self.usd_payload_triangle_budget <= 100_000_000:
            errors.append("USD Payload 三角面预算必须在 1 万到 1 亿之间")
        if not math.isfinite(self.collision_min_scene_ratio) or not 0 <= self.collision_min_scene_ratio <= 1:
            errors.append("小物体碰撞相对尺寸阈值必须在 0 到 1 之间")
        if not re.fullmatch(r"/Game(?:/[\w]+)+", self.content_root):
            errors.append("UE 内容目录须位于 /Game 下")
        vmds = {}
        for key, track in (("motion", "bone"), ("lipsync", "morph"), ("camera", "camera")):
            path = getattr(self, key)
            if path and Path(path).is_file():
                try:
                    info = inspect_vmd(path)
                    vmds[key] = info
                    if not info["tracks"].get(track):
                        errors.append(f"{key} 没有 {track} 轨道；请检查是否把口型与镜头选反")
                except ValueError as exc:
                    errors.append(str(exc))
        if self.model and Path(self.model).is_file():
            with open(self.model, "rb") as stream:
                if stream.read(4) != b"PMX ":
                    errors.append("模型不是有效 PMX")
        if errors:
            raise ValueError("\n".join(errors))
        return vmds


def desktop_folder():
    if os.name == 'nt':
        path = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, path) == 0:
            return Path(path.value)
    return Path.home() / 'Desktop'


def task_transfer_folder(settings):
    if settings.character_output_dir:
        return Path(settings.character_output_dir).resolve()
    parts = [Path(value).stem for value in (settings.model, settings.scene) if value]
    name = '.'.join(parts) or Path(settings.camera).stem or 'Task'
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')[:100]
    desktop = desktop_folder()
    desktop.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        folder = desktop / f'Middle_{name}_{number:02d}'
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise RuntimeError(f'中转目录编号已用尽: {name}')


def ue_asset_folder(data):
    """Use the persisted namespace; old jobs retain their original A_ folder."""
    folder = data.get('asset_folder') or 'A_' + data['asset_id']
    if not re.fullmatch(r'\w+', folder):
        raise ValueError('UE 任务目录名称无效')
    return folder


def ue_asset_root(data):
    return data['settings']['content_root'].rstrip('/') + '/' + ue_asset_folder(data)


class Job:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "job.json"

    @classmethod
    def create(cls, root, settings):
        vmds = settings.validate()
        kind = "Character" if settings.model else "Scene" if settings.scene else "Camera"
        source = Path(settings.model or settings.scene or settings.camera).stem
        stem = re.sub(r"[^A-Za-z0-9]+", "_", source).strip("_")[:32] or kind
        base = f"{kind}_{stem}"
        for number in range(1, 10000):
            identifier = f"{base}_{number:02d}"
            folder = Path(root) / identifier
            try:
                folder.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(f"任务目录编号已用尽: {base}")
        job = cls(folder)
        fingerprints = {key: file_hash(getattr(settings, key)) for key in
                        ("model", "motion", "lipsync", "camera", "scene") if getattr(settings, key)}
        model_id = identifier
        atomic_json(job.path, {"schema": SCHEMA, "id": identifier, "asset_id": model_id,
                              "settings": asdict(settings), "inputs": fingerprints, "vmds": vmds,
                              "created": time.time(), "resource_root": str(resource_root())})
        job.status("created", "任务已创建")
        return job

    @property
    def data(self):
        return read_json(self.path)

    def status(self, state, message, **extra):
        entry = {"state": state, "message": message, "updated": time.time(), **extra}
        atomic_json(self.folder / "status.json", entry)
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def command(self, name, **extra):
        atomic_json(self.folder / "command.json", {"name": name, "nonce": uuid.uuid4().hex, **extra})

    def verify_inputs(self):
        data = self.data
        for key, digest in data["inputs"].items():
            if file_hash(data["settings"][key]) != digest:
                raise ValueError(f"{key} 素材已改变；请新建任务，不能沿用旧结果")


def validate_fbx(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 128:
        raise ValueError(f"FBX 不存在或不完整: {path}")
    with path.open("rb") as stream:
        head = stream.read(64)
    if not (head.startswith(b"Kaydara FBX Binary") or b"FBX" in head):
        raise ValueError(f"无效 FBX: {path}")
    return {"path": str(path.resolve()), "sha256": file_hash(path), "size": path.stat().st_size}


class Cancelled(RuntimeError):
    pass


def scene_collision_enabled(enabled, object_size, scene_size, min_ratio=0.005, override=None):
    if not enabled or (override is not None and str(override).lower() == 'none'):
        return False
    if str(override).lower() == 'exact':
        return True
    return max(scene_size, default=0) <= 0 or max(object_size, default=0) >= max(scene_size) * min_ratio


def check_stop(job):
    if (job.folder / "stop").exists():
        raise Cancelled("用户已停止任务；已完成的输出保留")
