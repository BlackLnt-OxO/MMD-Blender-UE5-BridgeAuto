"""Exercise the sampled-camera dispatch inside the production sequence assembly."""
import ast
from pathlib import Path
from types import SimpleNamespace as N

import pytest


@pytest.mark.parametrize('flow,backend,files,has_samples,expected', [
    ('auto', 'mmdbridge_fbx', {'model': {}, 'animation': {}}, True, True),
    ('character', 'mmdbridge_fbx', {'model': {}, 'animation': {}}, True, True),
    ('camera', 'fbx_legacy', {}, True, True),
    ('auto', 'fbx_legacy', {'camera': {}}, True, False),
    ('auto', 'mmd2unreal', {}, False, False),
    ('scene', 'fbx_legacy', {}, False, False),
])
def test_sampled_camera_joins_sequence(tmp_path, flow, backend, files, has_samples, expected):
    calls = []
    samples = [{'frame': 0}, {'frame': 2}]
    sequence, actors, tag, report = object(), object(), object(), {}
    cfg = {'flow': flow, 'character_backend': backend}
    manifest = {'files': files}
    if has_samples:
        manifest['camera_samples'] = str(tmp_path / 'camera_samples.json')
    scope = dict(Path=Path, cfg=cfg, manifest=manifest, job=N(folder=tmp_path),
                 sequence=sequence, actor_sub=actors, tag=tag, report=report,
                 read_json=lambda path: samples,
                 bind_usd_camera=lambda *args: calls.append(args),
                 record_import_failure=lambda *args: pytest.fail(str(args)))
    exec(_camera_dispatch(), scope)
    assert calls == ([(sequence, samples, actors, tag, cfg, report)] if expected else [])


def test_sampled_camera_rejects_other_job_file(tmp_path):
    failures = []
    scope = dict(Path=Path, manifest={'files': {}, 'camera_samples': str(tmp_path.parent / 'other.json')},
                 job=N(folder=tmp_path), report={},
                 read_json=lambda path: pytest.fail('Must validate ownership before reading'),
                 record_import_failure=lambda *args: failures.append(args))
    exec(_camera_dispatch(), scope)
    assert len(failures) == 1
    assert failures[0][2] == 'camera'
    assert '不属于当前任务' in str(failures[0][-1])


@pytest.mark.parametrize('samples,files,upstream,replace', [
    ('samples.json', {}, None, True),
    (None, {'camera': {}}, None, True),
    (None, {}, object(), True),
    (None, {}, None, False),
])
def test_explicit_camera_replaces_scene_cuts(samples, files, upstream, replace):
    class CameraCut:
        pass
    scene_cuts, animation, subsequence = CameraCut(), object(), object()
    tracks = [scene_cuts, animation, subsequence]
    sequence = N(get_tracks=lambda: tracks, remove_track=tracks.remove)
    tree = ast.parse((Path(__file__).parents[1] / 'mmd_bridge/ue_host.py').read_text(encoding='utf-8-sig'))
    block = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and 'camera_sequence or' in ast.unparse(n.test))
    scope = dict(sequence=sequence, camera_sequence=upstream,
                 manifest={'files': files, 'camera_samples': samples}, u=N(MovieSceneCameraCutTrack=CameraCut))
    exec(compile(ast.Module(body=[block], type_ignores=[]), 'camera-priority', 'exec'), scope)
    assert tracks == ([animation, subsequence] if replace else [scene_cuts, animation, subsequence])


def _camera_dispatch():
    source = Path(__file__).parents[1] / 'mmd_bridge/ue_host.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'import_job')
    block = next(n for n in ast.walk(function) if isinstance(n, ast.If)
                 and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                         and child.func.id == 'bind_usd_camera' for child in ast.walk(n))
                 and 'camera_samples' in ast.unparse(n.test))
    return compile(ast.Module(body=[block], type_ignores=[]), str(source), 'exec')
