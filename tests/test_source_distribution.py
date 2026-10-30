import json
from pathlib import Path
import runpy
import subprocess
import zipfile
import pytest
from scripts import build_viewport_helper as helper
from scripts import build_native_plugins as native
from scripts.check_installation import RESOURCE_FILES, RUNTIME_SCRIPTS, check, plugin_bundle_files


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


def test_plugin_bundle_keeps_native_runtime_without_compiler_caches(tmp_path):
    files = ('plugins/unreal/Bridge/Binaries/Win64/UnrealEditor-Bridge.dll',
             'plugins/unreal/Bridge/Binaries/Win64/UnrealEditor.modules',
             'plugins/unreal/Bridge/Source/Bridge.cpp',
             'plugins/unreal/Bridge/Intermediate/Build/Generated.h',
             'plugins/unreal/Bridge/Binaries/Win64/UnrealEditor-Bridge.pdb',
             'plugins/blender/__pycache__/addon.pyc')
    for name in files:
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert {path.relative_to(tmp_path).as_posix() for path in plugin_bundle_files(tmp_path)} == set(files[:3])


@pytest.mark.parametrize('build_id', ['same-engine', 'wrong-engine'])
def test_helper_build_uses_private_output_and_preserves_old_binary_on_mismatch(tmp_path, monkeypatch, build_id):
    monkeypatch.setattr(native, '_check_build_path', lambda work: None)
    root = tmp_path/'source with spaces'
    plugin = root/'plugins/unreal/MMDBridgeViewport'
    binary = plugin/'Binaries/Win64/UnrealEditor-MMDBridgeViewport.dll'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'old')
    (plugin/'Source').mkdir()
    (plugin/'MMDBridgeViewport.uplugin').write_text('{}')
    unreal = tmp_path/'UE/Engine/Binaries/Win64/UnrealEditor.exe'
    unreal.parent.mkdir(parents=True)
    unreal.touch()
    unreal.with_name('UnrealEditor.modules').write_text(json.dumps({'BuildId':'same-engine'}))
    engine = unreal.parents[2]
    (engine/'Build').mkdir()
    (engine/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':8}))
    (engine/'Source').mkdir()
    ubt = engine/'Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll'
    ubt.parent.mkdir(parents=True)
    ubt.touch()
    dotnet = engine/'Binaries/ThirdParty/DotNet/8.0.412/win-x64/dotnet.exe'
    dotnet.parent.mkdir(parents=True)
    dotnet.touch()

    def compile_plugin(command, **kwargs):
        project = Path(next(arg.removeprefix('-Project=') for arg in command if arg.startswith('-Project=')))
        assert project.is_relative_to(tmp_path/'temporary_builds')
        assert command[:2] == [str(dotnet), str(ubt)]
        assert '-NoUBA' in command
        assert kwargs['env']['UBA_ROOT'] == str(project.parent.parent/'uba')
        target = project.parent/'Plugins/MMDBridgeViewport/Binaries/Win64'
        target.mkdir(parents=True)
        (target/'UnrealEditor-MMDBridgeViewport.dll').write_bytes(b'new')
        (target/'UnrealEditor.modules').write_text(json.dumps({
            'BuildId':build_id,'Modules':{'MMDBridgeViewport':'UnrealEditor-MMDBridgeViewport.dll'}}))
        return {'returncode': 0}

    monkeypatch.setattr(native, 'run_build', compile_plugin)
    if build_id == 'wrong-engine':
        with pytest.raises(ValueError, match='BuildId differs'):
            helper.build(unreal, root, build_root=tmp_path/'temporary_builds')
        assert binary.read_bytes() == b'old'
    else:
        package = helper.build(unreal, root, build_root=tmp_path/'temporary_builds')
        assert package.name == 'MMDBridgeViewport'
        assert binary.read_bytes() == b'new'


def test_helper_rejects_other_engine_before_running_build(tmp_path, monkeypatch):
    unreal = tmp_path/'UE/Engine/Binaries/Win64/UnrealEditor.exe'
    unreal.parent.mkdir(parents=True)
    unreal.touch()
    (unreal.parents[2]/'Build').mkdir()
    (unreal.parents[2]/'Build/Build.version').write_text(json.dumps({'MajorVersion':5,'MinorVersion':7}))
    monkeypatch.setattr(native, 'run_build', lambda *a, **kw: pytest.fail('Unexpected build'))
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

    monkeypatch.setattr(subprocess, 'run', freeze)
    runpy.run_path(str(root/'scripts/build.py'), run_name='__main__')
    assert (output/'MMDBridgeForUE/licenses/runtime/pywinauto/METADATA').is_file()
    assert (output/'MMDBridgeForUE/assets/app-icon.png').is_file()
    assert (output/'MMDBridgeForUE/AGENTS.md').is_file()
    with zipfile.ZipFile(output/'MMDBridgeForUE/Source.zip') as source:
        names = source.namelist()
        assert 'start.cmd' in names and 'scripts/setup.ps1' in names
        assert 'scripts/build_native_plugins.py' in names
        assert 'scripts/native_build_process.py' in names
        assert 'scripts/build_viewport_helper.py' in names
        assert 'AGENTS.md' in names and '.gitignore' in names
        assert 'docs/DEVELOPMENT_HANDOFF.md' in names
        assert 'scripts/blender_surface_layer.py' in names
        assert 'assets/app-icon.png' in names and 'assets/app-icon.ico' in names
        assert not any(set(Path(name).parts) & {'Binaries','Intermediate','__pycache__','bin','obj'} for name in names)
