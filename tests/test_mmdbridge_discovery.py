from pathlib import Path

import pytest

from mmd_bridge.discovery import discover_mmdbridge
from mmd_bridge.mmd_avi import _runtime, missing_runtime_files


FILES = ('MikuMikuDance_CHS.exe', 'd3d9.dll', 'python312.dll',
         'mmdbridge_scripts/mmdbridge_vmd_mode1.py',
         'mmdbridge_settings/MikuMikuDance_CHS.exe.ini')


def install(path):
    for name in FILES:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'untouched')
    return path


def test_finds_nested_installs_deduplicates_and_leaves_files_unchanged(tmp_path):
    first = install(tmp_path / 'MikumikuDance Exe' / 'MikuMikuDance_v932x64')
    second = install(tmp_path / 'tools' / 'mmdbridge')
    plain = tmp_path / 'plain MMD'
    plain.mkdir()
    (plain / FILES[0]).touch()
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert discover_mmdbridge([tmp_path], extra_roots=[first, first]) == [str(first), str(second)]
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('missing', FILES)
def test_detection_uses_same_required_files_as_export(tmp_path, missing):
    source = install(tmp_path / 'mmdbridge')
    (source / missing).unlink()
    assert missing_runtime_files(source) == [missing]
    assert discover_mmdbridge([source]) == []
    with pytest.raises(ValueError, match='安装目录缺少'):
        _runtime(source, tmp_path / 'runtime', 10, tmp_path / 'out', tmp_path / 'progress')
    assert not (tmp_path / 'runtime').exists()


def test_default_roots_and_remembered_location(tmp_path, monkeypatch):
    from mmd_bridge import mmm_runtime
    source = install(tmp_path / 'renamed' / 'arbitrary' / 'install')
    monkeypatch.setattr(mmm_runtime, '_candidate_roots', lambda: [tmp_path])
    assert discover_mmdbridge() == []
    assert discover_mmdbridge(extra_roots=[source]) == [str(source)]


def test_search_budget_and_unreadable_root(tmp_path, monkeypatch):
    from mmd_bridge import discovery
    source = install(tmp_path / 'mmdbridge')
    original = Path.iterdir
    def unreadable(path):
        if path == tmp_path / 'denied':
            raise PermissionError('denied')
        return original(path)
    (tmp_path / 'denied').mkdir()
    monkeypatch.setattr(Path, 'iterdir', unreadable)
    assert discover_mmdbridge([tmp_path / 'denied', source]) == [str(source)]
    ticks = iter([0, 5])
    monkeypatch.setattr(discovery.time, 'monotonic', lambda: next(ticks))
    assert discover_mmdbridge([source]) == []
