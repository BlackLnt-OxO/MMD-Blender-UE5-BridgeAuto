"""Live USD scene exporter built from Blender's evaluated dependency graph.

The source .blend is opened read-only for this process and is never saved.  All
geometry is taken from depsgraph.object_instances, so Geometry Nodes and other
instancers export their rendered result rather than their controller bounds.
"""
from __future__ import annotations

from array import array
import ctypes
import gzip
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

import bpy
from .scene_shader_data import object_random
from mathutils import Matrix, Vector

from .blender_materials import Materials, restore_field_socket_types
from .material_policy import classify_surface, MATERIAL_ANALYSIS_REVISION
from .core import SCHEMA, atomic_json, file_hash, read_json, stable_id, check_stop, scene_collision_enabled
from . import core

# Analysis depends on translator semantics; frozen render geometry does not.
def _ensure_analysis_revision(job, checkpoint, bake_fallback=False, complex_strategy='nodes', force_bake=None, conversion_timeout=120.0):
    if (checkpoint.get('material_analysis_revision', 1) == MATERIAL_ANALYSIS_REVISION and
            checkpoint.get('material_bake_fallback', False) == bool(bake_fallback) and
            checkpoint.get('material_complex_strategy', 'nodes') == complex_strategy and
            checkpoint.get('material_conversion_timeout', 120.0) == conversion_timeout and
            checkpoint.get('material_bake_retry',{}) == (force_bake or {})):
        return
    folder = job.folder / 'scene_checkpoint'
    archive = folder / ('PreviousAnalysis_' + str(time.time_ns()))
    archive.mkdir()
    atomic_json(archive / 'scene_checkpoint.json', checkpoint,compact=True)
    for pattern in ('Analyzed_*.json.gz', 'Payload_*.json.gz'):
        for path in folder.glob(pattern):
            path.rename(archive / path.name)
    for name, empty in (('completed', []), ('payloads', {}), ('image_hashes', {}),
                        ('material_records', {}), ('images', {}), ('material_warnings', []),
                        ('asset_names', [])):
        checkpoint[name] = empty
    checkpoint['material_analysis_revision'] = MATERIAL_ANALYSIS_REVISION
    checkpoint['material_bake_fallback'] = bool(bake_fallback)
    checkpoint['material_complex_strategy'] = complex_strategy
    checkpoint['material_conversion_timeout'] = conversion_timeout
    checkpoint['material_bake_retry'] = force_bake or {}
    atomic_json(job.folder / 'scene_checkpoint.json', checkpoint,compact=True)
    job.status('scene_export', '材质转换器已更新：复用冻结网格，重新读取源材质节点并生成通道')


def _write_compressed_json(path, data):
    temporary = path.with_suffix(path.suffix + '.partial')
    with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=3) as stream:
        json.dump(data, stream, ensure_ascii=False, separators=(',', ':'))
    os.replace(temporary, path)


