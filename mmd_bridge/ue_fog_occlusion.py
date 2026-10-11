"""Shared, transient viewport/MRQ fog option. Never save scene resources."""
from .fog_occlusion import decision, TemporaryOverrides

_CONSOLE = TemporaryOverrides()
_LAST = None
_COMPONENTS = {}
_COMPONENT_OVERRIDES = TemporaryOverrides()


def _component_read(key):
    component,name=_COMPONENTS[key]
    from .ue_appearance import native_property
    try:
        return native_property(component,'cast_raytraced_shadow',name) if name=='CastRaytracedShadow' else native_property(component,name)
    except Exception:
        return None  # A destroyed world relinquishes ownership.


def _component_write(key,value):
    component,name=_COMPONENTS[key]
    if name=='CastRaytracedShadow':component.set_cast_raytraced_shadows(value)
    else:component.set_editor_property(name,value)


def restore_components():
    _COMPONENT_OVERRIDES.end(_component_read,_component_write)
    _COMPONENTS.clear()


def apply_components(volume_components, light_components, requested='legacy', pipeline='lumen', volumes=()):
    """Explicit native targets; never choose the first actor from object iteration."""
    import unreal as u
    result=_decision(requested,pipeline,volumes)
    desired={}
    if result.get('effective') in ('vsm_finer','vsm_nocache'):
        if not volume_components:raise ValueError('实验雾补充缺少实际体积组件，拒绝只应用控制台参数')
        for component in volume_components:
            if not isinstance(component,u.HeterogeneousVolumeComponent):raise ValueError('补充目标不是异质体积组件')
            for name,value in result['component_overrides'].items():
                key=(component,name);_COMPONENTS[key]=(component,name);desired[key]=value
        for component in light_components:
            key=(component,'CastRaytracedShadow')
            _COMPONENTS[key]=(component,'CastRaytracedShadow');desired[key]=u.CastRayTracedShadow.DISABLED
    for key in desired:
        if _component_read(key) is None:raise ValueError('实验补充参数不可读，拒绝覆盖未知原值')
    _COMPONENT_OVERRIDES.apply(desired,_component_read,_component_write)
    result=dict(result,component_targets=[c.get_path_name() for c in volume_components] if desired or result.get('effective')=='vsm_nocache' else [],
                light_targets=[c.get_path_name() for c in light_components] if desired else [],
                parameters_changed=bool(desired or result['cvars']),source_appearance_changed=False)
    return result


def apply_world(world, stages, volumes, requested='legacy', pipeline='lumen', light_bindings=()):
    """Loaded-world overrides, scoped to source prim paths and managed lights."""
    import unreal as u
    result=_decision(requested,pipeline,volumes)
    components=[];lights=[]
    if result.get('effective') in ('vsm_finer','vsm_nocache'):
        for path in result['volume_paths']:
            found=[stage.get_generated_component(path) for stage in stages]
            found=list({c.get_path_name():c for c in found if c is not None}.values())
            if len(found)!=1:raise ValueError('无法唯一定位源雾组件: '+path)
            components.append(found[0])
        names={item['actor_name'] for item in light_bindings if item.get('actor_name')}
        for actor in u.GameplayStatics.get_all_actors_of_class(world,u.Light):
            owned=actor.get_name() in names if names else 'MMDBridge' in [str(t) for t in actor.tags]
            if owned:
                component=actor.get_component_by_class(u.LightComponent)
                if component is not None:lights.append(component)
    return apply_components(components,lights,requested,pipeline,volumes)


def _read(name):
    import unreal as u
    return u.SystemLibrary.get_console_variable_float_value(name)


def _write(name,value):
    import unreal as u
    u.SystemLibrary.execute_console_command(None,name+' '+str(value))


def _decision(requested,pipeline,volumes):
    if requested=='legacy':return decision(requested,pipeline,volumes=volumes)
    import unreal as u
    return decision(requested,pipeline,engine=u.SystemLibrary.get_engine_version(),volumes=volumes)


def configure(config, requested='legacy', pipeline='lumen', volumes=()):
    result=_decision(requested,pipeline,volumes)
    import unreal as u
    console=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    console.remove_console_variable('r.VolumetricFog.InjectRaytracedLights',True)
    if result.get('renderer')=='volumetric_fog':
        from .fog_occlusion import EXPERIMENTAL_PROFILES
        for name in {k for p in EXPERIMENTAL_PROFILES.values() for k in p['cvars']}:
            console.remove_console_variable(name,True)
    # MRQ owns its temporary console values and restores them at teardown.
    # Production detail remains gated. Explicit experimental supplements keep
    # their own status and never enter the validated production registry.
    if result['cvars']:
        import unreal as u
        console=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
        for name,value in result['cvars'].items():console.add_or_update_console_variable(name,value)
    return result


def viewport(requested='legacy', pipeline='lumen', volumes=(), *, leaving=False):
    global _LAST
    if leaving:
        _CONSOLE.end(_read,_write);_LAST=None
        return
    result=_decision(requested,pipeline,volumes)
    _CONSOLE.apply(result['cvars'],_read,_write)
    if result!=_LAST and result['reviews']:
        import unreal as u
        u.log_warning('MMDBridge 雾光遮挡: '+ '; '.join(result['reviews']))
    _LAST=result
    return result
