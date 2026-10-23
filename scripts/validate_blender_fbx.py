import sys,json
from pathlib import Path
import bpy
from mathutils import Matrix
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/blender_mmd_tools-4.5.13'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/python'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_tools.compat.action_compat import get_action_fcurves
from mmd_bridge.blender_validate import animated_morph_names, position_error_check
from mmd_bridge.export_quality import check_quality

folder=Path(sys.argv[sys.argv.index("--")+1])
manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
policy=manifest.get('export_validation_policy','strict')
quality_issues=[]
def review(message):
    check_quality(False,message,quality_issues,policy)
(folder/'blender_roundtrip.json').unlink(missing_ok=True)
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
expected_animated = animated_morph_names(manifest, reference)
if not bone_curves or (expected_animated and not changing_morphs):
    review('动画 FBX 缺少骨骼或变化的表情动画曲线')
checks=[]
position_issues=[]
original_names={bone['exported']:bone['original'] for bone in manifest['bones']}
for sample in reference:
    bpy.context.scene.frame_set(sample["frame"])
    evaluated=arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
    errors=[]
    for name,matrix in sample["bones"].items():
        if name not in evaluated.pose.bones:
            review('动画 FBX 回读缺少骨骼: '+name)
            continue
        actual=evaluated.pose.bones[name].matrix.translation
        expected=Matrix(matrix).translation
        error=(actual-expected).length
        errors.append((name,error))
        check=position_error_check(error,expected,arm.data.bones[name].matrix_local.translation)
        if check['severity']!='ok':
            position_issues.append({'frame':sample['frame'],'bone':name,'original':original_names.get(name,name),
                                    'error_m':error,**check})
    morph_values = {key.name: key.value for obj in bpy.context.scene.objects if obj.type == 'MESH'
                    and obj.data.shape_keys for key in obj.data.shape_keys.key_blocks}
    morph_error = max((abs(morph_values.get(name, 0)-value) for name, value in sample.get('morphs', {}).items()), default=0)
    if morph_error > 1e-4:
        review('动画 FBX 回读表情权重偏差超过 0.0001')
    checks.append({"frame":sample["frame"],"max_morph_error":morph_error,"max_position_error_m":max((x[1] for x in errors),default=0),"worst":sorted(errors,key=lambda x:x[1],reverse=True)[:5]})
result={"pose_checks":checks,"bones":len(arm.data.bones),"morphs":sum(len(o.data.shape_keys.key_blocks)-1 for o in bpy.context.scene.objects if o.type=="MESH" and o.data.shape_keys),
        "bone_curves":len(bone_curves),"morph_curves":len(morph_curves),"changing_morphs":len(changing_morphs),
        'success':False,'position_issues':position_issues,'warnings':[]}
for issue in position_issues:
    if issue['severity']=='warning':
        result['warnings'].append(f"FBX 精度警告：帧 {issue['frame']}，骨骼 {issue['original']}，位置偏差 "
                                  f"{issue['error_m']*1000:.6f} 毫米（基准 0.1 毫米，坐标精度预算 "
                                  f"{issue['limit_m']*1000:.6f} 毫米）；已继续，需复核")
failures=[issue for issue in position_issues if issue['severity']=='error']
if failures:
    (folder/'blender_roundtrip_failure.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    worst=max(failures,key=lambda issue:issue['error_m'])
    review(f"动画 FBX 回读骨骼位置偏差超过精度预算：帧 {worst['frame']}，骨骼 {worst['original']}，"
                     f"偏差 {worst['error_m']*1000:.6f} 毫米，允许 {worst['limit_m']*1000:.6f} 毫米")
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.fbx(filepath=manifest['files']['model']['path'],use_anim=True,anim_offset=0)
if any(o.animation_data and o.animation_data.action for o in bpy.context.scene.objects):
    review('模型 FBX 不应包含对象动画')
if any(o.type=='MESH' and o.data.shape_keys and o.data.shape_keys.animation_data
       and o.data.shape_keys.animation_data.action for o in bpy.context.scene.objects):
    review('模型 FBX 不应包含表情动画')
if 'camera' in manifest['files']:
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.fbx(filepath=manifest['files']['camera']['path'],use_anim=True,anim_offset=0)
    cameras=[o for o in bpy.context.scene.objects if o.type=='CAMERA']
    curves=get_action_fcurves(cameras[0].animation_data.action) if len(cameras)==1 and cameras[0].animation_data and cameras[0].animation_data.action else []
    if len(cameras)!=1 or not curves:
        review('镜头 FBX 缺少摄像机动画')
    result['camera_curves']=len(curves)
result.update(success=not quality_issues,usable=True,validation_policy=policy,quality_issues=quality_issues,file_hashes={kind:entry['sha256'] for kind,entry in manifest['files'].items()})
result['warnings'].extend(issue+'；已继续，需复核' for issue in quality_issues)
(folder/"blender_roundtrip.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
(folder/'blender_roundtrip_failure.json').unlink(missing_ok=True)
print('ROUNDTRIP:'+json.dumps(result))
