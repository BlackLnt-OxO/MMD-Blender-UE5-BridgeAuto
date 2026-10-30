"""Sparse Sequencer frame-rate upgrade: hold camera jumps and move existing keys.

No actor, animation, or per-frame camera baking. Tick times remain authoritative.
"""
from __future__ import annotations

import datetime
import json
from fractions import Fraction

try:
    import unreal as u
except ImportError:
    u = None

VERSION = '1'


def rate(value):
    if isinstance(value, (list, tuple)):
        return Fraction(*value)
    if hasattr(value, 'numerator') and hasattr(value, 'denominator'):
        return Fraction(value.numerator, value.denominator)
    return Fraction(value)


def jump_plan(cut_frames, display_rate, tick_rate, original_fps=30, target_fps=60):
    """Cut is the first new pose. Only the preceding original frame is delayed."""
    display, ticks, original, target = map(rate, (display_rate, tick_rate, original_fps, target_fps))
    if min(display, ticks, original, target) <= 0 or target <= original:
        raise ValueError('目标帧率必须高于原始关键帧率')
    rows = []
    for frame in sorted(set(cut_frames)):
        cut = Fraction(frame) * ticks / display
        prior, delayed = cut - ticks / original, cut - ticks / target
        if any(t.denominator != 1 for t in (cut, prior, delayed)):
            raise ValueError(f'切点 {frame} 不能由当前 tick resolution 精确表示')
        rows.append({'cut_frame': frame, 'cut_tick': int(cut), 'prior_tick': int(prior),
                     'delayed_tick': int(delayed), 'target_cut_frame': float(cut / ticks * target),
                     'target_prior_frame': float(prior / ticks * target),
                     'target_delayed_frame': float(delayed / ticks * target)})
    return rows


def plan_key_moves(existing_ticks, jumps):
    """No missing-key insertion, neighbour deletion, or collision overwriting."""
    occupied = set(existing_ticks)
    moves = []
    for row in jumps:
        old, new = row['prior_tick'], row['delayed_tick']
        if old not in occupied:
            continue
        if new in occupied and new != old:
            raise ValueError(f'关键帧冲突：tick {old} 的目标 tick {new} 已有关键帧')
        moves.append((old, new))
    if len({new for old, new in moves}) != len(moves):
        raise ValueError('多个原关键帧指向同一目标时间')
    return moves


def _guid(binding):
    return binding.get_id().export_text()


def camera_bindings(sequence, world):
    cut_tracks = [t for t in sequence.get_tracks() if isinstance(t, u.MovieSceneCameraCutTrack)]
    sections = [s for t in cut_tracks for s in t.get_sections()]
    if len(sections) > 1:
        raise ValueError('Frame Rate Upgrade 需要未切分序列；请打开切分前的备份，现有分镜会保留')
    if not sections:
        raise ValueError('请先给原相机设置覆盖播放范围的一段 Camera Cut；升帧工具不会创建或切分相机段')
    selected = sections[0].get_camera_binding_id().get_editor_property('guid').export_text() if sections else None
    bindings = list(sequence.get_bindings())
    candidates = []
    parents = {}
    for b in bindings:
        parent = b.get_parent()
        if parent and parent.is_valid():
            parents[_guid(b)] = _guid(parent)
        if not any(isinstance(t, u.MovieScene3DTransformTrack) for t in b.get_tracks()):
            continue
        if any(isinstance(o, u.CameraActor) for o in sequence.locate_bound_objects(b, world)):
            candidates.append(b)
    selected_bindings = [b for b in candidates if _guid(b) in (selected, parents.get(selected))]
    if len(selected_bindings) != 1:
        raise ValueError('Camera Cut 未明确绑定可编辑的动画相机；请修复绑定后重试，不会猜选其它相机')
    root = selected_bindings[0]
    group = [root]
    for b in bindings:
        parent = b.get_parent()
        if parent and parent.is_valid() and _guid(parent) == _guid(root):
            group.append(b)
    return root, group


def _absolute(section):
    return 'Absolute' in section.get_blend_type().export_text()


