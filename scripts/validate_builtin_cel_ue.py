"""Native first-character fixture. All created assets stay under a new root.

Run with -run=pythonscript -script=<this> -CelValidationConfig=<json>.
The config supplies manifest, mesh, animation, output root and receipt paths.
"""
from pathlib import Path
import copy
import json
import sys

import unreal as u

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from mmd_bridge.character_cel import plan, reference
from mmd_bridge.core import file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_cel import build, blueprint

args = u.SystemLibrary.parse_command_line(u.SystemLibrary.get_command_line())[2]
cfg = json.loads(Path(args['CelValidationConfig']).read_text(encoding='utf-8'))
here = Path(cfg['output'])
manifest = json.loads(Path(cfg['manifest']).read_text(encoding='utf-8'))
digest = file_hash(cfg['model'])
assert digest == cfg['expected_model_sha256'], 'The source PMX changed since the validated export'
source_mesh = u.load_asset(cfg['mesh'])
animation = u.load_asset(cfg['animation'])
assert source_mesh and animation
root = cfg['asset_root']
assert not u.EditorAssetLibrary.does_directory_exist(root), 'Preserve the prior fixture; choose a new root'
ref = reference()
reports = {}
actor_sub = u.get_editor_subsystem(u.EditorActorSubsystem)
level_sub = u.get_editor_subsystem(u.LevelEditorSubsystem)
assert level_sub.new_level(root+'/Maps/CelComparison')
world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
sequence = u.AssetToolsHelpers.get_asset_tools().create_asset('LS_CelComparison', root+'/Sequences', u.LevelSequence, u.LevelSequenceFactoryNew())
sequence.set_display_rate(u.FrameRate(30, 1))
sequence.set_playback_start(0)
sequence.set_playback_end(6062)

for mode in ('neutral', 'role'):
    settings = {'character_backend': 'mmdbridge_fbx', 'character_material_profile': 'builtin_cel',
                'character_cel_color_mode': mode}
    report = {'warnings': [], 'failures': []}
    m = Materials(root+'/'+mode, 'builtin_cel_20261028_'+mode, report, settings)
    mesh = u.EditorAssetLibrary.duplicate_asset(source_mesh.get_path_name(), root+'/'+mode+'/Character/SK_CelMiku')
    assert mesh
    m.managed(mesh)
    records = copy.deepcopy(manifest['materials'])
    for record in records:
        note = record['character_material']
        note.pop('pbr', None)
        note.update(profile='builtin_cel', cel=plan(record, settings, digest, ref))
        note['role'] = note['cel']['role']
    by_slot = {r['slot_name']: r for r in records}
    slots = list(mesh.materials)
    for slot in slots:
        name = str(slot.get_editor_property('imported_material_slot_name'))
        slot.set_editor_property('material_interface', build(m, by_slot[name]))
    mesh.set_editor_property('materials', slots)
    assert u.EditorAssetLibrary.save_loaded_asset(mesh, only_if_is_dirty=False)
    m.protect_asset(mesh)
    bp = blueprint(m, mesh, records, animation)
    actor = actor_sub.spawn_actor_from_class(bp.generated_class(), u.Vector())
    actor.set_actor_label('Cel_'+('A_Neutral' if mode == 'neutral' else 'B_Role'))
    actor.tags = [u.Name('MMDBridgeCelFixture')]
    # Both characters share the same location. Only one is visible per capture.
    actor.set_actor_hidden_in_game(mode != 'role')
    actor_binding = sequence.add_possessable(actor)
    for target in (actor_binding, *[sequence.add_possessable(c) for c in (actor.filter_mesh, actor.outline_mesh)]):
        if target != actor_binding:
            target.set_parent(actor_binding)
        section = target.add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
        section.set_range(0, 6062)
        params = section.get_editor_property('params')
        params.animation = animation
        params.force_custom_mode = True
        section.set_editor_property('params', params)
    checks = []
    for index, slot in enumerate(slots):
        r = by_slot[str(slot.get_editor_property('imported_material_slot_name'))]
        f = actor.filter_mesh.get_material(index)
        assert u.MaterialEditingLibrary.get_material_instance_static_switch_parameter_value(f, 'Brush?') is False
        assert u.MaterialEditingLibrary.get_material_instance_static_switch_parameter_value(f, '启用边缘光') is False
        actual_base = u.MaterialEditingLibrary.get_material_instance_texture_parameter_value(f, 'PMXBaseTex')
        actual_cap = u.MaterialEditingLibrary.get_material_instance_texture_parameter_value(f, 'MatCapMap')
        assert actual_base
        checks.append({'index': index, 'slot': str(slot.get_editor_property('imported_material_slot_name')), 'name': r['name'],
                       'base': actual_base.get_path_name(), 'matcap': actual_cap.get_path_name(),
                       'plan': r['character_material']['cel']})
    origin, extent = actor.get_actor_bounds(False)
    report.update(blueprint=bp.get_path_name(), mesh=mesh.get_path_name(), actor=actor.get_path_name(),
                  bounds_origin=[origin.x, origin.y, origin.z], bounds_extent=[extent.x, extent.y, extent.z],
                  checks_by_slot=checks)
    reports[mode] = report

