import json
from pathlib import Path
import pytest
from mmd_bridge.core import read_json


def test_transient_windows_status_sharing_violation_is_retried(tmp_path,monkeypatch):
    path=tmp_path/'status.json';path.write_text('{"state":"complete"}',encoding='utf8')
    original=Path.read_text;calls=[]
    def locked(self,*args,**kwargs):
        calls.append(self)
        if len(calls)<3:raise PermissionError('transient sharing violation')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',locked)
    monkeypatch.setattr('mmd_bridge.core.time.sleep',lambda duration:None)
    assert read_json(path)=={'state':'complete'} and len(calls)==3


def test_permanent_permission_failure_stays_bounded_and_visible(monkeypatch):
    calls=[]
    def locked(self,*args,**kwargs):calls.append(self);raise PermissionError('access denied')
    monkeypatch.setattr(Path,'read_text',locked)
    monkeypatch.setattr('mmd_bridge.core.time.sleep',lambda duration:None)
    with pytest.raises(PermissionError,match='access denied'):read_json('status.json')
    assert len(calls)==20


def test_corrupt_json_is_not_treated_as_a_sharing_violation(tmp_path):
    path=tmp_path/'status.json';path.write_text('{',encoding='utf8')
    with pytest.raises(json.JSONDecodeError):read_json(path)
