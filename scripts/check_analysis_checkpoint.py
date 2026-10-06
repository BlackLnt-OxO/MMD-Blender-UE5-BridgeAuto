"""Native regression for incremental analysis persistence and lazy bake linking.

blender -b --factory-startup --python-exit-code 1 --python scripts/check_analysis_checkpoint.py
"""
from array import array
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge import blender_usd as exporter
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import Job, atomic_json, read_json, file_hash

folder = root / 'work/analysis_checkpoint_validation'
folder.mkdir(parents=True, exist_ok=True)
job = Job(folder / 'jobs' / str(time.time_ns()))
(job.folder / 'scene_checkpoint').mkdir(parents=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
mesh = obj.data
mesh.polygons[0].material_index = 1
material_ids = ['leaf', 'second']

# Preserve the previous cache identity byte for byte, including face order/UVs.
digest = hashlib.sha256()
coords = array('f', [0.0]) * (len(mesh.vertices) * 3)
mesh.vertices.foreach_get('co', coords)
digest.update(coords.tobytes())
digest.update(array('i', [len(p.vertices) for p in mesh.polygons]).tobytes())
digest.update(array('i', [i for p in mesh.polygons for i in p.vertices]).tobytes())
digest.update(array('i', [p.material_index for p in mesh.polygons]).tobytes())
digest.update(json.dumps(material_ids, ensure_ascii=False).encode('utf-8'))
digest.update(b'False')
for uv in mesh.uv_layers:
    digest.update(uv.name.encode('utf-8', errors='replace'))
    values = array('f', [0.0]) * (len(uv.data) * 2)
    uv.data.foreach_get('uv', values)
    digest.update(values.tobytes())
assert exporter._hash_mesh(mesh, material_ids, False) == digest.hexdigest()[:20]

checkpoint = {'items': [{'name': f'Leaf_{i}', 'matrix': list(range(16))} for i in range(43000)],
              'completed': [], 'payloads': {}, 'warnings': ['frozen warning'], 'skipped_items': []}
atomic_json(job.folder / 'scene_checkpoint.json', checkpoint)
original_hash = file_hash(job.folder / 'scene_checkpoint.json')
materials = SimpleNamespace(meters_per_unit=1, records={}, images={}, warnings=[])
exporter._analysis_checkpoint_state(checkpoint, materials)
geometry = exporter._mesh_data(mesh)
started = time.perf_counter()
for index in range(10):
    materials.records['leaf'] = {'id': 'leaf', 'source_aliases': list(range(index+1))}
    group = {'id': 'shared_leaf', 'geometry': geometry, 'materials': ['leaf'],
             'polygon_materials': [0] * len(mesh.polygons), 'collision': False,
             'source_types': {'MESH'}, 'instances': [{'name': f'Leaf_{index}', 'index': index,
             'matrix': [[1,0,0,index], [0,1,0,0], [0,0,1,0], [0,0,0,1]]}]}
    checkpoint['warnings'].append(f'warning_{index}')
    checkpoint['skipped_items'].append({'name': f'skip_{index}'})
    exporter._save_analyzed(job, checkpoint, [group], materials, index,
                            {'content_root': '/Game/Test', 'exact_collision': True}, {'asset_id': 'test'})
seconds = time.perf_counter() - started
assert file_hash(job.folder / 'scene_checkpoint.json') == original_hash
assert len(list((job.folder / 'scene_checkpoint').glob('Mesh_*.usdc'))) == 1
exporter._write_compressed_json(job.folder / 'scene_checkpoint/Payload_000000.json.gz', {'file': 'payload'})
restored = read_json(job.folder / 'scene_checkpoint.json')
new_materials = SimpleNamespace()
groups = {}
assert exporter._restore_analyzed(job, restored, groups, new_materials) == 10
assert new_materials.records['leaf']['source_aliases'] == list(range(10))
assert len(restored['warnings']) == 11 and len(restored['skipped_items']) == 10
assert restored['payloads']['0']['file'] == 'payload'

# First mesh after resume must keep aliases added to the restored mutable record.
new_materials.meters_per_unit = 1
new_materials.records['leaf']['source_aliases'].append(10)
exporter._save_analyzed(job, restored, [dict(group, instances=[])], new_materials, 10,
                       {'content_root': '/Game/Test', 'exact_collision': True}, {'asset_id': 'test'})
again = read_json(job.folder / 'scene_checkpoint.json')
exporter._load_analysis_journal(job, again)
assert again['material_records']['leaf']['source_aliases'] == list(range(11))
exporter._save_analysis_snapshot(job, restored, new_materials)
assert not list((job.folder / 'scene_checkpoint').glob('Analyzed_*.json.gz'))

# The old per-mesh receipt schema still loads the same geometry.
legacy = read_json(job.folder / 'scene_checkpoint.json')
legacy['completed'] = legacy['completed'][:1]
for record in legacy['completed'][0]['groups']:
    record.pop('geometry_file')
    record.pop('geometry_sha256')
assert exporter._restore_analyzed(job, legacy, {}, SimpleNamespace()) == 1

# A temporary mesh outside the scene must be linked before native Cycles bake.
mat = bpy.data.materials.new('BakeMaterial')
mat.use_nodes = True
obj.data.materials.clear()
obj.data.materials.append(mat)
for polygon in mesh.polygons:
    polygon.material_index = 0
for collection in list(obj.users_collection):
    collection.objects.unlink(obj)
assert not obj.users_collection
baker = Materials(folder / 'Bake', str(folder / 'source.blend'), resolution=32)
baker.surface_policy = {'category': 'small'}
shader = mat.node_tree.nodes.get('Principled BSDF')
shader.inputs['Base Color'].default_value = (.2, .6, .1, 1)
texture = baker.bake(obj, mat, shader.inputs['Base Color'], 'base_color', 'lazy_link_test')
assert obj.users_collection and obj.data.uv_layers.get('MMD_BakeUV')
image = bpy.data.images.load(texture['image']['path'], check_existing=False)
assert tuple(image.size) == (32, 32)
assert max(image.pixels[:]) > .5
atomic_json(folder / 'result.json', {'complete': True, 'frozen_records': 43000,
    'journal_meshes': 10, 'geometry_files': 1, 'checkpoint_seconds': seconds,
    'hash_compatible': True, 'legacy_resume': True, 'alias_resume': True, 'native_bake': True})
print('ANALYSIS_CHECKPOINT_AND_BAKE_PASSED', flush=True)
