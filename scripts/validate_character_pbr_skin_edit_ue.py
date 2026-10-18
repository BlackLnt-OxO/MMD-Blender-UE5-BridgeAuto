"""Read saved profile values and verify the UI's one-slot water apply/undo route."""
import os
import sys
from pathlib import Path
import unreal as u

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.ue_character_pbr_edit import apply, undo

folder = Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
receipt = read_json(folder/'ue_receipt.json')
manifest = read_json(folder/'manifest.json')
editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
actors = u.get_editor_subsystem(u.EditorActorSubsystem)
assert receipt['status'] == 'ue_assets_created', receipt.get('error')
assert editor.load_level(receipt['render_assets']['level'])


def current():
    actor = next(a for a in actors.get_all_level_actors() if isinstance(a, u.SkeletalMeshActor))
    actors.set_selected_level_actors([actor])
    assert list(actors.get_selected_level_actors()) == [actor], 'Use editor ExecutePythonScript mode'
    return actor.skeletal_mesh_component


def bindings(component):
    return [m.get_path_name() for m in component.get_materials()]


component = current()
before = bindings(component)
ledger_path = Path(u.Paths.project_saved_dir())/'MMDBridge/character_pbr_undo.json'
previous_ledger = ledger_path.read_bytes() if ledger_path.is_file() else None
if previous_ledger is not None:
    (folder/'undo_ledger_before_validation.json').write_bytes(previous_ledger)
face = next(r for r in manifest['materials'] if r['name'] == 'Face')
slot = next(i for i, path in enumerate(before) if path == receipt['assets']['materials'][face['id']])
lib = u.MaterialEditingLibrary
report = {'profile_readback': [], 'operations': [], 'slot': slot}
mesh = component.get_editor_property('skeletal_mesh_asset')
source_mesh = u.load_asset(receipt['source_mesh'])
geometry = {}
for label, item in (('source', source_mesh), ('candidate', mesh)):
    settings = u.SkeletalMeshEditorSubsystem.get_lod_build_settings(item, 0)
    geometry[label] = {key: settings.get_editor_property(key) for key in ('recompute_normals', 'recompute_tangents')}
assert geometry['source'] == geometry['candidate']
assert mesh.get_editor_property('skeleton') == source_mesh.get_editor_property('skeleton')
report['mesh_build_settings'] = geometry
report['target_bindings'] = []
for record in manifest['materials']:
    if record['character_material']['pbr']['type'] in ('face', 'skin', 'cloth'):
        path = receipt['assets']['materials'][record['id']]
        assert path in before, 'Skin/cloth slot retained a previous material: '+record['name']
        report['target_bindings'].append({'name': record['name'], 'material': path})
report['active_materials'] = before
for record in manifest['materials']:
    if record['character_material']['pbr']['type'] not in ('face', 'skin'):
        continue
    material = u.load_asset(receipt['assets']['materials'][record['id']])
    profile = material.get_editor_property('parent').get_editor_property('subsurface_profile')
    settings = profile.get_editor_property('settings')
    distance = settings.get_editor_property('mean_free_path_distance')
    expected = .18 if record['character_material']['pbr']['type'] == 'face' else .32
    assert settings.get_editor_property('enable_burley') and settings.get_editor_property('enable_mean_free_path')
    assert abs(distance-expected) < 1e-5, (distance, expected)
    assert lib.get_material_instance_scalar_parameter_value(material, 'EmissiveStrength') == 0.
    if record['character_material']['pbr']['type'] == 'face':
        assert lib.get_material_instance_scalar_parameter_value(material, 'SSSDetailPreservation') == 1.
        if 'sss_mask' in record['character_material']['pbr']['textures']:
            assert lib.get_material_instance_scalar_parameter_value(material, 'Use_sss_mask') == 1.
    water_normal = lib.get_material_instance_texture_parameter_value(material, 'wet_normal_Tex')
    assert not water_normal.get_editor_property('srgb')
    assert water_normal.get_editor_property('flip_green_channel')
    report['profile_readback'].append({'name': record['name'], 'mean_free_path_cm': distance,
                                      'saved_flags': True, 'water_normal_is_data': True})

for enabled in (1., 0.):
    cfg = {'character_material_overrides': {manifest['source_sha256']: {
        str(face['pmx_index']): {'params': {'WetEnabled': enabled, 'WetAmount': .85}}}}}
    result = apply({'model': manifest['model'], 'model_sha256': manifest['source_sha256'],
                    'pmx_index': face['pmx_index'], 'settings': cfg})
    assert not result['report']['failures'], result['report']['failures']
    after = bindings(component)
    assert [i for i, (a, b) in enumerate(zip(before, after)) if a != b] == [slot]
    material = component.get_material(slot)
    assert lib.get_material_instance_scalar_parameter_value(material, 'WetEnabled') == enabled
    assert lib.get_material_instance_scalar_parameter_value(material, 'Use_sss_mask') == 1.
    tiling = lib.get_material_instance_scalar_parameter_value(material, 'WetTiling')
    assert abs(tiling-face['character_material']['pbr']['params']['WetTiling']) < 1e-4
    assert editor.load_level(receipt['render_assets']['level'])
    component = current()
    assert lib.get_material_instance_scalar_parameter_value(component.get_material(slot), 'WetEnabled') == enabled
    undone = undo()
    assert undone['count'] == 1 and bindings(component) == before
    report['operations'].append({'enabled': enabled, 'apply': result['count'],
                                 'undo': undone['count'], 'saved_reloaded': True,
                                 'measured_uv_scale_preserved': True})

assert editor.load_level(receipt['render_assets']['level'])
assert bindings(current()) == before
protected = read_json(folder/'protected_asset_hashes.json')
changed = [path for path, digest in protected.items() if file_hash(path) != digest]
assert not changed, changed
report['previous_versions_and_project_preserved_files'] = len(protected)
report['source_preserved'] = file_hash(manifest['model']) == manifest['source_sha256']
assert report['source_preserved']
atomic_json(folder/'validation_undo_ledger.json', read_json(ledger_path))
if previous_ledger is None:
    ledger_path.unlink()
else:
    ledger_path.write_bytes(previous_ledger)
report['prior_undo_ledger_restored'] = True
atomic_json(folder/'skin_edit_receipt.json', report)
print('PBR_WATER_APPLY_UNDO_COMPLETE', slot, len(protected))
