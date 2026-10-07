"""Small native acceptance scene; source fixture is intentionally generated."""
import bpy, sys
from pathlib import Path
from mathutils import Vector
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'work/material_restoration/workflow_native_v1';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.samples=16
world=bpy.data.worlds.new('World');world.use_nodes=True;scene.world=world
world.node_tree.nodes.get('Background').inputs['Color'].default_value=(.6,.7,.9,1)
fog=world.node_tree.nodes.new('ShaderNodeVolumeScatter');fog.inputs['Density'].default_value=.0001
world.node_tree.links.new(fog.outputs[0],world.node_tree.nodes.get('World Output').inputs['Volume'])
blue=bpy.data.materials.new('SharedBluePaint');blue.use_nodes=True
bsdf=blue.node_tree.nodes.get('Principled BSDF');bsdf.inputs['Base Color'].default_value=(.05,.15,.55,1);bsdf.inputs['Metallic'].default_value=.6;bsdf.inputs['Coat Weight'].default_value=1
bpy.ops.mesh.primitive_cube_add(location=(-2,0,0));good=bpy.context.object;good.name='ProtectedBlue';protected=blue.copy();protected.name='ProtectedBluePaint';protected.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value=(.06,.15,.55,1);good.data.materials.append(protected)
bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8,location=(2,0,0));target=bpy.context.object;target.name='RepairTarget';target.data.materials.append(blue)
image=bpy.data.images.new('RepairUVPattern',width=4,height=4)
image.pixels.foreach_set([v for y in range(4) for x in range(4) for v in ((.05,.15,.55,1) if (x+y)%2 else (.1,.3,.7,1))]);image.pack()
uv=blue.node_tree.nodes.new('ShaderNodeUVMap');tex=blue.node_tree.nodes.new('ShaderNodeTexImage');tex.image=image
blue.node_tree.links.new(uv.outputs[0],tex.inputs['Vector']);blue.node_tree.links.new(tex.outputs['Color'],bsdf.inputs['Base Color'])
bpy.ops.mesh.primitive_plane_add(size=3,location=(0,-3,0));plane=bpy.context.object;plane.name='FineChecker'
checker=bpy.data.materials.new('FineChecker');checker.use_nodes=True;nodes=checker.node_tree.nodes;links=checker.node_tree.links
uv=nodes.new('ShaderNodeUVMap');tex=nodes.new('ShaderNodeTexChecker');tex.inputs['Scale'].default_value=250
links.new(uv.outputs[0],tex.inputs['Vector']);links.new(tex.outputs['Color'],nodes.get('Principled BSDF').inputs['Base Color']);plane.data.materials.append(checker)
bpy.ops.mesh.primitive_cube_add(size=2,location=(0,3,0));container=bpy.context.object;container.name='ProceduralFog';container.scale=(2,1,.5)
mat=bpy.data.materials.new('Fog');mat.use_nodes=True;nodes=mat.node_tree.nodes;nodes.clear();links=mat.node_tree.links
out=nodes.new('ShaderNodeOutputMaterial');scatter=nodes.new('ShaderNodeVolumeScatter');noise=nodes.new('ShaderNodeTexNoise');coord=nodes.new('ShaderNodeTexCoord');math=nodes.new('ShaderNodeMath');math.operation='MULTIPLY';math.inputs[1].default_value=.02
links.new(coord.outputs['Generated'],noise.inputs['Vector']);links.new(noise.outputs['Fac'],math.inputs[0]);links.new(math.outputs[0],scatter.inputs['Density']);links.new(scatter.outputs[0],out.inputs['Volume']);container.data.materials.append(mat)
bpy.ops.object.camera_add(location=(9,-12,8));camera=bpy.context.object;camera.rotation_euler=(Vector((0,0,0))-camera.location).to_track_quat('-Z','Y').to_euler();scene.camera=camera
scene.render.resolution_x=640;scene.render.resolution_y=360
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'Fixture.blend'))
print('WORKFLOW_FIXTURE_CREATED',flush=True)
