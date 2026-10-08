"""Run serial native fixture variants with normal resource guards and receipts."""
import argparse,os,sys,time,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.standalone_render import make_plan,finish_result,install_runtime
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent,terminate_pid

parser=argparse.ArgumentParser();parser.add_argument('--out',required=True)
parser.add_argument('--cases',nargs='+',default=['grid:baseline','grid:vsm','grid:path_tracing'])
parser.add_argument('--frames',type=int,default=1)
parser.add_argument('--width',choices=('2','5','10','masked'),default='5');args=parser.parse_args()
out=Path(args.out).resolve();fixtures=read_json(out/'fixtures.json')
project='F:/Ue5_Project/TESTON_1005_Clean/TESTON_1005_Clean.uproject'
install_runtime(project)
results=[]
for case in args.cases:
    kind,variant=case.split(':');fixture=next(x for x in fixtures['fixtures'] if x['kind']==kind)
    pipeline='path_tracing' if variant.startswith('path_tracing') else 'lumen'
    job=Job(out/(kind+'_job'))
    atomic_json(job.path,dict(settings=dict(project=project,unreal='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe',
        width=1280,height=720,ue_vram_pause_percent=90,ue_vram_resume_percent=80)))
    atomic_json(job.folder/'ue_report.json',dict(assets={k:fixture[k] for k in ('level','sequence','render_preset')}))
    run,request,command=make_plan(job,fixture['render_preset'],frame=16 if args.frames==1 or pipeline=='path_tracing' else None,pipeline=pipeline)
    request['fog_test_variant']=variant;request['capture_gpu_csv']=args.frames>1 and pipeline=='lumen'
    request['fog_test_fixture']=fixture;request['fog_test_fixture_version']=fixtures.get('version',1)
    request['fog_test_width_cm']=int(args.width) if args.width!='masked' else 'masked'
    atomic_json(run/'request.json',request)
    command.append('-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:StartupScripts='+str(ROOT/'scripts/ue_fog_occlusion_runtime.py').replace('\\','/'))
    env=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'))
    started=time.time();last=None
    with (run/'launcher.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=0x08000000)
        atomic_json(out/'active.json',dict(case=case,pid=child.pid,run=str(run),command=command))
        print('FOG_TEST',case,'PID',child.pid,flush=True)
        while child.poll() is None:
            time.sleep(2)
            try:rt=read_json(run/'runtime.json') if (run/'runtime.json').exists() else {}
            except (PermissionError,ValueError):continue
            if rt.get('state')!=last:
                last=rt.get('state');print('FOG_TEST',case,last,flush=True)
            budget=gpu_budget_percent(gpu_memory_info())
            if (budget is not None and budget>=90) or time.time()-started>600:
                terminate_pid(child.pid,expected_executable=command[0]);child.wait(timeout=30)
                atomic_json(run/'guard.json',dict(budget=budget,elapsed=time.time()-started))
        result=finish_result(run,child.returncode)
    atomic_json(out/(kind+'_'+variant+'_w'+args.width+'_result.json'),result)
    atomic_json(out/'active.json',dict(case=case,pid=child.pid,run=str(run),state='finished',complete=result['complete']))
    if args.width=='5':atomic_json(out/(kind+'_'+variant+'_result.json'),result)
    print('FOG_TEST',case,'COMPLETE',result['complete'],flush=True)
    results.append(dict(case=case,run=str(run),complete=result['complete']))
    atomic_json(out/'batch.json',results)
    if not result['complete']:
        raise RuntimeError('Fixture render failed: '+case+'; see '+str(run/'runtime.json'))
