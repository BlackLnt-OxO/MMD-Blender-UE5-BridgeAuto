"""UE editor capture and reload checks for validate_builtin_cel_ue.py output."""
from pathlib import Path
import json
import hashlib
import time
import traceback
import unreal as u

args = u.SystemLibrary.parse_command_line(u.SystemLibrary.get_command_line())[2]
cfg = json.loads(Path(args['CelValidationConfig']).read_text(encoding='utf-8'))
here = Path(cfg['output'])
receipt = json.loads((here/'native_receipt.json').read_text(encoding='utf-8'))
level_sub = u.get_editor_subsystem(u.LevelEditorSubsystem)
assert level_sub.load_level(receipt['map'].split('.')[0])
actor_sub = u.get_editor_subsystem(u.EditorActorSubsystem)
world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
actors = {mode: u.find_object(None, r['actor']) for mode, r in receipt['reports'].items()}
assert all(actors.values())
sequence = u.load_asset(receipt['sequence'])
lib = u.LevelSequenceEditorBlueprintLibrary
u.get_editor_subsystem(u.AssetEditorSubsystem).close_all_editors_for_asset(sequence)
binding_rows = [{'name': str(b.get_name()), 'id': b.get_id().export_text(),
                 'objects': [o.get_path_name() for o in sequence.locate_bound_objects(b, world)]}
                for b in sequence.get_bindings()]
(here/'binding_snapshot.json').write_text(json.dumps(binding_rows, indent=2), encoding='utf-8')
assert all(b['objects'] for b in binding_rows), 'Unresolved fixture binding'
player, playback_actor = u.LevelSequencePlayer.create_level_sequence_player(world, sequence, u.MovieSceneSequencePlaybackSettings())
assert player


def refresh_poses():
    for actor in actors.values():
        assert actor.refresh_pose() == 3


def set_frame(frame):
    params = u.MovieSceneSequencePlaybackParams(time=frame/30., position_type=u.MovieScenePositionType.TIME,
                                               update_method=u.UpdatePositionMethod.JUMP)
    player.set_playback_position(params)
    refresh_poses()
manifest = json.loads(Path(cfg['manifest']).read_text(encoding='utf-8'))
bones = [b['exported'] for b in manifest['bones']]
morphs = [m['exported'] for m in manifest['morphs']]
camera = actor_sub.spawn_actor_from_class(u.CineCameraActor, u.Vector(0, -550, 90))
camera.set_actor_label('Cel_Comparison_Camera')
cc = camera.get_cine_camera_component()
filmback = cc.get_editor_property('filmback')
filmback.sensor_width = 36
filmback.sensor_height = 20.25
cc.set_editor_property('filmback', filmback)
cc.set_editor_property('current_focal_length', 50)
focus = cc.get_editor_property('focus_settings')
focus.set_editor_property('focus_method', u.CameraFocusMethod.DISABLE)
cc.set_editor_property('focus_settings', focus)
post = next(a for a in actor_sub.get_all_level_actors() if isinstance(a, u.PostProcessVolume))
cc.set_editor_property('post_process_settings', post.get_editor_property('settings'))
cc.set_editor_property('post_process_blend_weight', 1.)
head_bone = next(b['exported'] for b in manifest['bones'] if b['original'] in ('頭', 'head', '头'))

jobs = [{'kind': 'animation', 'frame': f} for f in (0, 3030, 6060)]
for mode in ('neutral', 'role'):
    for view, pos, rot in [('body_front', (0, -550, 90), (0, 90, 0)),
                           ('head_front', (0, -145, 140), (0, 90, 0)),
                           ('head_side', (145, 0, 140), (0, 180, 0)),
                           ('head_back', (0, 145, 140), (0, -90, 0))]:
        jobs.append({'kind': 'capture', 'frame': 4119, 'mode': mode, 'view': view, 'position': pos, 'rotation': rot})
if cfg.get('capture_jobs'):
    jobs = cfg['capture_jobs']
state = {'index': 0, 'phase': 'setup', 'ready': time.monotonic()+15, 'samples': [], 'captures': [], 'started': time.monotonic()}


