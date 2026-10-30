import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import build_native_plugins as native
from scripts.native_build_process import BuildError, BuildLimits, run_build


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    # These tests fake the compiler; generated C++ path limits are checked separately.
    monkeypatch.setattr(native, '_check_build_path', lambda work: None)
    root = tmp_path/'checkout'
    for name in native.PLUGINS:
        plugin = root/'plugins/unreal'/name
        (plugin/'Source').mkdir(parents=True)
        (plugin/(name+'.uplugin')).write_text('{}')
        (plugin/'Source/source.cpp').write_text('// source')
        binaries = plugin/'Binaries/Win64'
        binaries.mkdir(parents=True)
        (binaries/('UnrealEditor-'+name+'.dll')).write_bytes(b'old '+name.encode())
        (binaries/'keep_other_target.bin').write_bytes(b'keep')
    engine = tmp_path/'engine/Engine'
    (engine/'Build').mkdir(parents=True)
    (engine/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':8,'PatchVersion':2}))
    (engine/'Source').mkdir()
    unreal = engine/'Binaries/Win64/UnrealEditor.exe'
    unreal.parent.mkdir(parents=True)
    unreal.touch()
    unreal.with_name('UnrealEditor.modules').write_text(json.dumps({'BuildId':'engine-id'}))
    ubt = engine/'Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll'
    ubt.parent.mkdir(parents=True)
    ubt.touch()
    dotnet = engine/'Binaries/ThirdParty/DotNet/8.0.412/win-x64/dotnet.exe'
    dotnet.parent.mkdir(parents=True)
    dotnet.touch()
    return root, unreal, tmp_path/'runs'


def fake_compiler(command, **kwargs):
    project = Path(next(arg[len('-Project='):] for arg in command if arg.startswith('-Project=')))
    for plugin in json.loads(project.read_text())['Plugins']:
        name = plugin['Name']
        destination = project.parent/'Plugins'/name/'Binaries/Win64'
        assert not destination.exists()
        destination.mkdir(parents=True)
        (destination/('UnrealEditor-'+name+'.dll')).write_bytes(b'new '+name.encode())
        (destination/'UnrealEditor.modules').write_text(json.dumps({
            'BuildId':'engine-id','Modules':{name:'UnrealEditor-'+name+'.dll'}}))
    return {'returncode':0}


def old_binary(root, name):
    return (root/'plugins/unreal'/name/'Binaries/Win64'/('UnrealEditor-'+name+'.dll')).read_bytes()


def test_both_plugins_use_private_writable_uba_root_before_compiler_starts(checkout, monkeypatch):
    root, unreal, runs = checkout

    def compile_checked(command, **kwargs):
        uba = Path(kwargs['env']['UBA_ROOT'])
        assert uba.is_relative_to(runs)
        assert (uba/'sessions').is_dir() and (uba/'cas').is_dir()
        assert '-UBARootDir='+str(uba) in command
        assert {'-NoUBA','-UBANoDetour','-UBADisableRemote','-UBADisableHorde','-NoXGE','-NoFASTBuild',
                '-NoSNDBS','-NoHotReloadFromIDE','-NoEngineChanges'} <= set(command)
        assert not any('RunUAT' in arg or 'BuildPlugin' in arg for arg in command)
        return fake_compiler(command, **kwargs)

    monkeypatch.setattr(native, 'run_build', compile_checked)
    work = native.build(unreal, root, build_root=runs)
    receipt = json.loads((work/'build_receipt.json').read_text())
    assert receipt['status'] == 'built' and len(receipt['published']) == 2
    for name in native.PLUGINS:
        assert old_binary(root, name) == b'new '+name.encode()
        assert (root/'plugins/unreal'/name/'Binaries/Win64/keep_other_target.bin').read_bytes() == b'keep'
        backup = next(row['previous_binaries'] for row in receipt['published'] if row['plugin'] == name)
        assert (Path(backup)/'Win64'/('UnrealEditor-'+name+'.dll')).read_bytes() == b'old '+name.encode()


def test_unwritable_workspace_is_rejected_before_launch(checkout, monkeypatch):
    root, unreal, runs = checkout
    monkeypatch.setattr(native, '_probe_directory', lambda p: (_ for _ in ()).throw(PermissionError(str(p))))
    monkeypatch.setattr(native, 'run_build', lambda *a, **kw: pytest.fail('Compiler must not start'))
    with pytest.raises(PermissionError):
        native.build(unreal, root, build_root=runs)
    assert all(old_binary(root, name).startswith(b'old') for name in native.PLUGINS)


def test_unwritable_uba_sessions_are_rejected_before_launch(checkout, monkeypatch):
    root, unreal, runs = checkout
    original = native._probe_directory

    def denied(directory):
        if directory.name == 'sessions':
            raise PermissionError('sessions are not writable')
        original(directory)

    monkeypatch.setattr(native, '_probe_directory', denied)
    monkeypatch.setattr(native, 'run_build', lambda *a, **kw: pytest.fail('Compiler must not start'))
    with pytest.raises(PermissionError, match='sessions'):
        native.build(unreal, root, build_root=runs)
    assert all(old_binary(root, name).startswith(b'old') for name in native.PLUGINS)
    receipt = json.loads(next(runs.glob('build_*/build_receipt.json')).read_text())
    assert receipt['status'] == 'failed'


def test_compiler_failure_preserves_both_previous_plugins(checkout, monkeypatch):
    root, unreal, runs = checkout

    def failed(command, **kwargs):
        raise BuildError('compile failed', kwargs['log_dir'])

    monkeypatch.setattr(native, 'run_build', failed)
    with pytest.raises(BuildError, match='compile failed'):
        native.build(unreal, root, build_root=runs)
    assert all(old_binary(root, name).startswith(b'old') for name in native.PLUGINS)


def test_one_bad_module_preserves_both_previous_plugins(checkout, monkeypatch):
    root, unreal, runs = checkout

    def wrong_second(command, **kwargs):
        result = fake_compiler(command, **kwargs)
        work = Path(kwargs['env']['UBA_ROOT']).parent
        manifest = work/'HostProject/Plugins/MMDBridgeViewport/Binaries/Win64/UnrealEditor.modules'
        data = json.loads(manifest.read_text())
        data['BuildId'] = 'different-engine'
        manifest.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(native, 'run_build', wrong_second)
    with pytest.raises(ValueError, match='BuildId differs'):
        native.build(unreal, root, build_root=runs)
    assert all(old_binary(root, name) == b'old '+name.encode() for name in native.PLUGINS)
    assert json.loads(next(runs.glob('build_*/build_receipt.json')).read_text())['status'] == 'failed'


def test_publish_error_rolls_back_the_first_plugin(checkout, monkeypatch):
    root, unreal, runs = checkout
    monkeypatch.setattr(native, 'run_build', fake_compiler)
    original = Path.replace

    def locked_second(path, target):
        if path.name == 'incoming' and Path(target) == root/'plugins/unreal/MMDBridgeViewport/Binaries':
            raise PermissionError('Simulated loaded DLL')
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', locked_second)
    with pytest.raises(PermissionError, match='loaded DLL'):
        native.build(unreal, root, build_root=runs)
    assert all(old_binary(root, name) == b'old '+name.encode() for name in native.PLUGINS)


def test_build_only_does_not_replace_checkout_plugins(checkout, monkeypatch):
    root, unreal, runs = checkout
    monkeypatch.setattr(native, 'run_build', fake_compiler)
    work = native.build(unreal, root, build_root=runs, install=False)
    assert all(old_binary(root, name).startswith(b'old') for name in native.PLUGINS)
    assert json.loads((work/'build_receipt.json').read_text())['published'] == []


def test_other_ue_version_requires_opt_in_and_stays_unverified(checkout, monkeypatch):
    root, unreal, runs = checkout
    (unreal.parents[2]/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':9}))
    monkeypatch.setattr(native, 'run_build', fake_compiler)
    with pytest.raises(ValueError, match='allow-untested-engine'):
        native.build(unreal, root, build_root=runs)
    work = native.build(unreal, root, build_root=runs, allow_untested_engine=True, install=False)
    receipt = json.loads((work/'build_receipt.json').read_text())
    assert 'untested' in receipt['engine']['version_policy']
    assert receipt['build_success_is_runtime_validation'] is False


@pytest.mark.parametrize('major', [4,6])
def test_other_major_requires_porting_even_with_opt_in(checkout, monkeypatch, major):
    root, unreal, runs = checkout
    (unreal.parents[2]/'Build/Build.version').write_text(json.dumps({'MajorVersion':major,'MinorVersion':0}))
    monkeypatch.setattr(native, 'run_build', lambda *a, **kw: pytest.fail('Unsupported major must not compile'))
    with pytest.raises(ValueError):
        native.build(unreal, root, build_root=runs, allow_untested_engine=True)


@pytest.mark.parametrize('field,value', [('timeout_seconds',0),('timeout_seconds',float('inf')),
                                         ('max_log_bytes',0),('repeated_errors',0)])
def test_invalid_limits_fail_before_launch(field, value):
    with pytest.raises(ValueError):
        BuildLimits(**{field:value})


@pytest.mark.skipif(os.name != 'nt', reason='UBT Windows generated path limit')
def test_long_build_path_rejected_with_short_root_instruction():
    with pytest.raises(ValueError, match='shorter --build-root'):
        native._check_build_path(Path('C:/')/('long_build_root_'*12))


def test_real_process_success_has_bounded_log(tmp_path):
    result = run_build([sys.executable,'-u','-c',"print('compiled successfully')"],
                       cwd=tmp_path, log_dir=tmp_path/'logs', limits=BuildLimits(10,4096,3))
    assert result['returncode'] == 0
    assert (tmp_path/'logs/console.log').read_text().strip() == 'compiled successfully'


def test_legacy_console_encoding_does_not_abort_compiler_or_change_raw_log(tmp_path, monkeypatch):
    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding='gbk', errors='strict', write_through=True)
    monkeypatch.setattr(sys, 'stdout', console)
    payload = '\ufffd \U0001f680\n'.encode('utf-8')
    code = 'import sys; sys.stdout.buffer.write(' + repr(payload) + ')'
    result = run_build([sys.executable, '-u', '-c', code], cwd=tmp_path, log_dir=tmp_path/'logs',
                       limits=BuildLimits(10, 4096, 3))
    assert result['returncode'] == 0
    assert (tmp_path/'logs/console.log').read_bytes() == payload
    assert b'\\ufffd' in output.getvalue() and b'\\U0001f680' in output.getvalue()


def test_real_process_failure_keeps_native_diagnostic(tmp_path):
    code = "import sys; print('C++ compile failed',flush=True); sys.exit(7)"
    with pytest.raises(BuildError, match='exited with code 7'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=tmp_path/'logs',
                  limits=BuildLimits(10,4096,3))
    assert 'C++ compile failed' in (tmp_path/'logs/console.log').read_text()


def test_real_process_known_uba_permission_failure_stops_immediately(tmp_path):
    code = "import time; print('UbaStorageServer - Failed to create directory sessions/1 (Access is denied.)',flush=True); time.sleep(20)"
    started = time.monotonic()
    with pytest.raises(BuildError, match='UBA could not create'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=tmp_path/'logs',
                  limits=BuildLimits(10,4096,20))
    assert time.monotonic()-started < 10


def test_real_process_repeated_numbered_error_is_detected(tmp_path):
    code = "import time\nfor n in range(100):\n print(f'error: failed request {n}',flush=True); time.sleep(.01)\ntime.sleep(20)"
    with pytest.raises(BuildError, match='Repeated compiler error'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=tmp_path/'logs',
                  limits=BuildLimits(10,4096,3))


def test_real_process_external_log_limit_is_monitored(tmp_path):
    logs = tmp_path/'logs'
    code = "import pathlib,time\np=pathlib.Path('logs/native.json')\nwith p.open('wb',buffering=0) as f:\n for n in range(200):\n  f.write(b'x'*1024); time.sleep(.01)\ntime.sleep(20)"
    with pytest.raises(BuildError, match='logs reached limit'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=logs,
                  limits=BuildLimits(10,4096,3))
    assert (logs/'native.json').stat().st_size < 32768


def test_real_process_no_newline_output_cannot_fill_log_or_memory(tmp_path):
    code = "import sys,time; sys.stdout.buffer.write(b'x'*100000); sys.stdout.flush(); time.sleep(20)"
    with pytest.raises(BuildError, match='logs reached limit'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=tmp_path/'logs',
                  limits=BuildLimits(10,4096,3))
    assert (tmp_path/'logs/console.log').stat().st_size <= 4096


def test_real_timeout_also_terminates_descendant(tmp_path):
    child_script = tmp_path/'child.py'
    child_script.write_text("import os,time,pathlib; pathlib.Path('grandchild.pid').write_text(str(os.getpid())); time.sleep(30)")
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'child.py']); time.sleep(30)"
    with pytest.raises(BuildError, match='timed out'):
        run_build([sys.executable,'-u','-c',code], cwd=tmp_path, log_dir=tmp_path/'logs',
                  limits=BuildLimits(2,4096,3))
    pid = int((tmp_path/'grandchild.pid').read_text())
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel.CloseHandle(handle)
            assert exit_code.value != 259
    else:
        # A killed descendant may briefly remain as a zombie until its parent is reaped.
        status = Path(f'/proc/{pid}/stat')
        assert not status.exists() or status.read_text().split()[2] == 'Z'
