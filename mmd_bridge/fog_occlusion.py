"""Opt-in fog shadow policy and measured acceptance; no surface material edits.

Certificates are shipped only after native Point/Spot and Masked fixtures pass.
An empty registry deliberately leaves the legacy renderer untouched.
"""
import math
from itertools import product

QUALITIES = ('legacy', 'detail', 'vsm_finer', 'vsm_nocache')
EXPERIMENTAL_PROFILES = {
    'vsm_finer': dict(cvars={'r.HeterogeneousVolumes.LightingCache':2,
                           'r.HeterogeneousVolumes.Scalability':3,
                           'r.Shadow.Virtual.Enable':1},
                     component_overrides={'StepFactor':.25,'LightingDownsampleFactor':.5}),
    'vsm_nocache': dict(cvars={'r.HeterogeneousVolumes.LightingCache':0,
                             'r.HeterogeneousVolumes.Scalability':3,
                             'r.Shadow.Virtual.Enable':1}, component_overrides={}),
}
CACHE_BUDGET_BYTES = 256 * 1024 * 1024
VALIDATED_PROFILES = {}  # Native validation receipts, never inferred from import success.


def _gpu_reasons(gpu, baseline):
    timing=gpu.get('gpu_ms',0)
    if (gpu.get('samples',0)<16 or gpu.get('output_frames',0)<16 or
            gpu.get('scope')!='producing_frames' or
            not isinstance(timing,(int,float)) or not math.isfinite(timing) or timing<=0):
        return ['gpu_unmeasured']
    if not isinstance(baseline,(int,float)) or not math.isfinite(baseline) or baseline<=0:
        return ['baseline_gpu_unmeasured']
    return ['gpu_over_25_percent'] if timing>baseline*1.25 else []


def quality(value='legacy'):
    if value not in QUALITIES:
        raise ValueError('未知细雾光遮挡方案: ' + str(value))
    return value


def accept_candidate(candidate, baseline_gpu_ms):
    """All mandatory cases and real GPU samples must pass, not averages of cases."""
    reasons=[]
    required={(fog,light,width) for fog in ('height','grid')
              for light in ('point','spot') for width in (2,5,10,'masked')}
    cases=candidate.get('cases',[])
    found={(c.get('fog'),c.get('light'),c.get('width_cm')) for c in cases}
    if not required.issubset(found):reasons.append('missing_fixture_cases')
    for c in cases:
        if not c.get('reference_valid') or not c.get('fog_only_roi'):
            reasons.append('invalid_reference_or_roi');continue
        ratio=c.get('contrast_ratio',0);edge=c.get('edge_error_px',float('inf'))
        if not isinstance(ratio,(int,float)) or not math.isfinite(ratio) or ratio<.6:reasons.append('contrast_below_60_percent')
        if not isinstance(edge,(int,float)) or not math.isfinite(edge) or edge>2:reasons.append('edge_error_over_2_pixels')
        if c.get('width_cm')==2 and not c.get('measurable_change'):reasons.append('no_2cm_change')
        # A fast Point result cannot certify Spot, another view, or another fog.
        reasons.extend(_gpu_reasons(c.get('gpu',{}),c.get('baseline_gpu_ms')))
    gpu=candidate.get('gpu',{})
    reasons.extend(_gpu_reasons(gpu,baseline_gpu_ms))
    cache=candidate.get('additional_cache_bytes')
    if cache is None or cache<0 or cache>CACHE_BUDGET_BYTES:reasons.append('cache_over_budget_or_unknown')
    if not candidate.get('manual_review_passed'):reasons.append('visual_review_pending')
    if candidate.get('density_or_material_changed'):reasons.append('protected_appearance_changed')
    return {'passed':not reasons,'reasons':sorted(set(reasons))}


def choose_candidate(candidates, baseline_gpu_ms):
    accepted=[c for c in candidates if accept_candidate(c,baseline_gpu_ms)['passed']]
    return min(accepted,key=lambda c:c['gpu']['gpu_ms']) if accepted else None


def decision(requested='legacy', pipeline='lumen', *, engine=None, volumes=()):
    quality(requested)
    result=dict(requested=requested,effective='legacy',status='legacy',cvars={},
                component_overrides={},reviews=[],parameters_changed=False)
    if requested=='legacy' or pipeline!='lumen':return result
    if requested in EXPERIMENTAL_PROFILES:
        if str(engine).split('-')[0]!='5.8.2':
            result.update(status='limited',reviews=['实验雾补充方案仅验证 UE 5.8.2，保留主方案'])
            return result
        eligible=[v for v in volumes if v.get('status')=='converted' and
                  v.get('kind') in ('grid','world_grid','local_grid','mesh_grid') and v.get('prim_path')]
        if not eligible:
            result.update(status='limited',reviews=['没有已转换的异质体积；补充方案不替换普通高度雾'])
            return result
        profile=EXPERIMENTAL_PROFILES[requested]
        result.update(effective=requested,status='experimental',cvars=dict(profile['cvars']),
                      component_overrides=dict(profile['component_overrides']),
                      volume_paths=[v['prim_path'] for v in eligible],
                      light_shadow_mode='vsm',visual_status='needs_review',
                      reviews=['实验性补充：不改变源雾密度、颜色、范围或表面材质；散射与动态画质仍需复核'])
        return result
    profile=VALIDATED_PROFILES.get(engine)
    if not profile:
        result.update(status='limited',reviews=['细雾光遮挡尚无通过验收的原生测试配置，保留原行为'])
        return result
    eligible=[]
    for volume in volumes:
        if volume.get('phase_approximation') or volume.get('field_approximation'):
            result['reviews'].append(str(volume.get('id',''))+': complex_volume_requires_review')
            continue
        eligible.append(volume)
    if eligible:
        result.update(effective='detail',status='experimental',cvars=dict(profile['cvars']),
                      component_overrides=dict(profile.get('component_overrides',{})))
    return result


