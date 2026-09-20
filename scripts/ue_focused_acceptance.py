"""Import focused MyGO fixture and capture its real UE rendering."""
import sys
import traceback
from pathlib import Path
import unreal as u
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mmd_bridge.ue_host import import_job
from mmd_bridge.core import atomic_json, read_json

job = Path((root/'work/upstream58/focused_job.txt').read_text(encoding='utf-8').strip())
result = {'passed': False}
try:
    import_job(str(job))
    report = read_json(job/'ue_report.json')
    result['report'] = str(job/'ue_report.json')
    result['failures'] = report.get('failures', [])
    result['usd'] = report.get('checks', {}).get('usd_stage')
    if result['failures'] or not result['usd'] or not result['usd']['generated_static_meshes']:
        raise RuntimeError('材质或 USD 导入未通过，不能继续渲染验收')
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    actors = u.get_editor_subsystem(u.EditorActorSubsystem)
    sun = actors.spawn_actor_from_class(u.DirectionalLight, u.Vector(0,0,500), u.Rotator(-50,-30,0))
    sun.light_component.set_intensity(5.)
    capture = actors.spawn_actor_from_class(u.SceneCapture2D, u.Vector(200,-200,140))
    component = capture.get_component_by_class(u.SceneCaptureComponent2D)
    target = u.RenderingLibrary.create_render_target2d(world, 960, 640, u.TextureRenderTargetFormat.RTF_RGBA8)
    component.set_editor_property('texture_target', target)
    component.set_editor_property('capture_source', u.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    component.set_editor_property('capture_every_frame', False)
    component.set_editor_property('fov_angle', 65.)
    result['captures'] = []
    for name, location, focus in [('mygo_near.png', u.Vector(150,-150,120), u.Vector(0,0,10)),
                                  ('mygo_far.png', u.Vector(-4100,-3900,18000), u.Vector(-4100,-3900,0))]:
        capture.set_actor_location(location, False, False)
        capture.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location, focus), False)
        component.capture_scene()
        u.RenderingLibrary.export_render_target(world, target, str(root/'work/upstream58'), name)
        result['captures'].append(name)
    result['passed'] = True
except Exception:
    result['error'] = traceback.format_exc()
atomic_json(root/'work/upstream58/focused_ue_result.json', result)
