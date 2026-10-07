"""Compare USD geometry semantics across fresh namespaces, including normals/UV data."""
import argparse
import hashlib
import sys
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/python'))
from pxr import Sdf
from mmd_bridge.core import read_json, atomic_json, file_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('baseline', type=Path)
parser.add_argument('candidate', type=Path)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
contracts = []
for folder in (args.baseline, args.candidate):
    job = read_json(folder / 'job.json')
    scene = read_json(folder / 'manifest.json')['scene']
    root = job['settings']['content_root'].rstrip('/') + '/A_' + job['asset_id']
    files = {}
    for resource in scene['usd']['files']:
        relative = resource['relative'].replace('\\', '/')
        if relative != 'Geometry.usda' and not relative.startswith('Geometry/'):
            continue
        layer = Sdf.Layer.FindOrOpen(resource['path'])
        assert layer, resource['path']
        text = layer.ExportToString().replace(root, '<IMPORT_ROOT>')
        files[relative] = {'file_sha256': file_hash(resource['path']),
                           'semantic_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    receipt = {'schema': 1, 'job': str(folder.resolve()), 'namespace_normalized': root,
               'files': files, 'normalization': 'only this import root; all layer data retained'}
    atomic_json(folder / 'audit/geometry_contract.json', receipt)
    contracts.append(receipt)
a, b = contracts
different = [key for key in sorted(set(a['files']) | set(b['files']))
             if a['files'].get(key, {}).get('semantic_sha256') != b['files'].get(key, {}).get('semantic_sha256')]
result = {'complete': True, 'geometry_semantically_equal': not different, 'differences': different,
          'baseline': a, 'candidate': b, 'visual_acceptance': False}
atomic_json(args.output, result)
print('GEOMETRY_CONTRACT_EQUAL', not different, 'different layers', len(different), flush=True)