def _read_compressed_json(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def _write_frozen_library(path, meshes):
    # Material slots retain their indices, but references come from the source
    # file on reload. Do not duplicate packed textures in every geometry batch.
    slots = {mesh: list(mesh.materials) for mesh in meshes}
    temporary = path.with_suffix('.partial.blend')
    try:
        for mesh, materials in slots.items():
            for index in range(len(materials)):
                mesh.materials[index] = None
        bpy.data.libraries.write(str(temporary), set(meshes), path_remap='ABSOLUTE', fake_user=True, compress=True)
        os.replace(temporary, path)
    finally:
        for mesh, materials in slots.items():
            for index, material in enumerate(materials):
                mesh.materials[index] = material


def _frozen_item_record(item):
    return {**{k: v for k, v in item.items() if k not in ('mesh', 'source_materials', 'instances', 'source_types')},
            'mesh': item['mesh'].name, 'source_types': sorted(item['source_types']),
            'source_materials': [mat.name if mat else None for mat in item['source_materials']],
            'instances': [{**instance, 'matrix_object': [list(row) for row in instance['matrix_object']]}
                          for instance in item['instances']]}


def _volume_records(volumes):
    meshes, records = set(), []
    for volume in volumes:
        if isinstance(volume, dict):
            records.append(volume)
        else:
            record, socket, mesh = volume
            material = bpy.data.materials.get(record.get('material', ''))
            if material is None:
                # Older checkpoints did not record material identity. Evaluated
                # sockets belong to copied trees; compare their original tree.
                tree = getattr(socket.id_data, 'original', socket.id_data)
                material = next((mat for mat in mesh.materials if mat and
                                 getattr(mat.node_tree, 'original', mat.node_tree) == tree), None)
            if material is None:
                raise ValueError(f"体积 {record.get('name', mesh.name)} 的源材质无法关联，不能保存体积断点")
            meshes.add(mesh)
            records.append({'record': record, 'mesh': mesh.name, 'material': material.name,
                            'source_materials': [mat.name if mat else None for mat in mesh.materials]})
    return records, meshes


def _save_frozen(job, usable, volumes, materials, warnings, skipped_items, key):
    folder = job.folder / 'scene_checkpoint'
    folder.mkdir(exist_ok=True)
    library = folder / 'Frozen.blend'
    meshes = {item['mesh'] for item in usable}
    volume_records, volume_meshes = _volume_records(volumes)
    _write_frozen_library(library, meshes | volume_meshes)
    items = [_frozen_item_record(item) for item in usable]
    checkpoint = {'schema': 1, 'key': key, 'library_sha256': file_hash(library), 'items': items,
                  'volumes': volume_records, 'world_bounds': materials.world_bounds,
                  'material_world_bounds': materials.material_world_bounds,
                  'warnings': warnings, 'skipped_items': skipped_items, 'completed': [], 'payloads': {},
                  'material_analysis_revision': MATERIAL_ANALYSIS_REVISION,
                  'material_bake_fallback': materials.bake_fallback,
                  'material_complex_strategy': materials.complex_strategy,
                  'material_conversion_timeout': materials.conversion_timeout}
    atomic_json(job.folder / 'scene_checkpoint.json', checkpoint,compact=True)
    return checkpoint


def _load_frozen(job, checkpoint, materials):
    library = job.folder / 'scene_checkpoint/Frozen.blend'
    usable, volumes = _load_frozen_records(library, checkpoint)
    # Older snapshots cut the mesh before saving its original texture space.
    # Reanalysing such chunks cannot recover ORCO by matching vertex indices.
    from .scene_shader_data import has_generated_layers
    dependencies = {}
    for item in usable:
        if (item.get('auto_chunked') and not item.get('generated_error') and
                _needs_generated_coordinates(item['source_materials'], bpy.context.scene.render.engine, dependencies) and
                not has_generated_layers(item['mesh'])):
            raise ValueError('旧冻结分块缺少分块前的 Generated 坐标：' + item['source_name'] +
                             '；请保留当前任务并新建场景转换任务，重新生成冻结网格；仅重做材质无法修复')
    materials.world_bounds = checkpoint['world_bounds']
    materials.material_world_bounds = checkpoint['material_world_bounds']
    return usable, volumes


def _restore_shader_instance_data(job,items,deps,checkpoint=None):
    from .scene_shader_data import instance_identity,restore_instance_values
    missing=[instance for item in items for instance in item['instances']
             if 'object_random' not in instance.get('shader_data',{})]
    if not missing:return
    job.status('scene_export','补齐旧几何断点的源实例随机数据，复用已有冻结网格')
    wanted={instance_identity(i) for i in missing};records=[]
    for index,instance in enumerate(deps.object_instances):
        if index%256==0:check_stop(job)
        source=getattr(instance.object,'original',instance.object)
        owner=getattr(getattr(instance,'parent',None),'original',None) or source
        record={'name':owner.name,'source_name':source.name,'matrix_object':instance.matrix_world}
        if instance_identity(record) in wanted:
            records.append(dict(record,object_random=object_random(source.name,instance.random_id if instance.is_instance else None)))
    restored=restore_instance_values(missing,records)
    for instance,values in zip(missing,restored):instance.setdefault('shader_data',{}).update(values)
    if checkpoint:
        for saved,item in zip(checkpoint['items'],items):
            for old,new in zip(saved['instances'],item['instances']):old['shader_data']=new['shader_data']
        atomic_json(job.folder/'scene_checkpoint.json',checkpoint,compact=True)


def _load_frozen_records(library, checkpoint):
    if not library.is_file() or file_hash(library) != checkpoint['library_sha256']:
        raise ValueError('场景还原点的冻结网格缺失或已改变，请新建任务')
    material_names = {name for item in checkpoint['items'] for name in item['source_materials'] if name}
    source_materials = {name:bpy.data.materials.get(name) for name in material_names}
    if any(material is None for material in source_materials.values()):
        raise ValueError('源文件缺少冻结断点中的材质，不能安全恢复')
    names = list(dict.fromkeys([item['mesh'] for item in checkpoint['items']] +
                              [v['mesh'] for v in checkpoint['volumes'] if 'record' in v]))
    if names:
        with bpy.data.libraries.load(str(library), link=False) as (source, loaded):
            loaded.meshes = list(names)
        meshes = dict(zip(names, loaded.meshes))
    else:
        meshes = {}
    if any(mesh is None for mesh in meshes.values()):
        raise ValueError('场景还原点缺少冻结网格，请新建任务')
    usable = []
    for saved in checkpoint['items']:
        item = dict(saved, mesh=meshes[saved['mesh']], source_types=set(saved['source_types']),
                    source_materials=[source_materials[name] if name else None for name in saved['source_materials']],
                    source_material_names=list(saved['source_materials']),
                    instances=[dict(instance, matrix_object=Matrix(instance['matrix_object'])) for instance in saved['instances']])
        for slot, material in enumerate(item['source_materials']):
            item['mesh'].materials[slot] = material
        usable.append(item)
    volumes = []
    for saved in checkpoint['volumes']:
        if 'record' in saved:
            from .blender_appearance import material_channels
            socket = material_channels(bpy.data.materials[saved['material']], bpy.context.scene.render.engine)[1]
            for index, name in enumerate(saved.get('source_materials', [])):
                meshes[saved['mesh']].materials[index] = bpy.data.materials[name] if name else None
            volumes.append((dict(saved['record'], material=saved['material']), socket, meshes[saved['mesh']]))
        else:
            volumes.append(saved)
    return usable, volumes


def _save_freeze_batch(job, key, state, cursor, digest, volumes, lights, warnings, skipped, complete=False):
    folder = job.folder / 'scene_checkpoint'
    folder.mkdir(exist_ok=True)
    stem = f"Freeze_{state['batch']:06d}"
    new_volumes, volume_meshes = _volume_records(volumes[state['volumes']:])
    meshes = {item['mesh'] for item in state['new_items']} | volume_meshes
    library = folder / (stem + '.blend')
    if meshes:
        _write_frozen_library(library, meshes)
    record = {'schema': 1, 'key': key, 'cursor': cursor, 'digest': digest, 'complete': complete,
              'library': library.name if meshes else None,
              'library_sha256': file_hash(library) if meshes else None,
              'items': [_frozen_item_record(dict(item, instances=[])) for item in state['new_items']],
              'instances': state['new_instances'], 'volumes': new_volumes,
              'lights': lights[state['lights']:], 'warnings': warnings[state['warnings']:],
              'skipped': skipped[state['skipped']:]}
    _write_compressed_json(folder / (stem + '.json.gz'), record)
    state.update(batch=state['batch']+1, cursor=cursor, digest=digest, complete=complete,
                 volumes=len(volumes), lights=len(lights), warnings=len(warnings), skipped=len(skipped),
                 new_items=[], new_instances=[])


def _load_freeze_batches(job, key, volumes, lights, warnings, skipped):
    state = dict(batch=1, cursor=0, digest=hashlib.sha256().hexdigest(), complete=False,
                 volumes=len(volumes), lights=len(lights), warnings=len(warnings), skipped=len(skipped),
                 new_items=[], new_instances=[])
    items = {}
    folder = job.folder / 'scene_checkpoint'
    for path in sorted(folder.glob('Freeze_*.json.gz')):
        if path.name != f"Freeze_{state['batch']:06d}.json.gz":
            raise ValueError('冻结阶段断点批次不连续，请检查任务目录')
        saved = _read_compressed_json(path)
        if saved['key'] != key:
            raise ValueError('冻结阶段断点与素材、参数或 Blender 版本不一致，请新建任务')
        if saved['library']:
            loaded_items, loaded_volumes = _load_frozen_records(folder / saved['library'], saved)
        else:
            loaded_items, loaded_volumes = [], saved['volumes']
        items.update((item['_freeze_index'], item) for item in loaded_items)
        for record in saved['instances']:
            item = items[record['item']]
            instance = dict(record['instance'])
            instance['matrix_object'] = Matrix(instance['matrix_object'])
            item['instances'].append(instance)
            item['source_types'].add(record['type'])
        volumes.extend(loaded_volumes)
        lights.extend(saved['lights'])
        warnings.extend(saved['warnings'])
        skipped.extend(saved['skipped'])
        state.update(batch=state['batch']+1, cursor=saved['cursor'], digest=saved['digest'], complete=saved['complete'],
                     volumes=len(volumes), lights=len(lights), warnings=len(warnings), skipped=len(skipped))
    return state, items


def _instance_signature(instance):
    obj = getattr(instance.object, 'original', instance.object)
    owner = getattr(getattr(instance, 'parent', None), 'original', None)
    return json.dumps([obj.name, owner.name if owner else None, instance.is_instance,
                       list(instance.persistent_id), [list(row) for row in instance.matrix_world]],
                      separators=(',', ':')).encode('utf-8')


def _tuple_key(value):
    return tuple(_tuple_key(v) for v in value) if isinstance(value, list) else value


def _load_analysis_journal(job, checkpoint):
    folder = job.folder / 'scene_checkpoint'
    for path in sorted(folder.glob('Analyzed_*.json.gz')):
        index = int(path.name.split('_')[1].split('.')[0])
        if index <= len(checkpoint['completed']):
            continue  # Already consolidated into the main snapshot.
        if index != len(checkpoint['completed']) + 1:
            raise ValueError('网格分析断点不连续，不能安全恢复')
        event = _read_compressed_json(path)
        checkpoint['completed'].append(event['mesh'])
        records = checkpoint.setdefault('material_records', {})
        for identity, record in event['materials'].items():
            aliases = records.get(identity, {}).get('source_aliases', []) + event['aliases'].get(identity, [])
            records[identity] = record
            if aliases:
                record['source_aliases'] = aliases
        checkpoint.setdefault('images', {}).update(event['images'])
        checkpoint.setdefault('image_hashes', {}).update(event['image_hashes'])
        checkpoint.setdefault('material_warnings', []).extend(event['warnings'])
        checkpoint.setdefault('asset_names', []).extend(event['asset_names'])
        checkpoint.setdefault('warnings', []).extend(event.get('export_warnings', []))
        checkpoint.setdefault('skipped_items', []).extend(event.get('skipped_items', []))
    for path in sorted(folder.glob('Payload_*.json.gz')):
        checkpoint['payloads'][str(int(path.name.split('_')[1].split('.')[0]))] = _read_compressed_json(path)


def _restore_analyzed(job, checkpoint, groups, materials):
    from pxr import Usd, UsdGeom
    _load_analysis_journal(job, checkpoint)
    exported = 0
    verified = set()
    for saved in checkpoint['completed']:
        check_stop(job)
        stages = {}
        for record in saved['groups']:
            if record['id'] in groups:
                groups[record['id']]['instances'].extend(record['instances'])
                groups[record['id']]['source_types'].update(record['source_types'])
                exported += len(record['instances'])
                continue
            path = job.folder / 'scene_checkpoint' / (record.get('geometry_file') or saved['file'])
            digest = record.get('geometry_sha256') or saved['sha256']
            if path not in verified:
                if not path.is_file() or file_hash(path) != digest:
                    raise ValueError('已分析网格的还原点缺失或损坏，请新建任务')
                verified.add(path)
            stage = stages.get(path)
            if stage is None:
                stage = stages[path] = Usd.Stage.Open(str(path))
            mesh = UsdGeom.Mesh(stage.GetPrimAtPath(record['prim_path'] + '/Mesh'))
            geometry = {'points': mesh.GetPointsAttr().Get(), 'counts': mesh.GetFaceVertexCountsAttr().Get(),
                        'indices': mesh.GetFaceVertexIndicesAttr().Get(), 'normals': mesh.GetNormalsAttr().Get() or [],
                        'triangles': record['triangles'], 'uvs': [
                            {'name': uv.GetPrimvarName(), 'source_name': uv.GetAttr().GetCustomDataByKey('MMDBridge:sourceName'),
                             'values': uv.Get()} for uv in UsdGeom.PrimvarsAPI(mesh).GetPrimvars() if uv.GetPrimvarName().startswith('st')]}
            primvars=UsdGeom.PrimvarsAPI(mesh)
            color=primvars.GetPrimvar('displayColor');opacity=primvars.GetPrimvar('displayOpacity')
            if color and opacity:geometry['colors']={'name':'displayColor','rgb':color.Get(),'alpha':opacity.Get()}
            group = {k: v for k, v in record.items() if k != 'triangles'}
            group.update(geometry=geometry, source_types=set(record['source_types']), instances=[])
            cached = groups.setdefault(group['id'], group)
            cached['instances'].extend(record['instances'])
            exported += len(record['instances'])
    materials.records = checkpoint.get('material_records', {})
    for record in materials.records.values():
        restore_field_socket_types(record)
        for definition in record.get('function_definitions',[]):
            materials.functions.definitions[definition['id']]=definition
    materials.images = checkpoint.get('images', {})
    materials.warnings = checkpoint.get('material_warnings', [])
    for record in materials.images.values():
        path = Path(record['path'])
        if not path.is_file() or file_hash(path) != checkpoint.get('image_hashes', {}).get(str(path)):
            raise ValueError('还原点引用的贴图缺失或已改变，请新建任务')
    core._ASSET_NAMES.update((tuple(key), value) for key, value in checkpoint.get('asset_names', []))
    _analysis_checkpoint_state(checkpoint, materials)
    return exported


def _analysis_checkpoint_state(checkpoint, materials):
    state = getattr(materials, '_checkpoint_state', None)
    if state is None:
        geometries = {}
        for saved in checkpoint['completed']:
            for group in saved['groups']:
                geometries.setdefault(group['id'], {'file': group.get('geometry_file') or saved['file'],
                    'sha256': group.get('geometry_sha256') or saved['sha256'], 'prim_path': group['prim_path']})
        state = materials._checkpoint_state = dict(geometries=geometries, images=set(checkpoint.get('images', {})),
            assets={tuple(k) for k, _ in checkpoint.get('asset_names', [])}, warnings=len(checkpoint.get('material_warnings', [])),
            export_warnings=len(checkpoint.get('warnings', [])), skipped=len(checkpoint.get('skipped_items', [])),
            aliases={k: len(v.get('source_aliases', [])) for k, v in checkpoint.get('material_records', {}).items()})
    return state


def _save_analyzed(job, checkpoint, changed, materials, index, cfg, data):
    from .bake_cache import material_texture_paths
    state = _analysis_checkpoint_state(checkpoint, materials)
    folder = job.folder / 'scene_checkpoint'
    path = folder / f'Mesh_{index+1:06d}.usdc'
    new_groups = [group for group in changed if group['id'] not in state['geometries']]
    digest = None
    if new_groups:
        _write_chunk(path, 0, new_groups, materials.meters_per_unit, cfg.get('exact_collision', True),
                     cfg['content_root'], data['asset_id'], job, definitions_only=True)
        digest = file_hash(path)
        for group in new_groups:
            state['geometries'][group['id']] = {'file': path.name, 'sha256': digest, 'prim_path': group['prim_path']}
    groups = []
    for group in changed:
        geometry = state['geometries'][group['id']]
        groups.append({**{k: v for k, v in group.items() if k not in ('geometry', 'source_types')},
            'prim_path': geometry['prim_path'], 'geometry_file': geometry['file'], 'geometry_sha256': geometry['sha256'],
            'triangles': group['geometry']['triangles'], 'source_types': sorted(group['source_types'])})
    mesh_record = {'file': path.name if new_groups else None, 'sha256': digest, 'groups': groups}
    touched = {identity for group in changed for identity in group['materials'] if identity}
    records = {identity: {k: v for k, v in materials.records[identity].items() if k != 'source_aliases'} for identity in touched}
    aliases = {identity: materials.records[identity].get('source_aliases', [])[state['aliases'].get(identity, 0):] for identity in touched}
    referenced = material_texture_paths(records)
    unused = [k for k, v in materials.images.items() if k not in state['images'] and Path(v['path']).resolve() not in referenced]
    for key in unused:
        del materials.images[key]
    images = ({k: v for k, v in materials.images.items() if k not in state['images']}
              if len(materials.images) != len(state['images']) else {})
    hashes = checkpoint.setdefault('image_hashes', {})
    new_hashes = {}
    for record in images.values():
        if record['path'] not in hashes:
            new_hashes[record['path']] = file_hash(record['path'])
    assets = ([(k, v) for k, v in core._ASSET_NAMES.items() if k not in state['assets']]
              if len(core._ASSET_NAMES) != len(state['assets']) else [])
    event = dict(mesh=mesh_record, materials=records, aliases=aliases, images=images,
                 image_hashes=new_hashes, warnings=materials.warnings[state['warnings']:], asset_names=assets,
                 export_warnings=checkpoint.get('warnings', [])[state['export_warnings']:],
                 skipped_items=checkpoint.get('skipped_items', [])[state['skipped']:])
    _write_compressed_json(folder / f'Analyzed_{index+1:06d}.json.gz', event)
    checkpoint['completed'].append(mesh_record)
    hashes.update(new_hashes)
    state['images'].update(images)
    state['assets'].update(k for k, _ in assets)
    state['warnings'] = len(materials.warnings)
    state['export_warnings'] = len(checkpoint.get('warnings', []))
    state['skipped'] = len(checkpoint.get('skipped_items', []))
    state['aliases'].update((identity, len(materials.records[identity].get('source_aliases', []))) for identity in touched)


def _save_analysis_snapshot(job, checkpoint, materials):
    from .bake_cache import material_texture_paths
    referenced = material_texture_paths(list(materials.records.values()))
    materials.images = {k:v for k,v in materials.images.items() if Path(v['path']).resolve() in referenced}
    checkpoint.update(material_records=materials.records, images=materials.images,
                      material_warnings=materials.warnings, asset_names=list(core._ASSET_NAMES.items()))
    atomic_json(job.folder / 'scene_checkpoint.json', checkpoint,compact=True)
    for path in (job.folder / 'scene_checkpoint').iterdir():
        if re.fullmatch(r'(Analyzed|Payload)_\d{6}\.json\.gz', path.name):
            path.unlink()


def _name(value):
    clean = re.sub(r"[^A-Za-z0-9_]", "_", str(value)).strip("_")
    clean = clean[:80] or "Item"
    return "Item_" + clean if clean[0].isdigit() else clean


def render_visible_objects(scene):
    """Names reachable through collections enabled for Blender rendering."""
    visible = set()

    def visit(collection):
        if collection.hide_render:
            return
        visible.update(obj.name for obj in collection.objects if not obj.hide_render)
        for child in collection.children:
            visit(child)

    visit(scene.collection)
    return visible


def missing_external_files():
    """Report unavailable source dependencies that Blender may silently ignore."""
    paths = []
    for datablock in (*bpy.data.cache_files, *bpy.data.libraries):
        source = getattr(datablock, "filepath", "")
        if source:
            resolved = Path(bpy.path.abspath(source))
            if not resolved.is_file():
                paths.append(str(resolved))
    return sorted(set(paths))


def _peak_working_set_mb(current=False):
    """Best-effort peak process working set for export comparison reports."""
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    try:
        from ctypes import wintypes
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        memory_info.restype = wintypes.BOOL
        ok = memory_info(current_process(), ctypes.byref(counters), counters.cb)
        size = counters.WorkingSetSize if current else counters.PeakWorkingSetSize
        return round(size / (1024 * 1024), 1) if ok else None
    except Exception:
        return None


def _matrix(value):
    # Blender stores translation in the last column while Gf.Matrix4d follows
    # the USD row-vector convention.  Transposing preserves the world transform.
    from pxr import Gf
    return Gf.Matrix4d(*[float(value[c][r]) for r in range(4) for c in range(4)])


def _mesh_points(mesh):
    import numpy as np
    points = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', points)
    return points.reshape((-1, 3))


def _bounds(points, matrix):
    import numpy as np
    if len(points) == 0:
        return None
    # Keep the actual evaluated vertex bounds: transforming just eight local
    # box corners would enlarge rotated meshes and alter spatial materials.
    transform = np.asarray(matrix, dtype=np.float32)
    world = np.asarray(points, dtype=np.float32) @ transform[:3, :3].T
    world += transform[:3, 3]
    low, high = world.min(axis=0), world.max(axis=0)
    return {'min': low.tolist(), 'max': high.tolist(), 'size': (high - low).tolist()}


def _rigid_matrix(matrix):
    # Logical parents must not introduce shear when UE composes child TRS.
    location, rotation, _ = matrix.decompose()
    return Matrix.LocRotScale(location, rotation, Vector((1, 1, 1)))


def _instance_geometry(group, matrix, cache=None):
    """Keep TRS instances shared; bake only the affine residual UE cannot store."""
    location, rotation, scale = matrix.decompose()
    trs = Matrix.LocRotScale(location, rotation, scale)
    if max(abs(trs[r][c] - matrix[r][c]) for r in range(3) for c in range(3)) <= (
            1e-5 * max(1e-8, max(abs(matrix[r][c]) for r in range(3) for c in range(3)))):
        return group, matrix
    residual = trs.inverted() @ matrix
    identity = stable_id(group['id'], 'affine-v1', json.dumps(
        [[round(v, 9) for v in row] for row in residual]))
    if cache is not None and identity in cache:
        return cache[identity], trs
    transformed = _merge_owner(identity, [(group, {'matrix': matrix})], trs)
    transformed['id'] = identity
    return transformed, trs


def _mesh_span(mesh):
    if not mesh.vertices:
        return (0.0, 0.0, 0.0)
    low = [math.inf] * 3
    high = [-math.inf] * 3
    for vertex in mesh.vertices:
        for axis in range(3):
            low[axis] = min(low[axis], vertex.co[axis])
            high[axis] = max(high[axis], vertex.co[axis])
    return tuple(high[axis] - low[axis] for axis in range(3))


def _scene_wide_instance_array(instance, source, evaluated_mesh):
    if not instance.is_instance or source.type != "MESH" or not any(
            modifier.type == "ARRAY" for modifier in source.modifiers):
        return False
    base = _mesh_span(source.data)
    expanded = _mesh_span(evaluated_mesh)
    return sum(expanded[axis] > max(base[axis], 0.01) * 20
               for axis in range(2)) == 2


def _material_dependencies(material, renderer, cache):
    """Source materials are immutable while each evaluated mesh is analysed."""
    from .material_graph import analyze_material
    key = (material.as_pointer(), renderer)
    if key not in cache:
        cache[key] = analyze_material(material, renderer)
    return cache[key]


def _needs_generated_coordinates(materials, renderer, cache):
    # Generated is distinct from Object coordinates, normals and instance data.
    return any('generated' in node.get('data_domains', [])
               for material in materials if material and material.use_nodes
               for node in _material_dependencies(material, renderer, cache).get('nodes', []))


def _prepare_generated_coordinates(item, source, renderer, cache):
    """Store ORCO on the whole evaluated mesh, before cutting changes topology."""
    if not _needs_generated_coordinates(item['source_materials'], renderer, cache):
        return
    from .scene_shader_data import ensure_generated_layers
    if source is None:
        raise ValueError('缺少原始对象，无法保存 Generated 坐标')
    item['generated_channels'] = ensure_generated_layers(source, item['mesh'])
    item.pop('generated_error', None)


def _chunked_meshes(mesh, matrix, meters_per_unit):
    """Cut faces, interpolating existing loop UVs including original ORCO carriers."""
    import bmesh

    bm = bmesh.new()
    cut_mesh = None
    pieces = []
    try:
        bm.from_mesh(mesh)
        for vertex in bm.verts:
            vertex.co = matrix @ vertex.co
        low = [min(vertex.co[axis] for vertex in bm.verts) for axis in range(3)]
        high = [max(vertex.co[axis] for vertex in bm.verts) for axis in range(3)]
        axes = sorted(range(3), key=lambda axis: high[axis] - low[axis], reverse=True)[:2]
        axes = [axis for axis in axes if (high[axis] - low[axis]) * meters_per_unit >= 6.0]
        if not axes:
            return []
        edge = max(4.0, max(high[axis] - low[axis] for axis in axes) * meters_per_unit / 8.0) / meters_per_unit
        for axis in axes:
            boundary = low[axis] + edge
            while boundary < high[axis] - 1e-5:
                normal = [0.0, 0.0, 0.0]
                normal[axis] = 1.0
                origin = [0.0, 0.0, 0.0]
                origin[axis] = boundary
                bmesh.ops.bisect_plane(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                                       dist=1e-5, plane_co=origin, plane_no=normal)
                boundary += edge
        bm.faces.index_update()
        tiles = {}
        for face in bm.faces:
            center = face.calc_center_median()
            key = tuple(max(0, int((center[axis] - low[axis]) / edge)) for axis in axes)
            tiles.setdefault(key, set()).add(face.index)
        if len(tiles) < 2:
            return []
        inverse = matrix.inverted()
        for vertex in bm.verts:
            vertex.co = inverse @ vertex.co
        cut_mesh = mesh.copy()
        bm.to_mesh(cut_mesh)
        bm.free()
        bm = None
        for number, keep in enumerate(tiles.values(), 1):
            part = cut_mesh.copy()
            work = bmesh.new()
            try:
                work.from_mesh(part)
                work.faces.index_update()
                bmesh.ops.delete(work, geom=[face for face in work.faces if face.index not in keep],
                                 context='FACES')
                work.to_mesh(part)
                part.update()
            finally:
                work.free()
            if not part.polygons:
                bpy.data.meshes.remove(part)
                raise RuntimeError("分块后出现空网格")
            part.name = f"{mesh.name}_Chunk_{number:02d}"
            pieces.append(part)
        return pieces
    except Exception:
        for part in pieces:
            bpy.data.meshes.remove(part)
        raise
    finally:
        if bm is not None:
            bm.free()
        if cut_mesh is not None:
            bpy.data.meshes.remove(cut_mesh)


def _hash_mesh(mesh, material_ids, collision):
    digest = hashlib.sha256()
    coords = array("f", [0.0]) * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", coords)
    digest.update(coords.tobytes())
    counts = array('i', [0]) * len(mesh.polygons)
    indices = array('i', [0]) * len(mesh.loops)
    slots = array('i', [0]) * len(mesh.polygons)
    mesh.polygons.foreach_get('loop_total', counts)
    mesh.loops.foreach_get('vertex_index', indices)
    mesh.polygons.foreach_get('material_index', slots)
    digest.update(counts.tobytes())
    digest.update(indices.tobytes())
    digest.update(slots.tobytes())
    digest.update(json.dumps(material_ids, ensure_ascii=False).encode("utf-8"))
    digest.update(str(bool(collision)).encode("ascii"))
    for layer in mesh.uv_layers:
        digest.update(layer.name.encode("utf-8", errors="replace"))
        values = array("f", [0.0]) * (len(layer.data) * 2)
        layer.data.foreach_get("uv", values)
        digest.update(values.tobytes())
    for attribute in mesh.color_attributes:
        digest.update((attribute.name+attribute.domain+attribute.data_type).encode('utf-8'))
        values=array('f',[0.])*(len(attribute.data)*4)
        attribute.data.foreach_get('color',values)
        digest.update(values.tobytes())
    return digest.hexdigest()[:20]


def _spatial_material(material, seen=None):
    """Conservatively isolate evaluated instances with position-dependent shading."""
    if not material or not material.use_nodes:
        return False
    from .material_graph import analyze_material
    analysis=analyze_material(material,bpy.context.scene.render.engine)
    domains=set(analysis['domains'])
    if analysis['status']=='analyzed':
        if bpy.context.scene.render.engine=='CYCLES' and all(
                n['output']=='Random' for n in analysis['nodes'] if n['type']=='ShaderNodeObjectInfo'):
            if not any(n['capability']['route']=='local_cache' and 'instance' in n.get('data_domains',[])
                       for n in analysis['nodes']):
                domains.discard('instance')  # direct values are persisted separately
        # Object-local fields and supported source instance data can share.
        return bool(domains & {'world','instance','view','unknown','time'})
    def visit(tree, seen):
        if tree.as_pointer() in seen:
            return False
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type in ("NEW_GEOMETRY", "OBJECT_INFO", "TEX_NOISE", "TEX_VORONOI"):
                return True
            if node.type == "TEX_COORD" and any(output.is_linked for output in node.outputs if output.name != "UV"):
                return True
            if node.type == "GROUP" and node.node_tree and visit(node.node_tree, seen):
                return True
        return False
    return visit(material.node_tree, set())


def _mesh_data(mesh):
    import numpy as np
    from pxr import Vt
    mesh.calc_loop_triangles()
    def vectors(collection, attribute, width, constructor):
        buffer = np.empty(len(collection) * width, dtype=np.float32)
        collection.foreach_get(attribute, buffer)
        return constructor.FromNumpy(buffer.reshape((-1, width)))
    def integers(collection, attribute):
        buffer = np.empty(len(collection), dtype=np.int32)
        collection.foreach_get(attribute, buffer)
        return Vt.IntArray.FromNumpy(buffer)
    points = vectors(mesh.vertices, 'co', 3, Vt.Vec3fArray)
    counts = integers(mesh.polygons, 'loop_total')
    indices = integers(mesh.loops, 'vertex_index')
    triangles = len(mesh.loop_triangles)
    if len(points) < 3 or not counts or triangles < 1:
        return None
    uvs = []
    for index, layer in enumerate(mesh.uv_layers):
        uvs.append({
            "name": "st" if index == 0 else "st" + str(index),
            "source_name": layer.name,
            "values": vectors(layer.data, 'uv', 2, Vt.Vec2fArray),
        })
    normals = []
    try:
        normals = vectors(mesh.corner_normals, 'vector', 3, Vt.Vec3fArray)
    except (AttributeError, RuntimeError):
        pass
    colors=[]
    attribute=mesh.color_attributes.active_color
    if attribute:
        buffer=np.empty(len(attribute.data)*4,dtype=np.float32)
        attribute.data.foreach_get('color',buffer)
        rgba=buffer.reshape((-1,4))
        if attribute.domain=='POINT':rgba=rgba[np.asarray(indices,dtype=np.int32)]
        if len(rgba)==len(indices):
            colors={'name':attribute.name,'rgb':Vt.Vec3fArray.FromNumpy(rgba[:,:3].copy()),
                    'alpha':Vt.FloatArray.FromNumpy(rgba[:,3].copy())}
    return {"points": points, "counts": counts, "indices": indices,
            "triangles": triangles, "uvs": uvs, "normals": normals, 'colors':colors}


def _merge_owner(name, parts, owner_matrix):
    """Make one mesh, with material subsets, from an Outliner object's evaluated pieces."""
    import numpy as np
    from mathutils import Matrix
    from pxr import Vt

    points, counts, indices, normals = [], [], [], []
    colors,alphas=[],[]
    has_colors=any(group['geometry'].get('colors') for group,_ in parts)
    uvs = {}
    slots, faces, sources = [], [], set()
    vertex_offset = 0
    triangle_count = 0
    collision = parts[0][0]["collision"]
    if any(group["collision"] != collision for group, _ in parts):
        raise RuntimeError(f"Blender 物体的碰撞设置不一致: {name}")
    uv_names = {uv["name"]: uv["source_name"] for group, _ in parts
                for uv in group["geometry"]["uvs"]}
    for group, item in parts:
        data = group["geometry"]
        local = owner_matrix.inverted() @ Matrix(item["matrix"])
        linear = np.asarray([[local[row][col] for col in range(3)] for row in range(3)], dtype=np.float64)
        translation = np.asarray([local[row][3] for row in range(3)], dtype=np.float64)
        source_points = np.asarray(data["points"], dtype=np.float32)
        source_counts = np.asarray(data["counts"], dtype=np.int32)
        source_indices = np.asarray(data["indices"], dtype=np.int32)
        if np.linalg.det(linear) < 0:
            starts = np.repeat(np.cumsum(source_counts) - source_counts, source_counts)
            order = starts + np.repeat(source_counts, source_counts) - 1 - (np.arange(len(source_indices)) - starts)
        else:
            order = slice(None)
        points.append((source_points @ linear.T + translation).astype(np.float32))
        counts.append(source_counts)
        indices.append(source_indices[order] + vertex_offset)
        source_normals = np.asarray(data["normals"], dtype=np.float32)
        if len(source_normals) != len(source_indices):
            raise RuntimeError(f"Blender 物体缺少逐角法线: {name}")
        transformed = source_normals[order] @ np.linalg.inv(linear)
        transformed /= np.maximum(np.linalg.norm(transformed, axis=1, keepdims=True), 1e-20)
        normals.append(transformed.astype(np.float32))
        if has_colors:
            color=data.get('colors')
            colors.append(np.asarray(color['rgb'],dtype=np.float32)[order] if color else np.ones((len(source_indices),3),dtype=np.float32))
            alphas.append(np.asarray(color['alpha'],dtype=np.float32)[order] if color else np.ones(len(source_indices),dtype=np.float32))
        source_uvs = {uv["name"]: np.asarray(uv["values"], dtype=np.float32)
                      for uv in data["uvs"]}
        for uv_name in uv_names:
            values = source_uvs.get(uv_name)
            uvs.setdefault(uv_name, []).append(
                values[order] if values is not None else np.zeros((len(source_indices), 2), dtype=np.float32))
        material_remap = []
        for material_id in group["materials"] or [None]:
            if material_id not in slots:
                slots.append(material_id)
            material_remap.append(slots.index(material_id))
        faces.extend(material_remap[index] for index in group["polygon_materials"])
        sources.update(group["source_types"])
        vertex_offset += len(source_points)
        triangle_count += data["triangles"]
    geometry = {
        "points": Vt.Vec3fArray.FromNumpy(np.concatenate(points)),
        "counts": Vt.IntArray.FromNumpy(np.concatenate(counts)),
        "indices": Vt.IntArray.FromNumpy(np.concatenate(indices)),
        "normals": Vt.Vec3fArray.FromNumpy(np.concatenate(normals)),
        "uvs": [{"name": uv_name, "source_name": source_name,
                 "values": Vt.Vec2fArray.FromNumpy(np.concatenate(uvs[uv_name]))}
                for uv_name, source_name in uv_names.items()],
        "triangles": triangle_count,
    }
    identity = stable_id(name, *[group["id"] for group, _ in parts])
    if has_colors:geometry['colors']={'name':'displayColor','rgb':Vt.Vec3fArray.FromNumpy(np.concatenate(colors)),
                                     'alpha':Vt.FloatArray.FromNumpy(np.concatenate(alphas))}
    return {"id": identity, "geometry": geometry, "materials": slots,
            "polygon_materials": faces, "collision": collision,
            "instances": [], "source_types": sources}


def _write_material_layer(path, records, content_root, asset_id):
    from pxr import Sdf, Usd, UsdGeom, UsdShade
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Scope.Define(stage, "/World/Materials")
    for record in records:
        material = UsdShade.Material.Define(stage, "/World/Materials/M_" + record["id"])
        shader = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("UnrealShader"))
        name = "MI_" + record["asset_name"]
        asset = f"{content_root}/A_{asset_id}/Materials/{name}.{name}"
        shader.SetSourceAsset(Sdf.AssetPath(asset), "unreal")
        output = shader.CreateOutput("out", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput("unreal").ConnectToSource(output)
        material.GetPrim().SetCustomDataByKey("MMDBridge:strategy", record.get("strategy", "nodes"))
    stage.GetRootLayer().Save()


def _bind_material(prim, material_id, material_root):
    from pxr import Sdf, UsdShade
    UsdShade.MaterialBindingAPI.Apply(prim)
    prim.CreateRelationship("material:binding").SetTargets(
        [Sdf.Path(material_root + "/M_" + material_id)])


def _write_chunk(path, number, groups, meters_per_unit, exact_collision, content_root, asset_id,
                 job=None, definitions_only=False, material_names=None):
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
    started = time.perf_counter()
    def progress(message, **fields):
        if job is not None:
            check_stop(job)
            elapsed = round(time.perf_counter() - started, 2)
            memory = _peak_working_set_mb(current=True)
            message += f"；分块耗时 {elapsed:.1f}s"
            if memory is not None:
                message += f"；进程内存 {memory:.0f} MB"
            job.status('scene_export', message, payload=number + 1,
                       payload_elapsed_seconds=elapsed, working_set_mb=memory, **fields)
    # Anonymous layer avoids exposing an incomplete payload as a finished file.
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    root_path = f"/World/Payload_{number+1:02d}"
    root = UsdGeom.Xform.Define(stage, root_path)
    material_root = root_path + "/Materials"
    UsdGeom.Scope.Define(stage, material_root)
    for material_id in ([] if definitions_only else sorted(
            {item for group in groups for item in group["materials"] if item})):
        material = UsdShade.Material.Define(stage, material_root + "/M_" + material_id)
        shader = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("UnrealShader"))
        name = "MI_" + (material_names or {}).get(material_id, material_id)
        asset = f"{content_root}/A_{asset_id}/Materials/{name}.{name}"
        shader.SetSourceAsset(Sdf.AssetPath(asset), "unreal")
        output = shader.CreateOutput("out", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput("unreal").ConnectToSource(output)
    owner_mode = len(groups) == 1 and "owner_name" in groups[0]
    if not owner_mode:
        prototypes = UsdGeom.Scope.Define(stage, root_path + "/__Prototypes")
        prototypes.GetPrim().SetSpecifier(Sdf.SpecifierClass)
        UsdGeom.Scope.Define(stage, root_path + "/Instances")
    for group_index, group in enumerate(groups):
        progress(f"USD 分块 {number+1}：写入原型 {group_index+1}/{len(groups)}；"
                 f"{group['geometry']['triangles']} 三角形 / {len(group['instances'])} 实例",
                 prototype=group_index+1, prototypes=len(groups))
        proto_path = root_path if owner_mode else f"{root_path}/__Prototypes/Prototype_{group_index+1:02d}"
        group["prim_path"] = proto_path
        proto = UsdGeom.Xform.Define(stage, proto_path)
        if definitions_only:
            # Bind inside the referenced subtree: USD discards relationship
            # targets outside an external reference's scope.
            material_root = proto_path + "/Materials"
            UsdGeom.Scope.Define(stage, material_root)
            for material_id in sorted(set(group["materials"]) - {None}):
                material = UsdShade.Material.Define(stage, material_root + "/M_" + material_id)
                shader = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("UnrealShader"))
                name = "MI_" + (material_names or {}).get(material_id, material_id)
                asset = f"{content_root}/A_{asset_id}/Materials/{name}.{name}"
                shader.SetSourceAsset(Sdf.AssetPath(asset), "unreal")
                material.CreateSurfaceOutput("unreal").ConnectToSource(
                    shader.CreateOutput("out", Sdf.ValueTypeNames.Token))
        mesh = UsdGeom.Mesh.Define(stage, proto_path + "/Mesh")
        if group.get("disable_nanite"):
            # UE's Nanite fallback may reduce a large combined mesh to almost
            # nothing (lamps: 1.13M source triangles -> 886 fallback triangles).
            # A component-level Disallow Nanite still renders that fallback.
            for prim in (proto.GetPrim(), mesh.GetPrim()):
                prim.CreateAttribute("unrealNanite", Sdf.ValueTypeNames.Token,
                                     custom=False, variability=Sdf.VariabilityUniform).Set("disable")
        data = group["geometry"]
        mesh.CreatePointsAttr(data["points"])
        mesh.CreateFaceVertexCountsAttr(data["counts"])
        mesh.CreateFaceVertexIndicesAttr(data["indices"])
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        if data["normals"] and len(data["normals"]) == len(data["indices"]):
            mesh.CreateNormalsAttr(data["normals"])
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
        primvars = UsdGeom.PrimvarsAPI(mesh)
        colors=data.get('colors')
        if colors:
            primvars.CreatePrimvar('displayColor',Sdf.ValueTypeNames.Color3fArray,
                                   UsdGeom.Tokens.faceVarying).Set(colors['rgb'])
            primvars.CreatePrimvar('displayOpacity',Sdf.ValueTypeNames.FloatArray,
                                   UsdGeom.Tokens.faceVarying).Set(colors['alpha'])
        for uv in data["uvs"]:
            var = primvars.CreatePrimvar(uv["name"], Sdf.ValueTypeNames.TexCoord2fArray,
                                         UsdGeom.Tokens.faceVarying)
            var.Set(uv["values"])
            var.GetAttr().SetCustomDataByKey("MMDBridge:sourceName", uv["source_name"])
        material_ids = group["materials"]
        polygon_materials = group["polygon_materials"]
        if len(material_ids) == 1 and material_ids[0]:
            _bind_material(mesh.GetPrim(), material_ids[0], material_root)
        elif material_ids:
            faces_by_slot = {}
            for face, slot in enumerate(polygon_materials):
                faces_by_slot.setdefault(slot, []).append(face)
            for slot, material_id in enumerate(material_ids):
                if not material_id:
                    continue
                faces = faces_by_slot.get(slot, [])
                if not faces:
                    continue
                subset = UsdGeom.Subset.Define(stage, mesh.GetPath().AppendChild(f"Material_{slot}"))
                subset.CreateElementTypeAttr(UsdGeom.Tokens.face)
                subset.CreateFamilyNameAttr("materialBind")
                subset.CreateIndicesAttr(faces)
                _bind_material(subset.GetPrim(), material_id, material_root)
            UsdGeom.Subset.SetFamilyType(mesh, "materialBind", UsdGeom.Tokens.partition)
        collision = bool(group["collision"] and exact_collision)
        if collision:
            UsdPhysics.CollisionAPI.Apply(mesh.GetPrim()).CreateCollisionEnabledAttr(True)
            UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr("none")
        mesh.GetPrim().SetCustomDataByKey("MMDBridge:topologyHash", group["id"])
        proto.GetPrim().SetInstanceable(False)
    if owner_mode or definitions_only:
        temporary = Path(str(path) + '.partial.usdc')
        if not stage.GetRootLayer().Export(str(temporary)):
            raise RuntimeError(f'USD 保存失败: {temporary}')
        os.replace(temporary, path)
        return root_path
    layer = stage.GetRootLayer()
    # Never call Usd APIs inside ChangeBlock. Sdf batching prevents rebuilding
    # the stage's entire instance composition after every reference mutation.
    total = sum(len(group['instances']) for group in groups)
    written = 0
    with Sdf.ChangeBlock():
        for group in groups:
            for item in group['instances']:
                if written % 50 == 0:
                    progress(f"USD 分块 {number+1}：写入实例 {written}/{total}", instances_written=written)
                prim_path = f"{root_path}/Instances/I_{item['index']:07d}_{_name(item['name'])}"
                spec = Sdf.CreatePrimInLayer(layer, prim_path)
                spec.specifier = Sdf.SpecifierDef
                spec.typeName = 'Xform'
                spec.referenceList.prependedItems = [Sdf.Reference('', Sdf.Path(group['prim_path']))]
                spec.instanceable = True
                Sdf.AttributeSpec(spec, 'xformOp:transform', Sdf.ValueTypeNames.Matrix4d).default = _matrix(item['matrix'])
                Sdf.AttributeSpec(spec, 'xformOpOrder', Sdf.ValueTypeNames.TokenArray,
                                  Sdf.VariabilityUniform).default = ['xformOp:transform']
                spec.customData = {'MMDBridge': {'sourceObject': item['name'], 'sourceId': item['source_id']}}
                item['prim_path'] = prim_path
                written += 1
        progress(f"USD 分块 {number+1}：实例 {written}/{total}，正在提交 USD 组合")
    progress(f"USD 分块 {number+1}：正在保存文件（{written} 实例）")
    temporary = Path(str(path) + '.partial.usdc')
    if not layer.Export(str(temporary)):
        raise RuntimeError(f'USD 保存失败: {temporary}')
    if job is not None:
        check_stop(job)
    os.replace(temporary, path)
    return root_path


