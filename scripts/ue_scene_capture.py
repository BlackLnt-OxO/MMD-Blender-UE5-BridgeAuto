import unreal as u,json
from pathlib import Path
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
folder=Path((root/'work/final_scene_job.txt').read_text().strip())
report=json.loads((folder/'ue_report.json').read_text(encoding='utf-8'))
u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level'])
world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
sub=u.get_editor_subsystem(u.EditorActorSubsystem)
actors=sub.get_all_level_actors()
camera=next(a for a in actors if isinstance(a,u.CineCameraActor))
capture=sub.spawn_actor_from_class(u.SceneCapture2D,camera.get_actor_location(),camera.get_actor_rotation())
component=capture.get_component_by_class(u.SceneCaptureComponent2D)
target=u.RenderingLibrary.create_render_target2d(world,960,540,u.TextureRenderTargetFormat.RTF_RGBA8)
component.set_editor_property('texture_target',target)
component.set_editor_property('capture_source',u.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
component.set_editor_property('fov_angle',camera.get_cine_camera_component().field_of_view)
component.set_editor_property('capture_every_frame',False)
component.capture_scene()
u.RenderingLibrary.export_render_target(world,target,str(root/'work'),'pool-preview.png')
counts={'actors':len(actors),'static_meshes':sum(isinstance(a,u.StaticMeshActor) for a in actors),
        'cameras':sum(isinstance(a,u.CineCameraActor) for a in actors),'lights':sum(isinstance(a,u.Light) for a in actors)}
(folder/'scene_check.json').write_text(json.dumps(counts,indent=2),encoding='utf-8')
sub.destroy_actor(capture)
