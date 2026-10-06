"""Export object/material reasons and actual native receipts for review."""
import sys
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.material_results import restoration_rows
OUT=ROOT/'work/material_restoration/full_scene'
folder=Path(read_json(OUT/'job_path.json')['job'])
manifest=read_json(folder/'manifest.json');report=read_json(folder/'ue_report.json')
rows=restoration_rows(manifest,report)
instances=Counter(item['name'] for item in manifest['scene']['instances'])
skips=[dict(item,generated_instance_count=instances.get(item['name'],0),
    classification='empty_host_with_generated_instances' if instances.get(item['name'],0) else 'needs_geometry_review')
    for item in manifest['scene'].get('skipped_items',[])]
source_probe={r['name']:r for r in read_json(OUT/'empty_host_source_probe.json')} if (OUT/'empty_host_source_probe.json').is_file() else {}
for item in skips:
    if item['name'] in source_probe:
        item['source_probe']=source_probe[item['name']]
        if not source_probe[item['name']]['final_polygons'] and not item['generated_instance_count']:
            item['classification']='source_has_no_surface_faces'
receipt={'job':str(folder),'source_sha256':file_hash(Path(manifest['scene']['source'])) if manifest['scene'].get('source') else None,
    'conversion_counts':dict(Counter(row['conversion_status'] for row in rows)),
    'ue_failures':report['failures'],'compile_errors':report['checks'].get('material_compile_errors'),
    'ocio':report['checks'].get('ocio'),'skipped_geometry':skips,'materials':rows,
    'material_visual_findings':report.get('checks',{}).get('material_visual_findings',{}),
    'visual_acceptance':'needs_review'}
atomic_json(OUT/'material_restoration_audit.json',receipt)
lines=['# ParkingLots 材质还原结果','',f'任务：`{folder}`','',
    f"材质记录 {len(rows)}；UE 失败 {len(report['failures'])}；状态 {receipt['conversion_counts']}。",'',
    '视觉验收仍需复核；转换成功、占位导入与视觉一致性分别统计。','',
    '| 物体 | 材质 | 策略 | 转换结果 | 视觉结果 | 原因与限制 |','|---|---|---|---|---|---|']
def cell(value):return str(value).replace('|','\\|').replace('\n','；')
for row in rows:
    lines.append('| '+' | '.join(cell(v) for v in (', '.join(row['objects']),row['material'],row['strategy'],
        row['conversion_status'],row['visual_result'],'；'.join(row['reasons'])))+' |')
lines+=['','## 空几何宿主','', '| 宿主 | 已生成实例数量 | 判断 |','|---|---:|---|']
for item in skips:lines.append(f"| {cell(item['name'])} | {item['generated_instance_count']} | {item['classification']} |")
(OUT/'material_restoration_audit.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(receipt['conversion_counts'],'UE failures',len(report['failures']),flush=True)
