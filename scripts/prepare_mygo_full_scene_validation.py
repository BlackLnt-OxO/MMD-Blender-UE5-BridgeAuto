"""Create an isolated full MyGO validation job using the user's existing settings."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import Job,Settings,atomic_json,read_json,stable_id
OUT=ROOT/'work/material_restoration/full_scene';OUT.mkdir(parents=True,exist_ok=True)
(OUT/'export').mkdir(exist_ok=True)
receipt=OUT/'job_path.json'
if receipt.exists() and '--new-output' not in sys.argv:
    job=Job(read_json(receipt)['job'])
else:
    cfg=read_json(Path(r'D:\Codex_Projects_RebuildAutoMMD\MMDBridge_Jobs\Scene_It_sMyGO_ParkingLots_01\job.json'))['settings']
    cfg.update(character_output_dir=str(OUT/'export'),output_override='',
        content_root='/Game/MMDBridgeValidation/MyGO3',scene_object_filter=[],
        material_strategy='auto',material_bake_fallback=True,material_complex_strategy='pbr_bake',
        material_conversion_timeout=120,bake_resolution=1024,exact_collision=False,
        restore_scene_appearance=True,scene_render_pipeline='lumen',unattended=True)
    job=Job.create(OUT/'jobs',Settings(**cfg))
    data=job.data;data['asset_id']=data['id']+'_'+stable_id(str(job.folder))[:8]
    atomic_json(job.path,data)
    if receipt.exists():
        atomic_json(OUT/('previous_job_'+str(data['id'])+'.json'),read_json(receipt))
    atomic_json(receipt,{'job':str(job.folder)})
print(job.folder)
