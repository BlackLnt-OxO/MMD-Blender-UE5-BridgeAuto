"""Native GPU measurements are qualified by source, hardware and execution context."""
import csv,math,statistics
from pathlib import Path
from .core import atomic_json,read_json
from .material_graph import signature,select_candidate


def calibration_key(gpu,engine,converter,graph,context):
    return signature(dict(gpu=gpu,engine=engine,converter=converter,graph=graph,context=context))


def gpu_csv(path,warmup=16,scene_only=False):
    """CPU FrameTime and CSV event columns never count as GPU timings."""
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.reader(stream))
    if not rows:raise ValueError('GPU CSV 为空')
    headers=rows[0]
    # UE LaunchEngineLoop records GPUTime from RHIGetGPUFrameCycles in ms.
    candidates=[i for i,h in enumerate(headers) if h in ('GPUTime','GPU/FrameTime','GPU/Total','GPU/GPUTime','GPU/Frame')]
    if not candidates:raise ValueError('CSV 缺少受支持的 GPU 时间列，不能用 CPU FrameTime 替代')
    column=candidates[0];values=[]
    scene_columns=[i for i,h in enumerate(headers) if h in
                   ('GPU/Basepass','GPU/NaniteBasePass','GPU/LumenScreenProbeGather')]
    if scene_only and not scene_columns:
        raise ValueError('CSV 缺少场景 GPU 通道，不能区分实际渲染与 MRQ 空闲帧')
    for row in rows[1:]:
        try:
            if scene_only and not any(float(row[i] or 0)>0 for i in scene_columns):continue
            value=float(row[column])
        except (ValueError,IndexError):continue
        if math.isfinite(value) and value>0:values.append(value)
    values=values[warmup:]
    if len(values)<16:raise ValueError('预热后有效 GPU 样本不足 16 帧')
    return dict(gpu_ms=statistics.median(values),gpu_p95_ms=sorted(values)[min(len(values)-1,int(len(values)*.95))],
                samples=len(values),column=headers[column],warmup=warmup,scene_only=scene_only,
                source=str(Path(path).resolve()))


def save_measurement(folder,identity,context,candidates,budget_mb):
    result=select_candidate(candidates,budget_mb=budget_mb)
    path=Path(folder)/(identity+'.json')
    record=dict(version=1,key=identity,context=context,candidates=candidates,decision=result)
    atomic_json(path,record);return record


def lookup(folder,identity,budget_mb):
    path=Path(folder)/(identity+'.json')
    if not path.is_file():return None
    record=read_json(path)
    if record.get('key')!=identity or record.get('version')!=1:return None
    return select_candidate(record['candidates'],budget_mb=budget_mb)
