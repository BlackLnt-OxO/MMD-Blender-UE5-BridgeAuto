"""Run with Blender --background --python scripts/blender_volume_fallback_smoke.py."""
import sys
import tempfile
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.blender_materials import Materials

mesh = bpy.data.meshes.new("VolumeMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new("VolumeObject", mesh)
bpy.context.scene.collection.objects.link(obj)
material = bpy.data.materials.new("VolumeScatter")
material.use_nodes = True
nodes = material.node_tree.nodes
nodes.clear()
output = nodes.new("ShaderNodeOutputMaterial")
volume = nodes.new("ShaderNodeVolumeScatter")
material.node_tree.links.new(volume.outputs["Volume"], output.inputs["Volume"])
mesh.materials.append(material)

with tempfile.TemporaryDirectory() as folder:
    materials = Materials(folder, "VolumeScene")
    record = materials.analyze(material, obj)
    assert record["requires_visual_review"]
    assert record["material_error"] == "无有效 Surface 无法可靠还原"
    assert record["channels"]["base_color"]["op"] == "constant"
print("Volume-only material fallback: OK")
