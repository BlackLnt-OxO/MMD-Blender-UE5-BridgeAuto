import json
from pathlib import Path
import runpy
import zipfile
import pytest
from scripts import build_viewport_helper as helper
from scripts.check_installation import RESOURCE_FILES, RUNTIME_SCRIPTS, check


def test_source_resources_are_relocatable_and_missing_worker_is_reported(tmp_path):
    checkout = tmp_path/'new computer source'
    for name in (*RESOURCE_FILES, *('scripts/'+s for s in RUNTIME_SCRIPTS)):
        path = checkout/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    check(checkout)
    (checkout/'scripts/blender_surface_layer.py').unlink()
    with pytest.raises(RuntimeError, match='blender_surface_layer.py'):
        check(checkout)


@pytest.mark.parametrize('build_id', ['same-engine', 'wrong-engine'])
def test_helper_build_uses_private_output_and_preserves_old_binary_on_mismatch(tmp_path, monkeypatch, build_id):
    root = tmp_path/'source with spaces'
    plugin = root/'plugins/unreal/MMDBridgeViewport'
    binary = plugin/'Binaries/Win64/UnrealEditor-MMDBridgeViewport.dll'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'old')
    unreal = tmp_path/'UE/Engine/Binaries/Win64/UnrealEditor.exe'
    unreal.parent.mkdir(parents=True)
    unreal.touch()
    unreal.with_name('UnrealEditor.modules').write_text(json.dumps({'BuildId':'same-engine'}))
    engine = unreal.parents[2]
    (engine/'Build/BatchFiles').mkdir(parents=True)
    (engine/'Build/BatchFiles/RunUAT.bat').touch()
    (engine/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':8}))
    monkeypatch.setattr(helper.tempfile, 'gettempdir', lambda: str(tmp_path/'temporary_builds'))

    def compile_plugin(command, **kwargs):
        package = Path(next(arg.removeprefix('-Package=') for arg in command if arg.startswith('-Package=')))
        assert package.is_relative_to(tmp_path/'temporary_builds/MMDBridgeNative') and not package.exists()
        assert kwargs == {'check':True,'cwd':root}
        assert command[0] == str(engine/'Build/BatchFiles/RunUAT.bat')
        target = package/'Binaries/Win64'
        target.mkdir(parents=True)
        (target/'UnrealEditor-MMDBridgeViewport.dll').write_bytes(b'new')
        (target/'UnrealEditor.modules').write_text(json.dumps({
            'BuildId':build_id,'Modules':{'MMDBridgeViewport':'UnrealEditor-MMDBridgeViewport.dll'}}))

    monkeypatch.setattr(helper.subprocess, 'run', compile_plugin)
    if build_id == 'wrong-engine':
        with pytest.raises(ValueError, match='BuildId differs'):
            helper.build(unreal, root)
        assert binary.read_bytes() == b'old'
    else:
        helper.build(unreal, root)
        assert binary.read_bytes() == b'new'


def test_helper_rejects_other_engine_before_running_build(tmp_path, monkeypatch):
    unreal = tmp_path/'UE/Engine/Binaries/Win64/UnrealEditor.exe'
    unreal.parent.mkdir(parents=True)
    unreal.touch()
    (unreal.parents[2]/'Build').mkdir()
    (unreal.parents[2]/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':7}))
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **kw: pytest.fail('Unexpected build'))
    with pytest.raises(ValueError, match='UE 5.8'):
        helper.build(unreal, tmp_path/'checkout')


def test_distribution_includes_surface_worker_and_source_bootstrap_without_build_caches(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path/'distribution'
    monkeypatch.setenv('MMDBRIDGE_DIST_ROOT', str(output))
    monkeypatch.syspath_prepend(str(root/'scripts'))

    def freeze(command, **kwargs):
        resources = [command[i+1] for i, value in enumerate(command) if value == '--add-data']
        assert str(root/'scripts/blender_surface_layer.py')+';bridge_resources/scripts' in resources
        assert str(root/'assets')+';bridge_resources/assets' in resources
        assert command[command.index('--icon')+1] == str(root/'assets/app-icon.ico')
        assert command[command.index('--name')+1] == 'MMDBridgeForUE'
        assert command[command.index('--version-file')+1] == str(root/'assets/windows-version.txt')
        (output/'MMDBridgeForUE').mkdir(parents=True)

    monkeypatch.setattr(helper.subprocess, 'run', freeze)
    runpy.run_path(str(root/'scripts/build.py'), run_name='__main__')
    assert (output/'MMDBridgeForUE/licenses/runtime/pywinauto/METADATA').is_file()
    assert (output/'MMDBridgeForUE/assets/app-icon.png').is_file()
    with zipfile.ZipFile(output/'MMDBridgeForUE/Source.zip') as source:
        names = source.namelist()
        assert 'start.cmd' in names and 'scripts/setup.ps1' in names
        assert 'scripts/blender_surface_layer.py' in names
        assert 'assets/app-icon.png' in names and 'assets/app-icon.ico' in names
        assert not any(set(Path(name).parts) & {'Binaries','Intermediate','__pycache__','bin','obj'} for name in names)
