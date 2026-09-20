from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from mmd_bridge.core import read_json,atomic_json

summary={'date':'2026-09-18','jobs':{}}
for name,pointer in (('character','full_job.txt'),('scene','final_scene_job.txt'),('combined','combined_job.txt'),('projection_switch','ortho_job.txt')):
    folder=Path((root/'work'/pointer).read_text().strip())
    status=read_json(folder/'status.json')
    result={'task':folder.name,'state':status['state']}
    if (folder/'ue_report.json').exists():
        report=read_json(folder/'ue_report.json')
        result.update(complete=report['complete'],engine=report['engine_version'],assets=report['assets'].get('level'),
                      checks={k:v for k,v in report['checks'].items() if k not in ('morph_names','curve_names','material_slots')},
                      material_slot_count=len(report['checks'].get('material_slots',[])),warnings=len(report['warnings']))
    summary['jobs'][name]=result
for name,file in (('dialog','dialog_test.json'),('scale','scale_result.json'),('restart','plugin_restart_check.json')):
    summary[name]=read_json(root/'work'/file)
for name,file in (('native_default','ui-export-result.json'),('native_manual','ui-manual-result.json')):
    data=read_json(root/'work'/file)
    summary[name]={'finished':data.get('finished',False),'validated_files':list(data.get('files',{}))}
scene=Path((root/'work/final_scene_job.txt').read_text().strip())
summary['water']=read_json(scene/'water_check.json')
atomic_json(root/'docs/validation-summary.json',summary)
print({name:result['state'] for name,result in summary['jobs'].items()})
