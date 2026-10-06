import sys,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
OUT=ROOT/'work/material_restoration/full_scene'
job=Job(read_json(OUT/'job_path.json')['job']);cfg=job.data['settings']
assert job.folder.is_relative_to(OUT)
command=[cfg['blender'],'--background','--factory-startup','--disable-autoexec','--threads','4',
    '--python-exit-code','1','--python',str(ROOT/'scripts/blender_entry.py'),'--',str(job.folder),'scene']
with (OUT/'blender.log').open('w',encoding='utf-8') as log:
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
atomic_json(OUT/'export_result.json',dict(exit_code=result.returncode,status=read_json(job.folder/'status.json')))
print('FULL_EXPORT_EXIT',result.returncode,flush=True)
