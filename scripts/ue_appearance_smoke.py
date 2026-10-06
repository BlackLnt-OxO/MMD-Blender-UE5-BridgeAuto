"""Execute in UE 5.8 with -ExecutePythonScript; writes live native evidence."""
import sys
import traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge import ue_host

out=ROOT/'work/scene_appearance_smoke'
try:
    probe={}
    for name in ('OpenColorIOConfigurationFactory','MoviePipelineMasterConfigFactory','TextureCubeFactory',
                 'TextureFactory','OpenColorIODisplayExtensionWrapper'):
        cls=getattr(u,name,None)
        probe[name]=cls.__doc__ if cls else None
    probe['texture_cube']=u.TextureCube.__doc__
    atomic_json(out/'ue_api_probe.json',probe)
    try:
        from pxr import Usd,UsdVol
        data=read_json(Path(read_json(out/'export_result.json')['job'])/'manifest.json')['scene']
        stage=Usd.Stage.Open(data['usd']['root_layer'])
        atomic_json(out/'ue_usd_probe.json',[{'path':str(p.GetPath()),'type':p.GetTypeName(),'volume':p.IsA(UsdVol.Volume)} for p in stage.Traverse()])
    except Exception:
        atomic_json(out/'ue_usd_probe.json',{'error':traceback.format_exc()})
    record=read_json(out/'export_result.json')
    folder=Path(record['job'])
    ue_host.import_job(folder)
    report=read_json(folder/'ue_report.json')
    actor_sub=u.get_editor_subsystem(u.EditorActorSubsystem)
    stages=[a for a in actor_sub.get_all_level_actors() if isinstance(a,u.UsdStageActor)]
    volumes=[]
    for stage in stages:
        scene=read_json(folder/'manifest.json')['scene']
        for v in scene['volumes']:
            c=stage.get_generated_component(v.get('prim_path',''))
            if c:
                volumes.append({'prim_path':v['prim_path'],'class':c.get_class().get_name(),
                                'material':c.get_material(0).get_path_name() if c.get_material(0) else None,
                                'label':c.get_owner().get_actor_label()})
    result={'complete':report['complete'],'report':report,'native_volumes':volumes}
    atomic_json(out/'ue_result.json',result)
except Exception:
    atomic_json(out/'ue_result.json',{'complete':False,'error':traceback.format_exc()})
    raise
