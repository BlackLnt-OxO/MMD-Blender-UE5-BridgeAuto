from pathlib import Path

import pytest

from mmd_bridge.mmd_avi import _runtime, scan_vmd, cleanup_export_avi
from tests.test_vmd_reader import _bone_frame, _morph_frame, make_vmd


@pytest.mark.parametrize('wrong_pid', [False, True])
def test_attempt_requires_current_process_frame_zero_ack_before_rendering(tmp_path, monkeypatch, wrong_pid):
    from contextlib import nullcontext
    from types import SimpleNamespace as N
    from mmd_bridge import mmd_avi as m
    from mmd_bridge.core import Job, atomic_json, read_json
    job = Job(tmp_path / 'job')
    job.folder.mkdir()
    atomic_json(job.path, {'settings':{}})
    folder = job.folder / 'mmd_full'
    stopped, waits, validations = [], [], []
    process = N(pid=123, poll=lambda: 0 if stopped else None,
                terminate=lambda: stopped.append(True), wait=lambda **kw: 0)
    class Ui:
        def __init__(self, *args): pass
        def load_model(self, path): pass
        def load_motion(self, path): pass
        def _visible_dialogs(self): return []
        def start_avi(self, path, end):
            path.write_bytes(b'AVI' * 1024)
            (folder / 'vmd/model.vmd').write_bytes(b'validated by stub')
            atomic_json(folder / 'progress.json', {'frame': end, 'completed': True})
    monkeypatch.setattr(m, 'MmdUi', Ui)
    monkeypatch.setattr(m, 'reusable_runtime', lambda *args: nullcontext(tmp_path / 'cache/runtime/MMD.exe'))
    monkeypatch.setattr(m.shutil, 'disk_usage', lambda path: N(free=10**12))
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **kw: process)
    def sleep(seconds):
        # Even a completed progress file cannot stand in for the frame-zero receipt.
        assert read_json(job.folder / 'status.json')['state'] == 'mmd_prepare'
        waits.append(seconds)
        atomic_json(folder / 'render_started.json', {'frame': 0, 'end': 2, 'pid': 999 if wrong_pid else 123})
    monkeypatch.setattr(m.time, 'sleep', sleep)
    monkeypatch.setattr(m, 'validate_mode1', lambda *a, **kw: validations.append(a) or {'valid': True})
    prepared = N(source_model=Path('original.pmx'), source_motion=Path('original.vmd'),
                 model=Path('fixed.pmx'), motion=Path('motion.vmd'), lipsync=None)
    if wrong_pid:
        with pytest.raises(ValueError, match='首帧回执'):
            m._attempt(job, tmp_path / 'source', prepared, 2, 'full')
        assert not validations and not read_json(folder / 'attempt.json')['validated']
    else:
        _, result = m._attempt(job, tmp_path / 'source', prepared, 2, 'full')
        assert result['valid'] and validations
        assert read_json(folder / 'attempt.json')['render_started_at']
    assert waits == [.05] and stopped


def test_stream_scan_counts_tracks_and_rejects_truncation(tmp_path):
    path = tmp_path / "motion.vmd"
    path.write_bytes(make_vmd(
        bone_frames=[_bone_frame("髪", frame) for frame in range(3)],
        morph_frames=[_morph_frame("笑い", 2, .5)]))
    report = scan_vmd(path)
    assert report["bone_tracks"]["髪"] == 3
    assert report["morph_tracks"]["笑い"] == 1
    assert report["bone_min"] == 0 and report["bone_max"] == 2
    path.write_bytes(path.read_bytes()[:-7])
    with pytest.raises(ValueError, match="截断|尾部"):
        scan_vmd(path)


@pytest.mark.parametrize('missing_weight', [0.0, 0.5, 1e-9])
def test_mode1_allows_omitted_zero_lipsync_tracks_but_requires_active_tracks(
        tmp_path, monkeypatch, missing_weight):
    from types import SimpleNamespace as N
    from mmd_bridge import mmd_avi as m
    motion, lip, baked = [tmp_path / name for name in ('main.vmd', 'lip.vmd', 'baked.vmd')]
    bones = [_bone_frame('髪', frame) for frame in range(3)]
    motion.write_bytes(make_vmd(bone_frames=bones))
    lip.write_bytes(make_vmd(morph_frames=[
        _morph_frame('あ', 1, .5), _morph_frame('ω', 0, 0),
        _morph_frame('ω', 2, missing_weight)]))
    baked.write_bytes(make_vmd(bone_frames=bones, morph_frames=[_morph_frame('あ', 1, .5)]))
    monkeypatch.setattr(m, 'read_pmx', lambda path: N(bones=[N(name_jp='髪')]))
    prepared = N(expected_bones=('髪',), model=tmp_path / 'model.pmx', motion=motion, lipsync=lip)
    assert scan_vmd(lip)['active_morph_tracks']['あ'] == 1
    if missing_weight:
        with pytest.raises(ValueError, match='烘焙丢失口型轨道'):
            m.validate_mode1(baked, prepared, 2, full=True)
    else:
        report = m.validate_mode1(baked, prepared, 2, full=True)
        assert report['lipsync_zero_tracks_omitted'] == ['ω']
        assert report['physics_tracks_found'] == 1


