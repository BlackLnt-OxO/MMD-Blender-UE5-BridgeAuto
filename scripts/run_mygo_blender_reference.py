"""Source-camera reference in a separate receipt directory, preserving import status."""
import sys,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,read_json,atomic_json
OUT=ROOT/'work/material_restoration/full_scene'
source=Job(read_json(OUT/'job_path.json')['job'])
folder=OUT/'blender_reference_job';folder.mkdir(exist_ok=True)
atomic_json(folder/'job.json',source.data)
command=[source.data['settings']['blender'],'--background','--factory-startup','--disable-autoexec',
    '--threads','4','--python-exit-code','1','--python',str(ROOT/'scripts/blender_entry.py'),'--',str(folder),'reference']
with (OUT/'blender_reference.log').open('w',encoding='utf-8') as log:
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
atomic_json(OUT/'blender_reference_result.json',dict(exit_code=result.returncode,folder=str(folder),
    status=read_json(folder/'status.json')))
print('BLENDER_REFERENCE_EXIT',result.returncode,flush=True)
