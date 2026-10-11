"""Compile only our editor plugin against the user's installed UE, not the engine."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def build(unreal, root=ROOT):
    unreal = Path(unreal).resolve(strict=True)
    if unreal.name.lower() != 'unrealeditor.exe':
        raise ValueError('Select Engine/Binaries/Win64/UnrealEditor.exe')
    engine = unreal.parents[2]
    version = json.loads((engine/'Build/Build.version').read_text(encoding='utf-8-sig'))
    if (version['MajorVersion'], version['MinorVersion']) != (5, 8):
        raise ValueError('This helper targets UE 5.8.x')
    engine_id = json.loads(unreal.with_name('UnrealEditor.modules').read_text(encoding='utf-8-sig'))['BuildId']
    if not engine_id:
        raise ValueError('The selected engine has no BuildId')
    runner = engine/'Build/BatchFiles/RunUAT.bat'
    if not runner.is_file():
        raise FileNotFoundError(runner)
    plugin = root/'plugins/unreal/MMDBridgeViewport'
    # UBT rejects generated paths over 260 characters even on long-path-enabled Windows.
    work = Path(tempfile.gettempdir())/'MMDBridgeNative'
    work.mkdir(parents=True, exist_ok=True)
    # BuildPlugin may clear Package; give it a new, empty destination owned by this run.
    package = Path(tempfile.mkdtemp(prefix='build_', dir=work))/'package'
    subprocess.run([str(runner), 'BuildPlugin', '-Plugin='+str(plugin/'MMDBridgeViewport.uplugin'),
                    '-Package='+str(package), '-TargetPlatforms=Win64', '-Rocket'], check=True, cwd=root)
    modules = package/'Binaries/Win64/UnrealEditor.modules'
    data = json.loads(modules.read_text(encoding='utf-8-sig'))
    if data.get('BuildId') != engine_id:
        raise ValueError('Plugin BuildId differs from the engine; existing binaries were preserved. Build output: '+str(package))
    dll = modules.parent/data['Modules']['MMDBridgeViewport']
    if not dll.is_file():
        raise FileNotFoundError(dll)
    shutil.copytree(package/'Binaries', plugin/'Binaries', dirs_exist_ok=True)
    print('UE helper ready: '+str(plugin/'Binaries')+' (BuildId '+engine_id+')')
    print('In the GUI, install the UE bridge into your closed project, then reopen it.')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal', required=True, help='Path to UnrealEditor.exe (UE 5.8.x)')
    build(parser.parse_args().unreal)
