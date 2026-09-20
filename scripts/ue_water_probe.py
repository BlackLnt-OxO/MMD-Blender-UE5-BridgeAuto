import unreal as u,json
from pathlib import Path
out={}
for cls,names in ((u.MovieSceneParameterSection,['add_scalar_parameter_key']),
                  (u.MovieSceneScriptingFloatKey,['set_interpolation_mode']),
                  (u.Actor,['attach_to_actor'])):
    for name in names:
        out[name]=getattr(cls,name).__doc__
Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork/work/water_api.json').write_text(json.dumps(out,indent=2))
