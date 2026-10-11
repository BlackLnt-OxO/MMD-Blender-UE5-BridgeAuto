import itertools
import math
from mmd_bridge.material_policy import pane_point_evidence


def box(size, angle=0):
    c, s = math.cos(angle), math.sin(angle)
    return [(x*c-z*s+10000, y-40000, x*s+z*c+30000)
            for x,y,z in itertools.product(*[(-v/2,v/2) for v in size])]


def test_rotated_translated_thin_solid_is_fitted_in_its_own_axes():
    for angle in (0,.4,1.2):
        evidence = pane_point_evidence(box((4,2,.014), angle))
        assert evidence['planar_pane']
        assert math.isclose(evidence['fitted_thickness_ratio'], .007, abs_tol=1e-7)


def test_folded_thick_and_unreadable_geometry_are_not_thin_panes():
    assert not pane_point_evidence(box((2,2,2), .4))['planar_pane']
    assert not pane_point_evidence([(0,0,0),(2,0,0),(2,2,0),(2,2,2),(0,2,2)])['planar_pane']
    for points in ([], [(0,0,0)]*3, [(float('nan'),0,0)]*3):
        assert not pane_point_evidence(points)['planar_pane']
