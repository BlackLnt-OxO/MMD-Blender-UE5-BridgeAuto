"""Minimal sequential PMX reader.

The reader walks the file once, in section order, and records where the
Display Frame and RigidBody sections begin in the *original* byte stream.

That ordering matters.  PMX sections are variable length (UTF-16LE strings,
1/2/4 byte indices, variable morph and rigid-body payloads), so there is no way
to "jump" to a section.  Reading in order is the only correct approach, and it
gives us the exact byte ranges needed for a Surgical Patch:

    [0 .. display_frame_start)      -> reused verbatim
    [display_frame_start .. rigid_body_start) -> rebuilt from parsed data
    [rigid_body_start .. EOF)       -> reused verbatim
"""
from __future__ import annotations

from pathlib import Path

from .pmx_binary import ByteReader, PMXFormatError
from .pmx_model import (
    PMXBone,
    PMXDisplayFrame,
    PMXDisplayItem,
    PMXHeader,
    PMXModel,
    PMXRigidBody,
)

PMX_MAGIC = b"PMX "

# Sphere / shape mode tables.  Only the byte width matters here.
_IK_LINK_MAX = 1_000_000


def _read_header(reader: ByteReader) -> PMXHeader:
    version = reader.read_float()
    if not 1.9 <= version <= 2.2:
        raise PMXFormatError(f"不支持的 PMX 版本: {version}（需要 2.0 / 2.1）")
    header = PMXHeader(version=version)
    # PMX header layout, confirmed against mmd_tools core/pmx/__init__.py:
    #   [globals count, always 8][encoding][additional UV count][6 index sizes]
    # The leading 8 is a fixed marker; skipping it is mandatory, otherwise every
    # subsequent field is shifted by one byte and strings decode as garbage.
    globals_count = reader.read_int(1)
    if globals_count != 8:
        raise PMXFormatError(f"PMX Header 标记异常: 期望 8，实际 {globals_count}")
    header.encoding = reader.read_int(1)
    header.additional_uvs = reader.read_int(1)
    header.vertex_index_size = reader.read_int(1)
    header.texture_index_size = reader.read_int(1)
    header.material_index_size = reader.read_int(1)
    header.bone_index_size = reader.read_int(1)
    header.morph_index_size = reader.read_int(1)
    header.rigid_body_index_size = reader.read_int(1)
    if header.encoding not in (0, 1):
        raise PMXFormatError(f"Header encoding 非法: {header.encoding}（0=UTF-16LE, 1=UTF-8）")
    reader.text_encoding = "utf-8" if header.encoding else "utf-16-le"
    if not 0 <= header.additional_uvs <= 4:
        raise PMXFormatError(f"Header 附加 UV 数非法: {header.additional_uvs}")
    for name, size in header.index_sizes().items():
        if size not in (1, 2, 4):
            raise PMXFormatError(f"Header {name} index size 非法: {size}")
    return header


def _read_vertices(reader: ByteReader, header: PMXHeader) -> dict[str, int]:
    bone_index = header.bone_index_size
    # co(3) + normal(3) + uv(2) + 4 floats per additional UV pair.
    float_count = 8 + header.additional_uvs * 4
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"顶点数非法: {count}")
    weight_counts = {"BDEF1": 0, "BDEF2": 0, "BDEF4": 0, "SDEF": 0, "QDEF": 0}
    for _ in range(count):
        reader.skip(float_count * 4)
        weight_type = reader.read_int(1)
        if weight_type > 4:
            raise PMXFormatError(f"未知顶点权重类型: {weight_type}")
        weight_counts[("BDEF1", "BDEF2", "BDEF4", "SDEF", "QDEF")[weight_type]] += 1
        if weight_type == 0:  # BDEF1
            reader.skip(bone_index)
        elif weight_type == 1:  # BDEF2
            reader.skip(bone_index * 2 + 4)
        elif weight_type in (2, 4):  # BDEF4 / PMX 2.1 QDEF, same payload width
            reader.skip(bone_index * 4 + 16)
        elif weight_type == 3:  # SDEF
            reader.skip(bone_index * 2 + 4 + 36)
        else:
            raise PMXFormatError(
                f"未知顶点权重类型: {weight_type}（PMX 仅定义 0=BDEF1 1=BDEF2 "
                f"2=BDEF4 3=SDEF 4=QDEF）")
        reader.skip(4)  # edge scale
    return weight_counts


def _read_faces(reader: ByteReader, header: PMXHeader) -> None:
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"面索引数非法: {count}")
    reader.skip(count * header.vertex_index_size)


