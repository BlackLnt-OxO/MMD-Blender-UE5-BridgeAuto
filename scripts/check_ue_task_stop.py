"""UE-hosted persistence check in work/ue_stop_resume_validation only (NullRHI)."""
import sys
import time
from pathlib import Path

import unreal as u

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.core import Job, atomic_json, read_json
from mmd_bridge.ue_host import load_ue_checkpoint, save_ue_checkpoint

folder = root / 'work/ue_stop_resume_validation'
command = read_json(folder / 'native.json')
job = Job(folder / 'job')
asset_path = '/Game/StopResume/M_Completed'
try:
    checkpoint, resumed = load_ue_checkpoint(job, job.data, job.folder / 'manifest.json')
    if command['mode'] == 'interrupt':
        tools = u.AssetToolsHelpers.get_asset_tools()
        material = u.load_asset(asset_path) or tools.create_asset('M_Completed', '/Game/StopResume', u.Material, u.MaterialFactoryNew())
        assert material and u.EditorAssetLibrary.save_loaded_asset(material)
        save_ue_checkpoint(job, checkpoint, 'materials', 'completed_fixture')
        job.status('ue_scene', 'UE native interruption persistence check')
        atomic_json(folder / 'ready.json', {'asset': material.get_path_name(), 'pid': __import__('os').getpid()})
        time.sleep(300)  # Stand in for a native import that cannot yield to Python.
        raise AssertionError('The controller should have terminated this editor')
    else:
        assert resumed and 'completed_fixture' in checkpoint['completed']['materials']
        assert u.EditorAssetLibrary.does_asset_exist(asset_path) and u.load_asset(asset_path)
        atomic_json(folder / 'result.json', {'complete': True, 'checkpoint_reloaded': True,
                                          'saved_asset_reloaded': True, 'engine': u.SystemLibrary.get_engine_version()})
except Exception:
    import traceback
    atomic_json(folder / 'failure.json', {'error': traceback.format_exc()})
    raise
finally:
    u.SystemLibrary.quit_editor()
