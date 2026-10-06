"""Render one source-camera frame from the isolated saved scene, with GPU guard."""
import os,sys,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
from mmd_bridge.standalone_render import install_runtime,make_plan,finish_result
OUT=ROOT/'work/material_restoration/full_scene'
job=Job(read_json(OUT/'job_path.json')['job']);cfg=job.data['settings']
report=read_json(job.folder/'ue_report.json')
assert not report['failures'],report['failures']
assert report['checks']['ocio']['status']=='validated'
percent=gpu_budget_percent(gpu_memory_info())
if percent is None or percent>=80:raise RuntimeError('GPU must be below 80%')
install_runtime(cfg['project'])
run,request,command=make_plan(job,report['assets']['render_preset'],frame=cfg.get('scene_frame',1),pipeline='lumen')
request['expected_resolution']=read_json(job.folder/'manifest.json')['scene']['reference_camera']['resolution']
if '--front-probe' in sys.argv:
    request['material_overrides']=read_json(OUT/'front_probe_overrides.json')
atomic_json(run/'request.json',request)
command+=['-EnablePlugins=MMDBridge,USDImporter,MovieRenderPipeline']
env=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'))
deadline=time.monotonic()+1200;reason=None;peak={}
with (run/'launcher.log').open('w',encoding='utf-8') as log:
    child=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=0x08000000)
    atomic_json(run/'plan.json',dict(command=command,request=request,pid=child.pid,started=True))
    atomic_json(OUT/'render_run.json',dict(run=str(run)))
    while child.poll() is None:
        time.sleep(2);memory=gpu_memory_info();percent=gpu_budget_percent(memory)
        for key,value in (memory or {}).items():
            if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
        if percent is not None and percent>=90:reason='gpu_budget_exceeded'
        if time.monotonic()>deadline:reason='validation_timeout'
        if reason:
            child.terminate();child.wait(timeout=30)
            runtime=read_json(run/'runtime.json') if (run/'runtime.json').exists() else {}
            atomic_json(run/'runtime.json',dict(runtime,success=False,stopped_by=reason,gpu=memory))
result=finish_result(run,child.returncode);result['gpu_peak']=peak
atomic_json(OUT/'render_result.json',result)
print('MYGO_RENDER_COMPLETE',result['complete'],reason,flush=True)
