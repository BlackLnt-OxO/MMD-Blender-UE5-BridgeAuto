import math
from mmd_bridge.blender_hair_normals import smooth_fans,coherent_field,interpolated_field


def test_fan_never_switches_only_one_missing_corner():
    fans=smooth_fans([(0,1,2),(2,1,3)],[True,True])
    reference={i:(0,0,1) for i in range(6)}
    proxy={i:(.5,0,math.sqrt(.75)) for i in range(6)};proxy.pop(1)
    result,audit=coherent_field(fans,proxy,reference,[1]*6)
    assert result[1]==result[4]==(0.,0.,1.)
    assert audit['whole_fan_fallbacks'] and not audit['angular_threshold_used_for_selection']


def test_sharp_flat_double_sided_and_nonmanifold_domains_are_separate():
    assert len(smooth_fans([(0,1,2),(2,1,3)],[True,True],[(1,2)]))==6
    assert len(smooth_fans([(0,1,2),(2,1,3)],[True,False]))==6
    assert len(smooth_fans([(0,1,2),(2,1,0)],[True,True]))==6
    assert len(smooth_fans([(0,1,2),(2,1,3),(1,2,4)],[True]*3))==9


def test_disconnected_close_sheets_do_not_share_fans():
    fans=smooth_fans([(0,1,2),(3,4,5)],[True,True])
    assert len(fans)==6
    ref={i:((0,0,1) if i<3 else (0,0,-1)) for i in range(6)}
    result,_=coherent_field(fans,ref,ref,[1]*6)
    assert all(result[i][2]==(1 if i<3 else -1) for i in range(6))


def test_large_proxy_angle_is_audit_only_and_fan_is_coherent():
    ref={0:(0,0,1),1:(0,0,1)}
    proxy={0:(1,0,0),1:(.99,.1,0)}
    result,audit=coherent_field([[0,1]],proxy,ref,[1,2])
    assert result[0]==result[1] and audit['angular_15deg_audit_corners']==2


def test_cage_field_preserves_interpolation_and_invalid_fan_falls_back_together():
    field=[(1,0,1),(0,1,1),(0,0,0),(1,0,0)]
    result,audit=interpolated_field([[0,1],[2,3]],field,[(0,0,1)]*4)
    assert result[0]!=result[1] and result[2]==result[3]==(0.,0.,1.)
    assert audit['whole_fan_fallbacks']==[1]
