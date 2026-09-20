"""Focused MyGO regression; hide unrelated objects only in the unsaved session."""
import bpy
import sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.core import Job
from mmd_bridge.blender_usd import export_usd_scene

@bpy.app.handlers.persistent
def select_fixture(_):
    source = bpy.data.objects.get('f speaker')
    if source is not None:
        deps = bpy.context.evaluated_depsgraph_get()
        evaluated = source.evaluated_get(deps)
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=deps)
        fixture = bpy.data.objects.new('Acceptance_Speaker', mesh)
        bpy.context.scene.collection.objects.link(fixture)
        fixture.matrix_world = source.matrix_world
    for obj in bpy.data.objects:
        obj.hide_render = obj.name not in {'ground', 'Acceptance_Speaker'}

bpy.app.handlers.load_post.append(select_fixture)
try:
    export_usd_scene(Job(sys.argv[sys.argv.index('--') + 1]))
finally:
    bpy.app.handlers.load_post.remove(select_fixture)

