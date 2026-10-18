"""Create isolated v3 wet/dry controls and camera views on the original GenZ mesh."""
import copy
import runpy
from pathlib import Path
import unreal as u

ROOT = Path(__file__).resolve().parents[1]
state = runpy.run_path(str(ROOT/'scripts/refine_character_pbr_ue.py'))
manager, report, root, folder = [state[k] for k in ('manager', 'report', 'root', 'folder')]
manifest, actor, camera, sun, sky, animation = [state[k] for k in ('manifest', 'actor', 'camera', 'sun', 'sky', 'animation')]
from mmd_bridge.core import atomic_json
from mmd_bridge.ue_host import bind_float


def profile_values(profile):
    settings = profile.get_editor_property('settings')
    keys = ('enable_burley', 'enable_mean_free_path', 'mean_free_path_distance', 'world_unit_scale')
    return {key: settings.get_editor_property(key) for key in keys}


def variant(material, suffix, parameters):
    target = material.get_path_name().split('.')[0]+'_'+suffix
    result = u.load_asset(target)
    if result:
        manager.verify_asset(result)
    else:
        result = u.EditorAssetLibrary.duplicate_asset(material.get_path_name(), target)
    manager.managed(result)
    for key, value in parameters.items():
        manager.lib.set_material_instance_scalar_parameter_value(result, key, value)
    manager.lib.update_material_instance(result)
    u.EditorAssetLibrary.save_loaded_asset(result, only_if_is_dirty=False)
    manager.protect_asset(result)
    return result


def static_sequence(name, selected_camera, yaw=145., sky_intensity=.4):
    sequence = u.load_asset(root+'/'+name)
    if not sequence:
        sequence = u.AssetToolsHelpers.get_asset_tools().create_asset(name, root, u.LevelSequence, u.LevelSequenceFactoryNew())
    for binding in list(sequence.get_bindings()): binding.remove()
    for track in list(sequence.get_tracks()): sequence.remove_track(track)
    sequence.set_display_rate(u.FrameRate(30, 1)); sequence.set_playback_start(0); sequence.set_playback_end(241)
    section = sequence.add_possessable(actor).add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
    section.set_range(0, 241)
    params = section.get_editor_property('params'); params.animation = animation; params.force_custom_mode = True
    params.play_rate = u.MovieSceneTimeWarpExtensions.make_time_warp(.25)
    section.set_editor_property('params', params)
    binding = sequence.add_possessable(selected_camera)
    cut = sequence.add_track(u.MovieSceneCameraCutTrack).add_section(); cut.set_range(0, 241)
    identity = u.MovieSceneObjectBindingID(); identity.set_editor_property('guid', binding.get_id()); cut.set_camera_binding_id(identity)
    light = sequence.add_possessable(sun)
    channels = light.add_track(u.MovieScene3DTransformTrack).add_section()
    channels.set_range(0, 241)
    for channel, value in zip(channels.get_all_channels(), (0., 0., 300., 0., -25., yaw, 1., 1., 1.)):
        channel.set_default(value)
    bind_float(sequence, light, sun.light_component, 'Intensity', 'Intensity', [(0, 10.), (240, 10.)])
    bind_float(sequence, sequence.add_possessable(sky), sky.light_component,
               'Intensity', 'Intensity', [(0, sky_intensity), (240, sky_intensity)])
    u.EditorAssetLibrary.save_loaded_asset(sequence, only_if_is_dirty=False)
    return sequence.get_path_name()