def _channels(bindings):
    for binding in bindings:
        for track in binding.get_tracks():
            for section in track.get_sections():
                for channel in section.get_all_channels():
                    yield binding, track, section, channel


def _ticks(key):
    time = key.get_time(time_unit=u.MovieSceneTimeUnit.TICK_RESOLUTION)
    if abs(time.sub_frame) > 1e-6:
        raise ValueError('关键帧包含不能精确移动的分数 tick')
    return time.frame_number.value


def detect(sequence, world, baker, original_fps=30):
    """Read native channels at the original cadence, including already-60fps files."""
    root, group = camera_bindings(sequence, world)
    transform = [s for t in root.get_tracks() if isinstance(t, u.MovieScene3DTransformTrack)
                 for s in t.get_sections() if _absolute(s)]
    if not transform:
        raise ValueError('相机没有可读的原始变换轨道')
    base = max(transform, key=lambda s: max((len(c.get_keys()) for c in s.get_all_channels()), default=0))
    channels = list(base.get_all_channels())[:6]
    display = rate(sequence.get_display_rate())
    sample_range = sequence.make_range(sequence.get_playback_start(), sequence.get_playback_end() - sequence.get_playback_start())
    values = [list(ch.evaluate_keys(sample_range, u.FrameRate(original_fps, 1))) for ch in channels]
    focal = None
    for b, t, s, ch in _channels(group):
        if isinstance(t, u.MovieSceneFloatTrack) and str(t.get_property_name()) == 'CurrentFocalLength' and _absolute(s) and ch.get_keys():
            focal = list(ch.evaluate_keys(sample_range, u.FrameRate(original_fps, 1)))
            break
    count = min(map(len, values))
    if count < 2:
        return {'candidates': [], 'details': [], 'probe_count': count, 'original_fps': original_fps}
    positions, rotations, focals = [], [], []
    previous = None
    for i in range(count):
        positions.append(tuple(v[i] for v in values[:3]))
        current = baker.unwrap_rotator(previous, tuple(v[i] for v in values[3:6]))
        rotations.append(current)
        previous = current
        focals.append(focal[i] if focal is not None and i < len(focal) else 0.)
    pos = [baker.vector_delta(a, b) for a, b in zip(positions, positions[1:])]
    rot = [baker.rotator_delta(a, b) for a, b in zip(rotations, rotations[1:])]
    lens = [abs(a - b) for a, b in zip(focals, focals[1:])]
    cfg = {name: getattr(baker, name) for name in ('DETECT_WINDOW', 'DETECT_ABS_POS_CM', 'DETECT_ABS_ROT_DEG',
        'DETECT_ABS_FOCAL_MM', 'DETECT_REL_POS', 'DETECT_REL_ROT', 'DETECT_REL_FOCAL', 'DETECT_SUPPRESS')}
    start = int(Fraction(sequence.get_playback_start()) / display * original_fps)
    hits = baker.detect_candidates(pos, rot, lens, start, cfg)
    details = []
    for frame, score, detail in hits:
        native_frame = Fraction(frame) * display / original_fps
        if native_frame.denominator != 1:
            raise ValueError('跳切位置不是整数显示帧，请先设置可精确表示原帧率的显示帧率')
        details.append({'frame': int(native_frame), 'score': score, 'detail': detail})
    return {'candidates': [d['frame'] for d in details], 'details': details, 'probe_count': count,
            'original_fps': original_fps, 'display_rate': [display.numerator, display.denominator],
            'camera_binding': _guid(root), 'read_only': True}


