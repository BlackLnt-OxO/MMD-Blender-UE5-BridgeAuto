"""Shared, transient viewport/MRQ fog option. Never save scene resources."""
from .fog_occlusion import decision, TemporaryOverrides

_CONSOLE = TemporaryOverrides()
_LAST = None


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
    # MRQ owns its temporary console values and restores them at teardown.
    # No unvalidated candidate or diagnostic cache-off setting is written here.
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
