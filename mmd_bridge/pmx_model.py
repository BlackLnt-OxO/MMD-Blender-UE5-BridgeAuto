"""In-memory PMX structures needed by the PhysicsBake preflight.

Deliberately partial: geometry and morph payloads remain in the original byte
stream. Material summaries are read-only; surgical patches still copy their
original bytes verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Display frame item types (SPEC §5)
FRAME_ITEM_BONE = 0
FRAME_ITEM_MORPH = 1

# RigidBody physics modes (SPEC §4)
PHYSICS_MODE_FOLLOW_BONE = 0
PHYSICS_MODE_PHYSICS = 1
PHYSICS_MODE_PHYSICS_AND_BONE = 2

#: Modes whose related bone actually receives a simulated transform and therefore
#: has to be reachable from the MMM timeline.
BAKEABLE_PHYSICS_MODES = (PHYSICS_MODE_PHYSICS, PHYSICS_MODE_PHYSICS_AND_BONE)

# PMX bone flag bits
BONE_FLAG_TAIL_INDEX = 0x0001
BONE_FLAG_ROTATABLE = 0x0002
BONE_FLAG_MOVABLE = 0x0004
BONE_FLAG_VISIBLE = 0x0008
BONE_FLAG_ENABLED = 0x0010
BONE_FLAG_IK = 0x0020

#: Name of the dedicated display frame this tool creates.
PHYSICS_BAKE_FRAME_NAME = "PhysicsBake"


@dataclass
class PMXHeader:
    """Only the fields that decide how indices are encoded."""

    version: float = 2.0
    encoding: int = 0
    additional_uvs: int = 0
    vertex_index_size: int = 4
    texture_index_size: int = 4
    material_index_size: int = 4
    bone_index_size: int = 4
    morph_index_size: int = 4
    rigid_body_index_size: int = 4

    def index_sizes(self) -> dict[str, int]:
        return {
            "vertex": self.vertex_index_size,
            "texture": self.texture_index_size,
            "material": self.material_index_size,
            "bone": self.bone_index_size,
            "morph": self.morph_index_size,
            "rigid_body": self.rigid_body_index_size,
        }


@dataclass
class PMXBone:
    index: int
    name_jp: str = ""
    name_en: str = ""
    flags: int = 0

    #: ``name_jp`` 在原始文件里的字节范围 ``(start, end)``，
    #: **包含前面 4 字节的长度前缀** —— 改名字会改变长度，所以必须连前缀一起替换。
    #: 由 :func:`mmd_bridge.pmx_reader.read_pmx` 填充；
    #: 手工构造的 PMXBone（测试用）可以是 ``None``。
    name_jp_range: tuple[int, int] | None = None

    #: 同上，``name_en`` 的字节范围。
    name_en_range: tuple[int, int] | None = None

    @property
    def visible(self) -> bool:
        return bool(self.flags & BONE_FLAG_VISIBLE)


@dataclass
class PMXDisplayItem:
    item_type: int
    index: int


@dataclass
class PMXDisplayFrame:
    name_jp: str = ""
    name_en: str = ""
    special_flag: int = 0
    items: list[PMXDisplayItem] = field(default_factory=list)
    #: Byte range of this frame's record in the ORIGINAL file, excluding the
    #: leading frame-count field.  Preserved so frames we do not touch can be
    #: copied verbatim instead of being re-encoded (see pmx_patcher).
    raw_start: int = -1
    raw_end: int = -1

    def bone_indices(self) -> list[int]:
        return [item.index for item in self.items if item.item_type == FRAME_ITEM_BONE]

    @property
    def has_raw_range(self) -> bool:
        return 0 <= self.raw_start <= self.raw_end


@dataclass
class PMXRigidBody:
    index: int
    name_jp: str = ""
    name_en: str = ""
    related_bone_index: int = -1
    physics_mode: int = 0


@dataclass
class PMXModel:
    """A parsed model plus the byte offsets needed to patch it in place."""

    path: str = ""
    header: PMXHeader = field(default_factory=PMXHeader)
    bones: list[PMXBone] = field(default_factory=list)
    display_frames: list[PMXDisplayFrame] = field(default_factory=list)
    rigid_bodies: list[PMXRigidBody] = field(default_factory=list)
    vertex_weight_counts: dict[str, int] = field(default_factory=dict)
    textures: list[str] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    #: Section boundaries in the original byte stream.
    display_frame_start: int = 0
    rigid_body_start: int = 0
    encoded_name: str = ""
    encoded_comments: dict[str, str] = field(default_factory=dict)
    extra_display_frame_names: list[tuple[str, str]] = field(default_factory=list)
    morph_names: list[str] = field(default_factory=list)

    @property
    def bone_count(self) -> int:
        return len(self.bones)

    @property
    def rigid_body_count(self) -> int:
        return len(self.rigid_bodies)

    def bone_name(self, index: int, fallback: str = "") -> str:
        if 0 <= index < len(self.bones):
            return self.bones[index].name_jp or self.bones[index].name_en
        return fallback or f"Bone #{index}"

    def frame_by_name(self, name: str) -> PMXDisplayFrame | None:
        for frame in self.display_frames:
            if frame.name_jp == name or frame.name_en == name:
                return frame
        return None
