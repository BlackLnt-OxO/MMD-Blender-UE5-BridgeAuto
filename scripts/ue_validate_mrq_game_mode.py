"""Transient MRQ settings check: no map/asset writes, usable with NullRHI."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import atomic_json
from mmd_bridge.ue_appearance import configure_mrq_defaults
out=ROOT/'work/material_functions_implementation/mrq_game_mode';out.mkdir(parents=True,exist_ok=True)
result={'complete':False,'project':u.Paths.get_project_file_path(),'asset_writes':False,'image_rerendered':False}
try:
    assert Path(result['project']).stem=='TESTON1_1005_02'
    preset=u.new_object(u.MoviePipelinePrimaryConfig)
    for pipeline in ('lumen','path_tracing'):
        game=configure_mrq_defaults(preset,pipeline)
        selected=game.get_editor_property('soft_game_mode_override')
        path=u.get_default_object(selected).get_class().get_path_name()
        assert path=='/Script/MovieRenderPipelineCore.MoviePipelineGameMode',str(selected)
        result.setdefault('pipelines',[]).append({'pipeline':pipeline,'game_mode':path})
    cdo=u.get_default_object(u.MoviePipelineGameMode)
    pawn=cdo.get_editor_property('default_pawn_class')
    result['default_pawn_class']=str(pawn)
    assert cdo.get_class().get_path_name()=='/Script/MovieRenderPipelineCore.MoviePipelineGameMode'
    result['player_policy']='Engine MoviePipelineGameMode BeginPlay hides player and HUD'
    result['complete']=True
except Exception:result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(out/'result.json',result)
print('MRQ_GAME_MODE_PASS',result['complete'],result.get('error'),flush=True)
