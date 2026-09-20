"""UE 5.8 native API regression in a disposable workspace project."""
import os
import sys
import traceback
from pathlib import Path
import unreal as u
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ['CAMCUT_NO_AUTORUN'] = '1'
from mmd_bridge import ue_camera_cut_baker as cut
from mmd_bridge.ue_materials import Materials
from mmd_bridge.core import atomic_json

report = {'engine': u.SystemLibrary.get_engine_version(), 'checks': [], 'warnings': []}
try:
    u.get_editor_subsystem(u.LevelEditorSubsystem).new_level('/Game/Regression/Map')
    for fps in (30, 60):
        seq = u.AssetToolsHelpers.get_asset_tools().create_asset('Camera_'+str(fps), '/Game/Regression', u.LevelSequence, u.LevelSequenceFactoryNew())
        seq.set_display_rate(u.FrameRate(fps, 1))
        seq.set_tick_resolution_directly(u.FrameRate(24000, 1))
        seq.set_playback_start(0); seq.set_playback_end(60)
        config, _ = cut.load_config()
        config['USE_SPAWNABLE'] = False
        context = cut.Context(config); context.sequence = seq
        context.display_rate = (fps, 1); context.source_profile = {'projection': ['persp', 'ortho']}
        cuts = seq.add_track(u.MovieSceneCameraCutTrack)
        for index, (start, end) in enumerate(((0, 30), (30, 60))):
            record = cut.create_shot_camera(context, 'Native_'+str(index))
            cut.create_lens_tracks(context, record, start, end, context.source_profile)
            for frame in range(start, end):
                sample = cut.Sample(frame, frame, 0., frame, 0., False)
                state = {'projection': 'ortho' if index else 'persp', 'field_of_view': 45., 'ortho_width': 512.}
                cut.write_lens_keys(context, record, sample, state, u.MovieSceneKeyInterpolation.CONSTANT, (24000,1), (fps,1))
            section = cuts.add_section(); section.set_range(start, end)
            section.set_camera_binding_id(seq.get_binding_id(record['binding']))
            assert record['ordinary_camera']
            assert len(record['lens_tracks']['ProjectionMode']['channel'].get_keys()) == 30
        layout = cut.existing_cut_layout(seq)
        assert layout['contiguous'] and layout['distinct_cameras'] == 2
        report['checks'].append({'fps': fps, 'ordinary_camera_tracks': True, 'existing_cut_guard': layout})
    normal_path = next((root/'work/upstream58/mygo').rglob('Bake_normal_*.png'))
    material = Materials('/Game/Regression', 'regression', report, {'use_scene_master_material': False})
    record = {'id': 'uv1normal', 'name': 'UV1_Normal', 'asset_name': 'M_UV1_Normal', 'surface': 'pbr',
              'strategy': 'baked', 'channels': {'base_color': {'op':'constant','value':[.4,.4,.4]},
              'normal': {'op':'texture','normal_map':True,'extension':'EXTEND','uv':{'op':'uv','index':1},
              'image':{'id':'regressionnormal','path':str(normal_path),'color_space':'Non-Color'}}}}
    material.build(record, root/'work/upstream58/material_cache')
    report['checks'].append({'uv1_normal_material': True})
    report['passed'] = True
except Exception:
    report.update(passed=False, error=traceback.format_exc())
atomic_json(root/'work/upstream58/ue58_regression.json', report)
