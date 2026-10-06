from types import SimpleNamespace
import pytest
np = pytest.importorskip('numpy', reason='Mesh metrics run in the Blender Python runtime')
from mmd_bridge.blender_mesh_metrics import world_points, uv_spans, sheet_surface_evidence


class BulkCollection:
    def __init__(self, values):
        self.values = np.asarray(values, dtype=np.float32)

    def __len__(self):
        return len(self.values)

    def foreach_get(self, name, target):
        target[:] = self.values.reshape(-1)


def test_world_points_include_rotation_shear_mirror_and_translation():
    vertices = [[1, 2, 3], [-4, .5, 7], [0, -2, 0]]
    transform = np.array([[0, -2, .25, 6], [-1, 0, 0, -3], [0, .5, 3, 9], [0, 0, 0, 1]])
    mesh = SimpleNamespace(vertices=BulkCollection(vertices))
    actual = world_points(mesh, transform)
    expected = [(transform @ [*vertex, 1])[:3] for vertex in vertices]
    np.testing.assert_allclose(actual, expected)


@pytest.mark.parametrize('values,expected', [([[0, 1], [2, -3], [-1, .5]], [3, 4]),
    ([[.5, .5]], [0, 0]), ([], [1, 1])])
def test_uv_extents_keep_tiling_and_empty_layer_semantics(values, expected):
    assert uv_spans(SimpleNamespace(data=BulkCollection(values))) == expected
    assert uv_spans(None) == [1, 1]


def topology_mesh(faces, slots=None):
    class Polygons:
        def __len__(self): return len(faces)
        def foreach_get(self, name, target):
            values = {'loop_start': np.cumsum([0] + [len(f) for f in faces[:-1]]),
                      'loop_total': [len(f) for f in faces],
                      'material_index': slots or [0] * len(faces)}
            target[:] = values[name]
    class Loops:
        def __len__(self): return sum(map(len, faces))
        def foreach_get(self, name, target):
            target[:] = [v for face in faces for v in face]
    return SimpleNamespace(polygons=Polygons(), loops=Loops(),
                           vertices=range(1 + max((v for f in faces for v in f), default=-1)))


def test_open_cards_are_qualified_without_using_clump_bounds_or_names():
    mesh = topology_mesh([[0, 1, 2, 3], [4, 5, 6, 7]])
    result = sheet_surface_evidence(mesh, [0])
    assert result['thin_open_surface'] and result['open_components'] == 2
    assert not sheet_surface_evidence(mesh, [1])['thin_open_surface']


def test_material_subset_on_closed_solid_is_not_mistaken_for_open_sheet():
    cube = [[0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1],
            [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]]
    mesh = topology_mesh(cube + [[8, 9, 10, 11]], [0, 1, 1, 1, 1, 1, 2])
    assert not sheet_surface_evidence(mesh, [0])['thin_open_surface']
    assert not sheet_surface_evidence(mesh)['thin_open_surface']
    assert sheet_surface_evidence(mesh, [2])['thin_open_surface']


def test_nonmanifold_and_empty_surfaces_do_not_infer_thin_transmission():
    mesh = topology_mesh([[0, 1, 2], [1, 0, 3], [0, 1, 4]])
    result = sheet_surface_evidence(mesh)
    assert result['nonmanifold_components'] == 1 and not result['thin_open_surface']
    assert not sheet_surface_evidence(topology_mesh([]))['thin_open_surface']
