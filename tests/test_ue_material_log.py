import importlib
import sys
import types


def test_material_compile_errors_only_include_current_import(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "unreal", types.SimpleNamespace())
    host = importlib.import_module("mmd_bridge.ue_host")
    log = tmp_path / "Project.log"
    log.write_text("A_new Failed to compile Material\n", encoding="utf-8")
    marker = log.stat().st_size
    with log.open("a", encoding="utf-8") as stream:
        stream.write("A_old Failed to compile Material\n")
        stream.write("A_new Failed to compile Material\n")
        stream.write("LogPython: Error: importer warning to review\n")
    assert host.material_compile_errors(log, marker, "new") == ["A_new Failed to compile Material"]
    assert any(": Error:" in line for line in host.import_log_lines(log, marker))
