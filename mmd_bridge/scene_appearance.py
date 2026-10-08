"""Portable scene appearance units. No renderer or network dependency."""
import math
from functools import lru_cache
from pathlib import Path

from .core import read_json

RADIANCE_TO_UE = 683.


@lru_cache(maxsize=16)
def _scene_package(path, stamp):
    return read_json(Path(path))


def cached_scene_package(path):
    """Avoid reparsing large managed packages on every editor tick.

    Callers treat the cached record as read-only. Replacements and edits invalidate
    it; the cache stays bounded when the user opens several converted scenes.
    """
    path = Path(path).resolve()
    stat = path.stat()
    return _scene_package(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))


def volume_sampling_review(volume, meters_per_unit, pipeline, lighting_downsample=None):
    """Describe effective sampling without changing a validated grid or renderer.

    A density voxel / lighting-cache cell is not the surface shadow-map resolution.
    Retain this distinction in receipts instead of claiming occlusion equivalence.
    """
    voxel = volume.get('voxel_size')
    voxel_m = float(voxel) * meters_per_unit if voxel is not None else None
    stats = volume.get('sample_statistics') or {}
    reasons = []
    if volume.get('phase_approximation'):
        reasons.append('multiple_phase_functions_approximated')
    phase = stats.get('phase_range')
    if pipeline == 'lumen' and volume.get('kind') != 'height_fog' and phase and any(abs(g) > 1e-6 for g in phase):
        reasons.append('realtime_grid_phase_requires_review')
    if volume.get('field_approximation'):
        reasons.append('reference_frame_field_approximation')
    lighting_cell = (voxel_m * lighting_downsample
        if voxel_m is not None and lighting_downsample is not None else None)
    return dict(voxel_size_m=voxel_m, lighting_cell_estimate_m=lighting_cell,
        sample_statistics=stats or None, sampling_policy='preserve_existing',
        automatically_changed=False, reasons=reasons, visual_status='needs_review')


def fog_shadow_review(pipeline, volumes, lights, cvars):
    """Flag a possible shadow-path mismatch; settings alone do not prove a leak."""
    grid_fog = any(v.get('kind') != 'height_fog' and v.get('status') == 'converted' and
        (not v.get('sample_statistics') or v['sample_statistics'].get('phase_range') is not None)
        for v in volumes)
    candidates = []
    if pipeline == 'lumen' and grid_fog and not cvars.get('r.HeterogeneousVolumes.HardwareRayTracing', 0):
        for light in lights:
            state = light.get('actual', {})
            mode = str(state.get('cast_raytraced_shadow') or '')
            raytraced = ('ENABLED' in mode and 'DISABLED' not in mode) or (
                'USE_PROJECT_SETTING' in mode and bool(cvars.get('r.RayTracing.Shadows')))
            if state.get('cast_shadows') and state.get('cast_volumetric_shadow') and raytraced:
                candidates.append(light.get('component'))
    return dict(status='needs_review' if candidates else 'not_proven',
        possible_surface_volume_shadow_path_mismatch=candidates,
        occlusion_verified=False, automatically_changed=False)


def classify_appearance_reviews(scene):
    """A rendered approximation is not an omitted resource."""
    imported, skipped = [], []
    volumes={v['name']:v for v in scene.get('volumes',[])}
    for review in scene.get('needs_review',[]):
        stage=review.get('stage')
        usable=(stage in ('light','camera') or
                (stage=='volume' and volumes.get(review.get('name'),{}).get('status')=='converted') or
                (stage=='environment' and scene.get('environment',{}).get('surface')))
        (imported if usable else skipped).append(dict(review,status='needs_review'))
    return imported,skipped


def physical_light(light, scale=1):
    """Blender watts -> photometric UE units, preserving HDR color energy."""
    color = [max(0., float(v)) for v in light['color'][:3]]
    peak = max(color) or 1.
    chroma = [v / peak for v in color]
    # UE multiplies white-light lumens by its stored RGB color without normalizing luminance.
    efficacy = RADIANCE_TO_UE * peak
    watts = max(0., float(light['energy']))
    units = 'lux' if light['type'] == 'SUN' else 'lumens'
    if light['type'] != 'SUN' and light.get('normalize',True):
        watts *= light.get('meters_per_unit',1)**2
    if light['type'] == 'AREA' and not light.get('normalize', True):
        x, y = light.get('size', 1), light.get('size_y', light.get('size', 1))
        area = (math.pi * x * y / 4 if light.get('shape') in ('DISK', 'ELLIPSE') else x * y)
        watts *= area
    elif light['type'] != 'SUN' and not light.get('normalize', True):
        radius=light.get('radius',0)
        watts *= 4*math.pi*radius**2 if radius>0 else 4*light.get('meters_per_unit',1)**2
    if light['type'] == 'SPOT':
        # Blender clips a sphere-normalized emitter; UE normalizes lumens to the cone.
        watts *= (1-math.cos(light.get('spot_size',math.pi)/2))/2
    elif light['type'] == 'SUN' and not light.get('normalize',True):
        angle=light.get('sun_angle',0)
        watts *= math.pi*math.sin(angle/2)**2 if angle>0 else 1
    return {'intensity': watts * efficacy * scale, 'color': chroma, 'units': units,
            'luminous_flux':watts*RADIANCE_TO_UE*sum(v*w for v,w in zip(color,(.2126,.7152,.0722)))*scale}


def grid_layout(bounds, resolution):
    lo, hi = bounds
    spans = [hi[i] - lo[i] for i in range(3)]
    if any(not math.isfinite(v) or v <= 0 for v in spans):
        raise ValueError('体积范围必须有限且三轴长度大于零')
    step = max(spans) / resolution
    shape = [max(1, math.ceil(v / step)) for v in spans]
    return shape, step, [lo[i] + step / 2 for i in range(3)]
