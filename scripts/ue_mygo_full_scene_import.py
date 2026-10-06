import sys,traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.ue_host import import_job
OUT=ROOT/'work/material_restoration/full_scene'
job=Job(read_json(OUT/'job_path.json')['job'])
assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
assert job.data['settings']['content_root'] in ('/Game/MMDBridgeValidation/MyGORestorationV1/FullScene','/Game/MMDBridgeValidation/MyGORestorationV2/FullScene','/Game/MMDBridgeValidation/MyGO3')
try:import_job(job.folder)
except Exception:
    atomic_json(OUT/'native_exception.json',{'error':traceback.format_exc()})
    raise
print('FULL_MYGO_IMPORT_STATE',read_json(job.folder/'status.json')['state'],flush=True)
