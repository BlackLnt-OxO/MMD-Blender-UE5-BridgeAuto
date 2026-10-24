"""Blender worker for the same texture preparation used by normal import."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.core import read_json, atomic_json
from mmd_bridge.blender_custom_materials import prepare_record

request = Path(sys.argv[sys.argv.index('--') + 1])
data = read_json(request)
records = [prepare_record(data['settings'], row, request.parent, data.get('records', {}).get(str(row['pmx_index'])))
           for row in data['sources']]
atomic_json(request.with_name('prepared.json'), {'records': records})
