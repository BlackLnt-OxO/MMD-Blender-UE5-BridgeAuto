"""Real source material regression in isolated meshes; never save the source."""
import sys, traceback
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.scene_shader_data import ensure_generated_layers, pin_generated, object_random
from mmd_bridge.material_graph import analyze_material
from mmd_bridge.core import atomic_json, file_hash
from mmd_bridge.blender_render import with_render_depsgraph
SOURCE=Path(r"F:\MMD_Scene\Blender\SableFiSh\MyGO-ParkingLots\It'sMyGO!!!!!-ParkingLots.blend")
OUT=ROOT/'work/material_restoration/mygo';OUT.mkdir(parents=True,exist_ok=True)
before=file_hash(SOURCE)
bpy.ops.wm.open_mainfile(filepath=str(SOURCE),load_ui=False,use_scripts=False)
scene=bpy.context.scene
names={'tomori','Material.008','anno','Material.003','neko','Material.010','riki','Material.009','soyo','Material.007',
       'Bark_Aesculus-hippocastanum_bqm','Leaf_Aesculus-hippocastanum_bqm','riki_gold',
       'anno_y','anno_lo','rana_ufp2','rana_ubplogo','rana_con2','riki_db','riki_dw',
       'uwb_pinkt','uwb_violett','uwb_yellowt','uwb_greent','umb_wt','riki_lo',
       'Patterned_Concrete_ugfnfifo','Parking_Block_tltrecmfa'}
targets={}
for obj in bpy.data.objects:
    if obj.type != 'MESH':continue
    for mat in obj.data.materials:
        if mat and mat.name in names and mat.name not in targets:targets[mat.name]=obj
for name in ('Cube.049','Cube.050','Cube.051'):
    targets['GN_'+name]=bpy.data.objects[name]
frozen=[]
def capture(deps):
    for key,obj in targets.items():
        evaluated=obj.evaluated_get(deps)
        mesh=evaluated.data.copy()
        original_materials=list(mesh.materials)
        if any('generated' in analyze_material(mat,scene.render.engine).get('domains',[]) for mat in original_materials if mat):
            try:ensure_generated_layers(obj,mesh)
            except ValueError as error:mesh['_mmd_generated_error']=str(error)
        frozen.append((key,obj.name,obj.matrix_world.copy(),mesh,original_materials))
with_render_depsgraph(scene,capture)
scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=1
converter=Materials(OUT/'Scene',str(SOURCE),128,conversion_timeout=120)
converter.restore_scene_appearance=True;converter.isolated_baking=True
converter.progress=lambda state,message:print(message,flush=True)
records=[];failures=[]
for key,name,matrix,mesh,originals in frozen:
    obj=bpy.data.objects.new('Audit_'+name,mesh);scene.collection.objects.link(obj);obj.matrix_world=matrix
    obj['_mmd_object_random']=object_random(name);obj['_mmd_geometry_identity']=key
    if mesh.get('_mmd_generated_error'):obj['_mmd_generated_error']=mesh['_mmd_generated_error']
    copies=[]
    for index,mat in enumerate(originals):
        if not mat:continue
        copy=mat.copy();copies.append(copy);mesh.materials[index]=copy
        if copy.use_nodes:pin_generated(copy.node_tree,mesh)
    for index,mat in enumerate(originals):
        if not mat or (not key.startswith('GN_') and mat.name != key):continue
        copy=mesh.materials[index]
        try:
            record=converter.analyze(copy,obj,source_name=mat.name,geometry_identity=key)
            record.setdefault('audit_contexts',[]).append(key)
            records.append(record)
            print('MATERIAL_PASS',key,mat.name,record['strategy'],record['conversion_status'],flush=True)
        except Exception as error:
            fallback=converter.fallback(copy,str(error),mat.name)
            records.append(fallback);failures.append(dict(object=name,material=mat.name,error=traceback.format_exc()))
            print('MATERIAL_FAIL',key,mat.name,error,flush=True)
        atomic_json(OUT/'records.json',records)
        atomic_json(OUT/'result.json',dict(complete=False,failures=failures,records=len(records)))
    bpy.data.objects.remove(obj,do_unlink=True)
assert before==file_hash(SOURCE),'source changed'
atomic_json(OUT/'result.json',dict(complete=not failures,failures=failures,records=len(records),source_sha256=before,
    blender_version=bpy.app.version_string,missing_materials=sorted(names-set(targets))))
assert not failures,failures
print('MYGO_RESTORATION_PASS',len(records),flush=True)
