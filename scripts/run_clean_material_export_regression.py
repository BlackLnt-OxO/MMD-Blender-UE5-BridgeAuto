"""Fresh source export through Controller; compare material outputs without starting UE."""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.controller import Controller
from mmd_bridge.core import Settings, read_json, atomic_json, file_hash
from mmd_bridge.material_regression import capture, compare_exports
from mmd_bridge.usd_scene_optimizer import needs_import_compaction

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('baseline_job', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--namespace', required=True)
args = parser.parse_args()
out = args.output.resolve()
transfer = out / 'Transfer'
assert not transfer.exists(), 'fresh export requires an absent transfer directory'
original = read_json(args.baseline_job / 'job.json')
fields = {f.name for f in dataclasses.fields(Settings)}
cfg = {k: v for k, v in original['settings'].items() if k in fields}
assert cfg['material_workflow'] == 'legacy_main_v1'
cfg.update(character_output_dir=str(transfer), content_root=args.namespace)
source = Path(cfg['scene'])
before = file_hash(source)
assert not (Path(cfg['project']).parent / 'Content' / args.namespace.removeprefix('/Game/')).exists()
transfer.mkdir(parents=True)
baseline = capture(args.baseline_job)
atomic_json(out / 'preflight.json', {'source_sha256': before, 'namespace_absent': True,
    'empty_transfer': True, 'old_job_reused': False, 'old_geometry_reused': False,
    'old_bake_cache_reused': False, 'baseline': str(args.baseline_job.resolve())})
controller = Controller()
job = controller.start(Settings(**cfg))


def wait_children():
    previous = None
    while any(child.poll() is None for _, child in controller.processes):
        status = read_json(job.folder / 'status.json')
        atomic_json(out / 'current.json', {'job': str(job.folder), 'status': status})
        if status != previous:
            print(status['state'], status.get('message', '')[:180], flush=True)
            previous = status
        time.sleep(2)
    assert all(child.returncode == 0 for _, child in controller.processes), 'Blender export failed'
    controller.processes = []


wait_children()
if needs_import_compaction(job):
    job.status('scene_import_optimizing', '整理本次新导出的 USD 实例')
    controller.launch_blender('scene_import')
    wait_children()
assert before == file_hash(source)
candidate = capture(job.folder, export_only=True)
result = compare_exports(baseline, candidate)
result.update(source_unchanged=True, job=str(job.folder), fresh_export=True,
    baseline_materials=len(baseline['materials']), candidate_materials=len(candidate['materials']))
atomic_json(out / 'snapshot.json', candidate)
atomic_json(out / 'result.json', result)
job.status('needs_review', '干净导出回归完成；UE 原生和视觉验证另行记录')
print('CLEAN_EXPORT_REGRESSION', result['export_gate_passed'], len(result['differences']), flush=True)
sys.exit(0 if result['export_gate_passed'] else 2)