def pose_sample(actor, frame):
    components = [actor.skeletal_mesh_component, actor.filter_mesh, actor.outline_mesh]
    instances = [c.get_anim_instance() for c in components]
    assert all(instances) and len(set(instances)) == 3, 'Three animation instances must evaluate independently'
    transform_rows = []
    curve_rows = []
    for c, instance in zip(components, instances):
        transforms = []
        for b in bones:
            t = c.get_socket_transform(b, u.RelativeTransformSpace.RTS_COMPONENT)
            transforms.append([t.translation.x, t.translation.y, t.translation.z,
                               t.rotation.x, t.rotation.y, t.rotation.z, t.rotation.w])
        transform_rows.append(transforms)
        curve_rows.append([instance.get_curve_value(name) for name in morphs])
    # q and -q encode the same rotation. Inverted outline scale can select the
    # other representation when the socket transform is decomposed by UE.
    bone_error = 0.
    for row in transform_rows[1:]:
        for x, y in zip(transform_rows[0], row):
            position_error = max(abs(a-b) for a, b in zip(x[:3], y[:3]))
            rotation_error = min(max(abs(a-b) for a, b in zip(x[3:], y[3:])),
                                 max(abs(a+b) for a, b in zip(x[3:], y[3:])))
            bone_error = max(bone_error, position_error, rotation_error)
    morph_error = max(abs(a-b) for row in curve_rows[1:] for a, b in zip(curve_rows[0], row))
    assert bone_error < 1e-4 and morph_error < 1e-5, (bone_error, morph_error)
    animation = u.load_asset(cfg['animation'])
    expected_curves = {i: u.AnimationLibrary.get_float_value_at_time(animation, name, frame/30.)
                       for i, name in enumerate(morphs)
                       if u.AnimationLibrary.does_curve_exist(animation, name, u.RawCurveTrackTypes.RCT_FLOAT)}
    options = u.AnimPoseEvaluationOptions()
    options.set_editor_property('evaluation_type', u.AnimDataEvalType.COMPRESSED)
    options.set_editor_property('optional_skeletal_mesh', actor.skeletal_mesh_component.get_editor_property('skeletal_mesh_asset'))
    expected_pose = u.AnimPoseExtensions.get_anim_pose_at_time(animation, frame/30., options)
    expected_runtime = {i: u.AnimPoseExtensions.get_curve_weight(expected_pose, morphs[i]) for i in expected_curves}
    source_curve_error = max((abs(curve_rows[0][i]-v) for i, v in expected_runtime.items()), default=0.)
    raw_source_curve_error = max((abs(curve_rows[0][i]-v) for i, v in expected_curves.items()), default=0.)
    (here/('pose_debug_'+str(frame)+'_'+actor.get_actor_label()+'.json')).write_text(json.dumps({
        'frame': frame, 'instances': [i.get_class().get_name() for i in instances],
        'player_time': str(player.get_current_time()), 'source_curve_max_error': source_curve_error,
        'curve_mismatches': [{'name': morphs[i], 'actual': curve_rows[0][i], 'expected': v}
                             for i, v in expected_runtime.items() if abs(curve_rows[0][i]-v) > 1e-4]}, indent=2), encoding='utf-8')
    assert source_curve_error < 1e-4, ('Animation source curve mismatch', frame, source_curve_error)
    start = actor.get_actor_location()
    before = [c.get_world_location() for c in components]
    offset = u.Vector(17, 23, 11)
    actor.set_actor_location(start+offset, False, False)
    moved = [c.get_world_location() for c in components]
    actor.set_actor_location(start, False, False)
    movement_error = max(abs(v) for a, b in zip(before, moved) for v in ((b-a-offset).x, (b-a-offset).y, (b-a-offset).z))
    assert movement_error < 1e-5
    face_values = {r['original']: curve_rows[0][i] for i, r in enumerate(manifest['morphs'])
                   if not r['original'].startswith('mmd_sdef') and abs(curve_rows[0][i]) > .001}
    return {'actor': actor.get_path_name(), 'bones': len(bones), 'morphs': len(morphs),
            'bone_component_transform_max_error': bone_error, 'morph_curve_max_error': morph_error,
            'maximum_morph_value': max(abs(v) for v in curve_rows[0]), 'active_face_curves': face_values,
            'source_curve_count': len(expected_curves), 'source_curve_max_error': source_curve_error,
            'raw_source_curve_max_error': raw_source_curve_error,
            'bone_pose_sha256': hashlib.sha256(json.dumps(transform_rows[0]).encode()).hexdigest(),
            'three_layer_movement_max_error_cm': movement_error, 'independent_instances': True}