def _actions(sequence, world, cut_frames, original_fps, target_fps):
    root, group = camera_bindings(sequence, world)
    jumps = jump_plan(cut_frames, rate(sequence.get_display_rate()), rate(sequence.get_tick_resolution()), original_fps, target_fps)
    holds, moves, skipped = [], [], []
    camera_ids = {_guid(b) for b in group}
    for binding, track, section, channel in _channels(sequence.get_bindings()):
        keys = list(channel.get_keys())
        if not keys:
            continue
        by_tick = {_ticks(k): k for k in keys}
        label = f'{binding.get_name()} / {track.get_display_name()} / {channel.get_name()}'
        if _guid(binding) in camera_ids:
            is_base = isinstance(track, u.MovieScene3DTransformTrack) or (isinstance(track, u.MovieSceneFloatTrack) and str(track.get_property_name()) == 'CurrentFocalLength')
            if not is_base or not _absolute(section):
                continue
            for row in jumps:
                if row['prior_tick'] in by_tick and row['cut_tick'] in by_tick:
                    holds.append((by_tick[row['prior_tick']], label, row))
                elif row['cut_tick'] > max(by_tick):
                    skipped.append({'channel': label, 'cut': row['cut_frame'], 'reason': 'outside original camera key range'})
                else:
                    raise ValueError(f'{label} 的切点 {row["cut_frame"]} 缺少原始前后帧，未补键')
        else:
            try:
                for old, new in plan_key_moves(by_tick, jumps):
                    moves.append((by_tick[old], label, old, new))
            except ValueError as error:
                raise ValueError(f'{label}: {error}') from error
    return jumps, holds, moves, skipped


def _unique(path):
    candidate = path
    number = 2
    while u.EditorAssetLibrary.does_asset_exist(candidate):
        candidate = f'{path}_{number}'
        number += 1
    return candidate


def _key_count(sequence):
    return sum(len(ch.get_keys()) for b, t, s, ch in _channels(sequence.get_bindings()))


def _cut_layout(sequence):
    return [(s.get_start_frame(), s.get_end_frame(), s.get_camera_binding_id().export_text())
            for t in sequence.get_tracks() if isinstance(t, u.MovieSceneCameraCutTrack) for s in t.get_sections()]


def protect_bounded_sections(sequence):
    helper = getattr(u, 'MMDBridgeViewportLibrary', None)
    fn = getattr(helper, 'protect_bounded_sequence_sections', None) if helper else None
    if fn is None:
        raise ValueError('Frame Rate Upgrade 需要新版 MMDBridgeViewport 原生插件；请安装本批桥接插件并重启 UE，以保留片尾定格区间的 MRQ 行为')
    count = fn(sequence)
    if count < 0:
        raise ValueError('无法保护 MRQ 有限区间，未完成升帧')
    return count


def set_frame_open(configuration, target_fps):
    configuration.find_or_add_setting_by_class(u.MoviePipelineCameraSetting).set_editor_property('shutter_timing', u.MoviePipelineShutterTiming.FRAME_OPEN)
    output = configuration.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.set_editor_property('use_custom_frame_rate', True)
    output.set_editor_property('output_frame_rate', u.FrameRate(target_fps, 1))


