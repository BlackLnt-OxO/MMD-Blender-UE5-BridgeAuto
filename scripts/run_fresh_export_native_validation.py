"""First native import of a newly exported regression job; never reuse older jobs."""
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.controller import Controller
from mmd_bridge.core import read_json, atomic_json, file_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
parser.add_argument('--resume', action='store_true')
args = parser.parse_args()
out = args.output.resolve()
export = read_json(out / 'verified_result.json')
assert export['export_gate_passed'] and export['fresh_export']
folder = Path(export['candidate_job'])
data = read_json(folder / 'job.json')
namespace = data['settings']['content_root']
project = Path(data['settings']['project'])
content = project.parent / 'Content' / namespace.removeprefix('/Game/')
if args.resume:
    assert (folder / 'ue_checkpoint.json').is_file(), 'resume requires this job native checkpoint'
    assert read_json(folder / 'ue_checkpoint.json')['job_id'] == data['id']
    assert read_json(out / 'preflight.json')['old_job_reused'] is False
else:
    assert not content.exists()
baseline_report = read_json(Path(export['baseline_job']) / 'ue_report.json')
old_map = project.parent / 'Content' / (baseline_report['assets']['level'].removeprefix('/Game/') + '.umap')
before = file_hash(old_map)
controls = ROOT / 'work/railway_program/main_clean_20261007_02/preflight.json'
if controls.is_file():
    saved = read_json(controls)['protected_files']
    if str(old_map) in saved:
        assert before == saved[str(old_map)], 'original main map changed since initial preflight'
controller = Controller()
job = controller.resume(folder)
previous = None
status_read_failures = 0
while True:
    try:
        status = controller.poll()
        status_read_failures = 0
    except (PermissionError, FileNotFoundError) as exc:
        if Path(exc.filename or '').resolve() != (folder / 'status.json').resolve():
            raise
        status_read_failures += 1
        if status_read_failures > 15:
            raise
        time.sleep(2)
        continue
    atomic_json(out / 'native_current.json', {'job': str(folder), 'status': status})
    if status != previous:
        print(status['state'], status.get('message', '')[:180], flush=True)
        previous = status
    if status['state'] in ('complete', 'needs_review', 'failed', 'stopped') and not controller.processes:
        break
    time.sleep(2)
if status['state'] in ('failed', 'stopped'):
    raise RuntimeError(status['message'])
report = read_json(folder / 'ue_report.json')
assert not report['checks']['material_compile_errors']
assert file_hash(old_map) == before
outside = [p for p in report['assets']['source_packages'] if p.startswith('/Game/') and not p.startswith(namespace + '/')]
assert not outside, outside
atomic_json(out / 'native_result.json', {'complete': True, 'job': str(folder),
    'level': report['assets']['level'], 'root': report['root'],
    'material_restoration': report['checks']['material_restoration'],
    'old_main_map_unchanged': True, 'no_old_game_dependencies': True,
    'material_compile_errors': [], 'visual_status': 'needs_review'})
print('FRESH_NATIVE_REGRESSION_IMPORTED', flush=True)
