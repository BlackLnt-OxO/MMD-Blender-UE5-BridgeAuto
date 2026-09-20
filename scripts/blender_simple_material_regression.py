"""Run with Blender --background --python; no source scene is modified."""
import json
import sys
from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.blender_materials import Materials

out = root / 'work/upstream58/simple_material_regression'
bpy.ops.mesh.primitive_cube_add(size=0.3)
obj = bpy.context.object
checks = []
for case in ('constant', 'texture', 'normal', 'noise', 'forced_bake', 'unused_noise', 'plain'):
    mat = bpy.data.materials.new(case)
    mat.use_nodes = case != 'plain'
    if mat.use_nodes:
        shader = mat.node_tree.nodes.get('Principled BSDF')
        if case in ('texture', 'normal', 'forced_bake'):
            node = mat.node_tree.nodes.new('ShaderNodeTexImage')
            node.image = bpy.data.images.new(case, width=4, height=4)
            node.image.filepath_raw = str(out / (case + '.png'))
            if case == 'normal':
                normal = mat.node_tree.nodes.new('ShaderNodeNormalMap')
                mat.node_tree.links.new(node.outputs['Color'], normal.inputs['Color'])
                mat.node_tree.links.new(normal.outputs['Normal'], shader.inputs['Normal'])
            else:
                mat.node_tree.links.new(node.outputs['Color'], shader.inputs['Base Color'])
        if case in ('noise', 'unused_noise'):
            node = mat.node_tree.nodes.new('ShaderNodeTexNoise')
            if case == 'noise':
                mat.node_tree.links.new(node.outputs['Fac'], shader.inputs['Roughness'])
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    obj.pop('_mmd_uv_pinned', None)
    materials = Materials(out / case, 'regression', strategy='bake' if case == 'forced_bake' else 'auto')
    calls = []
    def fake_bake(*args, **kwargs):
        calls.append(args[3])
        return {'op': 'constant', 'value': 0.5}
    materials.bake = fake_bake
    record = materials.analyze(mat, obj=obj)
    expected_bake = case in ('noise', 'forced_bake')
    assert bool(calls) == expected_bake, (case, calls)
    assert record['simple_surface'] == (not expected_bake), case
    if not expected_bake:
        assert record['strategy'] == 'nodes' and not record['baked_channels'], case
        assert obj.data.uv_layers.get('MMD_BakeUV') is None, case
    checks.append({'case': case, 'strategy': record['strategy'], 'bake_calls': calls})
out.mkdir(parents=True, exist_ok=True)
(out / 'result.json').write_text(json.dumps({'passed': True, 'checks': checks}, indent=2), encoding='utf-8')
print('SIMPLE_MATERIAL_REGRESSION_PASSED')
