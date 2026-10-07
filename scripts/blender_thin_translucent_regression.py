"""Real Blender tests for main thin sheets, coverage and shared closure functions."""
import sys
from pathlib import Path
import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.material_contract import has_translucent_closure
from mmd_bridge.material_policy import masked_surface
from mmd_bridge.core import atomic_json, Settings

OUT = ROOT / 'work/material_restoration/main_translucent_native'
OUT.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
mesh = bpy.data.meshes.new('Sheet')
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
uv = mesh.uv_layers.new(name='UVMap')
for loop, value in zip(uv.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
    loop.uv = value
obj = bpy.data.objects.new('Sheet', mesh)
scene.collection.objects.link(obj)
image = bpy.data.images.new('IndependentCoverage', width=2, height=1, alpha=True)
image.pixels[:] = [1, 1, 1, 0, 1, 1, 1, 1]
image.filepath_raw = str(OUT / 'coverage.png')
image.file_format = 'PNG'
image.save()


def thin(tree):
    node = tree.nodes.new('ShaderNodeBsdfTranslucent')
    node.inputs['Color'].default_value = (.12, .44, .17, 1)
    normal = tree.nodes.new('ShaderNodeNormalMap')
    normal.inputs['Color'].default_value = (.5, .5, 1, 1)
    tree.links.new(normal.outputs['Normal'], node.inputs['Normal'])
    return node


group = bpy.data.node_groups.new('SharedThinSheet', 'ShaderNodeTree')
group.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
group_output = group.nodes.new('NodeGroupOutput')
group.links.new(thin(group).outputs[0], group_output.inputs[0])
converter = Materials(OUT / 'Scene', str(OUT / 'Fixture.blend'), 64, workflow='legacy_main_v1')
converter.restore_scene_appearance = True
records = []
for name in ('RootThin', 'CutoutThin', 'SharedThinA', 'SharedThinB'):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    source = tree.nodes.new('ShaderNodeGroup') if name.startswith('Shared') else thin(tree)
    if name.startswith('Shared'):
        source.node_tree = group
    if name == 'CutoutThin':
        transparent = tree.nodes.new('ShaderNodeBsdfTransparent')
        texture = tree.nodes.new('ShaderNodeTexImage')
        texture.image = image
        mix = tree.nodes.new('ShaderNodeMixShader')
        tree.links.new(texture.outputs['Alpha'], mix.inputs[0])
        tree.links.new(transparent.outputs[0], mix.inputs[1])
        tree.links.new(source.outputs[0], mix.inputs[2])
        source = mix
    tree.links.new(source.outputs[0], output.inputs['Surface'])
    mesh.materials.clear()
    mesh.materials.append(material)
    record = converter.analyze(material, obj, source_name=name, geometry_identity=name)
    expanded = converter.functions.expand(record['surface_graph'])
    assert has_translucent_closure(expanded)
    assert record['legacy_thin_sheet'] and record['thin_surface_transmission']
    assert masked_surface(record)
    assert not record['baked_channels'], record
    assert record['conversion_status'] == 'approximated'

    def leaves(graph):
        if graph['kind'] in ('mix', 'add'):
            return leaves(graph['a']) + leaves(graph['b'])
        return [graph]

    branch = next(g for g in leaves(expanded) if g['kind'] == 'bsdf_translucent')
    assert all(abs(a - b) < 1e-6 for a, b in zip(branch['channels']['base_color']['value'], (.12, .44, .17, 1)))
    assert branch['channels']['normal']['op'] == 'normal'
    if name == 'CutoutThin':
        assert record['channels']['opacity']['op'] != 'constant'
    else:
        assert record['channels']['opacity'] == {'op': 'constant', 'value': 1}
    records.append(record)
assert Settings().thin_surface_scattering == 'legacy_diffusion'
atomic_json(OUT / 'records.json', records)
atomic_json(OUT / 'result.json', {'complete': True, 'cases': len(records),
    'shared_function_thin_flag': True, 'independent_coverage': True,
    'source_color_normal_preserved': True, 'no_baking': True,
    'default_scattering': 'legacy_diffusion', 'optional_scattering': 'two_sided_wrap'})
print('MAIN_TRANSLUCENT_BLENDER_PASS', len(records), flush=True)
