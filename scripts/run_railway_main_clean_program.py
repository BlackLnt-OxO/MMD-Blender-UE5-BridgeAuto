"""Fresh Railway import through the GUI controller, using the common main workflow."""
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.controller import Controller, UE_CODE_MODULES
from mmd_bridge.core import Settings, atomic_json, read_json, file_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run-number', type=int, default=1)
parser.add_argument('--resume', action='store_true')
parser.add_argument('--recovery-code-receipt', type=Path,
                    help='audited checkpoint-reader fix; original preflight is retained')
args = parser.parse_args()
assert 1 <= args.run_number <= 99
suffix = f'{args.run_number:02d}'
OUT = ROOT / ('work/railway_program/main_clean_20261007_' + suffix)
transfer = OUT / 'Transfer'
namespace = '/Game/Railway_Main_Clean_20261007_' + suffix
project = Path('F:/Ue5_Project/TESTON1_1005_02/TESTON1_1005_02.uproject')
source = Path('F:/MMD_Scene/Blender/SableFiSh/railway/railway.blend')
controls = [
    project.parent / 'Content/MMDBridgeValidation/FullSceneV3/A_Scene_railway_01_9ca2172b/Maps/MMD_Preview.umap',
    project.parent / 'Content/On_9_24_开发_PlanV2/On_9_25/On_9_26/A_Scene_It_sMyGO_ParkingLots_01_487f7848/Maps/MMD_Preview.umap',
    project.parent / 'Content/MyGO_Main_Clean_20261007_01/A_Scene_It_sMyGO_ParkingLots_01_12df8fce/Maps/MMD_Preview.umap',
]
if args.run_number > 1:
    prior = read_json(ROOT / 'work/railway_program/main_clean_20261007_01/result.json')
    controls.append(project.parent / 'Content' / (prior['level'].removeprefix('/Game/') + '.umap'))
assert source.is_file()
assert all(path.is_file() for path in controls), 'historical controls must exist'
modules = set(UE_CODE_MODULES) | {path.name for path in (ROOT / 'mmd_bridge').glob('blender_*.py')} | {'scene_shader_data.py'}
if args.resume:
    preflight = read_json(OUT / 'preflight.json')
    assert preflight['namespace'] == namespace and preflight['prior_job_reused'] is False
    before = preflight['protected_files']
    code = dict(preflight['code_hashes'])
    if args.recovery_code_receipt:
        recovery = read_json(args.recovery_code_receipt)
        assert recovery['schema'] == 1 and recovery['native_regression_passed']
        assert set(recovery['changes']) == {'blender_usd.py'}
        assert recovery['scope'] == 'checkpoint_reader_absent_vertex_colors'
        assert Path(recovery['job']).resolve().is_relative_to(transfer.resolve())
        for name, change in recovery['changes'].items():
            assert code[name] == change['before_sha256']
            code[name] = change['after_sha256']
    assert all(file_hash(Path(path)) == digest for path, digest in before.items())
    assert all(file_hash(ROOT / 'mmd_bridge' / name) == digest for name, digest in code.items())
else:
    assert not args.recovery_code_receipt
    assert not transfer.exists(), 'fresh import requires an absent transfer directory'
    assert not (project.parent / 'Content' / namespace.removeprefix('/Game/')).exists()
    transfer.mkdir(parents=True)
    before = {str(path): file_hash(path) for path in [source, *controls]}
    code = {name: file_hash(ROOT / 'mmd_bridge' / name) for name in sorted(modules)}
    atomic_json(OUT / 'preflight.json', dict(source=str(source), protected_files=before,
        namespace=namespace, transfer=str(transfer), namespace_absent=True, empty_transfer=True,
        prior_job_reused=False, common_main_workflow='legacy_main_v1', code_hashes=code,
        git_base_commit='b46cdc8', workspace_code_hashes_recorded=True))
settings = Settings(flow='scene', scene=str(source),
    blender='F:/SteamLibrary/steamapps/common/Blender/blender.exe',
    unreal='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe',
    project=str(project), character_output_dir=str(transfer), content_root=namespace,
    material_workflow='legacy_main_v1', scene_import_profile='faithful', material_strategy='auto',
    restore_scene_appearance=True, scene_viewport_ocio=True, scene_render_pipeline='lumen',
    bake_resolution=1024, volume_resolution=256, scene_master_material='',
    material_bake_fallback=True, material_complex_strategy='pbr_bake',
    material_conversion_timeout=120, unattended=True)
controller = Controller()
if args.resume:
    folder = Path(read_json(OUT / 'current.json')['job'])
    assert folder.is_relative_to((transfer / 'MMDBridge_Jobs').resolve())
    if args.recovery_code_receipt:
        assert folder == Path(recovery['job']).resolve()
    controller.resume(folder)
    job = controller.job
else:
    job = controller.start(settings)
previous = None
status_read_failures = 0
while True:
    try:
        status = controller.poll()
        status_read_failures = 0
    except (PermissionError, FileNotFoundError) as exc:
        # Windows can briefly deny a read while the worker replaces status.json.
        # Retry only that progress file, with a bound; real resource failures propagate.
        if Path(exc.filename or '').resolve() != (job.folder / 'status.json').resolve():
            raise
        status_read_failures += 1
        if status_read_failures > 15:
            raise
        time.sleep(2)
        continue
    if status != previous:
        print(status['state'], status.get('message', '')[:240], flush=True)
        previous = status
    atomic_json(OUT / 'current.json', {'job': str(job.folder), 'status': status})
    if status['state'] in ('complete', 'needs_review', 'failed', 'stopped') and not controller.processes:
        break
    time.sleep(2)
if status['state'] in ('failed', 'stopped'):
    raise RuntimeError(status['message'])
report = read_json(job.folder / 'ue_report.json')
outside = [p for p in report['assets']['source_packages']
           if p.startswith('/Game/') and not p.startswith(namespace + '/')]
assert not outside, outside
assert all(file_hash(Path(path)) == digest for path, digest in before.items()), 'protected source/level changed'
assert all(file_hash(ROOT / 'mmd_bridge' / name) == digest for name, digest in code.items()), 'main changed during import'
assert not report['checks']['material_compile_errors']
assert report['checks']['ocio']['status'] == 'validated'
atomic_json(OUT / 'result.json', dict(import_finished=True, job=str(job.folder),
    status=status['state'], root=report['root'], level=report['assets']['level'],
    restoration=report['checks']['material_restoration'],
    white_materials=len(read_json(job.folder / 'white_materials.json')['materials']),
    protected_files_unchanged=True, main_code_unchanged=not bool(args.recovery_code_receipt),
    code_unchanged_during_resumed_run=True,
    recovery_code_receipt=str(args.recovery_code_receipt.resolve()) if args.recovery_code_receipt else None,
    no_old_game_dependencies=True,
    no_automatic_white_repair=not report.get('material_repairs'),
    visual_status='needs_review', ue_failures=report['failures'], checks=report['checks']))
print('RAILWAY_MAIN_CLEAN_IMPORT_FINISHED', flush=True)
