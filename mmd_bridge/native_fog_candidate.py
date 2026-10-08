"""Offline native fog experiment; not a production conversion option."""
import math

NATIVE_PROFILES = {
    'native_balanced': (8, 128),
    'native_quality': (4, 256),
    'native_detail': (2, 256),
}

def native_volume_plan(volumes, meters_per_unit=1.):
    """Preserve static SVT fields; report the native renderer's scalar/phase limits.

    No scene names, density tuning, source edits or geometry heuristics select it.
    The SVT continues to carry the local container mask and spatial variation.
    """
    grids=[];reviews=[];phases=[]
    for v in volumes:
        if v.get('status')!='converted':continue
        if v.get('kind')=='height_fog':
            phases.append(float(v.get('anisotropy',0.)));continue
        if not v.get('prim_path') or v.get('coefficient_units')!='per_voxel':
            raise ValueError('原生体积雾缺少源网格坐标或系数单位: '+str(v.get('name')))
        if v.get('time_policy','static_frame')!='static_frame':
            raise ValueError('原生体积雾尚未支持动态 SVT: '+str(v.get('name')))
        step=v.get('voxel_size',0)*meters_per_unit
        if not math.isfinite(step) or step<=0:raise ValueError('原生体积雾体素单位无效')
        # A zero sampled field is not a new global fog layer.
        stats=v.get('sample_statistics') or {}
        if stats.get('sampled_voxels',0)>0 and stats.get('occupied_voxels')==0:continue
        proof=v.get('uniform_source_proof') or {}
        coeff=proof.get('coefficients') or {}
        bounds=stats.get('phase_range')
        phase=coeff.get('anisotropy')
        if phase is None and bounds and abs(bounds[0]-bounds[1])<1e-6:phase=bounds[0]
        if phase is None:
            phase=0.
            reviews.append(str(v.get('name'))+': 未证明相函数恒定，原生雾使用各向同性近似')
        phases.append(float(phase))
        grids.append(dict(id=v['id'],prim_path=v['prim_path'],voxel_metres=step,
                          emission=bool(v.get('emission_path'))))
        if not coeff:
            reviews.append(str(v.get('name'))+': 原生雾将 RGB 消光取均值，彩色吸收及细密结构需复核')
    phase=phases[0] if phases else 0.
    if any(abs(p-phase)>1e-6 for p in phases):
        reviews.append('原生体积雾共享一个相函数；多个不同相函数使用各向同性近似')
        phase=0.
    if abs(phase)>.9:
        reviews.append('原生体积雾相函数限制在 -0.9 到 0.9')
        phase=max(-.9,min(.9,phase))
    return dict(grids=grids,phase_g=phase,reviews=reviews,visual_status='needs_review')


