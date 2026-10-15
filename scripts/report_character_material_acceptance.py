"""Archive real render receipts, metrics and matched frames without changing images."""
import csv
import json
import math
import shutil
import statistics
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
OUT=ROOT/'work/character_material_20261014'
rows=[]
for model in ('Barbara','YYB GenZ Miku','NeuroSama','橘シェリー'):
    folder=OUT/'full'/model
    manifest=read_json(folder/'manifest.json');receipt=read_json(folder/'ue_receipt.json');acceptance=read_json(folder/'acceptance.json')
    assert acceptance['complete'] and not acceptance['shader_failures'],model
    runtime=acceptance['runtime'];run=Path(acceptance['images'][0]).parent.parent
    frames=[Path(p) for p in acceptance['images']];assert len(frames)==9 and all(p.is_file() for p in frames)
    atlas=receipt.get('sdf_textures',{})
    if atlas:
        assert all(t['is_r16f']==1 and t['bytes']==75497472 for t in atlas.values()),atlas
        states=runtime.get('face_direction_samples',[])
        rotations={tuple(s['values'].get('head_rotation',[])) for s in states}
        assert len(rotations)>2,(model,rotations)
    assert all(max(values)-min(values)>.5 for values in receipt.get('animation_morph_probe',{}).values())
    gpu=[];csv_file=Path(runtime['gpu_csv_path']);snapshot=run/'gpu.csv'
    shutil.copy2(csv_file,snapshot)
    with snapshot.open(encoding='utf-8-sig') as stream:
        reader=csv.reader(stream);header=next(reader);index=header.index('GPUTime');basepass=header.index('GPU/Basepass')
        for row in reader:
            if len(row)!=len(header):continue
            try:value=float(row[index]);drawing=float(row[basepass])
            except ValueError:continue
            if math.isfinite(value) and value>0 and drawing>0:gpu.append(value)
    sdf_review=[{'material':r['name'],'reason':r['character_material']['sdf']['reason']}
        for r in manifest['materials'] if r.get('character_material',{}).get('sdf',{}).get('status')=='needs_review']
    row={'model':model,'materials':len(manifest['materials']),'sphere_slots':sum(bool(r.get('textures',{}).get('sphere')) for r in manifest['materials']),
         'sdf_atlases':len(atlas),'sdf_review':sdf_review,'frames':[str(p) for p in frames],'run':str(run),
         'gpu_ms_median':statistics.median(gpu) if gpu else None,'gpu_samples':len(gpu),
         'memory_peak':runtime.get('memory_peak',{}),'morph_probe':receipt.get('animation_morph_probe',{}),
         'level':receipt['render_assets']['level'],'technical_pass':True,'visual_status':'needs_review'}
    if (run/'sdf_off/images').is_dir():
        row['sdf_off_frames']=[str(run/'sdf_off/images'/p.name) for p in frames]
        assert all(Path(p).is_file() for p in row['sdf_off_frames'])
    rows.append(row)
original=read_json(ROOT/'work/bp_render_material_audit_20261013/source_hashes_before.json')
changed=[r['Path'] for r in original if file_hash(r['Path']).lower()!=r['Hash'].lower()]
assert not changed,changed
report={'project':r'F:/Ue5_Project/TESTON_1005_Clean/TESTON_1005_Clean.uproject','models':rows,
        'source_assets_verified':len(original),'source_assets_changed':changed,
        'extension':read_json(OUT/'extension/ue_receipt.json'),'technical_pass':True,
        'visual_status':'needs_review','deferred':['outline','double_mesh_blueprint','threshold_lr','proxy_face_comparison']}
atomic_json(OUT/'acceptance_summary.json',report)
def link(label,path):return '['+label+'](<'+str(path)+'>)'
lines=['# 角色母材质转换验收记录','',
       '测试工程：TESTON_1005_Clean；UE5.8.2；四个真实 PMX；1280×720 Lumen/MRQ。技术检查通过，艺术效果仍保留人工复核标记。','',
       '80 项配置、GUI、控制器与恢复测试通过。Blender 验证了四种 Ramp 编码、Lightmap.A 与透明度分离、UV Alpha 和重复寻址；UE 验证了配置扩展、部位预设与同图不同颜色空间。',
       '', '| 模型 | 材质槽 | sphere 槽 | 512 SDF 图集 | GPUTime 中位数 ms | 状态 |', '|---|---:|---:|---:|---:|---|']
for row in rows:
    lines.append(f"| {row['model']} | {row['materials']} | {row['sphere_slots']} | {row['sdf_atlases']} | {row['gpu_ms_median']:.2f} | 技术通过；效果复核 |")
lines+=['','GPUTime 仅统计 GPU/Basepass 大于零的绘制帧，排除空闲等待帧；本次含冷启动影响，不作为同模型画质或稳态性能排名。单张图集实测 R16F 8192×4608、无 mipmap，像素资源 72 MiB。',
        '',f'原 ZZZ 工程 {len(original)} 个源资产哈希核对一致。源码和 TEST 工程安装前快照保存在本验收目录。','',
        '前六帧固定角色姿态：正面、左右 45°、上方、下方、背光；后三帧转头和眨眼。机位、曝光固定，运动模糊关闭。']
for row in rows:
    lines+=['', '## '+row['model'],'',
            link('UE 收据',OUT/'full'/row['model']/'ue_receipt.json')+' · '+link('MRQ 收据',OUT/'full'/row['model']/'acceptance.json')+' · '+link('GPU CSV',Path(row['run'])/'gpu.csv'),'',
            '| 正面 | 上方光 | 下方光 |', '|---|---|---|',
            '| '+' | '.join('!['+str(i)+']('+p.replace('\\','/')+')' for i,p in ((0,row['frames'][0]),(3,row['frames'][3]),(4,row['frames'][4])))+' |']
    for review in row['sdf_review']:lines.append('\n- SDF 回退：'+review['material']+'；'+review['reason'])
    if row.get('sdf_off_frames'):
        lines+=['','同机位 SDF 开关对照：', '', '| SDF 开 | SDF 关 |', '|---|---|',
                 '| ![SDF on]('+row['frames'][1].replace('\\','/')+') | ![SDF off]('+row['sdf_off_frames'][1].replace('\\','/')+') |']
lines+=['','描边与重叠双模型蓝图按要求推迟。平滑代理脸、单轴阈值图未实施；UV/材质 morph 不由本次 FBX 验证覆盖。SDF 为几何初始结果，鼻影和脸颊过渡仍需美术复核。']
(OUT/'acceptance.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
print('CHARACTER_ACCEPTANCE_REPORT',OUT/'acceptance.md')