def upgrade(sequence, world, cut_frames, original_fps=30, target_fps=60):
    if u.EditorAssetLibrary.get_metadata_tag(sequence, 'FrameRateUpgradeVersion'):
        raise ValueError('当前序列已经升帧，未再次移动关键帧；请从原序列重新创建副本')
    # Preflight every destination before creating or modifying any result.
    jumps, holds, moves, skipped = _actions(sequence, world, cut_frames, original_fps, target_fps)
    if cut_frames and not holds:
        raise ValueError('没有找到可处理的原始相机跳切键，未生成结果')
    source = sequence.get_path_name().split('.')[0]
    parent = source.rsplit('/', 1)[0]
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = _unique(f'{parent}/_FrameRateBackups/{sequence.get_name()}_{stamp}')
    backup = u.EditorAssetLibrary.duplicate_asset(source, backup_path)
    if not backup or not u.EditorAssetLibrary.save_loaded_asset(backup, False):
        raise ValueError('序列备份失败，未改动源序列')
    output_path = _unique(f'{parent}/{sequence.get_name()}_{target_fps}fps')
    result = u.EditorAssetLibrary.duplicate_asset(source, output_path)
    if result is None:
        raise ValueError('不能创建升帧副本')
    before = _key_count(result)
    cut_layout_before = _cut_layout(result)
    jumps, holds, moves, skipped = _actions(result, world, cut_frames, original_fps, target_fps)
    bounded = protect_bounded_sections(result)
    for key, label, row in holds:
        key.set_interpolation_mode(u.RichCurveInterpMode.RCIM_CONSTANT)
    for key, label, old, new in moves:
        key.set_time(u.FrameNumber(new), time_unit=u.MovieSceneTimeUnit.TICK_RESOLUTION)
    assert all(key.get_interpolation_mode() == u.RichCurveInterpMode.RCIM_CONSTANT for key, label, row in holds)
    assert all(_ticks(key) == new for key, label, old, new in moves), '已有物体关键帧移动后回读失败'
    result.set_display_rate(u.FrameRate(target_fps, 1))
    assert _key_count(result) == before, '升帧不能增加或删除关键帧'
    # Display-frame ranges scale with fps; the serialized camera cut ticks do not.
    old_rate = rate(sequence.get_display_rate())
    cut_layout_after = _cut_layout(result)
    assert len(cut_layout_after) == len(cut_layout_before), '不能生成或切分 Camera Cuts'
    assert all(Fraction(a[0]) / old_rate == Fraction(b[0], target_fps) and
               Fraction(a[1]) / old_rate == Fraction(b[1], target_fps) and a[2] == b[2]
               for a, b in zip(cut_layout_before, cut_layout_after)), '原 Camera Cut 绑定或时长改变'
    info = {'source': source, 'sequence': result.get_path_name(), 'backup': backup.get_path_name(),
        'original_fps': original_fps, 'target_fps': target_fps, 'jumps': jumps,
        'hold_keys': len(holds), 'moved_keys': [{'channel': label, 'from_tick': old, 'to_tick': new} for key, label, old, new in moves],
        'skipped_camera_ranges': skipped, 'key_count_before': before, 'key_count_after': _key_count(result),
        'new_actors': 0, 'new_camera_cut_sections': 0, 'bounded_sections_protected': bounded,
        'mrq_shutter_timing': 'FRAME_OPEN'}
    u.EditorAssetLibrary.set_metadata_tag(result, 'FrameRateUpgradeVersion', VERSION)
    u.EditorAssetLibrary.set_metadata_tag(result, 'FrameRateUpgradeReport', json.dumps(info, ensure_ascii=False))
    if not u.EditorAssetLibrary.save_loaded_asset(result, False):
        raise ValueError('升帧副本保存失败')
    # Update only jobs referring to the source. Saved user queue assets are never overwritten.
    subsystem = u.get_editor_subsystem(u.MoviePipelineQueueSubsystem)
    jobs_changed = []
    presets = []
    for job in subsystem.get_queue().get_jobs():
        if source + '.' + sequence.get_name() not in job.get_editor_property('sequence').export_text():
            continue
        if str(job.get_editor_property('job_name')) == sequence.get_name():
            job.set_editor_property('job_name', result.get_name())
        job.set_editor_property('sequence', u.SoftObjectPath(result.get_path_name()))
        set_frame_open(job.get_configuration(), target_fps)
        jobs_changed.append(job.get_path_name())
        preset_path = _unique(f'{parent}/MRQ_{result.get_name()}')
        preset_folder, preset_name = preset_path.rsplit('/', 1)
        preset = u.AssetToolsHelpers.get_asset_tools().create_asset(preset_name, preset_folder,
            u.MoviePipelinePrimaryConfig, u.MoviePipelinePrimaryConfigFactory())
        if preset is None:
            raise ValueError('升帧序列已保存，但独立 MRQ 预设创建失败')
        preset.copy_from(job.get_configuration())
        if not u.EditorAssetLibrary.save_loaded_asset(preset, False):
            raise ValueError('升帧序列已保存，但独立 MRQ 预设保存失败')
        presets.append(preset.get_path_name())
    info['mrq_jobs_updated'] = jobs_changed
    info['mrq_presets'] = presets
    info['verification'] = {'pass': True, 'scope': 'sparse key times and camera section structure; native MRQ visual review remains separate'}
    return result, info
