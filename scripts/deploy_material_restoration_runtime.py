"""Back up and update only bridge-owned Python files; renderer settings unchanged."""
import sys,shutil,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import file_hash,atomic_json
from mmd_bridge.controller import UE_CODE_MODULES
project=Path(r'F:\Ue5_Project\TESTON1_1005_02\TESTON1_1005_02.uproject')
plugin=project.parent/'Plugins/MMDBridge';assert (plugin/'.mmd_bridge_managed').is_file()
target=plugin/'Content/Python/mmd_bridge'
backup=project.parent/'Saved/MMDBridge/RuntimeBackups'/str(time.time_ns())
modules=set(UE_CODE_MODULES)|{'__init__.py','scene_appearance.py','bake_cache.py','material_slots.py'}
changed=[]
for name in sorted(modules):
    source=ROOT/'mmd_bridge'/name;destination=target/name
    if destination.is_file() and file_hash(source)==file_hash(destination):continue
    if destination.is_file():backup.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,backup/name)
    shutil.copy2(source,destination);changed.append(name)
atomic_json(ROOT/'work/material_restoration/runtime_deployment.json',dict(project=str(project),
    backup=str(backup),changed=changed,hashes={name:file_hash(target/name) for name in sorted(modules)}))
print('DEPLOYED',changed,flush=True)
