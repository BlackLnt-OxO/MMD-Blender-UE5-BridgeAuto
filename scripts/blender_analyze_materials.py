"""Read source graphs without geometry evaluation, baking, or saving the blend."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bpy
from mmd_bridge.material_graph import analyze_material,VERSION
from mmd_bridge.core import atomic_json,read_json
args=sys.argv[sys.argv.index('--')+1:]
request=read_json(args[0])
bpy.ops.wm.open_mainfile(filepath=request['source'],load_ui=False,use_scripts=False)
scene=bpy.context.scene;scene.frame_set(int(request.get('frame',scene.frame_current)))
materials={slot.material.name:slot.material for obj in scene.objects for slot in obj.material_slots if slot.material}
start=time.perf_counter();records=[]
for name,material in sorted(materials.items()):
    if Path(request['stop']).exists():
        atomic_json(request['report'],{'complete':False,'state':'stopped','materials':records});break
    record=analyze_material(material,scene.render.engine);record['name']=name;records.append(record)
    print('MATERIAL_ANALYSIS '+name,flush=True)
else:
    atomic_json(request['report'],{'complete':True,'state':'analyzed','version':VERSION,
        'source':request['source'],'frame':scene.frame_current,'materials':records,
        'seconds':time.perf_counter()-start,'basis':'estimate','visual_status':'needs_review'})
