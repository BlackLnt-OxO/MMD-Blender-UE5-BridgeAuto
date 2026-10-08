import math
from mmd_bridge.core import Settings
from mmd_bridge.fog_occlusion import (decision,TemporaryOverrides,uniform_partition,
                                     dense_cache_bytes,accept_candidate,choose_candidate)


def test_default_and_unvalidated_option_never_change_legacy():
    assert Settings().fog_occlusion_quality=='legacy'
    assert decision()['cvars']=={}
    result=decision('detail',engine='5.8.2',volumes=[{'kind':'grid'}])
    assert result['effective']=='legacy' and result['status']=='limited'
    assert not result['component_overrides'] and not result['parameters_changed']
    assert decision('detail','path_tracing')['cvars']=={}


def test_temporary_values_restore_switch_and_respect_manual_changes():
    actual={'cache':2.,'shadow':0.};writes=[]
    def write(k,v):actual[k]=v;writes.append((k,v))
    owned=TemporaryOverrides()
    owned.apply({'cache':1.,'shadow':1.},actual.get,write)
    actual['shadow']=.5  # User override is retained, also during subsequent ticks.
    owned.apply({'shadow':1.},actual.get,write)
    assert actual=={'cache':2.,'shadow':.5}
    owned.apply({'shadow':1.},actual.get,write)
    assert actual['shadow']==.5
    owned.end(actual.get,write)
    assert actual=={'cache':2.,'shadow':.5}


def test_partition_exact_coverage_disjoint_and_uniform_only():
    proof=dict(method='constant_expressions',container='axis_aligned_box')
    boxes=uniform_partition([[0,0,0],[100,80,60]],[[20,30,10],[40,50,30]],proof)
    assert len(boxes)==27 and sum(x['detail'] for x in boxes)==1
    assert sum(math.prod(b-a for a,b in zip(*x['bounds'])) for x in boxes)==100*80*60
    for a in boxes:
        for b in boxes:
            if a is b:continue
            assert not all(max(al,bl)<min(ah,bh) for al,ah,bl,bh in
                           zip(*a['bounds'],*b['bounds']))
    import pytest
    with pytest.raises(ValueError):uniform_partition([[0]*3,[100]*3],[[0]*3,[10]*3],{})
    with pytest.raises(ValueError):uniform_partition([[0]*3,[100]*3],[[0]*3,[10]*3],dict(proof,mixed_phase=True))
    assert dense_cache_bytes([128]*3,.5)==256**3*10


def candidate(ms=10):
    return dict(cases=[dict(fog=fog,light=light,width_cm=width,reference_valid=True,fog_only_roi=True,
        contrast_ratio=.7,edge_error_px=1,measurable_change=True,baseline_gpu_ms=10,
        gpu=dict(gpu_ms=ms,samples=32,output_frames=48,scope='producing_frames'))
        for fog in ('height','grid') for light in ('point','spot') for width in (2,5,10,'masked')],
        gpu=dict(gpu_ms=ms,samples=32,output_frames=48,scope='producing_frames'),
        additional_cache_bytes=100,manual_review_passed=True)


def test_gate_rejects_missing_low_quality_unmeasured_and_over_budget():
    assert accept_candidate(candidate(),10)['passed']
    c=candidate();c['cases'][0]['contrast_ratio']=.59
    assert not accept_candidate(c,10)['passed']
    c=candidate();c['cases'].pop()
    assert not accept_candidate(c,10)['passed']
    c=candidate();c['gpu']['samples']=15
    assert not accept_candidate(c,10)['passed']
    c=candidate();c['additional_cache_bytes']=257*1024**2
    assert not accept_candidate(c,10)['passed']
    c=candidate();c['manual_review_passed']=False
    assert not accept_candidate(c,10)['passed']
    assert not accept_candidate(candidate(12.51),10)['passed']
    assert choose_candidate([candidate(12),candidate(10),candidate(13)],10)['gpu']['gpu_ms']==10
    c=candidate();c['cases'][0]['edge_error_px']=None
    assert not accept_candidate(c,10)['passed']
    c=candidate();c['cases'][-1]['gpu']['gpu_ms']=20
    assert not accept_candidate(c,10)['passed']
    c=candidate();del c['cases'][-1]['gpu']
    assert not accept_candidate(c,10)['passed']


def test_uniform_source_selection_is_semantic_and_rejects_unproved_complex_fog():
    from mmd_bridge.fog_occlusion import froxel_eligibility
    volume=dict(status='converted',path='volume.vdb',coefficient_units='per_voxel',voxel_size=.06,
        time_policy='static_frame',uniform_source_proof=dict(method='constant_expressions',
        coefficients=dict(extinction=.002,albedo=[1.,1.,1.],emission=[0.,0.,0.],anisotropy=0.)))
    assert froxel_eligibility(volume)['eligible']
    for name in ('Sphere','Unknown','任意场景物体'):
        assert froxel_eligibility(dict(volume,name=name))['eligible']
    for field in ('phase_approximation','field_approximation'):
        assert not froxel_eligibility(dict(volume,**{field:True}))['eligible']
    assert not froxel_eligibility(dict(volume,time_policy='animated'))['eligible']
    assert not froxel_eligibility(dict(volume,uniform_source_proof={}))['eligible']
    assert not froxel_eligibility(dict(volume,uniform_source_proof=None))['eligible']
    v=dict(volume,uniform_source_proof=dict(volume['uniform_source_proof']))
    v['uniform_source_proof']['coefficients']=dict(volume['uniform_source_proof']['coefficients'],anisotropy=.3)
    assert not froxel_eligibility(v)['eligible']


def test_metrics_detect_absent_or_shifted_beam_and_do_not_accept_blank_reference():
    from mmd_bridge.fog_occlusion_metrics import band_metrics
    profile=[.2 if 40<=x<60 else 0. for x in range(100)]
    result=band_metrics(profile,profile)
    assert result['contrast_ratio']==1 and result['edge_error_px']==0
    absent=band_metrics(profile,[0.]*100)
    assert absent['contrast_ratio']==0 and absent['edge_error_px'] is None
    shifted=[.2 if 44<=x<64 else 0. for x in range(100)]
    assert band_metrics(profile,shifted)['edge_error_px']==4
    assert not band_metrics([0.]*100,[0.]*100)['reference_valid']
