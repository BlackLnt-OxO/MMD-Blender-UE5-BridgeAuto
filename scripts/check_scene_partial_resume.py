"""Native freeze -> stop -> bounds -> stop -> resume check.

Run with Blender --background --factory-startup --python-exit-code 1 --python
scripts/check_scene_partial_resume.py -- <test-folder> freeze|bounds|resume
"""
import os
import sys
import time
from pathlib import Path

import bpy
from pxr import Usd, UsdPhysics

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge import blender_usd as exporter
from mmd_bridge.core import Job, Settings, atomic_json, read_json

folder = Path(sys.argv[sys.argv.index('--')+1]).resolve()
mode = sys.argv[sys.argv.index('--')+2]
folder.mkdir(parents=True, exist_ok=True)
source = folder / 'Source.blend'
if mode == 'freeze':
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    material = bpy.data.materials.new('Leaves')
    material.use_nodes = True
    image = bpy.data.images.new('PackedTexture', 128, 128)
    image.pixels[:] = [.2, .4, .1, 1] * (128*128)
    image.pack()
    texture = material.node_tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    material.node_tree.links.new(texture.outputs['Color'], material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
    second_material = bpy.data.materials.new('SecondSlot')
    bpy.ops.mesh.primitive_cube_add(size=100)
    ground = bpy.context.object
    ground.name = 'Ground'
    ground.data.materials.append(second_material)
    shared = None
    for index in range(1100):
        if index == 0 or index <= 520:
            mesh = bpy.data.meshes.new(f'LeafMesh_{index}')
            mesh.from_pydata([(0, 0, 0), (.1, 0, 0), (0, .1, .001)], [], [(0, 1, 2)])
            mesh.materials.append(second_material)
            mesh.materials.append(material)
            mesh.polygons[0].material_index = 1
            if index == 0:
                shared = mesh
        else:
            mesh = shared
        obj = bpy.data.objects.new(f'Leaf_{index:04d}', mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj.location = ((index % 30)*.2, (index//30)*.2, 51)
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    job = Job.create(folder / 'jobs', Settings(flow='scene', scene=str(source), blender=bpy.app.binary_path,
                                             character_output_dir=str(folder), restore_scene_appearance=False))
    atomic_json(folder / 'job_path.json', {'job': str(job.folder)})
else:
    job = Job(read_json(folder / 'job_path.json')['job'])
    (job.folder / 'stop').unlink(missing_ok=True)
remaining_bounds = (1101 - exporter._read_compressed_json(job.folder / 'scene_checkpoint/Bounds.json.gz')['measured_instances']
                    if mode == 'resume' else None)


def pause(phase, cursor):
    atomic_json(folder / (phase + '_ready.json'), {'pid': os.getpid(), 'job': str(job.folder), 'cursor': cursor})
    print('CHECKPOINT_READY ' + phase, flush=True)
    time.sleep(300)
    raise AssertionError('test driver must terminate the paused task')


save_freeze = exporter._save_freeze_batch
write_json = exporter._write_compressed_json
bounds = exporter._bounds
freeze_calls, bounds_calls = [], []


def track_freeze(*args, **kwargs):
    save_freeze(*args, **kwargs)
    state = args[2]
    freeze_calls.append(state['cursor'])
    if mode == 'freeze' and state['cursor'] >= 500 and not state['complete']:
        pause('freeze', state['cursor'])


def track_json(path, data):
    write_json(path, data)
    if mode == 'bounds' and path.name == 'Bounds.json.gz' and len(data['completed']) >= 500:
        pause('bounds', len(data['completed']))


def track_bounds(*args):
    bounds_calls.append(True)
    return bounds(*args)


exporter._save_freeze_batch = track_freeze
exporter._write_compressed_json = track_json
exporter._bounds = track_bounds
exporter.export_usd_scene(job)
assert mode == 'resume'
assert not freeze_calls, freeze_calls  # Completed freezing must not run again.
scene = read_json(job.folder / 'manifest.json')['scene']
assert scene['stats']['instances'] == 1101, scene['stats']
prototypes = {item['id']: item for item in scene['prototypes']}
stage = Usd.Stage.Open(scene['usd']['root_layer'])
for instance in scene['instances']:
    enabled = instance['name'] == 'Ground'
    assert prototypes[instance['prototype_id']]['collision'] == enabled
    prim = stage.GetPrimAtPath(instance['prim_path'] + '/Mesh')
    assert bool(prim.HasAPI(UsdPhysics.CollisionAPI)) == enabled
checkpoint = read_json(job.folder / 'scene_checkpoint.json')
assert len(checkpoint['items']) == 522, len(checkpoint['items'])
assert len(checkpoint['completed']) == 522
assert checkpoint['images'] and checkpoint['image_hashes']
assert len(bounds_calls) == remaining_bounds + 1101 + 522, len(bounds_calls)  # Policy bounds calculated once per analyzed mesh.
assert not list((job.folder / 'scene_checkpoint').glob('Freeze_*'))
assert not (job.folder / 'scene_checkpoint/Bounds.json.gz').exists()
atomic_json(folder / 'result.json', {'complete': True, 'freeze_reused': True, 'bounds_reused': 500,
                                   'instances': 1101, 'frozen_meshes': 522, 'shared_mesh_reused': True,
                                   'packed_texture_and_material_slots_preserved': True,
                                   'snapshot_bytes': (job.folder / 'scene_checkpoint/Frozen.blend').stat().st_size,
                                   'job': str(job.folder)})
print('PARTIAL_FREEZE_AND_BOUNDS_RESUME_PASSED', flush=True)
