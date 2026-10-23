import pytest
from mmd_bridge import character_resources as resources


def test_loaded_identical_runtime_file_is_reused_without_overwrite(tmp_path,monkeypatch):
    source=tmp_path/'source.dll';target=tmp_path/'target.dll'
    source.write_bytes(b'same binary');target.write_bytes(b'same binary')
    def locked(*a,**kw):raise PermissionError('DLL is loaded')
    monkeypatch.setattr(resources.shutil,'copy2',locked)
    assert resources.copy_runtime_file(source,target)==str(target)


def test_different_loaded_binary_still_requires_restart(tmp_path,monkeypatch):
    source=tmp_path/'source.dll';target=tmp_path/'target.dll'
    source.write_bytes(b'new binary');target.write_bytes(b'old binary')
    def locked(*a,**kw):raise PermissionError('DLL is loaded')
    monkeypatch.setattr(resources.shutil,'copy2',locked)
    with pytest.raises(PermissionError):resources.copy_runtime_file(source,target)
    assert target.read_bytes()==b'old binary'
