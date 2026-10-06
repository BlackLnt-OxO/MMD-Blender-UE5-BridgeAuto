"""Guarded A/B/B/A in the same saved railway scene with process-only materials."""
import os,sys,subprocess,time,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
from mmd_bridge.standalone_render import install_runtime,make_plan,finish_result
from mmd_bridge.material_calibration import gpu_csv

OUT=ROOT/'work/material_functions_implementation/leaf_transmission'
prep=read_json(OUT/'preparation.json');assert prep['complete'],prep
job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
assert Path(job.data['settings']['project']).stem=='TESTON1_1005_02'
install_runtime(job.data['settings']['project'])
results=[]
for index,variant in enumerate(('current','corrected','corrected','current')):
    info=gpu_memory_info();percent=gpu_budget_percent(info)
    assert percent is not None and percent<80,('GPU budget not below 80%',info)
    run,request,command=make_plan(job,prep['preset'],pipeline='lumen')
    request['material_overrides']=prep['materials'] if variant=='corrected' else prep['baseline_materials']
    request['variant']=variant
    atomic_json(run/'request.json',request)
    command+=['-EnablePlugins=MMDBridge,USDImporter,MovieRenderPipeline']
    environment=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'))
    deadline=time.monotonic()+1200;reason=None;previous=None;peak={}
    with (run/'launcher.log').open('w',encoding='utf-8') as stream:
        child=subprocess.Popen(command,env=environment,stdout=stream,stderr=subprocess.STDOUT)
        print('LEAF_AB_START',index,variant,str(run),flush=True)
        atomic_json(OUT/'progress.json',{'index':index,'variant':variant,'run':str(run),'pid':child.pid})
        while child.poll() is None:
            time.sleep(2)
            info=gpu_memory_info();percent=gpu_budget_percent(info)
            for key,value in (info or {}).items():
                if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
            state=read_json(run/'runtime.json') if (run/'runtime.json').exists() else {}
            if state.get('state')!=previous:
                previous=state.get('state');print('LEAF_AB_STATE',index,previous,flush=True)
            if percent is None:reason='gpu_budget_unavailable'
            elif percent>=90:reason='gpu_budget_exceeded'
            elif time.monotonic()>deadline:reason='validation_timeout'
            if reason:
                child.terminate();child.wait(timeout=20)
                atomic_json(run/'runtime.json',dict(state,success=False,stopped_by=reason,gpu=info))
    result=finish_result(run,child.returncode)
    if result['complete']:
        result['gpu_measurement']=gpu_csv(run/'gpu.csv',scene_only=True)
    result.update(variant=variant,index=index,gpu_global_peak=peak)
    if not result['runtime'].get('material_variants_ready'):result['complete']=False
    results.append(result);atomic_json(OUT/'runs.json',results)
    print('LEAF_AB_DONE',index,result['complete'],result.get('gpu_measurement'),flush=True)
    if not result['complete']:break
valid=len(results)==4 and all(r['complete'] and 'gpu_ms' in r.get('gpu_measurement',{}) for r in results)
summary={'complete':valid,'runs':[r['run'] for r in results],'original_assets_modified':False,'max_accepted_increase_percent':10}
if valid:
    for variant in ('current','corrected'):
        rs=[r for r in results if r['variant']==variant]
        summary[variant]={'gpu_ms':statistics.median(r['gpu_measurement']['gpu_ms'] for r in rs),
            'gpu_p95_ms':statistics.median(r['gpu_measurement']['gpu_p95_ms'] for r in rs),
            'individual_gpu_ms':[r['gpu_measurement']['gpu_ms'] for r in rs],
            'peak_dedicated_mb':max(r['gpu_global_peak'].get('dedicated_used_mb',0) for r in rs),
            'peak_shared_mb':max(r['gpu_global_peak'].get('shared_used_mb',0) for r in rs)}
    delta=summary['corrected']['gpu_ms']-summary['current']['gpu_ms']
    summary['delta_ms']=delta;summary['delta_percent']=100*delta/summary['current']['gpu_ms']
    summary['decision']='keep_current' if summary['delta_percent']>10 else 'use_corrected'
    summary['visual_status']='needs_review'
atomic_json(OUT/'comparison.json',summary)
print('LEAF_AB_RESULT',summary,flush=True)
