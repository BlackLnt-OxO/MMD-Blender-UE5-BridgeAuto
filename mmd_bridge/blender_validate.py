"""Read exporter output before advancing; never infer success from file existence."""
import json
import math
from pathlib import Path


def position_error_check(error, *positions):
    values = [value for position in positions for value in position]
    if error < 0 or not all(math.isfinite(value) for value in [error, *values]):
        raise ValueError('骨骼位置校验包含非有限数值或无效距离')
    extent = max((abs(value) for value in values), default=0)
    # FBX matrix conversions accumulate float32 rounding, especially for distant accessory bones.
    limit = max(1e-4, math.ldexp(32.0, math.frexp(extent)[1] - 24))
    return {'severity': 'ok' if error <= 1e-4 else 'warning' if error <= limit else 'error',
            'limit_m': limit}


def roundtrip_ready(folder, manifest):
    try:
        proof = json.loads((Path(folder) / 'blender_roundtrip.json').read_text(encoding='utf-8'))
        if not isinstance(proof, dict) or proof.get('success', True) is not True:
            return False
        hashes = proof.get('file_hashes')
        return bool(proof) and (hashes is None or hashes == {
            kind: entry['sha256'] for kind, entry in manifest.get('files', {}).items()})
    except (OSError, ValueError, KeyError, TypeError):
        return False

def animated_morph_names(manifest, reference):
    if 'animated_morphs' in manifest:
        return set(manifest['animated_morphs'])
    names = {name for sample in reference for name in sample.get('morphs', {})}
    return {name for name in names if len({round(sample.get('morphs', {}).get(name, 0), 5)
                                           for sample in reference}) > 1}


def inspect_export(path,kind,manifest,armature=None):
    from io_scene_fbx import parse_fbx
    tree,version = parse_fbx.parse(str(path))
    sections = {n.id:n for n in tree.elems}
    objects = sections[b'Objects'].elems
    properties = next(n for n in sections[b'GlobalSettings'].elems if n.id==b'Properties70')
    global_values = {n.props[0].decode():n.props[4] for n in properties.elems if n.id==b'P'}
    expected = dict(UpAxis=2,UpAxisSign=1,FrontAxis=1,FrontAxisSign=1,CoordAxis=0,CoordAxisSign=-1,UnitScaleFactor=100)
    for key,value in expected.items():
        if global_values.get(key)!=value:
            raise ValueError(f'FBX 坐标/单位配置不匹配: {key}={global_values.get(key)}，需要 {value}')
    def name(node):
        return node.props[1].split(b'\x00')[0].decode('utf-8')
    models = [n for n in objects if n.id==b'Model']
    bones = {n.props[0]:name(n) for n in models if n.props[-1]==b'LimbNode'}
    meshes = [n for n in models if n.props[-1]==b'Mesh']
    cameras = [n for n in models if n.props[-1]==b'Camera']
    animated = any(n.id==b'AnimationStack' for n in objects)
    if kind=='camera':
        if len(cameras)!=1 or meshes or bones or not animated:
            raise ValueError('镜头 FBX 必须仅包含一个已采样摄像机')
    else:
        if not meshes or cameras or not bones:
            raise ValueError('角色 FBX 未包含所需网格/骨架，或混入摄像机')
        if animated != (kind=='animation'):
            raise ValueError('模型和动画 FBX 的动画开关与当前步骤不匹配')
        wanted = {b.name for b in armature.data.bones}
        if set(bones.values())!=wanted:
            raise ValueError('导出骨骼列表不完整或混入其他骨架')
        model_ids={n.props[0] for n in models}
        connections = {n.props[1]:n.props[2] for n in sections[b'Connections'].elems if n.props[0]==b'OO' and n.props[2] in model_ids}
        for identity,bone_name in bones.items():
            expected_parent=armature.data.bones[bone_name].parent
            actual_parent=bones.get(connections.get(identity))
            if actual_parent != (expected_parent.name if expected_parent else None):
                raise ValueError('FBX 骨骼父子关系不一致: '+bone_name)
        shapes={name(n) for n in objects if n.id==b'Geometry' and n.props[-1]==b'Shape'}
        missing={m['exported'] for m in manifest['morphs']}-shapes
        if missing:
            raise ValueError('FBX 缺少形态键: '+', '.join(sorted(missing)))
    return {'version':version,'bones':len(bones),'meshes':len(meshes),'cameras':len(cameras),'animated':animated,'global':{key:global_values[key] for key in expected}}