def _read_textures(reader: ByteReader) -> None:
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"纹理数非法: {count}")
    for _ in range(count):
        reader.read_text()


def _read_materials(reader: ByteReader, header: PMXHeader) -> None:
    """Skip the Material section (layout per mmd_tools Material.load)."""
    texture_index = header.texture_index_size
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"材质数非法: {count}")
    for _ in range(count):
        reader.read_text()                # name JP
        reader.read_text()                # name EN
        reader.skip(16)                   # diffuse (4 floats)
        reader.skip(12)                   # specular (3 floats)
        reader.skip(4)                    # shininess
        reader.skip(12)                   # ambient (3 floats)
        reader.skip(1)                    # draw flags
        reader.skip(16)                   # edge color (4 floats)
        reader.skip(4)                    # edge size
        reader.skip(texture_index)        # base texture index
        reader.skip(texture_index)        # sphere texture index
        reader.skip(1)                    # sphere texture mode
        shared_toon = reader.read_int(1, signed=True)
        if shared_toon == 1:
            reader.skip(1)                # shared toon index
        else:
            reader.skip(texture_index)    # toon texture index
        reader.read_text()                # memo
        reader.skip(4)                    # surface count


def _read_bones(reader: ByteReader, header: PMXHeader) -> list[PMXBone]:
    bone_index = header.bone_index_size
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"骨骼数非法: {count}")
    bones: list[PMXBone] = []
    for index in range(count):
        # 记下两个名称字段的字节范围（含 4 字节长度前缀）——
        # 生成 Alias PMX 时要靠它做字节级替换，从而保证
        # **除名称外整个文件逐字节不变**。
        jp_start = reader.pos
        name_jp = reader.read_text()
        jp_end = reader.pos
        en_start = reader.pos
        name_en = reader.read_text()
        en_end = reader.pos

        reader.skip(12)                   # position
        reader.skip(bone_index)           # parent
        reader.skip(4)                    # deform layer
        flags = reader.read_int(2)
        bone = PMXBone(index=index, name_jp=name_jp, name_en=name_en, flags=flags,
                       name_jp_range=(jp_start, jp_end),
                       name_en_range=(en_start, en_end))
        # Tail: either the next bone index or a position offset.
        if flags & 0x0001:
            reader.skip(bone_index)
        else:
            reader.skip(12)
        # Inheritance: index + influence, then the optional fixed axis / IK.
        if flags & (0x0100 | 0x0200):
            reader.skip(bone_index)
            reader.skip(4)
        if flags & 0x0400:
            reader.skip(12)
        if flags & 0x0800:
            reader.skip(24)               # local X axis + local Z axis
        if flags & 0x2000:
            reader.skip(4)                # external translation key
        if flags & 0x0020:
            reader.skip(bone_index)       # IK target
            reader.skip(4)                # loop count
            reader.skip(4)                # limit angle
            link_count = reader.read_int(4)
            if not 0 <= link_count <= _IK_LINK_MAX:
                raise PMXFormatError(f"IK 链长度非法: {link_count}")
            for _ in range(link_count):
                reader.skip(bone_index)
                has_limit = reader.read_int(1)
                if has_limit:
                    reader.skip(24)
        bones.append(bone)
    return bones


def _read_morphs(reader: ByteReader, header: PMXHeader) -> None:
    """Skip the Morph section.

    Morph types follow mmd_tools core/pmx Morph.create(): 0=group, 1=vertex,
    2=bone, 3..7=UV variants, 8=material, 9=flip, 10=impulse.
    """
    bone_index = header.bone_index_size
    morph_index = header.morph_index_size
    vertex_index = header.vertex_index_size
    material_index = header.material_index_size
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"Morph 数非法: {count}")
    for _ in range(count):
        reader.read_text()
        reader.read_text()
        reader.read_int(1)                # category
        morph_type = reader.read_int(1, signed=True)
        offset_count = reader.read_int(4)
        if not 0 <= offset_count <= len(reader.data):
            raise PMXFormatError(f"Morph 偏移数非法: {offset_count}")
        if morph_type == 0:      # group: morph index + weight
            reader.skip(offset_count * (morph_index + 4))
        elif morph_type == 1:    # vertex: vertex index + 3 floats
            reader.skip(offset_count * (vertex_index + 12))
        elif morph_type == 2:    # bone: bone index + 3 floats + 4 floats
            reader.skip(offset_count * (bone_index + 28))
        elif morph_type in (3, 4, 5, 6, 7):  # UV: vertex index + 4 floats
            reader.skip(offset_count * (vertex_index + 16))
        elif morph_type == 8:    # material
            # index + offset type + diffuse(4) specular(3) shininess(1) ambient(3)
            # edge color(4) edge size(1) + texture/sphere/toon factor(4 each) = 28 floats
            reader.skip(offset_count * (material_index + 1 + 4 * 28))
        elif morph_type == 9:    # flip: morph index + value
            reader.skip(offset_count * (morph_index + 4))
        elif morph_type == 10:   # impulse: rigid index + local flag + velocity + torque
            reader.skip(offset_count * (header.rigid_body_index_size + 1 + 24))
        else:
            raise PMXFormatError(f"未知 Morph 类型: {morph_type}")


