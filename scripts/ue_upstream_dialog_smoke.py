"""Interactive acceptance: the user completes/cancels the official VMD dialog."""
import sys
import traceback
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.core import Job, atomic_json
from mmd_bridge.ue_mmd2unreal import import_sources, prepare_camera, ImportCancelled, UpstreamBlocked

job = Job((root / 'work/upstream58/dialog_job.txt').read_text(encoding='utf-8').strip())
report = {'interactive': True, 'complete': False}
try:
    receipt = import_sources(job)
    camera = prepare_camera(receipt, job)
    report.update(complete=True, receipt=receipt, camera=camera.get_path_name() if camera else None)
    job.status('complete', '官方 VMD 导入与相机组合副本验证完成')
except ImportCancelled as exc:
    report.update(cancelled=True, message=str(exc))
    job.status('cancelled', str(exc))
except Exception:
    report['error'] = traceback.format_exc()
    job.status('failed', report['error'])
atomic_json(root / 'work/upstream58/dialog_result.json', report)
