"""Bounded owned UE import worker, with an external dedicated/shared GPU guard."""
import os,sys,time,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,atomic_json,read_json
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
from mmd_bridge.ue_render_policy import memory_cvars,shader_worker_arguments
out=ROOT/'work/material_functions_implementation/full_scene'
job=Job(read_json(out/'job_path.json')['job']);cfg=job.data['settings']
assert Path(cfg['project']).stem=='TESTON1_1005_02'
percent=gpu_budget_percent(gpu_memory_info())
if percent is None or percent>=80:raise RuntimeError('GPU budget unavailable or not below 80%')
assert read_json(job.folder/'status.json')['state'] in ('scene_done','ue_wait_vram','ue_paused_resource_limit','failed','stopped','needs_review')
assert (job.folder/'manifest.json').is_file()
command=[str(Path(cfg['unreal']).with_name('UnrealEditor-Cmd.exe')),cfg['project'],'/Engine/Maps/Entry',
    '-unattended','-nosplash','-nosound','-RenderOffscreen','-EnablePlugins=USDImporter,MovieRenderPipeline,GeometryScripting',
    '-ExecutePythonScript='+str(ROOT/'scripts/ue_full_material_scene_import.py'),'-abslog='+str(out/'unreal_import.log'),
    '-ExecCmds='+','.join(f'{key} {value}' for key,value in memory_cvars('balanced').items())]+shader_worker_arguments(cfg)
deadline=time.monotonic()+1800;peak={};reason=None;previous=None
environment=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1')
with (out/'native_launcher.log').open('w',encoding='utf-8') as stream:
    child=subprocess.Popen(command,env=environment,cwd=str(Path(command[0]).parent),stdout=stream,stderr=subprocess.STDOUT)
    atomic_json(out/'import_launch.json',{'command':command,'pid':child.pid})
    while child.poll() is None:
        time.sleep(2)
        memory=gpu_memory_info();percent=gpu_budget_percent(memory)
        for key,value in (memory or {}).items():
            if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
        state=read_json(job.folder/'status.json')
        if state.get('message')!=previous:
            previous=state.get('message');print(previous,flush=True)
        if percent is not None and percent>=90:reason='gpu_budget_exceeded'
        if time.monotonic()>deadline:reason='validation_timeout'
        if reason:
            child.terminate();child.wait(timeout=20)
            job.status('ue_paused_resource_limit',reason,gpu=memory)
    report=read_json(job.folder/'ue_report.json') if (job.folder/'ue_report.json').is_file() else {}
    atomic_json(out/'import_result.json',dict(exit_code=child.returncode,stopped_by=reason,
        gpu_global_peak=peak,state=read_json(job.folder/'status.json'),
        report=str(job.folder/'ue_report.json'),failures=report.get('failures'),
        generated_level=report.get('assets',{}).get('level')))
print('FULL_IMPORT_EXIT',child.returncode,reason,flush=True)