try:
    report['wet_on_overrides'], report['no_sss_overrides'], report['cloth_no_detail_overrides'] = {}, {}, {}
    report['skin_checks'], report['cloth_checks'] = [], []
    for record in manifest['materials']:
        kind = record['character_material']['pbr']['type']
        material = manager.asset(record['id'])
        if kind in ('face', 'skin'):
            wet = variant(material, 'WetOn', {'WetEnabled': 1.})
            no_sss = variant(material, 'NoSSS', {'SSSStrength': 0.})
            report['wet_on_overrides'][material.get_path_name()] = wet.get_path_name()
            report['no_sss_overrides'][material.get_path_name()] = no_sss.get_path_name()
            settings = profile_values(material.get_editor_property('parent').get_editor_property('subsurface_profile'))
            assert settings['enable_burley'] and settings['enable_mean_free_path'], settings
            assert abs(settings['mean_free_path_distance']-(.18 if kind == 'face' else .32)) < 1e-5
            assert manager.lib.get_material_instance_scalar_parameter_value(material, 'WetEnabled') == 0.
            assert manager.lib.get_material_instance_scalar_parameter_value(wet, 'WetEnabled') == 1.
            report['skin_checks'].append({'name': record['name'], 'profile': settings,
                                          'wet_default': 0., 'wet_variant': 1.})
        if kind == 'cloth':
            plain = variant(material, 'NoWeave', {'DetailStrength': 0., 'Fuzz': 0.})
            report['cloth_no_detail_overrides'][material.get_path_name()] = plain.get_path_name()
            report['cloth_checks'].append({'name': record['name'],
                'shading_model': str(material.get_editor_property('parent').get_editor_property('shading_model')),
                'normal': manager.lib.get_material_instance_texture_parameter_value(material, 'detail_normal_Tex').get_path_name(),
                'fuzz': manager.lib.get_material_instance_scalar_parameter_value(material, 'Fuzz')})
    for label in ('wet_on', 'no_sss', 'cloth_no_detail'):
        atomic_json(folder/(label+'.json'), report[label+'_overrides'])
    previous_mesh = u.load_asset(report['source_mesh'])
    previous_skin = next(m.material_interface for m in previous_mesh.materials
                         if u.EditorAssetLibrary.get_metadata_tag(m.material_interface, 'MMDBridge.SourceMaterial') ==
                         next(r['id'] for r in manifest['materials'] if r['name'] == 'Face'))
    report['previous_profile_loaded'] = profile_values(previous_skin.get_editor_property('parent').get_editor_property('subsurface_profile'))
    report['geometry_workflow'] = 'duplicate existing Refined_v2 skeletal mesh; change material bindings only'

    actors = u.get_editor_subsystem(u.EditorActorSubsystem)
    # The shared builder reloads the level to check saved bindings. Its actor
    # wrappers refer to the world that was unloaded; query the current world.
    actor = next(a for a in actors.get_all_level_actors() if isinstance(a, u.SkeletalMeshActor))
    camera = next(a for a in actors.get_all_level_actors() if isinstance(a, u.CineCameraActor)
                  and not a.get_actor_label().startswith('PBR_'))
    sun = next(a for a in actors.get_all_level_actors() if isinstance(a, u.DirectionalLight))
    sky = next(a for a in actors.get_all_level_actors() if isinstance(a, u.SkyLight))
    head = next(b['exported'] for b in manifest['bones'] if b['original'] in ('頭', '头', 'head', 'Head'))
    center = actor.skeletal_mesh_component.get_socket_location(head)+u.Vector(0, 0, -1)

    def new_camera(label, location, target, focal=50.):
        result = next((a for a in actors.get_all_level_actors() if a.get_actor_label() == label), None)
        if result is None:
            result = actors.spawn_actor_from_class(u.CineCameraActor, location)
        result.set_actor_label(label); result.set_actor_location(location, False, False)
        result.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location, target), False)
        cine, source = result.get_cine_camera_component(), camera.get_cine_camera_component()
        cine.set_editor_property('post_process_settings', source.get_editor_property('post_process_settings'))
        cine.set_editor_property('filmback', source.get_editor_property('filmback'))
        cine.set_editor_property('focus_settings', source.get_editor_property('focus_settings'))
        cine.set_editor_property('current_focal_length', focal)
        return result

    three_quarter = new_camera('PBR_ThreeQuarter', center+u.Vector(78, -35, 1), center)
    macro = new_camera('PBR_SkinMacro', center+u.Vector(36, -6, -6), center+u.Vector(0, -2, -6), 70.)
    torso = new_camera('PBR_Cloth', center+u.Vector(75, -8, -28), center+u.Vector(0, 0, -28), 60.)
    report['review_views'] = {'portrait': static_sequence('S_PBR_Portrait', camera),
        'three_quarter': static_sequence('S_PBR_ThreeQuarter', three_quarter),
        'skin_macro': static_sequence('S_PBR_SkinMacro', macro),
        'cloth': static_sequence('S_PBR_Cloth', torso)}
    assert u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level()
    report['status'] = 'ue_assets_created'
    print('PBR_SKIN_WET_READY', len(report['skin_checks']), len(report['cloth_checks']))
except Exception:
    import traceback
    report['status'] = 'failed'; report['error'] = traceback.format_exc()
    raise
finally:
    atomic_json(folder/'ue_receipt.json', report)
