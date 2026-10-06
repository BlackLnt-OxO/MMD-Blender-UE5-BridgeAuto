"""Check interpolated ORCO, native Cycles pixels and frozen chunk reload."""
from pathlib import Path
import sys
import json

import bpy
import numpy as np
from mathutils import Matrix, Euler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.blender_usd import (_chunked_meshes, _prepare_generated_coordinates,
    _load_frozen, _save_frozen)
from mmd_bridge.scene_shader_data import ensure_generated_layers, pin_generated, has_generated_layers
from mmd_bridge.blender_materials import Materials, SurfaceConversionError
from mmd_bridge.core import Job, atomic_json, file_hash

OUT = ROOT / 'work/generated_chunk_regression'
OUT.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
mesh = bpy.data.meshes.new('WholeSource')
mesh.from_pydata([(-9,-6,-.5),(9,-6,.1),(9,6,.5),(-9,6,-.1)], [], [(0,1,2,3)])
mesh.update()
source = bpy.data.objects.new('WholeSource', mesh)
scene.collection.objects.link(source)
source.matrix_world = Matrix.Translation((3,-4,2)) @ Euler((.1,.2,.17)).to_matrix().to_4x4() @ Matrix.Diagonal((-1.2,.8,1.,1.))
bpy.context.view_layer.update()
uv = mesh.uv_layers.new(name='MMD_BakeUV')
for loop in mesh.loops:
    p = mesh.vertices[loop.vertex_index].co
    uv.data[loop.index].uv = (.05+.9*(p.x+9)/18, .05+.9*(p.y+6)/12)
mat = bpy.data.materials.new('GeneratedSurface')
mat.use_nodes = True
mesh.materials.append(mat)
tree = mat.node_tree
shader = tree.nodes.get('Principled BSDF')
coord = tree.nodes.new('ShaderNodeTexCoord')
tree.links.new(coord.outputs['Generated'], shader.inputs['Base Color'])
converter = Materials(OUT/'bakes', 'chunk-orco-fixture', 128)
converter.isolated_baking = True
converter.surface_policy = {'category': 'small'}

def pixels(expression):
    info = expression['image']
    image = bpy.data.images.load(info['path'], check_existing=False)
    image.colorspace_settings.name = info['color_space']
    try:
        return np.asarray(image.pixels[:], dtype=np.float32).reshape(image.size[1], image.size[0], 4)
    finally:
        bpy.data.images.remove(image)

# Independent native reference on the original, uncut object and its Generated output.
reference = pixels(converter.bake(source, mat, shader.inputs['Base Color'], 'field_reference', 'native-whole-v1'))
item = {'mesh': mesh.copy(), 'source_materials': [mat], 'source_name': source.name,
        'source_types': {'MESH'}, 'surface_class': 'chunked', 'material_override': None,
        'instances': [{'name': source.name, 'source_name': source.name, 'matrix_object': source.matrix_world.copy()}],
        'collision': False}
dependencies = {}
_prepare_generated_coordinates(item, source, 'CYCLES', dependencies)
assert has_generated_layers(item['mesh'])
parts = _chunked_meshes(item['mesh'], source.matrix_world, 1.)
assert len(parts) > 1
center = np.asarray(mesh.texspace_location)
size = np.asarray(mesh.texspace_size)
checks = []
samples = np.stack(np.meshgrid((np.arange(128)+.5)/128, (np.arange(128)+.5)/128), axis=-1)
for index, part in enumerate(parts):
    before = len(part.uv_layers)
    ensure_generated_layers(source, part)  # Existing carriers bypass vertex-count matching.
    assert len(part.uv_layers) == before
    vertices = np.asarray([part.vertices[loop.vertex_index].co[:] for loop in part.loops])
    expected = (vertices-center)/(size*2)+.5
    xy = np.asarray([v.uv[:] for v in part.uv_layers['MMDBridge_Generated_XY'].data])
    zw = np.asarray([v.uv[:] for v in part.uv_layers['MMDBridge_Generated_ZW'].data])
    error = float(np.max(np.abs(np.column_stack((xy,zw[:,0]))-expected)))
    assert error < 2e-6, (index, error)
    target = bpy.data.objects.new('Chunk_'+str(index), part)
    scene.collection.objects.link(target)
    target.matrix_world = source.matrix_world
    target_mat = mat.copy()
    part.materials[0] = target_mat
    pin_generated(target_mat.node_tree, part)
    target_shader = target_mat.node_tree.nodes.get('Principled BSDF')
    actual = pixels(converter.bake(target, target_mat, target_shader.inputs['Base Color'],
                                  'field_chunk', 'chunk-native-v1-'+str(index)))
    mask = np.zeros((128,128), dtype=bool)
    for face in part.polygons:
        polygon = np.asarray([part.uv_layers['MMD_BakeUV'].data[i].uv[:] for i in face.loop_indices])
        crosses = []
        for a,b in zip(polygon,np.roll(polygon,-1,axis=0)):
            edge = b-a
            crosses.append((edge[0]*(samples[...,1]-a[1])-edge[1]*(samples[...,0]-a[0]))/max(np.linalg.norm(edge),1e-9))
        cross = np.stack(crosses)
        mask |= np.all(cross > .002,axis=0) | np.all(cross < -.002,axis=0)
    # Rotated cuts can leave subpixel slivers. Their coordinates are still
    # checked above; only covered interior pixels admit a bake comparison.
    pixel_error = float(np.max(np.abs(actual[mask,:3]-reference[mask,:3]))) if mask.any() else None
    if pixel_error is not None:
        assert pixel_error < 2e-5, (index,pixel_error)
    checks.append({'chunk': index+1, 'vertices': len(part.vertices), 'coordinate_error': error,
                   'native_pixel_error': pixel_error, 'pixels': int(mask.sum())})
    part.materials[0] = mat
    bpy.data.objects.remove(target, do_unlink=True)
    bpy.data.materials.remove(target_mat)

