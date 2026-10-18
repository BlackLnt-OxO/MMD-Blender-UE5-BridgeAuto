"""Set the owned PBR review fixture to neutral studio lighting."""
import json
import os
from pathlib import Path
import unreal as u

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
receipt=json.loads((folder/'ue_receipt.json').read_text(encoding='utf-8'))
level=receipt['render_assets']['level']
assert level.startswith('/Game/MMDBridgePBR/')
editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
assert editor.load_level(level)
actors=u.get_editor_subsystem(u.EditorActorSubsystem)
for actor in list(actors.get_all_level_actors()):
    if isinstance(actor,u.SkyAtmosphere):
        actors.destroy_actor(actor)
sun=next(a for a in actors.get_all_level_actors() if isinstance(a,u.DirectionalLight))
sun.set_actor_rotation(u.Rotator(pitch=-25,yaw=180,roll=0),False)
sun.light_component.set_editor_property('light_color',u.Color(255,255,255,255))
print('PBR_STUDIO_BEFORE',sun.get_actor_rotation(),sun.light_component.intensity)
sky=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkyLight))
cube=u.load_asset('/Engine/MapTemplates/Sky/DaylightAmbientCubemap.DaylightAmbientCubemap')
assert isinstance(cube,u.TextureCube)
sky.light_component.set_editor_property('source_type',u.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
sky.light_component.set_editor_property('cubemap',cube)
sky.light_component.set_editor_property('real_time_capture',False)
sky.light_component.set_editor_property('intensity',1.)
camera=next(a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor))
cine=camera.get_cine_camera_component()
post=cine.get_editor_property('post_process_settings')
post.set_editor_property('auto_exposure_bias',-1.5)
cine.set_editor_property('post_process_settings',post)
seq=u.load_asset(receipt['render_assets']['sequence'])
for binding in seq.get_bindings():
    for track in binding.get_tracks():
        if isinstance(track,u.MovieSceneFloatTrack) and str(track.get_property_name())=='Intensity':
            for section in track.get_sections():
                channel=section.get_all_channels()[0]
                for key in channel.get_keys():
                    value=key.get_value()
                    if value>0:key.set_value(10. if value>1 else 1.)
u.EditorAssetLibrary.save_loaded_asset(seq)
assert editor.save_current_level()
print('PBR_STUDIO_UPDATED')
