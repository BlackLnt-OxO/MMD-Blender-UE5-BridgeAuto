import importlib
import json
from pathlib import Path
import unreal as u
from mmd_bridge import ue_materials
importlib.reload(ue_materials)
root = Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
folder = Path((root/'work/combined_job.txt').read_text().strip())
report = json.loads((folder/'ue_report.json').read_text(encoding='utf-8'))
manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
materials = ue_materials.Materials(report['root'], '', report)
materials.assets = {key:u.load_asset(path) for key,path in report['assets']['materials'].items()}
materials.assign(u.load_asset(report['assets']['mesh']), manifest['materials'])
(root/'work/material-slots-check.json').write_text(json.dumps(report['checks']['material_slot_preservation']),encoding='utf-8')
(folder/'ue_report.json').write_text(json.dumps(report, ensure_ascii=False,indent=2),encoding='utf-8')
