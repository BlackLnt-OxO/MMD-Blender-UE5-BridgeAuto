"""A fresh MyGO main-workflow import through the same Controller as the GUI."""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from mmd_bridge.controller import Controller
from mmd_bridge.core import Settings, atomic_json, read_json, file_hash

OUT = ROOT/'work/mygo_program/main_clean_20261007_01'
transfer = OUT/'Transfer'
namespace = '/Game/MyGO_Main_Clean_20261007_01'
project = Path('F:/Ue5_Project/TESTON1_1005_02/TESTON1_1005_02.uproject')
source = Path("F:/MMD_Scene/Blender/SableFiSh/MyGO-ParkingLots/It'sMyGO!!!!!-ParkingLots.blend")
old = project.parent/'Content/On_9_24_开发_PlanV2/On_9_25/On_9_26/A_Scene_It_sMyGO_ParkingLots_01_487f7848/Maps/MMD_Preview.umap'
assert not transfer.exists(), 'fresh import requires a new empty transfer directory'
assert not (project.parent/'Content'/namespace.removeprefix('/Game/')).exists(), 'namespace must be absent'
transfer.mkdir(parents=True)
before = {'source':file_hash(source), 'old_map':file_hash(old)}
atomic_json(OUT/'preflight.json', dict(before, transfer=str(transfer), namespace=namespace,
    empty_transfer=True, namespace_absent=True, prior_job_reused=False))
settings = Settings(flow='scene',scene=str(source),blender='F:/SteamLibrary/steamapps/common/Blender/blender.exe',
    unreal='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe',project=str(project),
    character_output_dir=str(transfer),content_root=namespace,material_workflow='legacy_main_v1',
    scene_import_profile='faithful', material_strategy='auto', restore_scene_appearance=True,
    scene_viewport_ocio=True, scene_render_pipeline='lumen',bake_resolution=1024,volume_resolution=256,
    scene_master_material='',material_bake_fallback=True,material_complex_strategy='pbr_bake',
    material_conversion_timeout=120,unattended=True)
controller = Controller(); job = controller.start(settings)
atomic_json(OUT/'current.json',{'job':str(job.folder),'phase':'running'})
previous = None
while True:
    status = controller.poll()
    if status != previous:
        print(status['state'],status.get('message','')[:240],flush=True);previous=status
    atomic_json(OUT/'current.json',{'job':str(job.folder),'phase':'running','status':status})
    if status['state'] in ('complete','needs_review','failed','stopped') and not controller.processes:
        break
    time.sleep(2)
if status['state'] in ('failed','stopped'): raise RuntimeError(status['message'])
manifest = read_json(job.folder/'manifest.json'); report = read_json(job.folder/'ue_report.json')
outside = [p for p in report['assets']['source_packages'] if p.startswith('/Game/') and not p.startswith(namespace+'/')]
assert not outside, outside
assert file_hash(source)==before['source'] and file_hash(old)==before['old_map'], 'read-only controls changed'
assert not report['checks']['material_compile_errors'], report['checks']['material_compile_errors']
assert report['checks']['ocio']['status']=='validated'
atomic_json(OUT/'result.json',{'import_finished':True,'job':str(job.folder),'status':status['state'],
    'root':report['root'],'level':report['assets']['level'],'restoration':report['checks']['material_restoration'],
    'white_materials':len(read_json(job.folder/'white_materials.json')['materials']),
    'source_unchanged':True,'old_map_unchanged':True,'no_old_game_dependencies':True,
    'no_automatic_white_repair':not report.get('material_repairs'),
    'visual_status':'needs_review','ue_failures':report['failures'],'checks':report['checks']})
print('MYGO_MAIN_CLEAN_IMPORT_FINISHED',flush=True)