def finish(error=None):
    u.unregister_slate_post_tick_callback(state['handle'])
    # Even a failed refresh must leave a diagnostic and release the test process.
    for mode, actor in actors.items():
        actor.set_actor_hidden_in_game(mode != 'role')
    result = {'status': 'failed' if error else 'captured_needs_user_review', 'error': error,
              'map': world.get_path_name(), 'sequence': sequence.get_path_name(),
              'animation_samples': state['samples'], 'captures': state['captures']}
    (here/cfg.get('receipt_name', 'capture_receipt.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('CEL_CAPTURE_FINISHED', result['status'])
    if not error:
        actor_sub.destroy_actor(playback_actor)
        assert level_sub.save_current_level()
    u.EditorPythonScripting.set_keep_python_script_alive(False)


def tick(delta):
    try:
        if time.monotonic()-state['started'] > 600:
            raise TimeoutError('Cel capture exceeded ten minutes')
        if time.monotonic() < state['ready']:
            return
        if state['index'] >= len(jobs):
            finish()
            return
        job = jobs[state['index']]
        if state['phase'] == 'setup':
            set_frame(job['frame'])
            if job['kind'] == 'capture':
                for mode, actor in actors.items():
                    actor.set_actor_hidden_in_game(mode != job['mode'])
                actor = actors[job['mode']]
                head = actor.skeletal_mesh_component.get_socket_location(head_bone)
                origin, extent = actor.get_actor_bounds(False)
                height = max(80., extent.z*2)
                target = u.Vector(head.x, head.y, head.z-height*.42) if job['view']=='body_front' else head
                distance = height*3.1 if job['view']=='body_front' else height*.9
                # The fixture's FBX faces UE +X; -Y is its side view.
                axis = (0, -1, 0) if job['view']=='head_side' else (-1, 0, 0) if job['view']=='head_back' else (1, 0, 0)
                location = target+u.Vector(*(v*distance for v in axis))
                camera.set_actor_location(location, False, False)
                camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location, target), False)
                job['actual_camera_location'] = [location.x, location.y, location.z]
                job['actual_camera_rotation'] = camera.get_actor_rotation().export_text()
                job['head_location'] = [head.x, head.y, head.z]
                (here/'capture_state.json').write_text(json.dumps(job, indent=2), encoding='utf-8')
            state.update(phase='warm', ready=time.monotonic()+5)
            return
        if state['phase'] == 'warm':
            refresh_poses()
            if job['kind'] == 'animation':
                state['samples'].append({'frame': job['frame'], 'characters': [pose_sample(a, job['frame']) for a in actors.values()]})
                state.update(index=state['index']+1, phase='setup')
                return
            path = here/(job['mode']+'_'+job['view']+'_native.png')
            state['task'] = u.AutomationLibrary.take_high_res_screenshot(1920, 1080, str(path), camera=camera,
                                                                       delay=2., force_game_view=True)
            state.update(phase='shot', path=str(path))
            return
        if not state['task'].is_task_done():
            return
        assert Path(state['path']).is_file(), state['path']
        state['captures'].append(dict(job, path=state['path'], width=1920, height=1080))
        print('CEL_CAPTURE_READY', state['path'])
        state.update(index=state['index']+1, phase='setup')
    except Exception:
        finish(traceback.format_exc())


state['handle'] = u.register_slate_post_tick_callback(tick)
u.EditorPythonScripting.set_keep_python_script_alive(True)
print('CEL_CAPTURE_STARTED', world.get_path_name())
