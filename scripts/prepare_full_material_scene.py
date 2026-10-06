"""Create a fresh, unfiltered railway export job for the authorized test project."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,Settings,atomic_json,read_json,stable_id
out=ROOT/'work/material_functions_implementation/full_scene'
receipt=out/'job_path.json'
if receipt.exists():
    job=Job(read_json(receipt)['job'])
else:
    (out/'export').mkdir(parents=True,exist_ok=True)
    settings=Settings(flow='scene',scene=r'F:\MMD_Scene\Blender\SableFiSh\railway\railway.blend',
        blender=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe',
        unreal=r'C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe',
        project=r'F:\Ue5_Project\TESTON1_1005_02\TESTON1_1005_02.uproject',
        character_output_dir=str(out/'export'),content_root='/Game/MMDBridgeValidation/FullSceneV3',
        width=2560,height=1080,fps=30,end=300,bake_resolution=2048,
        exact_collision=False,unattended=True)
    job=Job.create(out/'jobs',settings)
    data=job.data;data['asset_id']=data['id']+'_'+stable_id(str(job.folder))[:8]
    atomic_json(job.path,data);atomic_json(receipt,{'job':str(job.folder)})
print(job.folder)