# A neutral background makes black fabric and black outlines inspectable.
# This belongs only to the comparison fixture, never to imported scene defaults.
background = m.create('M_CelComparisonBackground', u.Material, u.MaterialFactoryNew())
previous = m.material
m.material = background
background.set_editor_property('shading_model', u.MaterialShadingModel.MSM_UNLIT)
background.set_editor_property('two_sided', True)
gray = m.constant([.06, .06, .06])
m.lib.connect_material_property(gray[0], gray[1], u.MaterialProperty.MP_EMISSIVE_COLOR)
m.lib.recompile_material(background)
assert u.EditorAssetLibrary.save_loaded_asset(background, only_if_is_dirty=False)
m.protect_asset(background)
m.material = previous
backdrop = actor_sub.spawn_actor_from_class(u.StaticMeshActor, u.Vector())
backdrop.set_actor_label('Cel_Comparison_Neutral_Background')
backdrop.static_mesh_component.set_static_mesh(u.load_asset('/Engine/BasicShapes/Sphere'))
backdrop.static_mesh_component.set_material(0, background)
backdrop.static_mesh_component.set_cast_shadow(False)
backdrop.static_mesh_component.set_collision_enabled(u.CollisionEnabled.NO_COLLISION)
backdrop.static_mesh_component.set_editor_property('visible_in_ray_tracing', False)
backdrop.static_mesh_component.set_editor_property('affect_dynamic_indirect_lighting', False)
backdrop.set_actor_scale3d(u.Vector(400, 400, 400))

light = actor_sub.spawn_actor_from_class(u.DirectionalLight, u.Vector(0, 0, 300))
light.set_actor_rotation(u.Rotator(-35, -50, 0), False)
light.light_component.set_editor_property('intensity', 2.5)
light.light_component.set_editor_property('mobility', u.ComponentMobility.MOVABLE)
light.light_component.set_editor_property('atmosphere_sun_light', True)
light.light_component.set_editor_property('atmosphere_sun_light_index', 0)
sky = actor_sub.spawn_actor_from_class(u.SkyLight, u.Vector(0, 0, 300))
sky.light_component.set_editor_property('mobility', u.ComponentMobility.MOVABLE)
sky.light_component.set_editor_property('intensity', .25)
sky.light_component.set_editor_property('source_type', u.SkyLightSourceType.SLS_CAPTURED_SCENE)
post = actor_sub.spawn_actor_from_class(u.PostProcessVolume, u.Vector())
post.set_editor_property('unbound', True)
post_settings = post.get_editor_property('settings')
for name, value in [('override_auto_exposure_method', True), ('auto_exposure_method', u.AutoExposureMethod.AEM_MANUAL),
                    ('override_auto_exposure_bias', True), ('auto_exposure_bias', 0.0),
                    ('override_auto_exposure_apply_physical_camera_exposure', True), ('auto_exposure_apply_physical_camera_exposure', False)]:
    post_settings.set_editor_property(name, value)
post.set_editor_property('settings', post_settings)
assert u.EditorAssetLibrary.save_loaded_asset(sequence, only_if_is_dirty=False)
assert level_sub.save_current_level()
result = {'status': 'native_assets_created_needs_visual_review', 'map': world.get_path_name(),
          'sequence': sequence.get_path_name(), 'source_mesh': cfg['mesh'], 'source_animation': cfg['animation'],
          'source_manifest': cfg['manifest'], 'source_model_sha256': digest, 'reports': reports}
(here/'native_receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print('CEL_NATIVE_FIXTURE_READY', world.get_path_name(), 'slots', len(slots), 'layers', 3)