@pytest.mark.parametrize('locked_replaces', [0, 2, 20])
@pytest.mark.parametrize('frame', [0, 60])
def test_runtime_changes_only_its_copy(tmp_path, monkeypatch, locked_replaces, frame):
    source = tmp_path / "source"
    (source / "mmdbridge_scripts").mkdir(parents=True)
    (source / "mmdbridge_settings").mkdir()
    (source / "out").mkdir()
    (source / "out" / "old.vmd").write_bytes(b"old")
    for name in ("MikuMikuDance_CHS.exe", "d3d9.dll", "python312.dll"):
        (source / name).write_bytes(b"dummy")
    ini = source / "mmdbridge_settings" / "MikuMikuDance_CHS.exe.ini"
    original = "\n".join(("ScriptName=old.py", "ScriptCallSetting=0",
                          "StartFrame=9", "EndFrame=9", "ExportFPS=24"))
    ini.write_text(original, encoding="utf-16")
    script = source / "mmdbridge_scripts" / "mmdbridge_vmd_mode1.py"
    script.write_text('from mmdbridge_vmd import *\ndst_dir = "out"\n', encoding="utf-8")
    target = tmp_path / "runtime"
    _runtime(source, target, 60, tmp_path / "vmd", tmp_path / "progress.json")
    assert ini.read_text(encoding="utf-16") == original
    assert script.read_text(encoding="utf-8").endswith('dst_dir = "out"\n')
    assert not (target / "out" / "old.vmd").exists()
    assert "EndFrame=60" in (target / "mmdbridge_settings" / ini.name).read_text(encoding="utf-16")
    text = (target / "mmdbridge_scripts" / script.name).read_text(encoding="utf-8")
    assert "messagebox = lambda" in text and "progress.json" in text
    import os
    import sys
    from types import ModuleType, SimpleNamespace
    monkeypatch.setitem(sys.modules, 'mmdbridge_vmd', ModuleType('mmdbridge_vmd'))
    progress = tmp_path / 'progress.json'
    progress.write_text('{"frame": -1}')
    replace = os.replace
    calls, sleeps = [], []
    def replace_locked(*args):
        calls.append(args)
        if len(calls) <= locked_replaces:
            raise PermissionError('A progress reader has the file open')
        return replace(*args)
    monkeypatch.setattr(os, 'replace', replace_locked)
    namespace = {'os': os, 'time': SimpleNamespace(sleep=sleeps.append),
                 'framenumber': frame, 'start_frame': 0, 'end_frame': 60}
    if locked_replaces == 20:
        with pytest.raises(PermissionError):
            exec(text, namespace)
        assert progress.read_text() == '{"frame": -1}'
        assert len(calls) == 20 and len(sleeps) == 19
    else:
        exec(text, namespace)
        import json
        assert json.loads(progress.read_text()) == {'frame': frame, 'end': 60, 'completed': frame == 60}
        assert len(calls) == locked_replaces + 1 and len(sleeps) == locked_replaces
    started = tmp_path / 'render_started.json'
    assert started.exists() == (frame == 0)
    if frame == 0:
        import json
        assert json.loads(started.read_text()) == {'frame': 0, 'end': 60, 'pid': os.getpid()}


def test_completed_export_deletes_only_its_capture_and_keeps_receipt(tmp_path):
    capture = tmp_path / 'capture.avi'
    capture.write_bytes(b'generated')
    other = tmp_path / 'user.avi'
    other.write_bytes(b'keep')
    attempt = {'validated':True,'avi_result':{'path':str(capture),'bytes':9}}
    result = cleanup_export_avi(tmp_path, attempt)
    assert result['status'] == 'deleted' and result['bytes'] == 9
    assert not capture.exists() and other.read_bytes() == b'keep'
    assert (tmp_path / 'attempt.json').exists() and attempt['avi_result']['deleted']
    assert cleanup_export_avi(tmp_path, attempt)['status'] == 'already_absent'


def test_failed_export_and_outside_capture_are_preserved(tmp_path):
    capture = tmp_path / 'capture.avi'
    capture.write_bytes(b'failed evidence')
    assert cleanup_export_avi(tmp_path, {'validated':False})['status'] == 'preserved'
    assert capture.exists()
    owned = tmp_path / 'attempt'; owned.mkdir()
    result = cleanup_export_avi(owned, {'validated':True,'avi_result':{'path':str(capture)}})
    assert result['status'] == 'failed' and capture.exists()


def test_locked_capture_records_cleanup_failure_without_invalidating_export(tmp_path, monkeypatch):
    capture = tmp_path / 'capture.avi'; capture.write_bytes(b'locked')
    original_unlink = Path.unlink
    def locked(self, *args, **kwargs):
        if self == capture:
            raise PermissionError('still locked')
        return original_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', locked)
    attempt = {'validated':True,'avi_result':{'path':str(capture)}}
    result = cleanup_export_avi(tmp_path, attempt)
    assert result['status'] == 'failed' and capture.exists() and attempt['validated']
