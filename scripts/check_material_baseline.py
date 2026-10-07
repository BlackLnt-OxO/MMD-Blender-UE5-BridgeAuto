"""Record or compare scene outputs; this never changes the conversion route."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.core import atomic_json, read_json
from mmd_bridge.material_regression import capture, compare

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('job', type=Path)
parser.add_argument('--baseline', required=True, type=Path)
parser.add_argument('--record', action='store_true', help='record technical output, without visual approval')
parser.add_argument('--report', type=Path)
args = parser.parse_args()
snapshot = capture(args.job)
if args.record:
    if args.baseline.exists():
        parser.error('baseline exists; recording must not overwrite historical evidence')
    atomic_json(args.baseline, snapshot)
    print('RECORDED_TECHNICAL_BASELINE', len(snapshot['materials']))
else:
    result = compare(read_json(args.baseline), snapshot)
    if args.report:
        atomic_json(args.report, result)
    print('MATERIAL_REGRESSION', result['output_contract_equal'],
          'renderer_changed', result['renderer_changed'], 'differences', len(result['differences']),
          'independent_import', result['independent_import'])
    sys.exit(0 if result['technical_gate_passed'] else 2)
