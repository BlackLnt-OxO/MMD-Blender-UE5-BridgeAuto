"""Archive measurements and receipts; never performs conversion or rendering."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--run',type=Path,required=True)
parser.add_argument('--revision',default='repair_scoped_constants')
args=parser.parse_args()
run=args.run
def read(path):return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def size(folder):return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()) if folder.exists() else None

folder=run/args.revision/'01_graffiti'
job=Path(read(folder/'job.json').get('settings',{}).get('scene','')).parent
job=Path(read(run/'driver_state.json')['scenes']['01_graffiti']['job'])
manifest=read(folder/'manifest.json');scene=manifest.get('scene',{})
report=read(folder/'ue_report.json')
materials=scene.get('materials',[])
events=[json.loads(line) for line in (run/'driver_events.jsonl').read_text(encoding='utf-8').splitlines()]
traces=[json.loads(line) for line in (run/'memory_trace.jsonl').read_text(encoding='utf-8').splitlines()]
started=read(run/'driver_state.json')['scenes']['01_graffiti']['started']
samples=[item for item in traces if item['time']>=started]
peaks={field:max((row.get('gpu',{}).get(field,0) or 0 for row in samples),default=0)
       for field in ('dedicated_used_mb','shared_used_mb')}
peaks['system_used_mb']=max((row.get('physical',{}).get('total_mb',0)-row.get('physical',{}).get('available_mb',0) for row in samples),default=0)
attempts=[]
starts=[event for event in events if event['kind']=='driver_started']
for position,event in enumerate(starts):
    end=starts[position+1]['time'] if position+1<len(starts) else float('inf')
    revision=event['revision'];location=run/revision/'01_graffiti'
    latest=not any(row['revision']==revision for row in starts[position+1:])
    old_scene=read(location/'manifest.json').get('scene',{}) if latest else {}
    old_report=read(location/'ue_report.json') if latest else {}
    phase_events=[row for row in events if event['time']<=row['time']<end and row['kind']=='phase']
    # Active phase durations exclude attention/wait-for-intervention periods.
    durations=collections.defaultdict(float)
    for current,next_row in zip(phase_events,phase_events[1:]):
        durations[current['phase']]+=next_row['time']-current['time']
    measured=[row for row in traces if event['time']<=row['time']<end]
    attempt={'revision':revision,'start':event['time'],'phase_seconds':dict(durations),
        'export_metrics':old_scene.get('metrics'),'ue_metrics':old_report.get('metrics'),
        'difference':read(location/'difference.json') if latest else {},
        'machine_peak_mb':{field:max((row.get('gpu',{}).get(field,0) or 0 for row in measured),default=0) for field in ('dedicated_used_mb','shared_used_mb')},
        'images_obtained':latest and (location/'ue.png').is_file(),
        'revision_evidence_latest':latest,
        'basic_pbr':[{'name':row['name'],'baked_channels':row.get('baked_channels',[]),**row['basic_pbr_bake']} for row in old_scene.get('materials',[]) if row.get('basic_pbr_bake')]}
    attempts.append(attempt)
sources=read(run/'initial_state/sources_verified.json') or read(run/'initial_state/sources.json')
source_rows={}
for key,item in sources.items():
    if not isinstance(item,dict) or not item.get('path'):continue
    path=Path(item['path']);current=digest(path)
    source_rows[key]={'path':str(path),'sha256':current,'unchanged':current==item.get('sha256')}
pictures={}
for path in sorted(run.rglob('*.png')):
    if path.name in ('blender.png','ue.png','comparison.png','difference.png'):
        pictures[path.relative_to(run).as_posix()]={'sha256':digest(path),'bytes':path.stat().st_size}
asset_root=Path('F:/Ue5_Project/TESTON1_1005_02/Content')/report.get('root','/Game/').removeprefix('/Game/')
referenced=report.get('assets',{}).get('source_packages',[])
referenced_files=[Path('F:/Ue5_Project/TESTON1_1005_02/Content')/(path.split('.')[0].removeprefix('/Game/')+suffix)
    for path in referenced if path.startswith('/Game/') for suffix in ('.uasset','.umap','.uexp','.ubulk')]
from PySide6.QtGui import QImage
reference=read(folder/'reference.json');render=read(folder/'ue_render.json')
blender=QImage(str(folder/'blender.png'));ue=QImage(str(folder/'ue.png'))
blender_resolution=[blender.width(),blender.height()]
ue_resolution=[ue.width(),ue.height()]
checks={'source_unchanged':all(row['unchanged'] for row in source_rows.values()),
    'blender_hash':digest(folder/'blender.png')==reference.get('image_sha256'),
    'ue_hash':digest(folder/'ue.png')==render.get('image_sha256'),
    'fixed_reference':digest(folder/'blender.png')==digest(run/'baseline/01_graffiti/blender.png'),
    'frame':reference.get('frame')==render.get('frame')==0,
    'camera':reference.get('camera')=='Camera',
    'resolution':blender_resolution==ue_resolution==reference.get('resolution')==render.get('resolution')==[2560,1080],
    'lumen':render.get('renderer',{}).get('mode')=='lumen' and render.get('renderer',{}).get('actual_passes')==['MoviePipelineDeferredPassBase'],
    'path_tracing_off':render.get('renderer',{}).get('ShowFlag.PathTracing')==0 and render.get('renderer',{}).get('path_tracing_force_off_observed') is True,
    'final_runtime':read(run/'final_implementation/runtime_code_validation.json').get('complete') is True,
    'reload':read(folder/'restart_comparison.json').get('complete') is True}
if not all(checks.values()):raise RuntimeError('Final evidence validation failed: '+str(checks))
stats={'source':source_rows,'export':scene.get('stats'),
       'evidence_checks':checks,'fixed_settings':read(folder/'job.json').get('settings',{}),
       'runtime_code_validation':read(run/'final_implementation/runtime_code_validation.json'),
       'baseline_difference':read(run/'baseline/01_graffiti/difference.json'),
       'material_routes':dict(collections.Counter(row.get('strategy') for row in materials)),
       'basic_pbr':[{'name':row['name'],'baked_channels':row.get('baked_channels',[]),**row['basic_pbr_bake']} for row in materials if row.get('basic_pbr_bake')],
       'material_errors':[{'name':row['name'],'error':row['material_error']} for row in materials if row.get('material_error')],
       'ue_failures':report.get('failures',[]),'skipped':report.get('skipped_items',[]),
       'ue_metrics':report.get('metrics',{}),'export_metrics':scene.get('metrics'),
       'machine_peak_mb':peaks,'attempts':attempts,
       'geometry_probe':read(ROOT/'work/scene_stage1_20261006_123614/cube_geometry_probe.json'),
       'restart':read(folder/'restart_comparison.json'), 'difference':read(folder/'difference.json'),
       'assets':report.get('assets',{}),'ue_asset_directory_bytes':size(asset_root),
       'referenced_package_bytes':sum(p.stat().st_size for p in set(referenced_files) if p.is_file()),
       'pictures':pictures,'render':read(folder/'ue_render.json')}
evidence=run/'regression'
evidence.mkdir(exist_ok=True)
for name in ('material_fallback_regression','basic_pbr_values_regression'):
    shutil.copy2(ROOT/'work'/name/'report.json',evidence/(name+'.json'))
shutil.copy2(ROOT/'work/scene_stage1_20261006_123614/pbr_constants_probe.json',evidence/'pbr_constants_probe.json')
shutil.copy2(ROOT/'work/scene_stage1_20261006_123614/cube_geometry_probe.json',evidence/'cube_geometry_probe.json')
(run/'stage1_measurements.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
index={'stage1':{'status':'needs_review','job':str(job),'revision':args.revision,
        'level':report['assets']['level'],'sequence':report['assets']['sequence'],
        'images':{'blender':str(folder/'blender.png'),'ue':str(folder/'ue.png'),'comparison':str(folder/'comparison.png')},
        'technical':{'export':'complete_with_material_limit','import':'complete','reload':'complete','render':'complete'},
        'remaining_material_errors':len(stats['material_errors']),'geometry_status':'needs_review'},
       'stage2':{'status':'awaiting_user_acceptance','job':'D:/Codex_Projects_RebuildAutoMMD/MMDBridge_Jobs/Scene_cherry_01','images':'not_taken_stage_not_authorized'},
       'stage3':{'status':'awaiting_user_acceptance','images':'not_taken_stage_not_authorized'}}
(run/'task_index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf-8')
def link(label,path):return f'[{label}]({str(path).replace(chr(92),"/")})'
def fmt(value):return f'{value:.2f}' if isinstance(value,(float,int)) else '未实测'
lines=['# 第一阶段：涂鸦墙验收与材质回退收紧',
       '', '结论：技术结果和视觉结果分开记录。当前视觉状态为 `needs_review`，等待用户验收；cherry 和 MyGO 未启动。',
       '', '## 实测结果', '',
       f'- 导出清单：{scene.get("stats")}',
       f'- 材质策略：{stats["material_routes"]}；完整 PBR 回退：'+', '.join(row['name'] for row in stats['basic_pbr']),
       f'- UE 导入失败 {len(stats["ue_failures"])} 项；空求值几何跳过 {len(stats["skipped"])} 项。',
       '- 重开资源验证：'+str(stats['restart']),
       '- 重开审计从 USD Prim 查询生成组件；旧版仅 Actor 组件的错误统计已保留为 *_actor_only.json，不能用作验收通过证据。',
       '- 两次重开均逐个加载 242 个引用资源包，全部通过；解析 501 个导出实例，当前引用 180 个不同网格。',
       '- 关卡：'+str(report.get('assets',{}).get('level')),
       '- 序列：'+str(report.get('assets',{}).get('sequence')),
       '- 固定 Blender 参考图：'+link('原图',folder/'blender.png'),
       '- UE 修复图：'+link('原图',folder/'ue.png'),
       '- 对照：'+link('对照图',folder/'comparison.png')+'；'+link('差分指标',folder/'difference.json'),
       '- 显示空间平均绝对差分由原基线 0.23389 降为本次 0.11733；95 分位从 0.49020 降为 0.27451。指标不能直接当作还原通过率。',
       '- 固定帧 0、Camera、2560×1080；原 Blender 图片复用并验证哈希，实际 UE Lumen Pass 及关闭路径追踪已验证。镜头焦距 50 mm、光圈 1.2、手动对焦约 578.51 cm；曝光补偿 0，场景／灯光／雾倍率为 1。',
       '- 三份源文件最终哈希均与本轮开始时一致。',
       '', '## 通用修复及回退边界', '',
       '1. 从有效输出递归解析节点组、嵌套输入、静音旁路与有效混合分支；不按名称、节点数量或未使用节点触发烘焙。',
       '2. 可靠节点保留。局部字段失败先尝试该字段或通道；完整 Shader 失败或实际完整 UE 材质编译失败才升级为基础 PBR。强制 nodes 禁止烘焙。',
       '3. 位移／法线问题保留局部诊断，不直接触发六通道回退。当前位移合入法线的通路不改变几何轮廓，相关差异仍需复核。',
       '4. 已证明常量的通道连接数值；兼容 UV、几何、材质面覆盖与对象依赖才复用。不同世界上下文的缓存资源路径也分离，避免覆盖旧贴图。',
       '5. 修复 TextureObject 输入 Tex 连接、Blender float3 UV 和 UE float2 运算边界；地板保留节点通路。',
       '6. 修复暂停断点无法在 GUI 选择、子进程完成与状态读取的竞态、重复 MRQ 预设未登记保护哈希；旧配置及图片保留。',
       '', '## 问题与剩余限制', '',
       '| 项目 | 根因／症状 | 结论 |', '|---|---|---|',
       '| 墙面 Cast_In_Situ_vehkejtew | 实际 Substrate 41/15 算子上限；首次 UE 灰／白面 | 增加开销后可改善；基础 PBR 保留实时光照，多层高光／法线只能近似还原 |',
       '| 地板 Material.004 | float3/float2 Multiply 编译错误 | 共用 UV 转换修复后保留节点；无需整套 PBR |',
       '| 叶片 Dead_Leaves_rbtpO | 旧判定把局部位移／不兼容字段扩大成六通道逐网格烘焙 | 已收紧；可靠节点和常量保留，必要局部字段仍可烘焙，接缝和远距覆盖率 needs_review |',
       '| MI_tfglaawra | 完整 Shader 的覆盖率／混合闭包转换失败 | 增加开销后可改善；静态 PBR 通道可保留，复杂闭包响应只能近似 |',
       '| 源 Cycles 闭包限制 | 原 Blender 参考图及原生 Normal 烘焙均出现 252 > 64 警告 | 源引擎已有约束；法线／分层高光 needs_review |',
       '| Cube 几何计数 | USD 3700 三角形、UE LOD0 3540；没有 Nanite，remove_degenerates=true；USD 恰有 160 个面积小于 1e-8 的三角形 | 推断是退化面清理；资源重开通过，数量差异单独 needs_review，未修改源几何或隐藏差异 |',
       '| 曝光／灯光及地面高光 | 单帧对照仍有标牌亮度、地面反射、阴影及采样噪声差异 | 只能近似还原；图片成功不代表视觉通过 |']
for row in stats['material_errors']:
    lines.append('| '+row['name']+' | '+row['error'].replace('|','/')+' | 当前通路无法可靠还原；保留灰色占位，属于仍出现白模／缺贴图类限制 |')
lines += ['', '跳过项仅对应求值后空几何，不以 Blender 原始物体数对应 UE Actor 数：']
lines += [f'- {row["name"]}: {row.get("reason")}' for row in stats['skipped']]
lines += ['', '## 耗时、内存与体积', '',
          '以下是实际回执与采样值。专用／共享显存和系统内存是全机采样，含其他应用；DXGI local/nonlocal 为原生 UE 回执。10 秒采样峰值可能低于瞬时峰值。',
          '- 当前 UE 导入指标：'+str(stats['ue_metrics']),
          '- 当前 Blender 材质重做及 USD 导出指标：'+str(stats['export_metrics']),
          '- 本轮全机采样峰值 MB：'+str(peaks),
          f'- 当前 UE 任务目录：{stats["ue_asset_directory_bytes"]} 字节，含保留的旧版本资产；当前引用包：{stats["referenced_package_bytes"]} 字节。历史转换时的磁盘峰值未实测。',
          '- 完整 PBR 实测：']
lines += [f'  - {row["name"]}: {fmt(row.get("seconds"))} 秒（原生通道提取及烘焙）；{row.get("resolution")}；实际贴图通道 {row.get("baked_channels")}；常量 {row.get("constant_channels")}' for row in stats['basic_pbr']]
lines += ['', '| 尝试 | Blender 导出秒 | UE 导入秒 | UE 渲染阶段秒 | 专用／共享显存峰值 MB | UE 图片 |', '|---|---:|---:|---:|---|---|']
for row in attempts:
    export=row.get('export_metrics') or {};ue=row.get('ue_metrics') or {};peak=row['machine_peak_mb']
    render=row['phase_seconds'].get('ue_render') if row['images_obtained'] else None
    lines.append(f'| {row["revision"]} | {fmt(export.get("elapsed_seconds"))} | {fmt(ue.get("elapsed_seconds"))} | {fmt(render)} | {peak["dedicated_used_mb"]}/{peak["shared_used_mb"]} | '+('已保留' if row['images_obtained'] else '未取得，见下文')+' |')
lines += ['', '- 渲染阶段秒包括调度等待及启动开销，不能等同纯 GPU 渲染时间。中止尝试及本轮开始前基线没有完整阶段耗时，不补写估计值。',
          '- 常量识别前墙面通道提取及 PBR 烘焙为 277.31 秒、六张贴图；本次 9.13 秒、三张贴图。几何冻结复用、贴图缓存及编辑器冷／热状态不同，因此不作为全新源文件首次转换速度的比较。',
          '- repair_direct_basic_pbr 因回退过宽停止，没有 UE 图片；repair_scoped_fallback 在完成导入及重开后遇到 MRQ 预设保护错误，没有新 UE 图片。错误及旧资产已保留，修复后本次补渲染成功。']
lines += ['- 固定 Blender 图复用首次回执，不把复用耗时当作新的 Blender 渲染耗时。渲染详情、图片哈希及 Lumen／关闭路径追踪证据见原始 ue_render.json。',
          '- 回退判定过宽的尝试已停止并保留；显存 90.4% 暂停证据、80% 恢复规则与编辑器正常重启记录见 repair_scoped_fallback/gpu_pause。',
          '', '## 回归与证据', '',
          '- 76 项针对性 Python/Qt 检查通过；匿名 Blender 节点组回归 7 个成功案例及严格模式／视角依赖拒绝案例通过。',
          '- 共用恢复回归覆盖运行时代码变化、匹配回执、停止子进程后重试、每个失败材质最多一次、严格 nodes 和关闭兜底；精确资源路径与材质 ID 防止名称／前缀碰撞造成误回退。',
          '- 图片转换版本、最终部署和运行时代码哈希分别归档。最终补渲染后进一步收紧了失败材质身份／路径匹配，补齐字符串／字典 USD 清单、零几何但合法灯光场景与子进程退出后调度；这些修复不改变已生成材质和几何，后续阶段使用更新后的共用流程。',
          '- 数值实测覆盖基础色、粗糙度、金属度、覆盖率、HDR 发光与默认法线；验证同图复用、UV 改变及世界依赖隔离。原始 JSON 见 regression。',
          '- 六张最终三场景基线中本阶段保留第一场景两张。第二、三阶段图片尚未取得，原因是按用户要求等待逐阶段验收。',
          '- baseline/01_graffiti 保留原始 Blender／UE／对照／差分；repair_nodes_and_fallback 保留第一次修复图；其他中止／失败尝试的缺图原因见其状态回执。',
          '', '## 自动步骤、人工介入与恢复', '',
          '- 自动：真实 Qt 按钮选择项目、安装桥接、重做材质、转换、参考渲染与对照；程序现有显存暂停恢复；编辑器原生保存、重开及资源核验。',
          '- 人工介入：收紧过宽策略、诊断实际程序错误、通过既有停止标记保留断点、正常重启任务自有编辑器及恢复调度阶段。未更改源 blend，未关闭其他应用。',
          '- 验收工具修复：替换漏计 USD 的 Actor-only 审计；最终运行时审计移除 GUI-only 模块依赖。错误回执均保留，修复后重跑对应核验。',
          '- 恢复：在 GUI 选择任务 '+str(job)+'，使用“恢复已有任务”继续已兼容断点；需要重做材质时使用“按当前策略重做材质”，先归档旧报告及图片并复用冻结几何。',
          '- 本阶段结束后停止，等待验收；后续场景基线注明所用程序版本。',
          '- 历史 driver_events 的 baseline_done 曾误用下一场景名称，属于调度完成标签问题，现已修复；实际 scenes 索引只包含 01_graffiti，未执行 cherry。原日志保留。',
          '- 通用性边界：转换器中没有场景／材质名称特判；匿名合成图与真实场景联合验证。第一场景通过不能推断所有场景等价还原，后续阶段使用同一流程检验新依赖与限制。',
          '', '完整数据：'+link('stage1_measurements.json',run/'stage1_measurements.json')+'；'+link('task_index.json',run/'task_index.json')]
(run/'STAGE1_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'report':str(run/'STAGE1_REPORT.md'),'pictures':len(pictures),'complete':bool(stats['render'].get('complete'))}))
