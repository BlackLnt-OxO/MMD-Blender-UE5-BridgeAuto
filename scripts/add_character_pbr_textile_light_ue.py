"""A separate softbox view exposes dark garments without changing materials."""
from pathlib import Path
import os,sys
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_host import bind_float
from mmd_bridge.character_pbr import VERSION
folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
report=read_json(folder/'ue_receipt.json')
assert report['status']=='ue_assets_created'
root=report['root']; editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
assert root.endswith('/Refined_v'+str(VERSION))
assert editor.load_level(report['render_assets']['level'])
actors=u.get_editor_subsystem(u.EditorActorSubsystem)
all_actors=actors.get_all_level_actors()
actor=next(a for a in all_actors if isinstance(a,u.SkeletalMeshActor))
source=next(a for a in all_actors if isinstance(a,u.CineCameraActor) and a.get_actor_label()=='PBR_Clothing')
sun=next(a for a in all_actors if isinstance(a,u.DirectionalLight))
sky=next(a for a in all_actors if isinstance(a,u.SkyLight))
manifest=read_json(folder/'manifest.json')
head=next(b['exported'] for b in manifest['bones'] if b['original'] in ('頭','头','head','Head'))
center=actor.skeletal_mesh_component.get_socket_location(head)+u.Vector(0,0,-1)
target=center+u.Vector(0,0,-51)
location=target+u.Vector(88,-12,1)
camera=next((a for a in all_actors if a.get_actor_label()=='PBR_BlackCloth'),None)
if camera is None:camera=actors.spawn_actor_from_class(u.CineCameraActor,location)
camera.set_actor_label('PBR_BlackCloth');camera.set_actor_location(location,False,False)
camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location,target),False)
cine,previous=camera.get_cine_camera_component(),source.get_cine_camera_component()
for key in ('post_process_settings','filmback','focus_settings'):
    cine.set_editor_property(key,previous.get_editor_property(key))
cine.set_editor_property('current_focal_length',70.)
light_position=target+u.Vector(85,-55,20)
softbox=next((a for a in all_actors if a.get_actor_label()=='PBR_TextileSoftbox'),None)
if softbox is None:softbox=actors.spawn_actor_from_class(u.RectLight,light_position)
softbox.set_actor_label('PBR_TextileSoftbox');softbox.set_actor_location(light_position,False,False)
softbox.set_actor_rotation(u.MathLibrary.find_look_at_rotation(light_position,target),False)
light=softbox.light_component
light.set_mobility(u.ComponentMobility.MOVABLE)
for key,value in {'intensity_units':u.LightUnits.LUMENS,'intensity':0.,'source_width':60.,'source_height':90.}.items():
    light.set_editor_property(key,value)
name='S_PBR_BlackCloth'
sequence=u.load_asset(root+'/'+name) or u.AssetToolsHelpers.get_asset_tools().create_asset(name,root,u.LevelSequence,u.LevelSequenceFactoryNew())
for b in list(sequence.get_bindings()):b.remove()
for t in list(sequence.get_tracks()):sequence.remove_track(t)
sequence.set_display_rate(u.FrameRate(30,1));sequence.set_playback_start(0);sequence.set_playback_end(241)
section=sequence.add_possessable(actor).add_track(u.MovieSceneSkeletalAnimationTrack).add_section();section.set_range(0,241)
params=section.get_editor_property('params');params.animation=u.load_asset(report['animation_asset'])
params.force_custom_mode=True;params.play_rate=u.MovieSceneTimeWarpExtensions.make_time_warp(.25)
section.set_editor_property('params',params)
binding=sequence.add_possessable(camera)
cut=sequence.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,241)
identity=u.MovieSceneObjectBindingID();identity.set_editor_property('guid',binding.get_id());cut.set_camera_binding_id(identity)
for item,value in ((sun,10.),(sky,.4),(softbox,1000.)):
    light_binding=sequence.add_possessable(item)
    bind_float(sequence,light_binding,item.light_component,'Intensity','Intensity',[(0,value),(240,value)])
    if item==sun:sun_binding=light_binding
transform=sun_binding.add_track(u.MovieScene3DTransformTrack).add_section();transform.set_range(0,241)
for channel,value in zip(transform.get_all_channels(),(0.,0.,300.,0.,-25.,145.,1.,1.,1.)):
    channel.set_default(value)
assert u.EditorAssetLibrary.save_loaded_asset(sequence,only_if_is_dirty=False)
assert editor.save_current_level()
report['review_views']['black_cloth']=sequence.get_path_name()
report['black_cloth_light']={'type':'RectLight','lumens':1000.,'width_cm':60.,'height_cm':90.,
    'default_intensity':0.,'only_enabled_in':'black_cloth','materials_unchanged':True}
atomic_json(folder/'ue_receipt.json',report)
print('PBR_BLACK_CLOTH_VIEW_READY')
