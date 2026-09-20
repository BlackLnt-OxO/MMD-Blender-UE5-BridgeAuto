"""Run each mode in a separate Blender process for comparable memory peaks."""
import importlib.util
import json
import sys
import time
from pathlib import Path
import bpy
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge import blender_usd as current
mode = sys.argv[sys.argv.index('--')+1]
module = current
if mode == 'old':
    spec = importlib.util.spec_from_file_location('mmd_bridge._old', root/'work/upstream58/blender_usd_before_perf.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=500, y_subdivisions=500)
start = time.perf_counter()
data = module._mesh_data(bpy.context.object.data)
extraction = time.perf_counter()-start
group = {'id':'dense','geometry':data,'materials':['a'],
         'polygon_materials':[], 'collision':True,'instances':[]}
out = root/'work/upstream58/usd_perf'
start = time.perf_counter()
module._write_chunk(out/(mode+'_dense.usdc'),0,[group],1,True)
result = {'mode':mode, 'triangles':data['triangles'], 'extract_seconds':extraction,
          'write_seconds':time.perf_counter()-start,
          'peak_mb':current._peak_working_set_mb(), 'working_mb':current._peak_working_set_mb(True)}
(out/(mode+'_memory.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result),flush=True)
