"""Offline native fog planner checks."""

def test_native_volume_plan_preserves_fields_and_reports_phase_limits():
    import copy,pytest
    from mmd_bridge.native_fog_candidate import native_volume_plan,NATIVE_PROFILES
    v=dict(id='v',name='local',status='converted',kind='local_grid',prim_path='/World/V',
        coefficient_units='per_voxel',voxel_size=.2,
        uniform_source_proof=dict(method='constant_expressions',coefficients=dict(anisotropy=.5)))
    before=copy.deepcopy(v)
    plan=native_volume_plan([v],2.)
    assert plan['phase_g']==.5 and plan['grids'][0]['voxel_metres']==.4 and v==before
    zero=dict(v,sample_statistics=dict(sampled_voxels=5,occupied_voxels=0))
    assert not native_volume_plan([zero])['grids']
    mixed=native_volume_plan([v,dict(v,id='v2',uniform_source_proof=None)])
    assert mixed['phase_g']==0 and mixed['reviews']
    with pytest.raises(ValueError):native_volume_plan([dict(v,time_policy='animated')])
    assert set(NATIVE_PROFILES)=={'native_balanced','native_quality','native_detail'}