def _write_geometry_index(path, chunks, meters_per_unit, camera_samples=None, fps=30,
                          hierarchy=None):
    from pxr import Kind, Sdf, Usd, UsdGeom
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    if hierarchy is None:
        UsdGeom.Scope.Define(stage, "/World/Chunks")
        for chunk in chunks:
            prim = UsdGeom.Xform.Define(stage, f"/World/Chunks/Chunk_{chunk['index']:03d}")
            prim.GetPrim().GetPayloads().AddPayload(
                Sdf.Payload("./Geometry/" + Path(chunk["path"]).name, chunk["root_prim"]))
    else:
        UsdGeom.Scope.Define(stage, "/World/Objects")
        # A geometry leaf can share a native USD prototype. Parent objects with
        # authored child objects must remain editable outside that prototype.
        object_ancestors = set()
        for item in hierarchy:
            ancestor = Sdf.Path(item['prim_path']).GetParentPath()
            while str(ancestor) not in ('/', ''):
                object_ancestors.add(str(ancestor))
                ancestor = ancestor.GetParentPath()
        instance_paths = []
        for item in hierarchy:
            prim = UsdGeom.Xform.Define(stage, item["prim_path"])
            Usd.ModelAPI(prim.GetPrim()).SetKind(Kind.Tokens.group)
            prim.AddTransformOp().Set(_matrix(item["local_matrix"]))
            prim.GetPrim().SetCustomDataByKey("MMDBridge:sourceObject", item["name"])
            prim.GetPrim().SetMetadata("displayName", item["name"])
            if item.get("parts") is not None:
                for part in item["parts"]:
                    target = prim if part["prim_path"] == item["prim_path"] else UsdGeom.Xform.Define(
                        stage, part["prim_path"])
                    if part["prim_path"] != item["prim_path"]:
                        target.AddTransformOp().Set(_matrix(part["local_matrix"]))
                        target.GetPrim().SetMetadata("displayName", part["label"])
                    target.GetPrim().GetReferences().AddReference(
                        "./Geometry/" + Path(chunks[part["chunk"]]["path"]).name,
                        part["source_prim"])
                    if part['prim_path'] not in object_ancestors:
                        instance_paths.append(part['prim_path'])
                continue
            if item.get("chunk") is not None:
                chunk = chunks[item["chunk"]]
                prim.GetPrim().GetPayloads().AddPayload(
                    Sdf.Payload("./Geometry/" + Path(chunk["path"]).name, chunk["root_prim"]))
        # Batch Sdf edits; changing thousands of instances through Usd APIs one
        # by one repeatedly rebuilds composition. Do not call Usd inside this.
        layer = stage.GetRootLayer()
        with Sdf.ChangeBlock():
            for instance_path in instance_paths:
                layer.GetPrimAtPath(instance_path).instanceable = True
    if camera_samples:
        stage.SetTimeCodesPerSecond(float(fps))
        stage.SetFramesPerSecond(float(fps))
        stage.SetStartTimeCode(float(camera_samples[0]["frame"]))
        stage.SetEndTimeCode(float(camera_samples[-1]["frame"]))
        camera = UsdGeom.Camera.Define(stage, "/World/Cameras/MMD_Camera")
        transform = camera.AddTransformOp()
        focal = camera.CreateFocalLengthAttr()
        horizontal = camera.CreateHorizontalApertureAttr()
        vertical = camera.CreateVerticalApertureAttr()
        for sample in camera_samples:
            frame = float(sample["frame"])
            matrix = [list(row) for row in sample["matrix"]]
            for axis in range(3):
                matrix[axis][3] /= meters_per_unit
            transform.Set(_matrix(matrix), frame)
            focal.Set(float(sample["lens"]), frame)
            horizontal.Set(float(sample["sensor_width"]), frame)
            vertical.Set(float(sample["sensor_height"]), frame)
        projections = {sample["projection"] for sample in camera_samples}
        if len(projections) != 1:
            raise RuntimeError("VMD 镜头混合透视与正交投影，USD Camera 无法无损表示")
        camera.CreateProjectionAttr().Set(
            UsdGeom.Tokens.orthographic if "ORTHO" in projections else UsdGeom.Tokens.perspective)
    stage.GetRootLayer().Save()


