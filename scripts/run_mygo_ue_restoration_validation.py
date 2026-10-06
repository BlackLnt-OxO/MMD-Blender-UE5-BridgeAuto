"""Owned native editor worker; preserve all pre-existing scene outputs."""
import os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.process import gpu_memory_info,gpu_budget_percent
OUT=ROOT/'work/material_restoration/mygo';OUT.mkdir(parents=True,exist_ok=True)
script='ue_mygo_restoration_regression.py'
if '--repair-viewport' in sys.argv:
    OUT=ROOT/'work/material_restoration/viewport';OUT.mkdir(parents=True,exist_ok=True)
    script='ue_repair_mygo_viewport_ocio.py'
elif '--paths' in sys.argv:
    OUT=ROOT/'work/material_restoration/asset_paths';OUT.mkdir(parents=True,exist_ok=True)
    script='ue_asset_path_regression.py'
elif '--full-import' in sys.argv:
    OUT=ROOT/'work/material_restoration/full_scene'
    script='ue_mygo_full_scene_import.py'
elif '--visual-diagnostics' in sys.argv:
    OUT=ROOT/'work/material_restoration/full_scene/diagnostics';OUT.mkdir(parents=True,exist_ok=True)
    script='ue_mygo_visual_diagnostics.py'
elif '--usage-repair' in sys.argv:
    OUT=ROOT/'work/material_restoration/full_scene/usage';OUT.mkdir(parents=True,exist_ok=True)
    script='ue_mygo_usage_repair.py'
elif '--scene-policy' in sys.argv:
    OUT=ROOT/'work/material_restoration/policy_v20';OUT.mkdir(parents=True,exist_ok=True)
    script='ue_scene_policy_regression.py'
cfg=read_json(Path(r'D:\Codex_Projects_RebuildAutoMMD\MMDBridge_Jobs\Scene_It_sMyGO_ParkingLots_01\job.json'))['settings']
assert Path(cfg['project']).stem=='TESTON1_1005_02'
percent=gpu_budget_percent(gpu_memory_info())
if percent is None or percent>=80:raise RuntimeError('GPU budget unavailable or not below 80%')
command=[str(Path(cfg['unreal']).with_name('UnrealEditor-Cmd.exe')),cfg['project'],'/Engine/Maps/Entry',
    '-unattended','-nosplash','-nosound','-RenderOffscreen','-ExecutePythonScript='+str(ROOT/'scripts'/script),
    '-abslog='+str(OUT/'unreal.log')]
env=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1')
peak={};reason=None;deadline=time.monotonic()+900
with (OUT/'launcher.log').open('w',encoding='utf-8') as stream:
    child=subprocess.Popen(command,env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=0x08000000)
    atomic_json(OUT/'launch.json',dict(command=command,pid=child.pid))
    while child.poll() is None:
        time.sleep(2)
        memory=gpu_memory_info();percent=gpu_budget_percent(memory)
        for key,value in (memory or {}).items():
            if key.endswith('_used_mb'):peak[key]=max(peak.get(key,0),value)
        if percent is not None and percent>=90:reason='gpu_budget_exceeded'
        if time.monotonic()>deadline:reason='validation_timeout'
        if reason:
            child.terminate();child.wait(timeout=30)
atomic_json(OUT/'launch_result.json',dict(exit_code=child.returncode,stopped_by=reason,gpu_peak=peak))
print('NATIVE_UE_EXIT',child.returncode,reason,flush=True)
