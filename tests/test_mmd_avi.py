from pathlib import Path

import pytest

from mmd_bridge.mmd_avi import _runtime, scan_vmd, cleanup_export_avi
from tests.test_vmd_reader import _bone_frame, _morph_frame, make_vmd


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


def test_runtime_changes_only_its_copy(tmp_path):
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
