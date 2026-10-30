"""Reproduce the bundled Cel template from an authorized local reference project.

The original project is never opened. UE reads and relocates a dependency copy.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys

ROLES = {'skin': 'MI_00', 'face': 'MI_04_Face', 'hair': 'MI_15_hair01',
         'eye': 'MI_07', 'brow': 'MI_08---13_Brow', 'mouth': 'MI_10_tough',
         'cloth': 'MI_25---36_Texture4', 'default': 'MI_08---13_Brow'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--work', required=True, type=Path)
    p.add_argument('--unreal', required=True, type=Path)
    p.add_argument('--collect', action='store_true', help='Collect a completed native authoring run without rerunning UE')
    args = p.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    from mmd_bridge.character_cel import VERSION, PREFIX
    here = args.work.resolve()
    if args.collect:
        collect(repo, here, args.source.resolve())
        return
    here.mkdir(parents=True, exist_ok=False)
    source = args.source.resolve()
    stringify = args.unreal.with_name('UnrealAssetStringify.exe')
    masters = ['Character/Materials/Master/'+s for s in ('M_PBR', 'M_NPR', 'M_Outline')]
    roots = masters + ['Character/MatCap/HairSpa1_psd']
    instances = ['Character/Materials/NPR/'+s for s in ROLES.values()]
    assets = {}

    def closure(paths):
        pending = list(paths)
        seen = set()
        while pending:
            rel = pending.pop()
            if rel in seen:
                continue
            seen.add(rel)
            f = source/'Content'/(rel+'.uasset')
            if rel not in assets:
                r = subprocess.run([str(stringify), str(f)], capture_output=True, check=True)
                text = r.stdout.decode('utf-8', 'replace')
                assets[rel] = json.loads(text[text.index('{'):])
            for ref in assets[rel]['__PackageSummary']['PackageImportTable']:
                if ref.startswith('uobject:/Game/'):
                    dep = ref[len('uobject:/Game/'):].split('.')[0].split(':')[0]
                    if (source/'Content'/(dep+'.uasset')).is_file():
                        pending.append(dep)
        return seen

    bundled = closure(roots)
    closure(instances)
    identities = []
    for rel in sorted(assets):
        src = source/'Content'/(rel+'.uasset')
        dst = here/'Content'/(rel+'.uasset')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        identities.append({'package': '/Game/'+rel, 'sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
                           'mtime_ns': src.stat().st_mtime_ns})
    (here/'source_identity.json').write_text(json.dumps(identities, indent=2), encoding='utf-8')
    config = {'roles': ROLES, 'bundled': sorted(bundled), 'repo': str(repo), 'work': str(here),
              'version': VERSION, 'asset_prefix': PREFIX}
    (here/'author_config.json').write_text(json.dumps(config), encoding='utf-8')
    (here/'CelTemplate.uproject').write_text(json.dumps({'FileVersion': 3, 'EngineAssociation': '5.8',
        'Plugins': [{'Name': 'PythonScriptPlugin', 'Enabled': True}, {'Name': 'EditorScriptingUtilities', 'Enabled': True}]}), encoding='utf-8')
    script = repo/'scripts/author_builtin_cel_template.py'
    command = [str(args.unreal.with_name('UnrealEditor-Cmd.exe')), str(here/'CelTemplate.uproject'),
               '-run=pythonscript', '-script='+str(script), '-CelAuthorConfig='+str(here/'author_config.json'),
               '-UserDir='+str(here/'cache')+'/', '-abslog='+str(here/'author.log'), '-nullrhi',
               '-unattended', '-stdout', '-FullStdOutLogOutput', '-UTF8Output', '-NoSound', '-NoAutoSave', '-NoSourceControl']
    with (here/'stdout.log').open('wb') as log:
        r = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    changed = [i['package'] for i in identities if hashlib.sha256((source/'Content'/(i['package'][6:]+'.uasset')).read_bytes()).hexdigest()!=i['sha256']]
    if changed or r.returncode or not (here/'native_receipt.json').is_file():
        raise RuntimeError(f'Cel template authoring failed: exit={r.returncode}, source_changed={changed}; see {here}')
    collect(repo, here, source)


def collect(repo, here, source):
    config = json.loads((here/'author_config.json').read_text(encoding='utf-8'))
    prefix = config.get('asset_prefix', '/Game/MMDBridge/BuiltinCel/v1/').removeprefix('/Game/')
    bundled = config['bundled']
    identities = json.loads((here/'source_identity.json').read_text(encoding='utf-8'))
    if not (here/'native_receipt.json').is_file():
        raise RuntimeError('Missing native authoring receipt')
    native = json.loads((here/'native_receipt.json').read_text(encoding='utf-8'))
    if config.get('version', 1) >= 2 and (native.get('filter_translucency_pass') != 'BeforeDOF'
            or native.get('version') != config['version'] or native.get('asset_prefix') != config['asset_prefix']):
        raise RuntimeError('当前卡通母版缺少 Before DOF 原生验证，请重新生成')
    assert all(hashlib.sha256((source/'Content'/(i['package'][6:]+'.uasset')).read_bytes()).hexdigest()==i['sha256'] for i in identities)
    dest = repo/'resources/character_materials/BuiltinCel'
    if dest.exists():
        raise FileExistsError(dest)
    for rel in bundled:
        src = here/'Content'/prefix/(rel+'.uasset')
        dst = dest/'Content'/prefix/(rel+'.uasset')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(here/'reference_parameters.json', dest/'reference_parameters.json')
    manifest = {'version': config.get('version', 1), 'source_project': source.name, 'sources': identities,
                'asset_prefix': config.get('asset_prefix', '/Game/MMDBridge/BuiltinCel/v1/'),
                'filter_translucency_pass': native.get('filter_translucency_pass', 'unverified'),
                'files': {str(f.relative_to(dest)).replace('\\', '/'): hashlib.sha256(f.read_bytes()).hexdigest()
                          for f in sorted(dest.rglob('*.uasset'))}}
    (dest/'provenance.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('CEL_TEMPLATE_READY', len(bundled), dest)


if __name__ == '__main__':
    main()
