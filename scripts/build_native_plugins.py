"""Build the two bridge plugins in a private project using the selected UE's UBT."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

if __package__:
    from .native_build_process import BuildError, BuildLimits, print_build_message, run_build
else:
    from native_build_process import BuildError, BuildLimits, print_build_message, run_build

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ('MMDBridgeCharacter', 'MMDBridgeViewport')


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _probe_directory(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='write_probe_', dir=directory) as folder:
        probe = Path(folder) / 'probe.bin'
        with probe.open('xb') as stream:
            stream.write(b'MMDBridge native build write check')
            stream.flush()
            os.fsync(stream.fileno())


def _dotnet(engine):
    directory = engine / 'Binaries/ThirdParty/DotNet'
    candidates = [path for path in directory.glob('*/win-x64/dotnet.exe') if path.is_file()]
    if not candidates:
        raise FileNotFoundError('The selected UE has no bundled Windows x64 .NET runtime: ' + str(directory))
    config = engine / 'Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.runtimeconfig.json'
    major = None
    if config.is_file():
        framework = _json(config).get('runtimeOptions', {}).get('framework', {})
        major = str(framework.get('version', '')).split('.')[0]
    matching = [path for path in candidates if path.parent.parent.name.split('.')[0] == major]
    def version_key(path):
        return tuple(int(part) if part.isdigit() else 0 for part in path.parent.parent.name.split('.'))
    return sorted(matching or candidates, key=version_key)[-1]


def _checked_binary(plugin, name, build_id):
    binary_dir = plugin / 'Binaries/Win64'
    modules_file = binary_dir / 'UnrealEditor.modules'
    modules = _json(modules_file)
    if modules.get('BuildId') != build_id:
        raise ValueError(f'Plugin BuildId differs from the selected engine: {name}')
    filename = modules.get('Modules', {}).get(name)
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith('.dll'):
        raise ValueError(f'Missing or invalid compiled module: {name}')
    binary = binary_dir / filename
    if not binary.is_file() or not binary.stat().st_size:
        raise FileNotFoundError(binary)
    return {'BuildId': build_id, 'dll': str(binary), 'dll_sha256': _sha(binary)}


def _check_build_path(work):
    longest_module = work / 'HostProject/Plugins/MMDBridgeCharacter/Intermediate/Build/Win64/x64/UnrealEditor/Development/MMDBridgeCharacter'
    if os.name == 'nt' and len(str(longest_module)) > 220:
        raise ValueError('Build path is too long for UBT generated files. Choose a shorter --build-root: ' + str(work))


def _long_path(path):
    """Publishing may need more path space than the short, isolated C++ build."""
    text = str(Path(path).absolute())
    if os.name != 'nt' or text.startswith('\\\\?\\'):
        return Path(text)
    if text.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + text[2:])
    return Path('\\\\?\\' + text)


def _publish(outputs, root, receipt):
    """Stage every plugin first, then replace directories with rollback on any failure."""
    token = uuid.uuid4().hex[:12]
    stages = []
    for name, source in outputs.items():
        plugin = (root / 'plugins/unreal' / name).resolve(strict=True)
        if not plugin.is_relative_to(root) or plugin.is_symlink():
            raise ValueError('Plugin output escaped the checkout: ' + str(plugin))
        transaction = plugin / 'Intermediate/MMDBridgePublish' / token
        if not transaction.resolve().is_relative_to(plugin):
            raise ValueError('Publish staging escaped the plugin: ' + str(transaction))
        destination = plugin / 'Binaries'
        if destination.is_symlink() or (destination.exists() and destination.resolve().parent != plugin):
            raise ValueError('Refusing to replace a linked Binaries directory: ' + str(destination))
        transaction = _long_path(transaction)
        transaction.mkdir(parents=True, exist_ok=False)
        incoming = transaction / 'incoming'
        if destination.exists():
            shutil.copytree(destination, incoming)
        shutil.copytree(source / 'Binaries', incoming, dirs_exist_ok=True)
        # Inspect the staged module manifest before changing either installed directory.
        modules = _json(incoming / 'Win64/UnrealEditor.modules')
        if modules.get('BuildId') != receipt['engine']['BuildId']:
            raise ValueError('Staged BuildId differs: ' + name)
        filename = modules['Modules'][name]
        if _sha(incoming / 'Win64' / filename) != receipt['plugins'][name]['dll_sha256']:
            raise ValueError('Staged plugin differs: ' + name)
        stages.append({'name': name, 'incoming': incoming, 'destination': destination,
                       'backup': transaction / 'previous', 'old_moved': False, 'new_moved': False})
    try:
        for stage in stages:
            destination = stage['destination']
            if destination.is_symlink() or (destination.exists() and destination.resolve().parent != destination.parent):
                raise ValueError('Refusing to replace a linked Binaries directory: ' + str(destination))
            if destination.exists():
                destination.replace(stage['backup'])
                stage['old_moved'] = True
            stage['incoming'].replace(destination)
            stage['new_moved'] = True
    except BaseException as error:
        rollback_errors = []
        for stage in reversed(stages):
            try:
                if stage['new_moved']:
                    stage['destination'].replace(stage['incoming'])
                if stage['old_moved']:
                    stage['backup'].replace(stage['destination'])
            except OSError as rollback_error:
                rollback_errors.append(f"{stage['backup']}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError('Publish failed; rollback needs attention. Preserved backups: '
                               + '; '.join(rollback_errors)) from error
        raise
    return [{'plugin': stage['name'], 'installed': str(stage['destination']),
             'previous_binaries': str(stage['backup']) if stage['old_moved'] else None} for stage in stages]


def build(unreal, root=ROOT, *, plugins=PLUGINS, allow_untested_engine=False,
          build_root=None, limits=BuildLimits(), install=True):
    unreal = Path(unreal).resolve(strict=True)
    if unreal.name.lower() != 'unrealeditor.exe':
        raise ValueError('Select Engine/Binaries/Win64/UnrealEditor.exe')
    engine = unreal.parents[2]
    version = _json(engine / 'Build/Build.version')
    engine_version = (version['MajorVersion'], version['MinorVersion'])
    if engine_version[0] != 5 or (engine_version != (5, 8) and not allow_untested_engine):
        raise ValueError('UE 5.8.x is the current target; other UE 5 versions require --allow-untested-engine')
    root = Path(root).resolve(strict=True)
    plugins = tuple(dict.fromkeys(plugins))
    if not plugins or any(name not in PLUGINS for name in plugins):
        raise ValueError('Select MMDBridgeCharacter and/or MMDBridgeViewport')
    build_id = _json(unreal.with_name('UnrealEditor.modules')).get('BuildId')
    if not isinstance(build_id, str) or not build_id:
        raise ValueError('The selected engine has no BuildId')
    ubt = engine / 'Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll'
    if not ubt.is_file():
        raise FileNotFoundError('The selected engine must already include a built UnrealBuildTool: ' + str(ubt))
    dotnet = _dotnet(engine)
    for name in plugins:
        source = root / 'plugins/unreal' / name
        if not (source / (name + '.uplugin')).is_file() or not (source / 'Source').is_dir():
            raise FileNotFoundError('Plugin source is missing: ' + str(source))
    parent = Path(build_root) if build_root else Path(tempfile.gettempdir()) / 'MMDBridgeNative'
    _probe_directory(parent)
    work = Path(tempfile.mkdtemp(prefix='build_', dir=parent)).resolve()
    receipt = {'started': datetime.now(timezone.utc).isoformat(), 'status': 'preparing',
               'engine': {'version': version, 'BuildId': build_id,
                          'version_policy': 'UE 5.8 target' if engine_version == (5, 8) else 'explicit untested UE 5 build'},
               'build_success_is_runtime_validation': False, 'plugins': {}, 'work': str(work),
               'install_requested': install, 'published': []}
    receipt_file = work / 'build_receipt.json'
    try:
        _check_build_path(work)
        host = work / 'HostProject'
        host.mkdir()
        project = host / 'MMDBridgeNative.uproject'
        _write_json(project, {'FileVersion': 3, 'DisableEnginePluginsByDefault': True,
                              'Plugins': [{'Name': name, 'Enabled': True} for name in plugins]})
        outputs = {}
        for name in plugins:
            destination = host / 'Plugins' / name
            shutil.copytree(root / 'plugins/unreal' / name, destination,
                            ignore=shutil.ignore_patterns('Binaries', 'Intermediate', '__pycache__', '.vs'))
            outputs[name] = destination
        uba = work / 'uba'
        for folder in (uba, uba / 'sessions', uba / 'cas', uba / 'castemp'):
            _probe_directory(folder)
        logs = work / 'logs'
        logs.mkdir()
        command = [str(dotnet), str(ubt), 'UnrealEditor', 'Win64', 'Development',
                   '-Project=' + str(project), '-NoUBTMakefiles', '-NoHotReload', '-NoHotReloadFromIDE', '-NoEngineChanges',
                   '-NoUBA', '-NoXGE', '-NoFASTBuild', '-NoSNDBS', '-UBANoDetour', '-UBADisableRemote', '-UBADisableHorde',
                   '-UBARootDir=' + str(uba), '-log=' + str(logs / 'ubt.log')]
        env = dict(os.environ, UBA_ROOT=str(uba), DOTNET_ROOT=str(dotnet.parent),
                   DOTNET_MULTILEVEL_LOOKUP='0', DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1',
                   DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_CLI_HOME=str(work / 'dotnet_home'))
        receipt.update(status='building', command=command, uba_root=str(uba),
                       limits={'timeout_seconds': limits.timeout_seconds, 'max_log_bytes': limits.max_log_bytes,
                               'repeated_errors': limits.repeated_errors})
        _write_json(receipt_file, receipt)
        print_build_message('Native plugin build workspace: ' + str(work))
        if engine_version != (5, 8):
            print_build_message('Untested UE version selected explicitly; a successful build does not establish runtime compatibility.')
        receipt['process'] = run_build(command, cwd=engine / 'Source', log_dir=logs, limits=limits, env=env)
        if _json(unreal.with_name('UnrealEditor.modules')).get('BuildId') != build_id:
            raise ValueError('Engine BuildId changed during compilation; existing binaries were preserved')
        for name, output in outputs.items():
            receipt['plugins'][name] = _checked_binary(output, name, build_id)
        if install:
            receipt['published'] = _publish(outputs, root, receipt)
        receipt['status'] = 'built'
        print_build_message('Plugin compilation and BuildId checks passed. Receipt: ' + str(receipt_file))
        if install:
            print_build_message('Binaries updated in the checkout. Save and close UE before installing through the GUI.')
        else:
            print_build_message('Build-only mode: checkout and UE project binaries were not replaced.')
    except BaseException as error:
        receipt.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        _write_json(receipt_file, receipt)
    return work


def main(argv=None, *, default_plugins=PLUGINS):
    selected_by_default = ' and '.join(default_plugins)
    parser = argparse.ArgumentParser(description='Build ' + selected_by_default + ' using the selected UE in a private project.')
    parser.add_argument('--unreal', required=True, help='Target Engine/Binaries/Win64/UnrealEditor.exe')
    parser.add_argument('--plugin', action='append', choices=PLUGINS,
                        help='Build only this plugin; repeat for both (default: ' + selected_by_default + ')')
    parser.add_argument('--allow-untested-engine', action='store_true', help='Explicitly try another UE 5 version')
    parser.add_argument('--build-root', type=Path, help='Parent directory for a new private build workspace')
    parser.add_argument('--no-install', action='store_true', help='Verify outputs without replacing checkout Binaries')
    parser.add_argument('--timeout-minutes', type=float, default=30)
    parser.add_argument('--max-log-mib', type=float, default=64)
    parser.add_argument('--repeated-errors', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        limits = BuildLimits(args.timeout_minutes * 60, int(args.max_log_mib * 1024 * 1024), args.repeated_errors)
        build(args.unreal, plugins=args.plugin or default_plugins, allow_untested_engine=args.allow_untested_engine,
              build_root=args.build_root, limits=limits, install=not args.no_install)
    except KeyboardInterrupt:
        print_build_message('Build cancelled; compiler processes were stopped.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print_build_message(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