def dense_cache_bytes(resolution, downsample=2., lod=1.):
    """Conservative RGBA16F inscattering + R16F transmittance dense textures."""
    if not math.isfinite(downsample) or downsample<=0 or not math.isfinite(lod) or lod<=0:
        raise ValueError('照明缓存采样倍率必须是有限正数')
    if len(resolution)!=3 or any(not math.isfinite(x) or x<=0 for x in resolution):
        raise ValueError('体积分辨率无效')
    dims=[min(limit,max(1,math.ceil(n/(downsample*lod))))
          for n,limit in zip(resolution,(1024,1024,512))]
    return math.prod(dims)*10


def froxel_eligibility(volume, meters_per_unit=1.):
    """Uniform coefficients may still have an arbitrary closed SVT shape mask."""
    result=dict(eligible=False,reason='source_uniformity_unproven')
    proof=volume.get('uniform_source_proof',{})
    if not isinstance(proof,dict) or proof.get('method')!='constant_expressions':return result
    if volume.get('time_policy','static_frame')!='static_frame':
        return dict(result,reason='animated_volume_not_validated')
    if (volume.get('status')!='converted' or not volume.get('path') or
        volume.get('coefficient_units')!='per_voxel'):
        return dict(result,reason='missing_valid_source_svt')
    if volume.get('phase_approximation') or volume.get('field_approximation'):
        return dict(result,reason='complex_phase_or_field')
    coefficients=proof.get('coefficients',{})
    try:
        numeric=[coefficients['extinction'],coefficients['anisotropy'],*coefficients['albedo'],*coefficients['emission']]
        if not all(math.isfinite(v) for v in numeric):
            return dict(result,reason='nonfinite_source_coefficients')
    except (KeyError,TypeError,ValueError):
        return dict(result,reason='invalid_uniform_source_proof')
    if volume.get('emission_path') or any(coefficients['emission']):
        return dict(result,reason='emission_not_validated')
    if abs(coefficients.get('anisotropy',1.))>1e-6:
        return dict(result,reason='non_isotropic_phase_not_validated')
    step=volume.get('voxel_size',0)*meters_per_unit
    if not math.isfinite(step) or step<=0:return dict(result,reason='invalid_coefficient_units')
    return dict(eligible=True,reason='uniform_static_svt_mask',voxel_metres=step,phase_g=0.)


def uniform_partition(bounds, focus_bounds, uniform_proof):
    """Disjoint boxes covering the original AABB; no overlapping fog density.

    Only a source constant proof and an axis-aligned box container permit this.
    Samples/cached maxima alone never prove uniformity or container shape.
    Native tiles must preserve the original coefficient units and source frame.
    """
    if (uniform_proof.get('method')!='constant_expressions' or
        uniform_proof.get('container')!='axis_aligned_box' or
        uniform_proof.get('mixed_phase') or uniform_proof.get('view_dependent')):
        raise ValueError('未证明是均匀密度的盒状体积，禁止局部细分')
    low,high=bounds;fl,fh=focus_bounds
    if any(not math.isfinite(x) for row in (low,high,fl,fh) for x in row):
        raise ValueError('体积边界必须有限')
    if any(a>=b for a,b in zip(low,high)) or any(a>=b for a,b in zip(fl,fh)):
        raise ValueError('体积边界无效')
    inner=[[max(a,c),min(b,d)] for a,b,c,d in zip(low,high,fl,fh)]
    if any(a>=b for a,b in inner):return [dict(bounds=bounds,detail=False)]
    cuts=[sorted(set((a,c,d,b))) for a,b,(c,d) in zip(low,high,inner)]
    boxes=[]
    for indices in product(*(range(len(c)-1) for c in cuts)):
        lo=[cuts[i][j] for i,j in enumerate(indices)]
        hi=[cuts[i][j+1] for i,j in enumerate(indices)]
        detail=all(c<=a and b<=d for a,b,(c,d) in zip(lo,hi,inner))
        boxes.append(dict(bounds=[lo,hi],detail=detail))
    return boxes


class TemporaryOverrides:
    """Compare-and-restore ownership. Manual changes relinquish managed control."""
    def __init__(self):self.values={};self.manual=set()

    def apply(self, desired, read, write):
        self.restore(read,write,keep=set(desired))
        for key,value in desired.items():
            current=read(key)
            if key in self.manual:continue
            old=self.values.get(key)
            if old and current!=old[1]:
                self.manual.add(key);self.values.pop(key);continue
            original=old[0] if old else current
            if current!=value:write(key,value)
            self.values[key]=(original,value)

    def restore(self, read, write, keep=frozenset()):
        for key,(original,applied) in list(self.values.items()):
            if key in keep:continue
            if read(key)==applied:write(key,original)
            else:self.manual.add(key)
            self.values.pop(key)

    def end(self, read, write):
        self.restore(read,write)
        self.manual.clear()
