"""Set the requested output size on this test job's owned MRQ preset only."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.ue_materials import Materials
out=ROOT/'work/material_functions_implementation/full_scene'
job=Job(read_json(out/'job_path.json')['job']);cfg=job.data['settings']
assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
report=read_json(job.folder/'ue_report.json');assert not report['failures']
root=report['root'];owner=job.data['asset_id']
assert root.startswith('/Game/MMDBridgeValidation/FullSceneV3/')
converter=Materials(root,owner,report,cfg)
preset=u.load_asset(report['assets']['render_preset']);converter.verify_asset(preset)
output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
output.set_editor_property('output_resolution',u.IntPoint(cfg['width'],cfg['height']))
assert u.EditorAssetLibrary.save_loaded_asset(preset,False);converter.protect_asset(preset)
sequence=u.load_asset(report['assets']['sequence'])
cuts=[section for track in sequence.get_tracks() if isinstance(track,u.MovieSceneCameraCutTrack)
      for section in track.get_sections()]
assert cuts,'Saved scene sequence must have a camera cut'
atomic_json(out/'render_preparation.json',{'complete':True,'resolution':[output.output_resolution.x,output.output_resolution.y],
    'preset':preset.get_path_name(),'sequence':sequence.get_path_name(),'camera_cuts':len(cuts)})
