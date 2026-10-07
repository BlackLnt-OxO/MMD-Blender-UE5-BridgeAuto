"""Fault injection is confined to a generated fixture, never a user scene."""
import sys,time,json,copy,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.controller import Controller
from mmd_bridge.core import Settings,read_json,atomic_json,file_hash
OUT=ROOT/'work/material_restoration/workflow_native_v1'
controller=Controller(); source=OUT/'Fixture.blend'
(OUT/'Transfer').mkdir(exist_ok=True)
settings=Settings(flow='scene',scene=str(source),blender='F:/SteamLibrary/steamapps/common/Blender/blender.exe',
    unreal='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe',
    project='F:/Ue5_Project/TESTON1_1005_02/TESTON1_1005_02.uproject',character_output_dir=str(OUT/'Transfer'),
    content_root='/Game/MMDBridgeValidation/WorkflowNativeV1',material_workflow='legacy_main_v1',
    bake_resolution=1024,volume_resolution=24,optimize_usd_scatter=False,unattended=True)
uv_fault = '--uv-fault' in sys.argv
resuming = len(sys.argv) > 1 and not sys.argv[1].startswith('--')
if resuming:
    path = Path(sys.argv[1]).resolve()
    assert path.is_relative_to((OUT/'Transfer/MMDBridge_Jobs').resolve()), 'only generated fixtures may resume'
    job=controller.load_scene_results(path)
else:
    job=controller.start(settings)
    atomic_json(OUT/'current.json',{'job':str(job.folder),'phase':'exporting'})
    for mode,child in controller.processes:
        child.wait()
        if child.returncode:raise RuntimeError('fixture export failed')
    controller.processes=[]
    if uv_fault:
        with (OUT/'uv_fault.log').open('w',encoding='utf-8') as log:
            subprocess.run([settings.blender,'--background','--factory-startup','--python-exit-code','1',
                '--python',str(ROOT/'scripts/blender_workflow_uv_fault.py'),'--',str(job.folder)],check=True,stdout=log,stderr=subprocess.STDOUT)
manifest=read_json(job.folder/'manifest.json')
pending_undo = None
if resuming:
    for candidate in sorted((job.folder/'MaterialRepairs').glob('R_*'), reverse=True):
        receipt=read_json(candidate/'receipt.json') if (candidate/'receipt.json').is_file() else {}
        if receipt.get('committed') and not receipt.get('undone'):
            pending_undo=candidate; manifest=read_json(candidate/'manifest.json'); break
from mmd_bridge.material_results import restoration_rows
rows=restoration_rows(manifest,{})
chosen=next(r['id'] for r in rows if 'RepairTarget' in r['objects'])
for record in manifest['scene']['materials']:
    if record['id']==chosen and not resuming:
        record['material_error']='Controlled native validation fault: material failed'
        record['conversion_status']='placeholder';record['requires_visual_review']=True
        record['channels']={'base_color':{'op':'constant','value':[.5,.5,.5,1]}}
if not resuming: atomic_json(job.folder/'manifest.json',manifest)
def wait(phase):
    previous=None
    for _ in range(1800):
        status=controller.poll()
        if status!=previous:print(phase,status['state'],status.get('message','')[:180],flush=True);previous=status
        atomic_json(OUT/'current.json',{'job':str(job.folder),'phase':phase,'status':status})
        if status['state'] in ('complete','needs_review','failed','stopped') and not controller.processes and not controller.material_repair:
            if status['state'] in ('failed','stopped'):raise RuntimeError(status['message'])
            return
        time.sleep(2)
    raise RuntimeError('native fixture timed out')
if not resuming: wait('baseline')
if pending_undo:
    controller.undo_material_repair(pending_undo.name);wait('recover_undo')
report=read_json(job.folder/'ue_report.json')
assert not report['checks']['material_compile_errors']
assert len(report['checks']['usd_stage']['volumes']) == 1
protected=copy.deepcopy([r for r in read_json(job.folder/'manifest.json')['scene']['materials'] if r['id']!=chosen])
batch=controller.repair_white_materials([chosen]);wait('repair')
receipt=read_json(batch/'receipt.json');assert receipt['committed'],receipt
added_uv=read_json(batch/'export.json')['layers'][chosen]['added_uv_channels']
if uv_fault: assert added_uv == [1], added_uv
after=read_json(job.folder/'manifest.json');assert [r for r in after['scene']['materials'] if r['id']!=chosen]==protected
controller.undo_material_repair(batch.name);wait('undo')
assert read_json(batch/'receipt.json')['undone']
assert read_json(job.folder/'manifest.json')==manifest
atomic_json(OUT/'result.json',{'complete':True,'job':str(job.folder),'batch':str(batch),'repair':receipt,
    'undo_verified':True,'unselected_materials_unchanged':True,'source_unchanged':file_hash(source)==job.data['inputs']['scene'],
    'added_uv_channels':added_uv,
    'baseline':report['checks'],'validation_scope':'native import, binding, UV guard, undo; visual acceptance separate'})
print('WORKFLOW_NATIVE_PASS',flush=True)
