"""Startup hook for an isolated -game -NullRHI validate_only MRQ job."""
import os
from pathlib import Path

import unreal as u
from mmd_bridge import ue_standalone_render as runtime
from mmd_bridge.ue_appearance import native_property
from mmd_bridge.core import atomic_json

prepare=runtime.prepare_job


def validated_prepare(queue):
    job=prepare(queue)
    component=u.new_object(u.HeterogeneousVolumeComponent)
    observed={}
    aliases={}
    for python_name,native_name,expected in (
        ('step_factor','StepFactor',1.),('shadow_step_factor','ShadowStepFactor',2.),
        ('shadow_bias_factor','ShadowBiasFactor',.5),('lighting_downsample_factor','LightingDownsampleFactor',2.)):
        aliases[python_name]=native_property(component,python_name)
        value=native_property(component,python_name,native_name)
        assert value==expected,(native_name,value)
        observed[python_name]=value
    atomic_json(Path(os.environ['MMDBRIDGE_WORLD_PROPERTY_OUT']),dict(complete=True,
        native_values=observed,python_alias_values=aliases,rendered=False))
    return job


runtime.prepare_job=validated_prepare