assert sum(check['pixels'] > 0 for check in checks) >= 3

job = Job(OUT/'restore_job')
job.folder.mkdir(exist_ok=True)
usable = [dict(item, mesh=p, auto_chunked=True) for p in parts]
checkpoint = _save_frozen(job, usable, [], converter, [], [], 'fixture')
frozen_hash = file_hash(job.folder/'scene_checkpoint/Frozen.blend')
restored, _ = _load_frozen(job, checkpoint, converter)
assert all(has_generated_layers(x['mesh']) for x in restored)
# Old missing-carrier snapshots must fail without changing their saved geometry.
old_mesh = parts[0].copy()
for name in ('MMDBridge_Generated_XY','MMDBridge_Generated_ZW'):
    old_mesh.uv_layers.remove(old_mesh.uv_layers[name])
old_job = Job(OUT/'old_restore_job')
old_job.folder.mkdir(exist_ok=True)
old = _save_frozen(old_job, [dict(item,mesh=old_mesh,auto_chunked=True)], [], converter, [], [], 'old')
old_hash = file_hash(old_job.folder/'scene_checkpoint/Frozen.blend')
try:
    _load_frozen(old_job, old, converter)
except ValueError as exc:
    assert '旧冻结分块缺少' in str(exc)
else:
    raise AssertionError('Missing carriers were accepted')
assert file_hash(old_job.folder/'scene_checkpoint/Frozen.blend') == old_hash
assert file_hash(job.folder/'scene_checkpoint/Frozen.blend') == frozen_hash
# Known, reported unsupported topology remains resumable as a partial export.
old['items'][0]['generated_error'] = 'Generated 原始顶点数量不对应'
known_failure, _ = _load_frozen(old_job, old, converter)
assert known_failure[0]['generated_error'] == 'Generated 原始顶点数量不对应'

# Force the full PBR fallback on a preserved chunk, including normal and masks.
obj = bpy.data.objects.new('FallbackChunk', parts[0])
scene.collection.objects.link(obj)
obj.matrix_world = source.matrix_world
copy = mat.copy()
parts[0].materials[0] = copy
pin_generated(copy.node_tree, parts[0])
pbr = Materials(OUT/'pbr', 'chunk-pbr-fixture', 32)
pbr.isolated_baking = True
native_analyze = pbr._analyze
def failed_surface(*args, **kwargs):
    native_analyze(*args, **kwargs)
    raise SurfaceConversionError('Fixture surface failed')
pbr._analyze = failed_surface
record = pbr.analyze(copy,obj,source_name='GeneratedSurface')
assert record['basic_pbr_bake']['status'] == 'baked'
assert all(channel['op']=='texture' for channel in record['channels'].values())
atomic_json(OUT/'report.json', {'complete': True, 'chunks': checks, 'saved_reload': True,
    'old_snapshot_rejected': True, 'known_failure_resumable': True,
    'snapshot_hashes_preserved': True, 'pbr_fallback': record})
print('GENERATED_CHUNK_REGRESSION='+json.dumps({'complete':True,'chunks':len(checks)}),flush=True)
