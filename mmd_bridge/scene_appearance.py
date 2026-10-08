"""Portable scene appearance units. No renderer or network dependency."""
import math

RADIANCE_TO_UE = 683.


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
