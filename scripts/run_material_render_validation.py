"""Monitor only our isolated UE worker; preserve the project's ini and uproject."""
import os,sys,shutil,subprocess,time,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.standalone_render import make_plan,finish_result
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
OUT=ROOT/'work/material_functions_implementation';job=Job(OUT/'tree_render_job')
data=read_json(job.path);project=Path(data['settings']['project']);assert project.stem=='TESTON1_1005_02'
data['settings']['unreal']=r'C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe'
atomic_json(job.path,data)
plugin=project.parent/'Plugins/MMDBridge';marker=plugin/'.mmd_bridge_managed'
if plugin.exists() and not marker.exists():raise RuntimeError('Existing plugin is not managed')
plugin.mkdir(parents=True,exist_ok=True)
shutil.copy2(ROOT/'plugins/unreal/MMDBridge/MMDBridge.uplugin',plugin/'MMDBridge.uplugin')
python=plugin/'Content/Python';modules=python/'mmd_bridge';modules.mkdir(parents=True,exist_ok=True)
for name in ('__init__.py','core.py','process.py','ue_standalone_render.py','ue_foliage.py','ue_render_policy.py','ue_shader_data.py'):
    shutil.copy2(ROOT/'mmd_bridge'/name,modules/name)
shutil.copy2(ROOT/'plugins/unreal/MMDBridge/Content/Python/init_unreal.py',python/'init_unreal.py')
marker.write_text('MMDBridge material validation runtime\n',encoding='utf-8')
report=read_json(job.folder/'ue_report.json');results=[]
frames=[None] if '--300' in sys.argv else [0]*3
for index,frame in enumerate(frames):
    memory=gpu_memory_info();percent=gpu_budget_percent(memory)
    if percent is None or percent>=80:
        results.append(dict(complete=False,state='waiting_gpu_budget',gpu=memory));break
    run,request,command=make_plan(job,report['assets']['render_preset'],frame=frame)
    command+=['-EnablePlugins=MMDBridge,USDImporter,MovieRenderPipeline']
    environment=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'))
    with (run/'launcher.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(command,env=environment,cwd=str(Path(command[0]).parent),stdout=log,stderr=subprocess.STDOUT)
        atomic_json(run/'plan.json',dict(request=request,command=command,pid=child.pid,started=True))
        while child.poll() is None:
            time.sleep(2)
            memory=gpu_memory_info();percent=gpu_budget_percent(memory)
            if percent is not None and percent>=90:
                child.terminate();child.wait(timeout=20)
                runtime=read_json(run/'runtime.json') if (run/'runtime.json').is_file() else {}
                atomic_json(run/'runtime.json',dict(runtime,success=False,gpu_budget_exceeded=True,gpu=memory,stopped_by='external_watchdog'))
    result=finish_result(run,child.returncode);results.append(result)
    print(json.dumps(dict(index=index,complete=result['complete'],run=str(run)),ensure_ascii=False),flush=True)
    atomic_json(OUT/('tree_300_result.json' if frame is None else 'tree_cold_result.json'),results)
    if not result['complete']:break
