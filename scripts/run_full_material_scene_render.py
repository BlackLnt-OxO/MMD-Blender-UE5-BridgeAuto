"""Guarded reference frame in the already imported full railway test scene."""
import os,sys,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
from mmd_bridge.standalone_render import install_runtime,make_plan,finish_result
from mmd_bridge.ue_render_policy import shader_worker_arguments
out=ROOT/'work/material_functions_implementation/full_scene'
job=Job(read_json(out/'job_path.json')['job']);cfg=job.data['settings']
assert Path(cfg['project']).stem=='TESTON1_1005_02'
assert not read_json(job.folder/'ue_report.json')['failures']
memory=gpu_memory_info();percent=gpu_budget_percent(memory)
if percent is None or percent>=80:raise RuntimeError('GPU memory must be below 80% before validation')
command=[str(Path(cfg['unreal']).with_name('UnrealEditor-Cmd.exe')),cfg['project'],'/Engine/Maps/Entry',
    '-unattended','-nosplash','-nosound','-RenderOffscreen','-EnablePlugins=USDImporter,MovieRenderPipeline',
    '-ExecutePythonScript='+str(ROOT/'scripts/ue_prepare_full_material_render.py'),
    '-abslog='+str(out/'render_preparation.log')]+shader_worker_arguments(cfg)
environment=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1')
with (out/'preparation_launcher.log').open('w',encoding='utf-8') as stream:
    subprocess.run(command,env=environment,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=180)
assert read_json(out/'render_preparation.json')['complete']
install_runtime(cfg['project'])
report=read_json(job.folder/'ue_report.json')
run,request,command=make_plan(job,report['assets']['render_preset'],frame=1)
command+=['-EnablePlugins=MMDBridge,USDImporter,MovieRenderPipeline']
environment['MMDBRIDGE_STANDALONE_REQUEST']=str(run/'request.json')
deadline=time.monotonic()+1200;reason=None;previous=None;peak={}
with (run/'launcher.log').open('w',encoding='utf-8') as stream:
    child=subprocess.Popen(command,env=environment,stdout=stream,stderr=subprocess.STDOUT)
    atomic_json(run/'plan.json',{'command':command,'request':request,'pid':child.pid,'started':True})
    print('FULL_RENDER_RUN',run,flush=True)
    while child.poll() is None:
        time.sleep(2)
        memory=gpu_memory_info();percent=gpu_budget_percent(memory)
        for key,value in (memory or {}).items():
            if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
        runtime=read_json(run/'runtime.json') if (run/'runtime.json').is_file() else {}
        state=runtime.get('state')
        if state!=previous:previous=state;print('FULL_RENDER_STATE',state,flush=True)
        if percent is not None and percent>=90:reason='gpu_budget_exceeded'
        if time.monotonic()>deadline:reason='validation_timeout'
        if reason:
            child.terminate();child.wait(timeout=20)
            atomic_json(run/'runtime.json',dict(runtime,success=False,stopped_by=reason,gpu=memory,
                gpu_budget_exceeded=reason=='gpu_budget_exceeded'))
result=finish_result(run,child.returncode);result['gpu_global_peak']=peak
atomic_json(out/'render_result.json',result)
print('FULL_RENDER_COMPLETE',result['complete'],reason,flush=True)
