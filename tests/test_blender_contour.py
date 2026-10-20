from mmd_bridge.blender_contour import smoothing_mask, smooth_coordinates, prepare_contour, sharp_protection
import pytest


def grid(size):
    points=[(float(x),float(y),1. if x==size//2 and y==size//2 else 0.) for y in range(size) for x in range(size)]
    faces=[(1,(y*size+x,y*size+x+1,(y+1)*size+x+1,(y+1)*size+x)) for y in range(size-1) for x in range(size-1)]
    edges=sorted({tuple(sorted((a,b))) for _,face in faces for a,b in zip(face,face[1:]+face[:1])})
    return points,faces,edges


def test_two_border_rings_and_unselected_material_stay_fixed():
    points,faces,edges=grid(9)
    mask=smoothing_mask(len(points),faces,{1})
    assert mask[4*9+4] and not mask[2*9+4] and not any(smoothing_mask(len(points),faces,{2}))
    result=smooth_coordinates(points,edges,mask)
    assert 0<result[40][2]<1
    assert all(result[i]==points[i] for i,enabled in enumerate(mask) if not enabled)
    assert len(result)==len(points)


def test_same_linear_smoothing_mapping_preserves_relative_key_deltas():
    points,faces,edges=grid(9);mask=smoothing_mask(len(points),faces,{1})
    shifted=[(x,y,z+.2) for x,y,z in points]
    a=smooth_coordinates(points,edges,mask);b=smooth_coordinates(shifted,edges,mask)
    assert all(b[i][2]-a[i][2]==pytest.approx(.2) for i in range(len(a)))


def test_default_is_compatible_and_experiments_require_explicit_targets():
    meshes=[object()]
    assert prepare_contour(meshes)==(meshes,{'mode':'none'})
    with pytest.raises(ValueError):prepare_contour(meshes,'smooth')
    with pytest.raises(ValueError):prepare_contour(meshes,'unknown',{1})


def test_protection_provenance_and_soft_surfaces():
    points=[(0,0,0),(1,0,0),(0,1,0),(0,0,1)]
    faces=[(0,1,2),(0,3,1)]
    soft=sharp_protection(points,faces,soft_surface=True)
    hard=sharp_protection(points,faces)
    assert not soft['sources']['dihedral']['edges']
    assert hard['sources']['dihedral']['edges']==[[0,1]]
    assert not hard['sources']['authored_crease']['edges']
    marked=sharp_protection(points,faces,soft_surface=True,sharp_edges=[(0,1)],edge_creases=[(1,2,.5)])
    assert marked['sources']['authored_sharp']['edges']==[[0,1]]
    assert marked['sources']['authored_crease']['edges']==[[1,2]]


def test_closed_tip_and_degenerate_exclusion():
    points=[(0,0,3),(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)]
    faces=[(0,1,2),(0,2,3),(0,3,4),(0,4,1),(4,3,2,1)]
    result=sharp_protection(points,faces,soft_surface=True)
    assert result['sources']['closed_tip']['vertices']==[0]
    points[2]=points[1]
    result=sharp_protection(points,faces,soft_surface=True)
    assert result['issues']['degenerate_faces']
    assert 0 not in result['sources']['closed_tip']['vertices']


def test_full_authored_crease_preserves_its_endpoints():
    points=[(0,0,3),(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)]
    faces=[(0,1,2),(0,2,3),(0,3,4),(0,4,1),(4,3,2,1)]
    result=sharp_protection(points,faces,soft_surface=True,edge_creases=[(1,2,1.)])
    assert {1,2}<=set(result['full_vertices'])
    assert result['sources']['authored_crease']['edges']==[[1,2]]


def test_legacy_shape_action_without_action_slots_is_preserved():
    from types import SimpleNamespace as N
    from mmd_bridge.blender_contour import _copy_animation
    action=object();new=N()
    source=N(animation_data=N(action=action,nla_tracks=[],drivers=[]))
    target=N(animation_data_create=lambda:new)
    _copy_animation(source,target,{})
    assert new.action is action
