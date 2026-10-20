"""Read-only rest geometry for desktop purpose evidence; no PMX bytes are changed."""
from pathlib import Path
import struct
from .pmx_binary import ByteReader, PMXFormatError
from .pmx_reader import _read_header, _read_textures, _read_materials, _read_bones
from .character_geometry_roles import bone_role, rest_evidence


def _attachment_candidates(points, indices, materials):
    """Read-only hints. Blender must still validate alpha, weights and all keys."""
    from collections import defaultdict
    slots=defaultdict(set);lookup=defaultdict(set);offset=0
    for material in materials:
        slot=material['pmx_index'];count=material['surface_count']
        for start in range(offset,offset+count,3):
            key=tuple(sorted(tuple(round(v,6) for v in points[i]) for i in indices[start:start+3]))
            slots[slot].add(key);lookup[key].add(slot)
        offset+=count
    candidates={}
    for slot,faces in slots.items():
        if not faces:continue
        hosts=set.intersection(*(lookup[key]-{slot} for key in faces))
        candidates[slot]=sorted(host for host in hosts if len(slots[host])>len(faces)*1.1)
    return candidates


def read_geometry(path, *, include_mesh=False):
    raw=Path(path).read_bytes();reader=ByteReader(raw)
    if raw[:4]!=b'PMX ':raise PMXFormatError('不是有效PMX')
    reader.skip(4);header=_read_header(reader)
    header_names=[reader.read_text() for _ in range(4)]
    count=reader.read_int(4);points=[];weights=[]
    if count>len(raw):raise PMXFormatError('顶点数非法')
    for _ in range(count):
        xyz=[reader.read_float() for _ in range(3)];points.append((xyz[0],xyz[2],xyz[1]))
        reader.skip((5+header.additional_uvs*4)*4)
        kind=reader.read_int(1)
        if kind==0:membership=[(reader.read_index(header.bone_index_size),1.)]
        elif kind in (1,3):
            ids=[reader.read_index(header.bone_index_size) for _ in range(2)];w=reader.read_float()
            membership=list(zip(ids,(w,1-w)))
            if kind==3:reader.skip(36)
        elif kind in (2,4):
            ids=[reader.read_index(header.bone_index_size) for _ in range(4)]
            membership=list(zip(ids,[reader.read_float() for _ in range(4)]))
        else:raise PMXFormatError('未知顶点权重类型')
        weights.append([(i,w) for i,w in membership if i>=0 and w>0]);reader.skip(4)
    count=reader.read_int(4)
    if count%3 or count*header.vertex_index_size>len(raw)-reader.pos:raise PMXFormatError('面数非法')
    indices=[reader.read_int(header.vertex_index_size) for _ in range(count)]
    textures=_read_textures(reader);materials=_read_materials(reader,header);source_bones=_read_bones(reader,header)
    bones=[]
    for b in source_bones:
        p=struct.unpack_from('<3f',raw,b.name_en_range[1]);pos=(p[0],p[2],p[1])
        bones.append({'name':b.name_jp,'original':b.name_jp,'position':pos})
    by_side={}
    for i,b in enumerate(bones):
        role=bone_role(b['name']);name=b['name'].casefold()
        side='left' if any(s in name for s in ('左','left','_l','l_')) else 'right' if any(s in name for s in ('右','right','_r','r_')) else None
        if side and role in ('thigh','knee','foot'):by_side[(side,role)]=i
    for side in ('left','right'):
        for role,next_role in (('thigh','knee'),('knee','foot')):
            if (side,role) in by_side and (side,next_role) in by_side:
                bones[by_side[(side,role)]]['end']=bones[by_side[(side,next_role)]]['position']
    rows={};offset=0;attachments=_attachment_candidates(points,indices,materials)
    for material in materials:
        used=sorted(set(indices[offset:offset+material['surface_count']]));offset+=material['surface_count']
        rows[material['pmx_index']]=rest_evidence([points[i] for i in used],[weights[i] for i in used],bones)
        rows[material['pmx_index']]['model_name_context']=' '.join(header_names[:2])+' '+Path(path).stem
        if attachments.get(material['pmx_index']):
            rows[material['pmx_index']]['candidate_hosts']=attachments[material['pmx_index']]
    return {'evidence':rows,'points':points,'weights':weights,'bones':bones,'materials':materials,'indices':indices} if include_mesh else rows
