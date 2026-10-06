"""Record observed mismatches separately from technical material conversion."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
OUT=ROOT/'work/material_restoration/full_scene'
job=Path(read_json(OUT/'job_path.json')['job']);manifest=read_json(job/'manifest.json');report=read_json(job/'ue_report.json')
render=read_json(OUT/'render_result.json')
assert not render.get('material_overrides'),'diagnostic renders cannot serve as final scene evidence'
standees={'tomori','anno','neko','riki','soyo','Material.008','Material.003','Material.010','Material.009','Material.007'}
umbrellas={'uwb_pinkt','uwb_violett','uwb_yellowt','uwb_greent','umb_wt'}
findings={}
source_sha256=file_hash(Path(manifest['scene']['source']))
for record in manifest['scene']['materials']:
    if record['name'] in standees:
        reasons=['原生 Lumen 源相机对照：立牌偏白，尚未可靠还原；颜色/Alpha 贴图绑定有效，正面分支诊断未消除问题，单闭包诊断显示人物。透射混合响应需要继续修复。']
    elif record['name'] in umbrellas:
        reasons=['原生 Lumen 源相机对照：雨伞的彩色透射与可见度低于 Blender 源参考，尚未通过视觉验收。']
    else:continue
    findings[record['id']]=dict(result='appearance_mismatch',reasons=reasons,
        source_sha256=source_sha256,
        source_reference=str(OUT/'blender_reference_job/visual/blender.png'),
        render=render['preview'])
report.setdefault('checks',{})['material_visual_findings']=findings
report['checks']['native_render_validation']={key:render.get(key) for key in (
    'complete','preview','resolution_match','material_runtime_failures','visual_status','gpu_peak')}
atomic_json(job/'ue_report.json',report)
print('VISUAL_FINDINGS',len(findings),flush=True)
