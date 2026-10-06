"""Native interruption/resume evidence for the owned Cypress validation scene."""
import json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,atomic_json,read_json,file_hash
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
from mmd_bridge.render_frames import ledger
from mmd_bridge.standalone_render import install_runtime,make_plan,resume_plan,finish_result

OUT=ROOT/'work/material_functions_implementation';job=Job(OUT/'tree_render_job')
assert Path(job.data['settings']['project']).stem=='TESTON1_1005_02'
install_runtime(job.data['settings']['project'])
report={'complete':False,'phases':[]}

def launch(run,request,command,interrupt=False):
    deadline=time.monotonic()+600
    command+=['-EnablePlugins=MMDBridge,USDImporter,MovieRenderPipeline']
    environment=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'))
    with (run/'launcher.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(command,env=environment,cwd=str(Path(command[0]).parent),stdout=log,stderr=subprocess.STDOUT)
        atomic_json(run/'plan.json',dict(request=request,command=command,pid=child.pid,started=True))
        reason=None
        while child.poll() is None:
            time.sleep(1)
            memory=gpu_memory_info();percent=gpu_budget_percent(memory)
            if percent is not None and percent>=90:reason='gpu_budget_exceeded'
            if time.monotonic()>deadline:reason='validation_timeout'
            runtime=read_json(run/'runtime.json') if (run/'runtime.json').is_file() else {}
            contract=runtime.get('frame_contract')
            if interrupt and contract and len(list(Path(request['output']).rglob('MMDBridge_*.png')))>=6:
                receipt=ledger(request['output'],contract)
                if len(receipt['complete_frames'])>=6:reason='intentional_stop'
            if reason:
                child.terminate();child.wait(timeout=20)
                atomic_json(run/'runtime.json',dict(runtime,success=False,stopped_by=reason,gpu=memory))
        result=finish_result(run,child.returncode)
    result['run']=str(run);report['phases'].append(result)
    atomic_json(OUT/'tree_resume_result.json',report)
    print(json.dumps(dict(run=str(run),complete=result['complete'],frames=len(result.get('frames',{}).get('complete_frames',[])))),flush=True)
    return result

try:
    percent=gpu_budget_percent(gpu_memory_info())
    assert percent is not None and percent<80,'GPU budget unavailable or not below 80%'
    preset=read_json(job.folder/'ue_report.json')['assets']['render_preset']
    run,request,command=make_plan(job,preset)
    partial=launch(run,request,command,interrupt=True)
    assert partial['runtime'].get('stopped_by')=='intentional_stop',partial['runtime']
    frames=partial['frames'];assert frames['complete_frames'] and frames['missing_frames']
    originals={item['path']:item['sha256'] for item in frames['files']}
    # Deliberately create one incomplete file in this test's private output to
    # exercise quarantine, rather than representing it as a completed frame.
    missing=frames['missing_frames'][0]
    broken=Path(request['output'])/('MMDBridge_FinalImage_'+str(missing).zfill(4)+'.png')
    if not broken.exists():broken.write_bytes(b'\x89PNG\r\n\x1a\n')
    deadline=time.monotonic()+60
    while gpu_budget_percent(gpu_memory_info()) is None or gpu_budget_percent(gpu_memory_info())>=80:
        assert time.monotonic()<deadline,'GPU budget did not recover below 80%'
        time.sleep(1)
    resumed,request,command=resume_plan(job,run)
    final=launch(resumed,request,command)
    assert final['complete'],final
    assert all(file_hash(path)==digest for path,digest in originals.items()),'A completed frame was overwritten'
    quarantine=read_json(resumed/'quarantined_frames.json')
    assert quarantine and all(Path(item['preserved']).is_file() for item in quarantine)
    report.update(complete=True,reused_frames=len(frames['complete_frames']),
                  quarantined_files=len(quarantine),resumed_ranges=request['ranges'],
                  original_hashes_preserved=True)
except Exception:
    import traceback
    report['error']=traceback.format_exc()
finally:
    atomic_json(OUT/'tree_resume_result.json',report)
print('NATIVE_RESUME_COMPLETE',report['complete'],flush=True)
raise SystemExit(0 if report['complete'] else 1)
