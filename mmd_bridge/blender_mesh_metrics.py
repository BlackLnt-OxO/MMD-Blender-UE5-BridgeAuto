"""Bulk mesh reads shared by material analysis and bake preparation."""
import numpy as np


def world_points(mesh, matrix):
    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', coordinates)
    transform = np.asarray(matrix, dtype=np.float32)
    points = coordinates.reshape(-1, 3) @ transform[:3, :3].T
    points += transform[:3, 3]
    return points


def uv_spans(layer):
    if not layer or not len(layer.data):
        return [1, 1]
    coordinates = np.empty(len(layer.data) * 2, dtype=np.float32)
    layer.data.foreach_get('uv', coordinates)
    coordinates = coordinates.reshape(-1, 2)
    # RNA floats are converted to Python doubles by the old min/max loop.
    # Subtract in float64 to preserve identical policy and bake-cache identities.
    return (coordinates.max(axis=0).astype(np.float64) -
            coordinates.min(axis=0).astype(np.float64)).tolist()


def pane_surface_evidence(mesh, matrix, material_indices):
    """Fit material geometry in world space, independent of axis-aligned bounds."""
    slots = np.empty(len(mesh.polygons), dtype=np.int32)
    starts = np.empty(len(mesh.polygons), dtype=np.int32)
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get('material_index', slots)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    loops = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get('vertex_index', loops)
    selected = np.flatnonzero(np.isin(slots, list(material_indices)))
    result = {'version':1, 'planar_pane':False}
    if not len(selected): return result
    indices = np.unique(np.concatenate([loops[starts[i]:starts[i]+totals[i]] for i in selected]))
    points = world_points(mesh, matrix)[indices].astype(np.float64)
    if len(points) < 3 or not np.isfinite(points).all(): return result
    centered = points-points.mean(axis=0)
    _, _, axes = np.linalg.svd(centered, full_matrices=False)
    extent = np.ptp(centered@axes.T, axis=0)
    if extent[1] <= 1e-9: return result
    ratio = float(extent[2]/extent[1])
    result.update(planar_pane=ratio <= .02, fitted_thickness_ratio=ratio,
                  fitted_size=[float(v) for v in extent])
    return result


def sheet_surface_evidence(mesh, material_indices=None):
    """Inspect whole connected surfaces, then select the material's components.

    A collection of open cards can have volumetric bounds. Conversely, one
    material on a closed solid must not appear open just because its other faces
    use another material. Non-manifold components remain unqualified.
    """
    count = len(mesh.polygons)
    result = dict(version=1, faces=0, components=0, open_components=0,
                  closed_components=0, nonmanifold_components=0, thin_open_surface=False)
    if not count:
        return result
    starts = np.empty(count, dtype=np.int32)
    totals = np.empty(count, dtype=np.int32)
    slots = np.empty(count, dtype=np.int32)
    vertices = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    mesh.polygons.foreach_get('material_index', slots)
    mesh.loops.foreach_get('vertex_index', vertices)
    selected = np.ones(count, dtype=bool) if material_indices is None else np.isin(slots, list(material_indices))
    result['faces'] = int(selected.sum())
    if not result['faces'] or np.any(totals < 3):
        return result
    following = np.arange(len(vertices), dtype=np.int32) + 1
    following[starts + totals - 1] = starts
    edges = np.sort(np.column_stack((vertices, vertices[following])), axis=1)
    edges, uses = np.unique(edges, axis=0, return_counts=True)
    parent = np.arange(len(mesh.vertices), dtype=np.int32)

    def root(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return int(value)

    for a, b in edges:
        ra, rb = root(int(a)), root(int(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    targets = {root(int(v)) for v in vertices[starts[selected]]}
    boundaries = {root(int(v)) for v in edges[uses == 1, 0]}
    nonmanifold = {root(int(v)) for v in edges[uses > 2, 0]}
    degenerate = {root(int(a)) for a, b in edges if a == b}
    invalid = nonmanifold | degenerate
    result.update(components=len(targets), open_components=len(targets & boundaries),
                  closed_components=len(targets - boundaries),
                  nonmanifold_components=len(targets & invalid),
                  thin_open_surface=bool(targets and targets <= boundaries and not targets & invalid))
    return result
