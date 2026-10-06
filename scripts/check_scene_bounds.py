"""Blender-hosted exact bounds check and leaf-instance timing comparison.

blender --background --factory-startup --python scripts/check_scene_bounds.py
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.blender_usd import _bounds, _mesh_points, export_usd_scene
from mmd_bridge.core import Job, Settings, atomic_json, read_json


def previous_bounds(points, matrix):
    low, high = [math.inf] * 3, [-math.inf] * 3
    for point in points:
        world = matrix @ Vector(point)
        for axis in range(3):
            low[axis] = min(low[axis], world[axis])
            high[axis] = max(high[axis], world[axis])
    return {'min': low, 'max': high, 'size': [hi-lo for lo, hi in zip(low, high)]}


mesh = bpy.data.meshes.new('LeafBounds')
# Asymmetric geometry detects accidental use of transformed bounding-box corners.
coords = np.random.default_rng(42).uniform(-.1, .1, (256, 3)).astype(np.float32)
coords[:, 2] *= .01
mesh.from_pydata(coords.tolist(), [], [(0, 1, 2)])
points = _mesh_points(mesh)
assert np.array_equal(points, coords)
assert _bounds([], Matrix.Identity(4)) is None
matrices = [Matrix.Identity(4),
            Matrix.Translation((10, -20, 30)) @ Matrix.Rotation(.72, 4, 'Y'),
            Matrix.Diagonal((-2, 3, .4, 1)),
            Matrix(((1, .3, .1, 2), (0, 2, .2, 3), (.4, 0, 1, -5), (0, 0, 0, 1)))]
for matrix in matrices:
    old, new = previous_bounds(coords, matrix), _bounds(points, matrix)
    for key in old:
        assert np.allclose(old[key], new[key], rtol=1e-5, atol=1e-5), (old, new)

count = 10000
transforms = [Matrix.Translation((index % 100, index // 100, 0)) @ matrices[1] for index in range(count)]
timings = {}
for name, function in [('previous_seconds', previous_bounds), ('optimized_seconds', _bounds)]:
    started = time.perf_counter()
    for matrix in transforms:
        function(points, matrix)
    timings[name] = round(time.perf_counter() - started, 4)

# Exercise the complete fresh export path, including stats, collision and snapshots.
folder = root / 'work/scene_bounds_validation'
folder.mkdir(parents=True, exist_ok=True)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
for name, size, override in [('Ground', (100, 100, 1), None), ('Leaf', (.1, .1, .001), None),
                             ('SmallSolid', (.1, .1, .1), 'exact')]:
    bpy.ops.mesh.primitive_cube_add(size=1)
    obj = bpy.context.object
    obj.name, obj.scale = name, size
    if override:
        obj['mmd_bridge_collision'] = override
    mat = bpy.data.materials.new(name + '_Material')
    obj.data.materials.append(mat)
source = folder / 'Source.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(source))
job = Job.create(folder / 'jobs', Settings(flow='scene', scene=str(source), blender=bpy.app.binary_path,
                                         character_output_dir=str(folder)))
export_usd_scene(job)
scene = read_json(job.folder / 'manifest.json')['scene']
prototypes = {item['id']: item for item in scene['prototypes']}
for instance in scene['instances']:
    assert prototypes[instance['prototype_id']]['collision'] == (instance['name'] != 'Leaf'), instance
assert scene['stats']['instances'] == 3
assert read_json(job.folder / 'scene_checkpoint.json')['world_bounds'] == [[-50, -50, -.5], [50, 50, .5]]
result = dict(timings, leaf_instances=count, vertices_per_leaf=len(points),
              speedup=round(timings['previous_seconds'] / timings['optimized_seconds'], 1),
              bounds_match=True, fresh_export_verified=True, job=str(job.folder))
atomic_json(folder / 'result.json', result)
print('SCENE_BOUNDS_PASSED ' + json.dumps(result), flush=True)
