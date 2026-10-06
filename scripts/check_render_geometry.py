"""Blender integration regression: render modifiers and embedded vegetation outputs."""
import sys
from pathlib import Path
import bpy
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.blender_render import with_render_depsgraph
from mmd_bridge.blender_appearance import material_channels, expose_material_output

obj = bpy.data.objects['Cube']
modifier = obj.modifiers.new('Render only subdivision', 'SUBSURF')
modifier.show_viewport = False
modifier.show_render = True
modifier.render_levels = 2
viewport_faces = len(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.polygons)
saved = bpy.context.scene.render.engine
def capture(deps):
    assert deps.mode == 'RENDER'
    return len(obj.evaluated_get(deps).data.polygons)
assert with_render_depsgraph(bpy.context.scene, capture) == 96
assert viewport_faces == 6
assert bpy.context.scene.render.engine == saved
try:
    with_render_depsgraph(bpy.context.scene, lambda deps: (_ for _ in ()).throw(ValueError('test failure')))
except ValueError as error:
    assert str(error) == 'test failure'
else:
    raise AssertionError('render callback failure was swallowed')
assert bpy.context.scene.render.engine == saved

material = bpy.data.materials.new('Vegetation embedded output')
material.use_nodes = True
material.node_tree.nodes.clear()
tree = bpy.data.node_groups.new('Shared vegetation', 'ShaderNodeTree')
shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
shader.inputs['Base Color'].default_value = (.1, .6, .2, 1)
terminal = tree.nodes.new('ShaderNodeOutputMaterial')
tree.links.new(shader.outputs[0], terminal.inputs['Surface'])
node = material.node_tree.nodes.new('ShaderNodeGroup')
node.node_tree = tree
assert material_channels(material, 'CYCLES')[0] is True
assert expose_material_output(material, 'CYCLES') is True
assert node.node_tree != tree
assert len(tree.interface.items_tree) == 0
root = material.node_tree.get_output_node('CYCLES')
assert root.inputs['Surface'].is_linked
assert material_channels(material, 'CYCLES') == (True, None)
assert expose_material_output(material, 'CYCLES') is False
volume = bpy.data.materials.new('Volume only')
volume.use_nodes = True
volume.node_tree.nodes.clear()
out = volume.node_tree.nodes.new('ShaderNodeOutputMaterial')
fog = volume.node_tree.nodes.new('ShaderNodeVolumeScatter')
volume.node_tree.links.new(fog.outputs[0], out.inputs['Volume'])
assert material_channels(volume, 'CYCLES')[0] is False
assert material_channels(volume, 'CYCLES')[1] is not None
print('RENDER_GEOMETRY_REGRESSION_PASSED', flush=True)
from mmd_bridge.core import Job, Settings, read_json
from mmd_bridge.blender_usd import export_usd_scene
folder = Path(__file__).resolve().parents[1] / 'work/render_geometry_regression'
folder.mkdir(parents=True, exist_ok=True)
obj.data.materials.clear()
obj.data.materials.append(material)
scene_file = folder / 'RenderModifiers.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(scene_file), check_existing=False)
job = Job.create(folder / 'jobs', Settings(flow='scene', scene=str(scene_file),
    blender=bpy.app.binary_path, restore_scene_appearance=False,
    character_output_dir=str(folder), content_root='/Game/RenderGeometryRegression'))
export_usd_scene(job)
record = read_json(job.folder / 'manifest.json')['scene']
assert record['evaluation_mode'] == 'RENDER'
assert record['stats']['triangles_unique'] == 192
assert record['stats']['instances'] == 1
assert not record['skipped_items']
print('USD_RENDER_MODIFIER_REGRESSION_PASSED', job.folder, flush=True)
