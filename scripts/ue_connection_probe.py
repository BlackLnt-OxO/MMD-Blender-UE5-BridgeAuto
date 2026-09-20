import unreal as u,json
from pathlib import Path
import mmd_bridge.ue_host as host
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
folder=Path((root/'work/full_job.txt').read_text().strip())
report=json.loads((folder/'ue_report.json').read_text(encoding='utf-8'))
mesh=u.load_asset(report['assets']['mesh'])
anim=u.load_asset(report['assets']['animation'])
result={'plugin_module':host.__file__,'skeleton_saved':mesh.get_editor_property('skeleton') is not None,
        'same_skeleton_after_restart':mesh.get_editor_property('skeleton')==anim.get_editor_property('skeleton'),
        'morphs_after_restart':len(mesh.get_editor_property('morph_targets')),'physics_asset':str(mesh.get_editor_property('physics_asset'))}
(root/'work/plugin_restart_check.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
