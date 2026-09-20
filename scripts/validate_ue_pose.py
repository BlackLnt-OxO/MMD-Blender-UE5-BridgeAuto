import unreal as u
import json,math
from pathlib import Path
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
folder=Path((root/'work/current_job.txt').read_text().strip())
manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
report=json.loads((folder/'ue_report.json').read_text(encoding='utf-8'))
anim=u.load_asset(report['assets']['animation'])
mesh=u.load_asset(report['assets']['mesh'])
options=u.AnimPoseEvaluationOptions(evaluate_curves=True,optional_skeletal_mesh=mesh,should_retarget=False)
results=[]
for sample in json.loads(Path(manifest['reference_samples']).read_text(encoding='utf-8')):
    pose=u.AnimPoseExtensions.get_anim_pose_at_time(anim,(sample['frame']-manifest['frame_range'][0])/manifest['fps'],options)
    rows=[]
    for name,m in sample['bones'].items():
        actual=u.AnimPoseExtensions.get_bone_pose(pose,u.Name(name),u.AnimPoseSpaces.WORLD).translation
        expected=(-m[1][3]*100,-m[0][3]*100,m[2][3]*100)
        value=(actual.x,actual.y,actual.z)
        error=math.dist(value,expected)
        rows.append({'name':name,'error_cm':error,'actual':value,'expected':expected})
    rows.sort(key=lambda x:x['error_cm'],reverse=True)
    curves=[abs(u.AnimPoseExtensions.get_curve_weight(pose,u.Name(name))-v) for name,v in sample['morphs'].items() if name!='Basis']
    results.append({'frame':sample['frame'],'worst':rows[:8],'max_curve_error':max(curves or [0]),'bones':len(rows)})
(folder/'ue_pose_check.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