def _write_user_root(path, meters_per_unit, camera_samples=None, fps=30):
    from pxr import Sdf, Usd, UsdGeom
    if path.exists():
        if not camera_samples:
            return False
        stage = Usd.Stage.Open(str(path), load=Usd.Stage.LoadNone)
        created = False
    else:
        stage = Usd.Stage.CreateNew(str(path))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
        stage.GetRootLayer().subLayerPaths = ["./Geometry.usda", "./ToolMaterials.usda"]
        stage.GetRootLayer().customLayerData = {
            "MMDBridge": "User override root. Re-export replaces only tool-owned sublayers."
        }
        created = True
    if camera_samples:
        stage.SetTimeCodesPerSecond(float(fps))
        stage.SetFramesPerSecond(float(fps))
        stage.SetStartTimeCode(float(camera_samples[0]["frame"]))
        stage.SetEndTimeCode(float(camera_samples[-1]["frame"]))
    stage.GetRootLayer().Save()
    return created


def export_usd_scene(job):
    started = time.perf_counter()
    try:
        from pxr import Usd  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("当前 Blender 缺少 USD Python 模块，无法安全回退到代理网格") from exc

    data, cfg = job.data, job.data["settings"]
    appearance_enabled = cfg.get('restore_scene_appearance', False)
    if appearance_enabled:
        from . import blender_appearance as appearance
        try:
            native_capabilities = appearance.capabilities()
        except ImportError as exc:
            raise RuntimeError('转换前请部署包含 OpenVDB 和 OpenColorIO 的 Blender 5.2；缺少: '+str(exc)) from exc
    existing = job.folder / "manifest.json"
    prior = read_json(existing) if existing.is_file() else {}
    camera_samples = []
    camera_source = None
    if cfg.get("camera") and cfg.get("character_backend") == "mmdbridge_fbx":
        camera_source = Path(prior.get("camera_samples", ""))
        if not camera_source.is_file() or not camera_source.resolve().is_relative_to(job.folder.resolve()):
            raise RuntimeError("本次角色任务缺少可信的 VMD 镜头逐帧采样")
        camera_samples = read_json(camera_source)
        if not camera_samples or any(camera_samples[i]["frame"] >= camera_samples[i+1]["frame"]
                                     for i in range(len(camera_samples)-1)):
            raise RuntimeError("VMD 镜头采样为空或帧顺序无效")
    job.status("scene_export", "读取 Blender 最终求值几何并生成 USD Payload")
    bpy.ops.wm.open_mainfile(filepath=cfg["scene"], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    frame = int(cfg.get("scene_frame", 1))
    scene.frame_set(frame)
    selected_objects = set(cfg.get("scene_object_filter") or [])
    if selected_objects:
        missing = selected_objects - {obj.name for obj in scene.objects}
        if missing:
            raise ValueError("场景筛选物体不存在: " + ", ".join(sorted(missing)))
    source_renderer = scene.render.engine
    from .blender_appearance import expose_material_output
    for material in bpy.data.materials:
        expose_material_output(material, source_renderer)
    export_root = Path(cfg.get("character_output_dir") or cfg.get("output_override") or
                       Path(cfg["scene"]).parent) / ("Scene_" + data["id"])
    package = export_root / "Scene"
    geometry_folder = package / "Geometry"
    geometry_folder.mkdir(parents=True, exist_ok=True)
    materials = Materials(package, cfg["scene"], int(cfg.get("bake_resolution", 0)),
                          strategy=cfg.get("material_strategy", "auto"),
                          bake_fallback=cfg.get("material_bake_fallback", True),
                          complex_strategy=cfg.get('material_complex_strategy', 'pbr_bake'),
                          force_bake=job.data.get('material_bake_retry',{}),
                          conversion_timeout=cfg.get('material_conversion_timeout', 120.0))
    materials.progress = job.status
    materials.restore_scene_appearance = appearance_enabled
    if appearance_enabled:
        def appearance_progress(state,message):
            appearance.check_resources(job)
            job.status(state,message)
        materials.progress = appearance_progress
    meters_per_unit = float(scene.unit_settings.scale_length) * float(cfg.get("scene_scale", 1.0))
    materials.meters_per_unit = meters_per_unit
    materials.isolated_baking = True
    render_optimized = cfg.get('scene_import_profile') == 'render_optimized'
    exact_collision = bool(cfg.get("exact_collision", True)) and not render_optimized
    warnings = []
    skipped_items = []
    missing_external = missing_external_files()
    warnings.extend(f"场景引用的外部文件不存在：{path}；求值结果可能不完整" for path in missing_external)
    authored = getattr(bpy.data, "version", None)
    if authored and tuple(authored) > tuple(bpy.app.version):
        warnings.append(
            f"源文件由 Blender {'.'.join(map(str, authored))} 保存；当前使用 "
            f"{'.'.join(map(str, bpy.app.version))} 读取，已继续但需检查版本差异")

    groups = {}
    checkpoint_path = job.folder / 'scene_checkpoint.json'
    visible_bounds = None
    export_settings = {k: v for k, v in cfg.items() if k not in ('project', 'unreal', 'ue_vram_pause_percent', 'ue_vram_resume_percent', 'material_bake_fallback', 'material_complex_strategy', 'material_conversion_timeout', 'scene_viewport_ocio')}
    checkpoint_key = stable_id(data['inputs'].get('scene'), bpy.app.version_string,
                               json.dumps(export_settings, sort_keys=True), 'frozen-render-v4-coverage')
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else None
    if checkpoint and checkpoint.get('key') != checkpoint_key:
        raise ValueError('场景还原点与素材、参数或 Blender 版本不一致，请新建任务')
    lights_and_cameras = []
    usable, frozen_volumes = [], []
    freeze_analysis_stats={'spatial_queries':0,'spatial_cache_hits':0,'spatial_materials':0}
    material_dependencies = {}

    def freeze_geometry(deps):
        nonlocal usable, frozen_volumes, warnings, skipped_items
        # One immutable depsgraph snapshot owns this cache. Do not carry it
        # across frame changes or later node edits during material conversion.
        spatial_cache={};channel_cache={}
        def spatial_cached(material):
            if material is None:return False
            freeze_analysis_stats['spatial_queries']+=1
            key=material.as_pointer()
            if key in spatial_cache:
                freeze_analysis_stats['spatial_cache_hits']+=1
            else:
                spatial_cache[key]=_spatial_material(material)
                freeze_analysis_stats['spatial_materials']+=1
            return spatial_cache[key]
        def channels_cached(material):
            key=material.as_pointer() if material else 0
            if key not in channel_cache:channel_cache[key]=appearance.material_channels(material,source_renderer)
            return channel_cache[key]
        # DepsgraphObjectInstance wrappers are ephemeral RNA views.  Materializing
        # them with list() invalidates earlier entries while Blender advances the
        # iterator, so consume the iterator directly.
        instances = deps.object_instances
        try:
            total = len(instances)
        except TypeError:
            total = sum(1 for _ in instances)
            instances = deps.object_instances
        render_visible = render_visible_objects(scene)
        for original in scene.objects:
            if selected_objects and original.name not in selected_objects:
                continue
            if original.name not in render_visible or original.type not in ("LIGHT", "CAMERA"):
                continue
            evaluated = original.evaluated_get(deps)
            entry = {"id": stable_id(cfg["scene"], original.name), "name": original.name,
                     "type": original.type, "matrix": [list(row) for row in evaluated.matrix_world],
                     "visible": True}
            if original.type == "LIGHT":
                entry["light"] = {"type": original.data.type, "color": list(original.data.color),
                                  "energy": original.data.energy, "size": getattr(original.data, "size", 1),
                                  "size_y": getattr(original.data, "size_y", getattr(original.data, "size", 1)),
                                  "spot_size": getattr(original.data, "spot_size", 1)}
            else:
                entry["camera"] = {"lens": original.data.lens, "sensor_width": original.data.sensor_width,
                                   "sensor_height": original.data.sensor_height, "projection": original.data.type}
            if appearance_enabled and original.type == 'LIGHT':
                entry['light'] = appearance.light_record(evaluated, source_renderer, meters_per_unit)
            if appearance_enabled and original.type == 'CAMERA':
                entry['camera'] = appearance.camera_record(evaluated,original == scene.camera)
            lights_and_cameras.append(entry)

        if checkpoint:
            job.status('scene_export', '恢复冻结几何还原点，跳过场景求值和已完成的分块')
            usable, frozen_volumes = _load_frozen(job, checkpoint, materials)
            warnings = checkpoint['warnings']
            skipped_items = checkpoint['skipped_items']
        else:
            # Pass 1 freezes evaluated meshes and transforms without linking any helper
            # object to the scene.  Linking while iterating object_instances mutates the
            # dependency graph and can recursively enumerate the export helpers.
            frozen_volumes = []
            visible_bounds = None
            freeze_state, restored = _load_freeze_batches(job, checkpoint_key, frozen_volumes,
                                                           lights_and_cameras, warnings, skipped_items)
            frozen = {('restored', index): item for index, item in restored.items()}
            resume_cursor = freeze_state['cursor']
            if resume_cursor or freeze_state['complete']:
                job.status('scene_export', f'恢复冻结阶段断点 {resume_cursor}/{total}；已保存网格 {len(restored)}')
            freeze_digest = hashlib.sha256()
            last_freeze_save = time.monotonic()
            processed = 0
            for index, instance in enumerate(() if freeze_state['complete'] else instances):
                if index >= resume_cursor and (index - freeze_state['cursor'] >= 500 or time.monotonic() - last_freeze_save >= 5):
                    _save_freeze_batch(job, checkpoint_key, freeze_state, index, freeze_digest.hexdigest(),
                                       frozen_volumes, lights_and_cameras, warnings, skipped_items)
                    last_freeze_save = time.monotonic()
                check_stop(job)
                freeze_digest.update(_instance_signature(instance))
                processed = index + 1
                if index < resume_cursor:
                    item = restored.get(index)
                    if item is not None:
                        evaluated = instance.object
                        pointer = evaluated.data.as_pointer() if getattr(evaluated, 'data', None) else evaluated.as_pointer()
                        frozen.pop(('restored', index))
                        frozen[(pointer, *_tuple_key(item['_freeze_key']))] = item
                    if processed == resume_cursor and freeze_digest.hexdigest() != freeze_state['digest']:
                        raise ValueError('场景实例顺序或变换与冻结断点不一致，不能安全恢复，请新建任务')
                    continue
                if index % 50 == 0:
                    job.status("scene_export", f"冻结场景最终求值结果 {index+1}/{total}")
                evaluated = instance.object
                source = getattr(evaluated, "original", evaluated)
                owner = getattr(getattr(instance, "parent", None), "original", None) or source
                if (not instance.is_instance and source.is_instancer and not source.show_instancer_for_render):
                    # Particle/collection emitters can be hidden while their instances render.
                    continue
                if instance.is_instance and not instance.show_self:
                    continue
                if selected_objects and owner.name not in selected_objects:
                    continue
                if appearance_enabled and instance.is_instance and evaluated.type == 'LIGHT' and owner.name in render_visible:
                    lights_and_cameras.append({'id':stable_id(cfg['scene'],owner.name,index,'light_instance'),
                        'name':f'{owner.name} / {source.name} / {index}', 'type':'LIGHT',
                        'matrix':[list(r) for r in instance.matrix_world], 'visible':True,
                        'light':appearance.light_record(evaluated,source_renderer,meters_per_unit)})
                    continue
                if appearance_enabled and evaluated.type == 'VOLUME' and owner.name in render_visible:
                    try:
                        frozen_volumes.append(appearance.freeze_vdb(evaluated,instance.matrix_world,
                            dict(cfg,source_renderer=source_renderer),index))
                    except (ValueError,RuntimeError) as exc:
                        skipped_items.append({'stage':'volume','name':owner.name,'reason':str(exc),'status':'needs_review'})
                    continue
                if evaluated.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META", "CURVES"):
                    continue
                if owner.hide_render or (not instance.is_instance and source.hide_render):
                    continue
                if owner.name not in render_visible:
                    continue
                source_materials = [getattr(slot.material, 'original', slot.material) for slot in evaluated.material_slots]
                if appearance_enabled:
                    channels = [channels_cached(mat) for mat in source_materials]
                    volume_sockets = [s for surface,s in channels if s is not None]
                    if volume_sockets:
                        volume_mesh = (evaluated.data.copy() if evaluated.type == 'MESH' else
                                       bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=deps))
                        for slot, material in enumerate(source_materials):
                            volume_mesh.materials[slot] = material
                        lo = [min(v.co[i] for v in volume_mesh.vertices) for i in range(3)]
                        hi = [max(v.co[i] for v in volume_mesh.vertices) for i in range(3)]
                        volume_record = {'id':stable_id(cfg['scene'],owner.name,index,'volume'), 'name':owner.name,
                                         'kind':'local_grid', 'bounds':[lo,hi], 'matrix':[list(r) for r in instance.matrix_world],
                                         'surface_present':any(surface for surface,s in channels)}
                        used_slots = {p.material_index for p in volume_mesh.polygons}
                        if len(volume_sockets) == 1 and all(i < len(channels) and channels[i][1] is volume_sockets[0] for i in used_slots):
                            volume_slot = next(i for i, (_, socket) in enumerate(channels) if socket is volume_sockets[0])
                            volume_record.update(material=source_materials[volume_slot].name, material_slot=volume_slot)
                            frozen_volumes.append((volume_record,volume_sockets[0],volume_mesh))
                        else:
                            bpy.data.meshes.remove(volume_mesh)
                            skipped_items.append({'stage':'volume','name':owner.name,'reason':'分区体积材质的区域边界需要复核','status':'needs_review'})
                        if not any(surface for surface,s in channels):
                            continue
                overrides = [value for value in (source.get('mmd_bridge_collision'), owner.get('mmd_bridge_collision')) if value is not None]
                collision_override = ('none' if 'none' in [str(v).lower() for v in overrides] else
                                      'exact' if 'exact' in [str(v).lower() for v in overrides] else None)
                collision = exact_collision and collision_override != 'none'
                pointer = evaluated.data.as_pointer() if getattr(evaluated, "data", None) else evaluated.as_pointer()
                surface_class = str(owner.get("mmd_bridge_surface_class", source.get("mmd_bridge_surface_class", "auto")))
                material_override = owner.get("mmd_bridge_material_strategy", source.get("mmd_bridge_material_strategy"))
                spatial = any(spatial_cached(slot.material) for slot in evaluated.material_slots)
                transform_key = tuple(float(v) for row in instance.matrix_world for v in row) if spatial else ()
                source_key = (pointer,
                              tuple(slot.material.name if slot.material else "" for slot in evaluated.material_slots),
                              collision, collision_override, surface_class, material_override, transform_key)
                item = frozen.get(source_key)
                if item is None:
                    try:
                        # new_from_object re-evaluates modifiers using the instance's
                        # transform. Mirror/Array can then shift local vertices while
                        # instance.matrix_world still applies the same transform.
                        mesh = (evaluated.data.copy() if evaluated.type == "MESH" else
                                bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=deps))
                    except Exception as exc:
                        reason = f"{type(exc).__name__}: {exc}"
                        warnings.append(f"物体 {source.name} 无法取得最终求值几何，已跳过：{reason}")
                        skipped_items.append({"stage": "scene_geometry", "name": source.name,
                                              "source": cfg["scene"], "reason": reason})
                        frozen[source_key] = False
                        continue
                    if _scene_wide_instance_array(instance, source, mesh):
                        bpy.data.meshes.remove(mesh)
                        # The instance wrapper can contain the entire array of poles.
                        # Evaluate the source once to retain its local ring/array, then
                        # apply each depsgraph instance transform exactly once.
                        mesh = source.evaluated_get(deps).data.copy()
                        warning = f"几何节点实例 {source.name} 的 Array 网格跨越多个实例位置；使用局部求值网格"
                        if warning not in warnings:
                            warnings.append(warning)
                    # Render-evaluated materials expire with the graph; retain source IDs.
                    for slot, material in enumerate(source_materials):
                        mesh.materials[slot] = material
                    # Capture the native shader coordinate domain before filtering
                    # faces, chunking or recalculating the final mesh's bounds.
                    if _needs_generated_coordinates(source_materials, source_renderer, material_dependencies):
                        try:
                            from .scene_shader_data import ensure_generated_layers
                            ensure_generated_layers(source, mesh)
                        except ValueError as exc:
                            mesh['_mmd_generated_error'] = str(exc)
                    if appearance_enabled and channels and any(not surface for surface,s in channels):
                        # A material without Surface contributes no visible shell or collision faces.
                        import bmesh
                        edit=bmesh.new()
                        edit.from_mesh(mesh)
                        hidden=[f for f in edit.faces if f.material_index<len(channels) and not channels[f.material_index][0]]
                        bmesh.ops.delete(edit,geom=hidden,context='FACES')
                        edit.to_mesh(mesh)
                        edit.free()
                    mesh.update()
                    if len(mesh.vertices) < 3 or not mesh.polygons:
                        bpy.data.meshes.remove(mesh)
                        frozen[source_key] = False
                        warnings.append(f"物体 {source.name} 为空网格或无多边形，已跳过")
                        skipped_items.append({"stage": "scene_geometry", "name": source.name,
                                              "source": cfg["scene"], "reason": "空网格或无多边形"})
                        continue
                    item = {"mesh": mesh, "source_name": source.name,
                            "_freeze_index": index, "_freeze_key": source_key[1:],
                            "surface_class": surface_class, "material_override": material_override,
                            "spatial": spatial,
                            "source_materials": list(mesh.materials),
                            "collision": collision,
                            "collision_override": collision_override,
                            "instances": [], "source_types": set()}
                    if mesh.get('_mmd_generated_error'):
                        item['generated_error'] = mesh['_mmd_generated_error']
                    frozen[source_key] = item
                    freeze_state['new_items'].append(item)
                if item is False:
                    continue
                matrix = instance.matrix_world.copy()
                item["instances"].append({"name": owner.name,
                                          "source_name": item["source_name"],
                                          "source_id": stable_id(cfg["scene"], owner.name),
                                          "matrix_object": matrix,
                                          "shader_data":{'object_random':object_random(
                                              source.name,instance.random_id if instance.is_instance else None)}})
                item["source_types"].add("INSTANCE" if instance.is_instance else evaluated.type)
                freeze_state['new_instances'].append({
                    'item': item['_freeze_index'], 'type': 'INSTANCE' if instance.is_instance else evaluated.type,
                    'instance': dict(item['instances'][-1], matrix_object=[list(row) for row in matrix])})

            if not freeze_state['complete']:
                if processed < resume_cursor:
                    raise ValueError('场景实例数量小于冻结断点，不能安全恢复，请新建任务')
                _save_freeze_batch(job, checkpoint_key, freeze_state, processed, freeze_digest.hexdigest(),
                                   frozen_volumes, lights_and_cameras, warnings, skipped_items, complete=True)
            usable = [item for item in frozen.values() if item is not False]

        _restore_shader_instance_data(job,usable,deps,checkpoint)

    from .blender_render import with_render_depsgraph
    job.status('scene_export', '按渲染设置求值修改器、粒子和几何节点实例')
    with_render_depsgraph(scene, freeze_geometry)
    if checkpoint:
        # Validate frozen coordinate carriers before archiving material analysis.
        _ensure_analysis_revision(job, checkpoint, materials.bake_fallback, materials.complex_strategy, materials.force_bake, materials.conversion_timeout)
    if not checkpoint:
        bounds_path = job.folder / 'scene_checkpoint/Bounds.json.gz'
        bounds_state = (_read_compressed_json(bounds_path) if bounds_path.exists() else
                        dict(key=checkpoint_key, completed=[], instance_index=0, item_size=[0.0]*3,
                             first_bounds=None, world_bounds=[[math.inf]*3, [-math.inf]*3],
                             material_world_bounds={}, measured_instances=0))
        if bounds_state['key'] != checkpoint_key or len(bounds_state['completed']) > len(usable):
            raise ValueError('尺寸统计断点与场景不一致，请新建任务')
        world_low, world_high = bounds_state['world_bounds']
        materials.material_world_bounds = bounds_state['material_world_bounds']
        bounds_started = time.monotonic()
        last_bounds_progress = bounds_started
        last_bounds_save = bounds_started
        measured_instances = bounds_state['measured_instances']
        total_instances = sum(len(item['instances']) for item in usable)
        job.status('scene_export', f"统计场景和物体尺寸 {len(bounds_state['completed'])}/{len(usable)}；"
                   f'恢复实例 {measured_instances}/{total_instances}')
        for index, item in enumerate(usable):
            check_stop(job)
            if index < len(bounds_state['completed']):
                saved = bounds_state['completed'][index]
                item.update(collision_size=saved['size'], first_bounds=saved['bounds'])
                continue
            points = _mesh_points(item['mesh'])
            item_size = bounds_state['item_size']
            if bounds_state['first_bounds'] is not None:
                item['first_bounds'] = bounds_state['first_bounds']
            for instance_index, instance in enumerate(item['instances']):
                if instance_index < bounds_state['instance_index']:
                    continue
                if instance_index % 100 == 0:
                    check_stop(job)
                    now = time.monotonic()
                    if now - last_bounds_progress >= 1:
                        job.status('scene_export', f'统计场景和物体尺寸 {index+1}/{len(usable)}；'
                                   f'实例 {measured_instances}/{total_instances}；耗时 {now-bounds_started:.1f}s')
                        last_bounds_progress = now
                    if now - last_bounds_save >= 5:
                        bounds_state.update(instance_index=instance_index, item_size=item_size,
                                            first_bounds=item.get('first_bounds'), measured_instances=measured_instances)
                        _write_compressed_json(bounds_path, bounds_state)
                        last_bounds_save = time.monotonic()
                bounds = _bounds(points, instance['matrix_object'])
                if instance_index == 0:
                    item['first_bounds'] = bounds
                measured_instances += 1
                for axis in range(3):
                    world_low[axis] = min(world_low[axis], bounds['min'][axis])
                    world_high[axis] = max(world_high[axis], bounds['max'][axis])
                    item_size[axis] = max(item_size[axis], bounds['size'][axis])
                for material in item['source_materials']:
                    if material:
                        low, high = materials.material_world_bounds.setdefault(material.name, ([math.inf]*3, [-math.inf]*3))
                        for axis in range(3):
                            low[axis] = min(low[axis], bounds['min'][axis])
                            high[axis] = max(high[axis], bounds['max'][axis])
            item['collision_size'] = item_size
            bounds_state['completed'].append({'size': item_size, 'bounds': item['first_bounds']})
            bounds_state.update(instance_index=0, item_size=[0.0]*3, first_bounds=None,
                                measured_instances=measured_instances)
            if len(bounds_state['completed']) % 500 == 0 or time.monotonic() - last_bounds_save >= 5:
                _write_compressed_json(bounds_path, bounds_state)
                last_bounds_save = time.monotonic()
            del points  # Release the vertex buffer before processing the next mesh.
        _write_compressed_json(bounds_path, bounds_state)
        job.status('scene_export', f'统计场景和物体尺寸 {len(usable)}/{len(usable)}；'
                   f'实例 {measured_instances}/{total_instances}；耗时 {time.monotonic()-bounds_started:.1f}s')
        materials.world_bounds = (world_low, world_high) if usable else ([0]*3, [0]*3)
        scene_size = [hi - lo for lo, hi in zip(*materials.world_bounds)]
        excluded = 0
        for item in usable:
            enabled = scene_collision_enabled(item['collision'], item['collision_size'], scene_size,
                                              cfg.get('collision_min_scene_ratio', .005), item['collision_override'])
            excluded += int(item['collision'] and not enabled)
            item['collision'] = enabled
        job.status('scene_export', f'相对尺寸碰撞筛选：{excluded}/{len(usable)} 个小网格不生成碰撞；开始材质分块与烘焙')
        usage={}
        for item in usable:
            for name in set(item.get('source_material_names',[m.name if m else None for m in item['source_materials']])):
                if name:usage[name]=usage.get(name,0)+len(item['instances'])
        for item in usable:
            item['material_usage']={name:usage[name] for name in item.get('source_material_names',
                [m.name if m else None for m in item['source_materials']]) if name in usage}
        expanded = []
        for item in usable:
            check_stop(job)
            try:
                _prepare_generated_coordinates(item, bpy.data.objects.get(item['source_name']), source_renderer, material_dependencies)
            except ValueError as exc:
                item['generated_error'] = str(exc)
                warnings.append(item['source_name'] + '：' + str(exc) + '；相关字段需要复核')
            mesh = item["mesh"]
            matrix = item["instances"][0]["matrix_object"]
            bounds = item.pop('first_bounds')
            size = [span * meters_per_unit for span in bounds["size"]]
            uv = mesh.uv_layers[0] if mesh.uv_layers else None
            uv_span = ([max((loop.uv[axis] for loop in uv.data), default=1) -
                        min((loop.uv[axis] for loop in uv.data), default=0) for axis in range(2)]
                       if uv else [1, 1])
            policy = classify_surface(size, uv_span, override=item["surface_class"],
                                      planar=min(size) < 0.02)
            names=[name for name in item.get('source_material_names',[m.name if m else None for m in item['source_materials']]) if name]
            repeated=bool(names) and all(usage.get(name,1)>=8 for name in names)
            forced_bake=item.get('material_override') in ('bake','baked') or cfg.get('material_strategy')=='bake'
            node_override=item.get('material_override')=='nodes' or cfg.get('material_strategy')=='nodes' or cfg.get('material_complex_strategy')=='nodes'
            repeated=repeated and not forced_bake
            if (node_override and not forced_bake) or repeated or policy["bake_category"] != "chunked" or (not forced_bake and not any(
                    material and material.use_nodes and not materials.simple_surface(material)
                    for material in item["source_materials"])):
                expanded.append(item)
                continue
            try:
                pieces = _chunked_meshes(mesh, matrix, meters_per_unit)
            except Exception as exc:
                warnings.append(f"物体 {item['source_name']} 分块失败，使用原网格继续导出：{type(exc).__name__}: {exc}")
                expanded.append(item)
                continue
            if not pieces:
                expanded.append(item)
                continue
            for piece in pieces:
                expanded.append({**item, "mesh": piece, "surface_class": "chunked",
                                 "auto_chunked": True,
                                 "instances": [dict(instance) for instance in item["instances"]]})
            bpy.data.meshes.remove(mesh)
            job.status("scene_export", f"大型立体物体 {item['source_name']} 已分为 {len(pieces)} 块独立烘焙")
        usable = expanded
        job.status('scene_export', '保存冻结几何还原点')
        for item in usable:
            item.pop('_freeze_index', None)
            item.pop('_freeze_key', None)
        checkpoint = _save_frozen(job, usable, frozen_volumes, materials, warnings, skipped_items, checkpoint_key)
    # Consolidation has committed: retain one compressed native snapshot, rather
    # than both it and the temporary freezing batches. Only task-owned files.
    for path in (job.folder / 'scene_checkpoint').iterdir():
        if re.fullmatch(r'Freeze_\d{6}(\.partial)?\.blend|Freeze_\d{6}\.json\.gz(\.partial)?', path.name) or path.name in ('Bounds.json.gz', 'Bounds.json.gz.partial'):
            path.unlink()
    exported = _restore_analyzed(job, checkpoint, groups, materials)
    completed_meshes = len(checkpoint['completed'])
    if completed_meshes:
        job.status('scene_export', f'恢复已分析网格 {completed_meshes}/{len(usable)}；从下一未完成网格继续')
    analysis_progress = time.monotonic()
    analysis_times = {'materials': 0.0, 'geometry': 0.0, 'checkpoint': 0.0}
    for frozen_index, item in enumerate(usable):
        check_stop(job)
        if frozen_index < completed_meshes:
            bpy.data.meshes.remove(item['mesh'])
            continue
        changed = {}
        mesh_started = time.perf_counter()
        materials.progress_context = f"网格 {frozen_index+1}/{len(usable)} · {item['source_name']}"
        mesh = item["mesh"]
        try:
            source=bpy.data.objects.get(item['source_name'])
            _prepare_generated_coordinates(item, source, source_renderer, material_dependencies)
        except ValueError as exc:
            item['generated_error']=str(exc)
            warnings.append(item['source_name']+'：'+str(exc)+'；相关字段需要复核')
        temp = bpy.data.objects.new("MMDBridge_Evaluated_" + _name(item["source_name"]), mesh)
        # Shader translation needs an object, but linking it to a large source
        # scene rebuilds its dependency graph. Link only if a bake is required.
        temp.matrix_world = item["instances"][0]["matrix_object"]
        temp['_mmd_world_size'] = _bounds(_mesh_points(mesh), temp.matrix_world)['size']
        temp["mmd_bridge_surface_class"] = item["surface_class"]
        random_data=item['instances'][0].get('shader_data',{})
        if 'object_random' in random_data:temp['_mmd_object_random']=random_data['object_random']
        if item.get('generated_error'):temp['_mmd_generated_error']=item['generated_error']
        if item.get("auto_chunked"):
            temp["_mmd_auto_chunked"] = True
        if item["material_override"] is not None:
            temp["mmd_bridge_material_strategy"] = item["material_override"]
        copies = [];generated_trees=[]
        try:
            material_ids = []
            geometry_identity = _hash_mesh(
                mesh, item.get('source_material_names',[mat.name if mat else None for mat in item["source_materials"]]), item["collision"])
            temp['_mmd_geometry_identity']=geometry_identity
            temp['_mmd_source_name']=item['source_name']
            if item["spatial"]:
                geometry_identity = stable_id(geometry_identity, str([list(row) for row in temp.matrix_world]),
                                              json.dumps({mat.name: materials.material_world_bounds.get(mat.name)
                                                          for mat in item["source_materials"] if mat}, sort_keys=True))
            copies = [material.copy() if material else None for material in item["source_materials"]]
            from .scene_shader_data import pin_generated
            for copy in copies:
                if copy and copy.use_nodes:pin_generated(copy.node_tree,mesh,created=generated_trees)
            for slot, copy in enumerate(copies):
                mesh.materials[slot] = copy
            for slot, source_material in enumerate(item["source_materials"]):
                if source_material is None:
                    blank = bpy.data.materials.new("Unassigned_Material")
                    copies.append(blank)
                    mesh.materials[slot] = blank
                    rec = materials.fallback(blank, "原始材质槽为空", "Unassigned_Material")
                    material_ids.append(rec["id"])
                    continue
                copy = copies[slot]
                source_name = item.get('source_material_names',[mat.name if mat else None for mat in item['source_materials']])[slot]
                temp['_mmd_material_usage']=item.get('material_usage',{}).get(source_name,len(item['instances']))
                try:
                    rec = materials.analyze(copy, temp, water=False,
                                            source_name=source_name,
                                            geometry_identity=geometry_identity)
                except Exception as exc:
                    rec = materials.fallback(copy, f"{type(exc).__name__}: {exc}",
                                             source_name)
                rec["source_name"] = source_name
                material_ids.append(rec["id"])
            analysis_times['materials'] += time.perf_counter() - mesh_started
            geometry_started = time.perf_counter()
            # Material baking may add MMD_BakeUV, so serialize geometry only
            # after analysis rather than freezing a pre-bake attribute layout.
            geometry = _mesh_data(mesh)
            if geometry is None:
                raise RuntimeError(f"最终求值网格在材质分析后变为空: {item['source_name']}")
            from .material_uv import used_channels,relocate_record
            channels=used_channels([materials.records[i] for i in material_ids],materials.functions)
            if len(channels)>4:
                raise ValueError(f"{item['source_name']} 的有效 UE 图需要 {len(channels)} 个 UV 通道，超过 USD 原生上限 4")
            if geometry['uvs']:
                if any(i>=len(geometry['uvs']) for i in channels):
                    raise ValueError('材质引用了不存在的 UV 通道: '+item['source_name'])
                mapping={old:new for new,old in enumerate(channels)}
                geometry['uvs']=[dict(geometry['uvs'][old],name='st' if new==0 else 'st'+str(new))
                                 for old,new in mapping.items()]
                relocated=[]
                for identity in material_ids:
                    old=materials.records[identity];record=relocate_record(old,mapping,materials.functions)
                    if record is not old:
                        record['asset_name']=core.asset_name('M',record['name'],record['id'])
                        materials.records[record['id']]=record
                    relocated.append(record['id'])
                material_ids=relocated
            topology = _hash_mesh(mesh, material_ids, item["collision"])
            candidate = {"id": topology, "geometry": geometry, "materials": material_ids,
                         "polygon_materials": [int(poly.material_index) for poly in mesh.polygons],
                         "collision": item["collision"], "instances": [],
                         "source_types": set(item["source_types"])}
            for source_instance in item["instances"]:
                matrix = source_instance.pop("matrix_object")
                try:
                    instance_group, exported_matrix = _instance_geometry(candidate, matrix, groups)
                except ValueError as exc:
                    skipped_items.append({'stage':'scene_transform','name':source_instance['name'],
                                          'status':'needs_review','reason':str(exc)})
                    warnings.append(f"实例变换无法可靠转换，需复核：{source_instance['name']}；{exc}")
                    continue
                cached = groups.setdefault(instance_group['id'], instance_group)
                if instance_group is not candidate:
                    source_instance['source_matrix'] = [list(row) for row in matrix]
                    source_instance['affine_baked'] = True
                matrix = exported_matrix
                source_instance.update(index=exported, matrix=[list(row) for row in matrix],
                                       prototype_id=cached["id"],
                                       bounds=_bounds(cached["geometry"]["points"], matrix))
                cached["instances"].append(source_instance)
                changed.setdefault(cached['id'], dict(cached, instances=[]))['instances'].append(source_instance)
                exported += 1
        finally:
            bpy.data.objects.remove(temp, do_unlink=True)
            mesh.use_fake_user = False
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            for material in copies:
                if material is not None:
                    material.use_fake_user = False
                    if material.users == 0:
                        bpy.data.materials.remove(material)
            for tree in reversed(generated_trees):bpy.data.node_groups.remove(tree)
        checkpoint['warnings'] = warnings
        checkpoint['skipped_items'] = skipped_items
        analysis_times['geometry'] += time.perf_counter() - geometry_started
        checkpoint_started = time.perf_counter()
        _save_analyzed(job, checkpoint, list(changed.values()), materials, frozen_index, cfg, data)
        analysis_times['checkpoint'] += time.perf_counter() - checkpoint_started
        if time.monotonic() - analysis_progress >= 1 or frozen_index+1 == len(usable):
            job.status("scene_export", f"分析最终网格 {frozen_index+1}/{len(usable)}；实例 {exported}；"
                       f"累计材质 {analysis_times['materials']:.1f}s / 几何 {analysis_times['geometry']:.1f}s / "
                       f"断点 {analysis_times['checkpoint']:.1f}s", analysis_seconds=analysis_times.copy())
            analysis_progress = time.monotonic()
    referenced_materials={identity for group in groups.values() for identity in group['materials']}
    materials.records={identity:record for identity,record in materials.records.items() if identity in referenced_materials}
    # USD's Unreal sourceAsset and the native creator must use the same bounded
    # name. Reserve the MI_ prefix here, before authoring any material bindings.
    if cfg.get('project'):
        from .material_contract import asset_name_for_path
        folder=cfg['content_root']+'/A_'+data['asset_id']+'/Materials'
        content=Path(cfg['project']).parent/'Content'
        for record in materials.records.values():
            original=record['asset_name']
            bounded=asset_name_for_path(folder,'MI_'+original,content).removeprefix('MI_')
            if bounded != original:
                record['source_asset_name']=original
                record['asset_name']=bounded
    _save_analysis_snapshot(job, checkpoint, materials)

    if not groups and not appearance_enabled:
        raise RuntimeError("场景没有可导出的最终求值网格；未生成代理 USD")

    # Preserve each evaluated piece and its own bake UVs. The Blender object is
    # its parent in the Stage; joining pieces here lowers texture density.
    owner_parts = {}
    for group in groups.values():
        for item in group["instances"]:
            owner_parts.setdefault(item["name"], []).append((group, item))
    source_objects = {obj.name: obj for obj in scene.objects}
    required = set(owner_parts)
    for name in tuple(required):
        obj = source_objects.get(name)
        if obj is None:
            raise RuntimeError(f"求值实例缺少 Blender 大纲物体: {name}")
        while obj.parent:
            obj = obj.parent
            required.add(obj.name)
    hierarchy = []
    paths = {}
    used_paths = set()
    def add_object(obj):
        if obj.name in paths:
            return
        if obj.parent:
            add_object(obj.parent)
        parent_path = paths.get(obj.parent.name, "/World/Objects") if obj.parent else "/World/Objects"
        path = parent_path + "/" + _name(obj.name)
        if path in used_paths:
            base = path
            number = 2
            while path in used_paths:
                path = f"{base}_{number:02d}"
                number += 1
        used_paths.add(path)
        paths[obj.name] = path
        world = _rigid_matrix(obj.matrix_world)
        local = _rigid_matrix(obj.parent.matrix_world).inverted() @ world if obj.parent else world
        hierarchy.append({"name": obj.name, "prim_path": path,
                          "source_matrix": [list(row) for row in obj.matrix_world],
                          "local_matrix": [list(row) for row in local], "chunk": None})
    for name in sorted(required):
        add_object(source_objects[name])
    hierarchy_by_name = {item["name"]: item for item in hierarchy}
    budget = int(cfg.get("usd_payload_triangle_budget", 1_000_000))
    bins = []
    for group in sorted(groups.values(), key=lambda item: item["geometry"]["triangles"], reverse=True):
        cost = group["geometry"]["triangles"]
        target = next((item for item in bins if item["triangles"] + cost <= budget), None)
        if target is None:
            target = {"groups": [], "triangles": 0}
            bins.append(target)
        target["groups"].append(group)
        target["triangles"] += cost
    chunks = []
    for number, payload in enumerate(bins):
        check_stop(job)
        target = geometry_folder / f"Payload_{number+1:02d}.usdc"
        chunk_started = time.perf_counter()
        payload_key = stable_id([(group['id'], group['materials']) for group in payload['groups']],
                                cfg['content_root'], data['asset_id'], meters_per_unit)
        saved = checkpoint['payloads'].get(str(number), {})
        if saved.get('key') == payload_key and target.is_file() and file_hash(target) == saved.get('sha256'):
            root_prim = f'/World/Payload_{number+1:02d}'
            for index, group in enumerate(payload['groups'], 1):
                group['prim_path'] = f'{root_prim}/__Prototypes/Prototype_{index:02d}'
            job.status('scene_export', f'复用已完成 USD Payload {number+1}/{len(bins)}')
        else:
            root_prim = _write_chunk(target, number, payload["groups"], meters_per_unit, exact_collision,
                                     cfg["content_root"], data["asset_id"], job, definitions_only=True,
                                     material_names={record["id"]: record["asset_name"]
                                                     for record in materials.records.values()})
            checkpoint['payloads'][str(number)] = {'key': payload_key, 'sha256': file_hash(target)}
            _write_compressed_json(job.folder / 'scene_checkpoint' / f'Payload_{number:06d}.json.gz', checkpoint['payloads'][str(number)])
        chunks.append({"index": number, "path": str(target), "root_prim": root_prim,
                       "prototype_count": len(payload["groups"]),
                       "instance_count": sum(len(group["instances"]) for group in payload["groups"]),
                       "triangles": payload["triangles"], "sha256": file_hash(target),
                       "write_seconds": round(time.perf_counter() - chunk_started, 3),
                       "working_set_mb": _peak_working_set_mb(current=True)})
        job.status("scene_export", f"写入散件 USD Payload {number+1}/{len(bins)}")
        for group in payload["groups"]:
            group["payload_index"] = number
            group["source_prim"] = group["prim_path"]
            group["vertex_count"] = len(group["geometry"]["points"])
            group["geometry"] = {"triangles": group["geometry"]["triangles"]}
            group["polygon_materials"] = []

    _save_analysis_snapshot(job, checkpoint, materials)
    for name, parts in owner_parts.items():
        obj = source_objects[name]
        entry = hierarchy_by_name[name]
        entry["parts"] = []
        for number, (group, item) in enumerate(sorted(parts, key=lambda part: part[1]["index"]), 1):
            local = _rigid_matrix(obj.matrix_world).inverted() @ Matrix(item["matrix"])
            if len(parts) == 1 and all(abs(local[row][col] - (1.0 if row == col else 0.0)) < 1e-5
                                       for row in range(4) for col in range(4)) and not any(
                    entry['prim_path']+'/'+child in used_paths for child in ('Mesh','Materials')):
                part_path, label = entry["prim_path"], name
            else:
                label = f"{name}_{number:02d}" if len(parts) > 1 else "Mesh"
                part_path = entry["prim_path"] + "/" + _name(label)
                base, suffix = part_path, 2
                while part_path in used_paths:
                    part_path = f'{base}_{suffix:02d}'
                    suffix += 1
                used_paths.add(part_path)
            item["prim_path"] = part_path
            item["label"] = label
            entry["parts"].append({"prim_path": part_path, "label": label,
                                   "local_matrix": [list(row) for row in local],
                                   "chunk": group["payload_index"], "source_prim": group["source_prim"]})
        entry["chunk"] = True
    for group in groups.values():
        group["prim_path"] = group["instances"][0]["prim_path"]

    # A refresh may produce fewer chunks or switch a channel from baked back to
    # translated nodes.  Remove only obsolete tool-generated payloads/textures;
    # Scene.usda is intentionally never touched here so user overrides survive.
    current_payloads = {Path(chunk["path"]).resolve() for chunk in chunks}
    for old_payload in geometry_folder.glob("Payload_*.usdc"):
        if old_payload.resolve() not in current_payloads:
            old_payload.unlink()
    # Do not remove previous bake outputs until UE has imported the current
    # package successfully. Only referenced files are deployed to the project.
    from .bake_cache import material_texture_paths
    current_textures = material_texture_paths(list(materials.records.values()))

    records = list(materials.records.values())
    material_layer = package / "ToolMaterials.usda"
    geometry_layer = package / "Geometry.usda"
    root_layer = package / "Scene.usda"
    _write_material_layer(material_layer, records, cfg["content_root"], data["asset_id"])
    _write_geometry_index(geometry_layer, chunks, meters_per_unit, camera_samples, cfg["fps"], hierarchy)
    appearance_record = {}
    if appearance_enabled:
        for group in groups.values():
            for instance_record in group['instances']:
                bounds = instance_record.get('bounds')
                if bounds:
                    if visible_bounds is None:
                        visible_bounds = [list(bounds['min']), list(bounds['max'])]
                    else:
                        for axis in range(3):
                            visible_bounds[0][axis] = min(visible_bounds[0][axis],bounds['min'][axis])
                            visible_bounds[1][axis] = max(visible_bounds[1][axis],bounds['max'][axis])
        appearance_record = appearance.export_appearance(scene,frozen_volumes,package,job,meters_per_unit,visible_bounds)
        appearance_record['capabilities'] = native_capabilities
        for desc in lights_and_cameras:
            for reason in desc.get('light',{}).get('needs_review',[]):
                appearance_record['needs_review'].append({'stage':'light','name':desc['name'],'reason':reason})
            if any(abs(desc.get('camera',{}).get(k,0))>1e-6 for k in ('shift_x','shift_y')):
                appearance_record['needs_review'].append({'stage':'camera','name':desc['name'],'reason':'源镜头 shift 需要偏心投影适配'})
        appearance_record['reference_camera'] = {'name':scene.camera.name if scene.camera else None,
            'resolution':[scene.render.resolution_x,scene.render.resolution_y],
            'percentage':scene.render.resolution_percentage,'pixel_aspect':[scene.render.pixel_aspect_x,scene.render.pixel_aspect_y]}
        appearance.write_volumes(geometry_layer,appearance_record['volumes'],package,cfg['content_root'],data['asset_id'])
    root_created = _write_user_root(root_layer, meters_per_unit, camera_samples, cfg["fps"])
    package_json = package / "package.json"
    record = {
        "route": "usd_stage", "source": cfg["scene"], "reference_frame": frame,
        "function_definitions": list(materials.functions.definitions.values()),
        "evaluation_mode": "RENDER",
        "blender_version": ".".join(map(str, bpy.app.version)),
        "source_blender_version": ".".join(map(str, authored)) if authored else None,
        "meters_per_unit": meters_per_unit, "objects": lights_and_cameras,
        "hierarchy": [{"name": item["name"], "prim_path": item["prim_path"],
                       "source_matrix": item['source_matrix'],
                       "transform_policy": "rigid_parent_mesh_scale",
                       "parent": source_objects[item["name"]].parent.name
                       if source_objects[item["name"]].parent else None,
                       "mesh": item["chunk"] is not None,
                       "parts": [{"name": part["label"], "prim_path": part["prim_path"]}
                                 for part in item.get("parts", [])]} for item in hierarchy],
        "materials": records, "warnings": warnings + materials.warnings,
        "skipped_items": skipped_items,
        "missing_external_files": missing_external,
        "camera": ({"prim_path": "/World/Cameras/MMD_Camera", "source": str(camera_source),
                    "sha256": file_hash(camera_source), "samples": len(camera_samples),
                    "frame_range": [camera_samples[0]["frame"], camera_samples[-1]["frame"]]}
                   if camera_samples else None),
        "usd": {"root_layer": str(root_layer), "geometry_layer": str(geometry_layer),
                "material_layer": str(material_layer), "payloads": chunks,
                "root_created": root_created},
        "stats": {"prototypes": len(groups), "instances": exported,
                  "triangles_unique": sum(group["geometry"]["triangles"] for group in groups.values()),
                  "triangles_instanced": sum(group["geometry"]["triangles"] * len(group["instances"])
                                             for group in groups.values()),
                  "payloads": len(chunks),
                  "instance_reuse_ratio": round(exported / max(1, len(groups)), 3)},
        "prototypes": [{"id": group["id"], "triangles": group["geometry"]["triangles"],
                        "vertices": group["vertex_count"],
                        "prim_path": group.get("prim_path"),
                        "material_slots": group["materials"], "collision": group["collision"],
                        "disable_nanite": group.get("disable_nanite", False),
                        "instances": len(group["instances"]),
                        "source_types": sorted(group["source_types"])} for group in groups.values()],
        "instances": [item for group in groups.values() for item in group["instances"]],
    }
    if render_optimized:
        from .usd_scene_optimizer import compact_geometry, remap_scene
        temporary_geometry = package / 'Geometry_Compact.usda'
        optimization = compact_geometry(geometry_layer, temporary_geometry)
        os.replace(temporary_geometry, geometry_layer)
        remap_scene(record, optimization)
        job.status('scene_export', f"散布实例减负：{optimization['batched_instances']} 散件合为 {optimization['generated_batches']} 批次；保留全部实例")
    labels = {item["prim_path"]: item["name"] for item in hierarchy}
    record.update(appearance_record)
    labels.update({v['prim_path']:v['name'] for v in appearance_record.get('volumes',[]) if v.get('prim_path')})
    labels.update({item["prim_path"]: item["label"] for item in record['instances']})
    labels.update({item['prim_path']: f"Scatter ({item['instances']} instances)" for item in record.get('scatter_batches', [])})
    atomic_json(package_json, {"schema": SCHEMA, "asset_id": data["asset_id"],
                               "root": "Scene.usda", "generated_layers": ["Geometry.usda", "ToolMaterials.usda"],
                               "payloads": ["Geometry/" + Path(chunk["path"]).name for chunk in chunks],
                               "actor_labels": labels,
                               "shader_data": [{'prim_path':i['prim_path'],
                                    **({'instance_index':i['instance_index']} if 'instance_index' in i else {}),
                                    **i['shader_data']} for i in record['instances'] if i.get('shader_data')],
                               "viewport_render_settings": {
                                   "auto_ocio": cfg.get('scene_viewport_ocio', True),
                                   "ue_render_memory_profile": cfg.get('ue_render_memory_profile', 'balanced'),
                                   "foliage_distance_scale": cfg.get('foliage_distance_scale', 2.)},
                               **appearance_record,
                               "stats": record["stats"],
                               "import_optimization": record.get('import_optimization', {})})
    record["usd"]["files"] = [
        {"path": str(path), "relative": str(path.relative_to(package)).replace("\\", "/"),
         "sha256": file_hash(path), "size": path.stat().st_size}
        for path in sorted((item for item in package.rglob("*") if item.is_file()
                            and (item.parent != package / 'Textures' or item.resolve() in current_textures)), key=lambda item: str(item))
    ]
    record["metrics"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_system_working_set_mb": _peak_working_set_mb(),
        "package_file_count": len(record["usd"]["files"]),
        "package_bytes": sum(item["size"] for item in record["usd"]["files"]),
        "bake_storage": materials.bake_stats,
        "freeze_analysis": freeze_analysis_stats,
        "ue_assets_requested": 2 + len(records),  # Stage actor/cache plus material assets
        "instance_reuse_ratio": record["stats"]["instance_reuse_ratio"],
    }
    manifest_path = job.folder / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {
        "schema": SCHEMA, "job_id": data["id"], "asset_id": data["asset_id"], "fps": cfg["fps"],
        "frame_range": [cfg["start"], cfg["end"] or cfg["fps"] * 10],
        "files": {}, "materials": [], "warnings": []}
    manifest["schema"] = SCHEMA
    manifest["scene"] = record
    atomic_json(manifest_path, manifest,compact=True)
    atomic_json(export_root / "manifest.json", manifest,compact=True)
    for warning in dict.fromkeys(record["warnings"]):
        job.status("scene_warning", warning)
    job.status("scene_done", f"USD 场景导出完成：{len(groups)} 原型 / {exported} 散件 / {len(chunks)} Payload",
               manifest=str(manifest_path), root_layer=str(root_layer))
