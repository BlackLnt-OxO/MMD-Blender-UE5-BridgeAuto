import sys,json
from pathlib import Path
import bpy
from mathutils import Matrix
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/blender_mmd_tools-4.5.13'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/python'))
from mmd_tools.compat.action_compat import get_action_fcurves

folder=Path(sys.argv[sys.argv.index("--")+1])
manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
reference=json.loads(Path(manifest["reference_samples"]).read_text(encoding="utf-8"))
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.context.scene.render.fps=manifest["fps"]
bpy.ops.import_scene.fbx(filepath=manifest["files"]["animation"]["path"],use_anim=True,anim_offset=0)
arm=next(o for o in bpy.context.scene.objects if o.type=="ARMATURE")
# Blender's FBX importer reconnects coincident bones; the exported physics
# rig deliberately keeps them independent so their translation keys apply.
bpy.context.view_layer.objects.active=arm
bpy.ops.object.mode_set(mode='EDIT')
for bone in arm.data.edit_bones:
    bone.use_connect=False
bpy.ops.object.mode_set(mode='OBJECT')
bone_action=arm.animation_data.action if arm.animation_data else None
bone_curves=[curve for curve in get_action_fcurves(bone_action) if curve.data_path.startswith('pose.bones[')] if bone_action else []
morph_curves=[curve for obj in bpy.context.scene.objects if obj.type=='MESH' and obj.data.shape_keys
              and obj.data.shape_keys.animation_data and obj.data.shape_keys.animation_data.action
              for curve in get_action_fcurves(obj.data.shape_keys.animation_data.action)
              if curve.data_path.startswith('key_blocks[')]
changing_morphs=[curve for curve in morph_curves
                 if len({round(key.co.y,5) for key in curve.keyframe_points})>1]
if not bone_curves or not changing_morphs:
    raise ValueError('动画 FBX 缺少骨骼或变化的表情动画曲线')
checks=[]
for sample in reference:
    bpy.context.scene.frame_set(sample["frame"])
    evaluated=arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
    errors=[]
    for name,matrix in sample["bones"].items():
        actual=evaluated.pose.bones[name].matrix.translation
        expected=Matrix(matrix).translation
        errors.append((name,(actual-expected).length))
    checks.append({"frame":sample["frame"],"max_position_error_m":max(x[1] for x in errors),"worst":sorted(errors,key=lambda x:x[1],reverse=True)[:5]})
result={"pose_checks":checks,"bones":len(arm.data.bones),"morphs":sum(len(o.data.shape_keys.key_blocks)-1 for o in bpy.context.scene.objects if o.type=="MESH" and o.data.shape_keys),
        "bone_curves":len(bone_curves),"morph_curves":len(morph_curves),"changing_morphs":len(changing_morphs)}
if max(check['max_position_error_m'] for check in checks)>1e-4:
    raise ValueError('动画 FBX 回读骨骼位置偏差超过 0.1 毫米')
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.fbx(filepath=manifest['files']['model']['path'],use_anim=True,anim_offset=0)
if any(o.animation_data and o.animation_data.action for o in bpy.context.scene.objects):
    raise ValueError('模型 FBX 不应包含对象动画')
if any(o.type=='MESH' and o.data.shape_keys and o.data.shape_keys.animation_data
       and o.data.shape_keys.animation_data.action for o in bpy.context.scene.objects):
    raise ValueError('模型 FBX 不应包含表情动画')
if 'camera' in manifest['files']:
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.fbx(filepath=manifest['files']['camera']['path'],use_anim=True,anim_offset=0)
    cameras=[o for o in bpy.context.scene.objects if o.type=='CAMERA']
    curves=get_action_fcurves(cameras[0].animation_data.action) if len(cameras)==1 and cameras[0].animation_data and cameras[0].animation_data.action else []
    if len(cameras)!=1 or not curves:
        raise ValueError('镜头 FBX 缺少摄像机动画')
    result['camera_curves']=len(curves)
(folder/"blender_roundtrip.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
print('ROUNDTRIP:'+json.dumps(result))