def _read_display_frames(reader: ByteReader, header: PMXHeader) -> list[PMXDisplayFrame]:
    bone_index = header.bone_index_size
    morph_index = header.morph_index_size
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"显示枠数非法: {count}")
    frames: list[PMXDisplayFrame] = []
    for _ in range(count):
        raw_start = reader.pos
        name_jp = reader.read_text()
        name_en = reader.read_text()
        special_flag = reader.read_int(1)
        item_count = reader.read_int(4)
        if not 0 <= item_count <= len(reader.data):
            raise PMXFormatError(f"显示枠条目数非法: {item_count}")
        items: list[PMXDisplayItem] = []
        for _ in range(item_count):
            item_type = reader.read_int(1)
            if item_type == 0:
                index = reader.read_index(bone_index)
            elif item_type == 1:
                index = reader.read_index(morph_index)
            else:
                raise PMXFormatError(f"未知显示枠条目类型: {item_type}")
            items.append(PMXDisplayItem(item_type=item_type, index=index))
        frames.append(PMXDisplayFrame(name_jp=name_jp, name_en=name_en,
                                      special_flag=special_flag, items=items,
                                      raw_start=raw_start, raw_end=reader.pos))
    return frames


def _read_rigid_bodies(reader: ByteReader, header: PMXHeader) -> list[PMXRigidBody]:
    bone_index = header.bone_index_size
    count = reader.read_int(4)
    if count < 0 or count > len(reader.data):
        raise PMXFormatError(f"刚体数非法: {count}")
    bodies: list[PMXRigidBody] = []
    for index in range(count):
        name_jp = reader.read_text()
        name_en = reader.read_text()
        related_bone = reader.read_index(bone_index)
        reader.skip(1)                    # collision group
        reader.skip(2)                    # no-collision group mask
        shape = reader.read_int(1)        # 0 sphere / 1 box / 2 capsule
        reader.skip(12)                   # size
        reader.skip(12)                   # position
        reader.skip(12)                   # rotation
        reader.skip(4)                    # mass
        reader.skip(4)                    # linear damping
        reader.skip(4)                    # angular damping
        reader.skip(4)                    # restitution
        reader.skip(4)                    # friction
        physics_mode = reader.read_int(1)
        if shape not in (0, 1, 2):
            raise PMXFormatError(f"未知刚体形状: {shape}")
        bodies.append(PMXRigidBody(index=index, name_jp=name_jp, name_en=name_en,
                                   related_bone_index=related_bone,
                                   physics_mode=physics_mode))
    return bodies


def read_pmx(path: str | Path) -> PMXModel:
    """Parse a PMX 2.0/2.1 file down to the sections this tool needs."""
    raw = Path(path).read_bytes()
    if raw[:4] != PMX_MAGIC:
        raise PMXFormatError("不是有效的 PMX 文件（缺少 'PMX ' 魔数）")
    reader = ByteReader(raw)
    reader.skip(4)
    header = _read_header(reader)

    model = PMXModel(path=str(path), header=header)
    # Model info is FOUR strings: name JP, name EN, comment JP, comment EN.
    # Reading only three shifts the vertex section into the text area.
    model.encoded_name = reader.read_text()
    reader.read_text()  # model name EN
    model.encoded_comments = {
        "jp": reader.read_text(),
        "en": reader.read_text(),
    }

    model.vertex_weight_counts = _read_vertices(reader, header)
    _read_faces(reader, header)
    _read_textures(reader)
    _read_materials(reader, header)
    model.bones = _read_bones(reader, header)
    _read_morphs(reader, header)

    model.display_frame_start = reader.pos
    model.display_frames = _read_display_frames(reader, header)
    model.rigid_body_start = reader.pos
    model.rigid_bodies = _read_rigid_bodies(reader, header)

    return model
