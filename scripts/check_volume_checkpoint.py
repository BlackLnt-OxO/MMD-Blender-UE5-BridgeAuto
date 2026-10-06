"""Native regression for evaluated volume sockets, object slots and reload.

blender --background --factory-startup --python-exit-code 1 --python scripts/check_volume_checkpoint.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.core import Job, Settings, atomic_json, read_json, file_hash
from mmd_bridge.blender_appearance import material_channels
from mmd_bridge.blender_usd import export_usd_scene, _volume_records, _load_frozen

folder = root / 'work/volume_checkpoint_validation'
folder.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine = 'CYCLES'
for name, pure, position in [('SurfaceVolume', False, (0, 0, 0)), ('PureVolume', True, (3, 0, 0))]:
    bpy.ops.mesh.primitive_cube_add(size=2, location=position)
    obj = bpy.context.object
    obj.name = name
    data_material = bpy.data.materials.new(name + '_DataMaterial')
    obj.data.materials.append(data_material)
    material = bpy.data.materials.new(name + '_ObjectMaterial')
    material.use_nodes = True
    output = material.node_tree.nodes.get('Material Output')
    if pure:
        for link in list(output.inputs['Surface'].links):
            material.node_tree.links.remove(link)
    node = material.node_tree.nodes.new('ShaderNodeVolumeScatter')
    node.inputs['Density'].default_value = .02
    node.inputs['Color'].default_value = (.6, .8, .9, 1)
    material.node_tree.links.new(node.outputs['Volume'], output.inputs['Volume'])
    obj.material_slots[0].link = 'OBJECT'
    obj.material_slots[0].material = material
    if not pure:
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        socket = material_channels(evaluated.material_slots[0].material, 'CYCLES')[1]
        assert socket != output.inputs['Volume']  # The original failure trigger.
        copy = evaluated.data.copy()
        copy.materials[0] = material
        # Legacy input without recorded material must resolve the original tree.
        records, _ = _volume_records([({'name': name}, socket, copy)])
        assert records[0]['material'] == material.name
        bpy.data.meshes.remove(copy)

bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -2))
bpy.context.object.name = 'Floor'
bpy.context.object.data.materials.append(bpy.data.materials.new('FloorMaterial'))
source = folder / 'Source.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(source))
source_hash = file_hash(source)
job = Job.create(folder / 'jobs', Settings(flow='scene', scene=str(source), blender=bpy.app.binary_path,
                                         character_output_dir=str(folder), volume_resolution=8, bake_resolution=1024))
export_usd_scene(job)
assert file_hash(source) == source_hash
checkpoint = read_json(job.folder / 'scene_checkpoint.json')
assert len(checkpoint['volumes']) == 2
_, volumes = _load_frozen(job, checkpoint, SimpleNamespace())
for record, socket, mesh in volumes:
    material = bpy.data.materials[record['material']]
    assert socket == material_channels(material, 'CYCLES')[1]
    assert mesh.materials[record['material_slot']] == material
records, _ = _volume_records(volumes)
assert {record['material'] for record in records} == {'SurfaceVolume_ObjectMaterial', 'PureVolume_ObjectMaterial'}
# Older valid snapshots lack material identity inside the volume record.
for saved in checkpoint['volumes']:
    saved['record'].pop('material')
    saved['record'].pop('material_slot')
_, legacy_volumes = _load_frozen(job, checkpoint, SimpleNamespace())
assert len(_volume_records(legacy_volumes)[0]) == 2
scene = read_json(job.folder / 'manifest.json')['scene']
assert len(scene['volumes']) == 2
assert all(volume['status'] == 'converted' for volume in scene['volumes']), scene['volumes']
atomic_json(folder / 'result.json', {'complete': True, 'evaluated_socket_mismatch_reproduced': True,
                                   'object_material_overrides_preserved': True, 'volume_count': 2,
                                   'snapshot_reload_verified': True, 'legacy_snapshot_verified': True,
                                   'source_unchanged': True, 'job': str(job.folder)})
print('VOLUME_CHECKPOINT_FULL_EXPORT_PASSED', flush=True)
