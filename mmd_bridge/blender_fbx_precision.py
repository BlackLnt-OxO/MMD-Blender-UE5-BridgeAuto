"""Stabilize sampled bone transforms without editing the rig or mesh data."""
from contextlib import contextmanager

IDENTITY_SCALE_EPSILON = 1e-4


@contextmanager
def animation_scale_precision(kind):
    if kind != 'animation':
        yield None
        return
    from io_scene_fbx.fbx_utils import ObjectWrapper
    original = ObjectWrapper.fbx_object_tx
    existing = getattr(original, '_mmdbridge_precision', None)
    if existing is not None:
        yield existing
        return
    report = {'version': 1, 'identity_scale_epsilon': IDENTITY_SCALE_EPSILON,
              'samples_changed': 0, 'max_scale_correction': 0.0}

    def stable(wrapper, *args, **kwargs):
        values = original(wrapper, *args, **kwargs)
        scale = values[2]
        if wrapper.is_bone and all(abs(value - 1.0) <= IDENTITY_SCALE_EPSILON for value in scale):
            correction = max(abs(value - 1.0) for value in scale)
            if correction:
                scale = scale.copy()
                scale[:] = (1.0, 1.0, 1.0)
                report['samples_changed'] += 1
                report['max_scale_correction'] = max(report['max_scale_correction'], correction)
                return values[:2] + (scale,) + values[3:]
        return values

    stable._mmdbridge_precision = report
    ObjectWrapper.fbx_object_tx = stable
    try:
        yield report
    finally:
        ObjectWrapper.fbx_object_tx = original
