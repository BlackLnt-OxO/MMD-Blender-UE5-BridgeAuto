"""Real Blender fallback coverage for deadlines, early errors and strict mode."""
from pathlib import Path
import sys
import time

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.blender_materials import Materials, MaterialConversionTimeout
from mmd_bridge.core import atomic_json, Cancelled

OUT = ROOT / 'work/pbr_timeout_regression'
OUT.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine = 'CYCLES'
bpy.context.scene.cycles.device = 'CPU'
bpy.ops.mesh.primitive_plane_add()
obj = bpy.context.object
obj['_mmd_uv_pinned'] = True
results = []

for case in ('early_timeout', 'late_timeout', 'conversion_error', 'strict_nodes', 'disabled', 'cancelled'):
    mat = bpy.data.materials.new(case)
    mat.use_nodes = True
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    converter = Materials(OUT / case, 'timeout-fixture', 32,
                          strategy='nodes' if case == 'strict_nodes' else 'auto',
                          bake_fallback=case != 'disabled', conversion_timeout=1)
    converter.isolated_baking = True
    nodes_before = len(mat.node_tree.nodes)
    if case == 'early_timeout':
        native_expr = converter.expr
        def expire(*args, **kwargs):
            converter._conversion_deadline = time.perf_counter() - 1
            return native_expr(*args, **kwargs)
        converter.expr = expire
    else:
        native_analyze = converter._analyze
        def fail(*args, **kwargs):
            record = native_analyze(*args, **kwargs)
            if case == 'cancelled':
                raise Cancelled('用户已停止任务')
            if case == 'conversion_error':
                raise RuntimeError('fixture translator failed')
            converter._conversion_deadline = time.perf_counter() - 1
            converter.check_conversion_budget()
            return record
        converter._analyze = fail
    try:
        record = converter.analyze(mat, obj, source_name=case)
    except (MaterialConversionTimeout, Cancelled):
        assert case in ('strict_nodes', 'disabled', 'cancelled'), case
        assert not converter._active_record.get('basic_pbr_bake')
        results.append({'case': case, 'result': 'explicit_policy_preserved'})
    else:
        assert case not in ('strict_nodes', 'disabled', 'cancelled')
        assert record['basic_pbr_bake']['status'] == 'baked'
        assert set(record['channels']) == {'base_color', 'metallic', 'roughness', 'normal', 'opacity', 'emission'}
        assert all(v['op'] in ('texture','constant') for v in record['channels'].values())
        assert record['fallback_attempts'][0]['route']=='local_fields'
        assert record['fallback_attempts'][-1]['route']=='pbr_bake'
        assert all(Path(v['image']['path']).is_file() for v in record['channels'].values() if v['op']=='texture')
        assert record['requires_visual_review']
        assert not record.get('surface_graph')
        assert converter._conversion_deadline is None
        atomic_json(OUT / (case + '.json'), record)
        results.append({'case': case, 'result': 'pbr_texture_bake', 'channels': list(record['channels'])})
    assert len(mat.node_tree.nodes) == nodes_before

atomic_json(OUT / 'report.json', {'complete': True, 'cases': results})
print('PBR_TIMEOUT_REGRESSION=complete')
