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


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


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
    for mesh in manifest.get("scene", {}).get("meshes", []):
        if mesh.get("name"):
            packages.add(root + "/Scene/" + mesh["name"])

    image_ids = set()

    def visit(value):
        if isinstance(value, dict):
            image = value.get("image")
            if isinstance(image, dict) and image.get("id"):
                image_ids.add(str(image["id"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(manifest.get("materials", []))
    visit(manifest.get("scene", {}).get("materials", []))
    for image_id in image_ids:
        for suffix in ("C", "N", "D"):
            packages.add(f"{root}/Textures/T_{image_id}_{suffix}")

    # Only unfinished materials are safe to rebuild and reclaim. A completed
    # material unexpectedly missing its owner marker remains a hard conflict.
    completed = set(checkpoint.get("completed", {}).get("materials", []))
    records = manifest.get("materials", []) + manifest.get("scene", {}).get("materials", [])
    for record in records:
        identity = str(record.get("id", ""))
        if identity and identity not in completed:
            if record.get("asset_name"):
                packages.add(root + "/Materials/" + record["asset_name"])
            packages.add(root + "/Materials/MI_" + identity)
    return packages


def scene_mesh_record_is_importable(record):
    """Whether an exported scene mesh contains geometry UE can create."""
    try:
        vertices = int(record.get("vertices", 0))
        polygons = record.get("polygons")
        return vertices >= 3 and (polygons is None or int(polygons) > 0) and not record.get("degenerate", False)
    except (TypeError, ValueError):
        return False


def asset_name(prefix, name, identity):
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")[:36] or "Asset"
    return f"{prefix}_{clean}_{identity[:10]}"


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
    character_backend: str = "mmd2unreal"
    blender: str = ""
    unreal: str = ""
    model: str = ""
    motion: str = ""
    lipsync: str = ""
    camera: str = ""
    scene: str = ""
    project: str = ""
    working_blend: str = ""
    output_override: str = ""  # CLI/automated acceptance only
    content_root: str = "/Game/MMDBridge"
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
    scene_frame: int = 1
    scene_format: str = "usd_stage"
    material_strategy: str = "auto"
    scene_master_material: str = "/Game/SelfMakeMaterial/Material/M_00"
    use_scene_master_material: bool = False
    bake_resolution: int = 0
    usd_payload_triangle_budget: int = 1_000_000
    exact_collision: bool = True
    ue_vram_pause_percent: int = 90
    ue_vram_resume_percent: int = 80
    unattended: bool = False

    def validate(self):
        errors = []
        if self.character_backend not in ("mmd2unreal", "fbx_legacy"):
            errors.append("未知角色通路")
        legacy = self.character_backend == "fbx_legacy"
        if not self.model and not self.scene and not self.camera:
            errors.append("至少选择 PMX 角色、VMD 相机或 Blender 场景")
        if legacy and bool(self.model) != bool(self.motion):
            errors.append("角色流程必须同时选择模型和动作")
        if not self.model and (self.motion or self.lipsync or (legacy and self.camera)):
            errors.append("角色动作和口型需要 PMX 模型")
        for key in ("blender", "unreal", "model", "motion", "lipsync", "camera", "scene", "working_blend", "project"):
            value = getattr(self, key)
            if value and not Path(value).is_file():
                errors.append(f"{key} 文件不存在: {value}")
        if not self.blender and (self.scene or legacy):
            errors.append("请选择 Blender")
        for key in ("scale", "scene_scale", "camera_scale"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                errors.append(f"{key} 必须为有限正数")
        if not 1 <= self.fps <= 240 or min(self.width, self.height) <= 0:
            errors.append("帧率或画幅无效")
        if self.end and self.end < self.start:
            errors.append("结束帧不得小于开始帧")
        if self.scene_format not in ("fbx_legacy", "usd_stage"):
            errors.append("场景通路必须为 fbx_legacy 或 usd_stage")
        if self.material_strategy not in ("auto", "nodes", "hybrid", "bake"):
            errors.append("材质策略必须为 auto/nodes/hybrid/bake")
        if self.scene_master_material and not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)+", self.scene_master_material):
            errors.append("场景通用母材质须位于 /Game 下，使用英文、数字或下划线")
        if self.bake_resolution not in (0, 1024, 2048, 4096, 8192):
            errors.append("烘焙分辨率必须为自动/1K/2K/4K/8K")
        if not 10_000 <= self.usd_payload_triangle_budget <= 100_000_000:
            errors.append("USD Payload 三角面预算必须在 1 万到 1 亿之间")
        if not 1 <= self.ue_vram_resume_percent < self.ue_vram_pause_percent <= 99:
            errors.append("UE 显存恢复阈值必须低于暂停阈值，且都在 1% 到 99% 之间")
        if not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)+", self.content_root):
            errors.append("UE 内容目录须位于 /Game 下，使用英文、数字或下划线")
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


class Job:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "job.json"

    @classmethod
    def create(cls, root, settings):
        vmds = settings.validate()
        identifier = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        job = cls(Path(root) / identifier)
        fingerprints = {key: file_hash(getattr(settings, key)) for key in
                        ("model", "motion", "lipsync", "camera", "scene") if getattr(settings, key)}
        model_id = stable_id(str(Path(settings.model or settings.scene or settings.camera).resolve()))
        atomic_json(job.path, {"schema": SCHEMA, "id": identifier, "asset_id": model_id,
                              "settings": asdict(settings), "inputs": fingerprints, "vmds": vmds,
                              "created": time.time(), "resource_root": str(resource_root())})
        job.status("created", "任务已创建")
        return job

    @property
    def data(self):
        return read_json(self.path)

    def status(self, state, message, **extra):
        atomic_json(self.folder / "status.json", {"state": state, "message": message,
                    "updated": time.time(), **extra})

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


def check_stop(job):
    if (job.folder / "stop").exists():
        raise Cancelled("用户已停止任务；已完成的输出保留")
