import sys,json,math
from pathlib import Path
import bpy
from mathutils import Matrix,Vector

folder=Path(sys.argv[sys.argv.index("--")+1])
manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
reference=json.loads(Path(manifest["reference_samples"]).read_text(encoding="utf-8"))
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.context.scene.render.fps=manifest["fps"]
bpy.ops.import_scene.fbx(filepath=manifest["files"]["animation"]["path"],use_anim=True,anim_offset=0)
arm=next(o for o in bpy.context.scene.objects if o.type=="ARMATURE")
checks=[]
for sample in reference:
    bpy.context.scene.frame_set(sample["frame"])
    evaluated=arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
    errors=[]
    for name,matrix in sample["bones"].items():
        actual=evaluated.matrix_world @ evaluated.pose.bones[name].head
        expected=Matrix(matrix).translation
        errors.append((name,(actual-expected).length))
    checks.append({"frame":sample["frame"],"max_position_error_m":max(x[1] for x in errors),"worst":sorted(errors,key=lambda x:x[1],reverse=True)[:5]})
result={"pose_checks":checks,"bones":len(arm.data.bones),"morphs":sum(len(o.data.shape_keys.key_blocks)-1 for o in bpy.context.scene.objects if o.type=="MESH" and o.data.shape_keys)}
(folder/"blender_roundtrip.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
print('ROUNDTRIP:'+json.dumps(result))
