"""Import only the fresh full-scene validation job, never the original Clean map."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.ue_host import import_job
out=ROOT/'work/material_functions_implementation/full_scene'
job=Job(read_json(out/'job_path.json')['job'])
assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
assert job.data['settings']['content_root']=='/Game/MMDBridgeValidation/FullSceneV3'
try:
    import_job(job.folder)
except Exception:
    atomic_json(out/'native_exception.json',{'error':traceback.format_exc()})
    raise
print('FULL_SCENE_IMPORT_STATE',read_json(job.folder/'status.json')['state'],flush=True)
