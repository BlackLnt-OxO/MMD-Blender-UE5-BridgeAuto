from types import SimpleNamespace
try:
    import numpy as np
except ModuleNotFoundError:
    import pytest
    pytest.skip('Geometry checks run in Blender bundled NumPy', allow_module_level=True)
from mmd_bridge.blender_mesh_metrics import pane_surface_evidence


class Values:
    def __init__(self, rows): self.rows = rows
    def __len__(self): return len(self.rows)
    def foreach_get(self, name, output): output[:] = np.asarray([r[name] for r in self.rows]).reshape(-1)


def mesh(points, faces, slots=None):
    loops, polygons = [], []
    for index, face in enumerate(faces):
        polygons.append({'loop_start':len(loops),'loop_total':len(face), 'material_index':slots[index] if slots else 0})
        loops += [{'vertex_index':v} for v in face]
    return SimpleNamespace(vertices=Values([{'co':p} for p in points]),loops=Values(loops),polygons=Values(polygons))


def test_rotated_pane_is_identified_from_material_faces_excluding_other_slots():
    value = mesh([[-2,-1,0],[2,-1,0],[2,1,0],[-2,1,0],[0,0,10]], [(0,1,2,3),(0,1,4)], [0,1])
    angle = np.pi/4
    matrix = np.array([[np.cos(angle),0,np.sin(angle),0],[0,1,0,0],[-np.sin(angle),0,np.cos(angle),0],[0,0,0,1]])
    assert pane_surface_evidence(value,matrix,[0])['planar_pane']
    assert not pane_surface_evidence(value,matrix,[0,1])['planar_pane']


def test_folded_geometry_does_not_become_a_flat_pane():
    value = mesh([[-2,-1,0],[0,-1,0],[0,1,0],[-2,1,0],[0,-1,2],[0,1,2]],[(0,1,2,3),(1,4,5,2)])
    assert not pane_surface_evidence(value,np.eye(4),[0])['planar_pane']
