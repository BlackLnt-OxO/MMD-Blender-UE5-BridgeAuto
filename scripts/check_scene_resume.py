"""Blender-hosted stop/resume and relative collision regression (CPU, tiny scene).

Run with Blender -b --python scripts/check_scene_resume.py -- <folder> interrupt|resume.
The interrupt run pauses after one durable mesh so the desktop controller can kill it.
"""
import sys
import os
import time
from pathlib import Path

import bpy
from pxr import Usd, UsdPhysics

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.core import Job, Settings, atomic_json, read_json
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_usd import export_usd_scene
from mmd_bridge.blender_usd import _load_analysis_journal

folder = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
mode = sys.argv[sys.argv.index('--') + 2]
folder.mkdir(parents=True, exist_ok=True)
source = folder / 'source.blend'
if mode == 'interrupt':
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    image = bpy.data.images.new('CheckpointTexture', 8, 8)
    image.filepath_raw = str(folder / 'source_texture.png')
    image.file_format = 'PNG'
    image.save()
    for name, size, location, override in (
            ('Ground', (100, 100, 1), (0, 0, 0), None),
            ('Leaf', (.1, .1, .02), (0, 0, 1), None),
            ('SmallSolid', (.1, .1, .1), (1, 0, 1), 'exact')):
        bpy.ops.mesh.primitive_cube_add(size=1, location=location)
        obj = bpy.context.object
        obj.name = name
        obj.scale = size
        if override:
            obj['mmd_bridge_collision'] = override
        mat = bpy.data.materials.new(name + '_Material')
        mat.use_nodes = True
        texture = mat.node_tree.nodes.new('ShaderNodeTexImage')
        texture.image = image
        mat.node_tree.links.new(texture.outputs['Color'], mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
        obj.data.materials.append(mat)
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    job = Job.create(folder / 'jobs', Settings(flow='scene', scene=str(source), blender=bpy.app.binary_path,
                                             character_output_dir=str(folder), bake_resolution=1024))
    atomic_json(folder / 'job_path.json', {'job': str(job.folder)})
else:
    job = Job(read_json(folder / 'job_path.json')['job'])
    (job.folder / 'stop').unlink(missing_ok=True)
if mode == 'resume':
    saved = read_json(job.folder / 'scene_checkpoint.json')
    _load_analysis_journal(job, saved)
    resumed_meshes = len(saved['completed'])
else:
    resumed_meshes = 0

analyze = Materials.analyze
calls = []
def track(self, *args, **kwargs):
    calls.append(kwargs.get('source_name'))
    if mode == 'interrupt' and len(calls) == 2:
        atomic_json(folder / 'running.json', {'pid': os.getpid(), 'job': str(job.folder)})
        print('CHECKPOINT_READY', flush=True)
        time.sleep(300)
    return analyze(self, *args, **kwargs)
Materials.analyze = track
export_usd_scene(job)
assert mode == 'resume', 'interrupt process should have been killed'
checkpoint = read_json(job.folder / 'scene_checkpoint.json')
assert len(checkpoint['completed']) == 3
assert checkpoint['images'] and checkpoint['image_hashes']
assert len(calls) == 3 - resumed_meshes, calls  # Completed meshes must not be analyzed again.
scene = read_json(job.folder / 'manifest.json')['scene']
prototypes = {p['id']: p for p in scene['prototypes']}
stage = Usd.Stage.Open(scene['usd']['root_layer'])
for instance in scene['instances']:
    enabled = prototypes[instance['prototype_id']]['collision']
    assert enabled == (instance['name'] != 'Leaf'), instance
    mesh = stage.GetPrimAtPath(instance['prim_path'] + '/Mesh')
    assert bool(mesh.HasAPI(UsdPhysics.CollisionAPI)) == enabled
assert scene['stats']['instances'] == 3
atomic_json(folder / 'result.json', {'complete': True, 'resumed_meshes': resumed_meshes, 'new_meshes': len(calls),
                                   'collision_filter_verified': True, 'job': str(job.folder)})
print('RESUME_AND_COLLISION_PASSED', flush=True)
