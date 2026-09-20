import unreal as u
from pathlib import Path
import json
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
names=['get_anim_pose_at_time','get_bone_pose','get_curve_weight','get_bone_names']
out={n:getattr(u.AnimPoseExtensions,n).__doc__ for n in names}
out['spaces']=[x for x in dir(u.AnimPoseSpaces) if x.isupper()]
out['sequence_position']=u.MovieSceneSequencePlayer.set_playback_position.__doc__
(root/'work/ue_pose_api.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
