import sys,json
from pathlib import Path
import bpy
root=Path(__file__).resolve().parents[1]
for path in (root,root/'vendor/blender_mmd_tools-4.5.13',root/'vendor/python'):
    sys.path.insert(0,str(path))
from mmd_bridge.blender_host import Character
from mmd_bridge.core import Job
from mmd_bridge.core import atomic_json
source_job=Job((root/'work/current_job.txt').read_text(encoding='utf-8'))
job=Job(root/'work/debug_job')
atomic_json(job.path,source_job.data)
character=Character(job)
character.prefix += '_Debug'
character.prepare()
character.set_kind('animation')
bpy.context.scene.frame_set(30)
deps=bpy.context.evaluated_depsgraph_get()
source=bpy.data.objects['MMD_Rig'].evaluated_get(deps)
target=character.armature.evaluated_get(deps)
rows=[]
for a in source.pose.bones:
    b=target.pose.bones[a.name]
    error=(a.head-b.head).length
    rows.append({'name':a.name,'error':error,'a':list(a.head),'b':list(b.head),'parent':a.parent.name if a.parent else None,'rotation_mode':b.rotation_mode,'inherit_rotation':b.bone.use_inherit_rotation,'local_location':b.bone.use_local_location,'basis':list(b.location),'scale_a':list(a.matrix.to_scale()),'scale_b':list(b.matrix.to_scale())})
rows.sort(key=lambda r:r['error'],reverse=True)
result={'source_world':[list(r) for r in source.matrix_world],'target_world':[list(r) for r in target.matrix_world],'worst':rows[:10],'first':rows[-5:]}
(job.folder/'debug_pose.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result))
character.export_all()
bpy.context.scene.frame_set(30)
controls=[]
for name in ('b_0735_eb0fa25a','b_0736_82d9dc71','b_0738_37a9fa0f','b_0739_26994873'):
    a=bpy.data.objects['MMD_Rig'].evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones[name]
    b=character.armature.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones[name]
    controls.append({'name':name,'a_head':list(a.head),'a_matrix':list(a.matrix.translation),'b_head':list(b.head),'b_matrix':list(b.matrix.translation)})
(job.folder/'controls.json').write_text(json.dumps(controls,indent=2))
