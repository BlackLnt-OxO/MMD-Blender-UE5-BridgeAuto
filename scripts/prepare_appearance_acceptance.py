"""Prepare independent static acceptance jobs from the two existing source jobs."""
import shutil
from pathlib import Path
from mmd_bridge.core import Job,Settings,atomic_json,read_json

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'work/scene_appearance_acceptance'
OUT.mkdir(parents=True,exist_ok=True)
jobs={}
for key,old_id in [('street','Scene_street_01'),('break_me_down','Scene_BREAK_ME_DOWN_20260902_01')]:
    old=ROOT/'work/TEST_ON_927_Jobs/jobs'/old_id
    data=read_json(old/'job.json')
    cfg=dict(data['settings'])
    cfg.update(restore_scene_appearance=True,scene_light_scale=1.,fog_density_scale=1.,scene_exposure_offset=0.,
               volume_resolution=256,world_volume_distance=100.,scene_object_filter=None,
               output_override=str(OUT/'exports'),content_root='/Game/TEST_ON_928/SceneAppearance/'+key,
               unattended=True)
    cfg.pop('scene_object_filter')
    settings=Settings(**{k:v for k,v in cfg.items() if k in Settings.__dataclass_fields__})
    job=Job.create(OUT/'jobs',settings)
    package=OUT/'exports'/('Scene_'+job.data['id'])/'Scene'
    prior=read_json(old/'manifest.json')
    old_package=Path(prior['scene']['usd']['root_layer']).parent
    # Reuse only byte-for-byte old native bake cache inputs. HDR emission cache v3
    # deliberately misses old PNG caches and is rebuilt by the converter.
    for src,dst in [(old_package/'Textures',package/'Textures'),(old_package.parent/'BakeCache',package.parent/'BakeCache')]:
        if src.is_dir():
            shutil.copytree(src,dst,dirs_exist_ok=True)
    jobs[key]=str(job.folder)
atomic_json(OUT/'jobs.json',jobs)
print(jobs)
