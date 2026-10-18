"""Verify saved textile bindings and one-slot hosiery apply/reload/undo."""
from pathlib import Path
import os, sys, json
import unreal as u
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.ue_character_pbr_edit import apply, undo

folder = Path(os.environ['MMDBRIDGE_PBR_FIXTURE'])
receipt, manifest = read_json(folder/'ue_receipt.json'), read_json(folder/'manifest.json')
assert receipt['status'] == 'ue_assets_created', receipt.get('error')
editor = u.get_editor_subsystem(u.LevelEditorSubsystem)
actors = u.get_editor_subsystem(u.EditorActorSubsystem)
assert editor.load_level(receipt['render_assets']['level'])
lib = u.MaterialEditingLibrary


def current():
    actor = next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actors.set_selected_level_actors([actor])
    assert list(actors.get_selected_level_actors()) == [actor]
    return actor.skeletal_mesh_component


def bindings(component):
    return [m.get_path_name() for m in component.get_materials()]


component = current()
mesh = component.get_editor_property('skeletal_mesh_asset')
before = bindings(component)
assert before == [s.material_interface.get_path_name() for s in mesh.materials]
assert len(before) == len(manifest['materials']) == 49
checks = []
for record in manifest['materials']:
    material = u.load_asset(receipt['assets']['materials'][record['id']])
    assert material.get_path_name() in before
    meta = json.loads(u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.PBRRecord'))
    assert meta['character_material']['pbr']['model_sha256'] == manifest['source_sha256']
    pbr = record['character_material']['pbr']
    parent = material.get_editor_property('parent')
    if pbr['type'] in ('cloth','satin','hosiery'):
        assert lib.get_material_instance_scalar_parameter_value(material,'EmissiveStrength') == 0.
        normal = lib.get_material_instance_texture_parameter_value(material,'detail_normal_Tex')
        assert not normal.get_editor_property('srgb') and normal.get_editor_property('flip_green_channel')
        if pbr['type'] == 'cloth' and pbr['alpha_mode'] != 'translucent':
            assert parent.get_editor_property('shading_model') == u.MaterialShadingModel.MSM_CLOTH
        if pbr['type'] in ('satin','hosiery'):
            for key in ('Anisotropy','FabricDirection'):
                assert abs(lib.get_material_instance_scalar_parameter_value(material,key)-pbr['params'][key]) < 1e-4
        if pbr['type'] == 'hosiery':
            assert parent.get_editor_property('blend_mode') == u.BlendMode.BLEND_TRANSLUCENT
            assert parent.get_editor_property('translucency_lighting_mode') == u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING
        checks.append({'name':record['name'],'type':pbr['type'],'saved_normal':normal.get_path_name()})
    if pbr['type'] in ('skin','face'):
        assert lib.get_material_instance_scalar_parameter_value(material,'WetEnabled') == 0.
        profile = parent.get_editor_property('subsurface_profile').get_editor_property('settings')
        assert abs(profile.mean_free_path_distance-(.18 if pbr['type']=='face' else .32)) < 1e-5

stocking = next(r for r in manifest['materials'] if r['character_material']['pbr']['type']=='hosiery')
slot = before.index(receipt['assets']['materials'][stocking['id']])
ledger = Path(u.Paths.project_saved_dir())/'MMDBridge/character_pbr_undo.json'
prior_ledger = ledger.read_bytes() if ledger.is_file() else None
if prior_ledger: (folder/'undo_ledger_before_validation.json').write_bytes(prior_ledger)
operations = []
for density in (.3,.75):
    cfg = {'character_material_overrides':{manifest['source_sha256']:{str(stocking['pmx_index']):{
        'params':{'HosieryCoverage':density}}}}}
    result = apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
                    'pmx_index':stocking['pmx_index'],'settings':cfg})
    assert result['count']==1 and not result['report']['failures']
    assert [i for i,(a,b) in enumerate(zip(before,bindings(component))) if a!=b]==[slot]
    assert editor.load_level(receipt['render_assets']['level'])
    component=current(); material=component.get_material(slot)
    assert abs(lib.get_material_instance_scalar_parameter_value(material,'HosieryCoverage')-density)<1e-5
    for key in ('DetailTiling','FabricDirection'):
        assert abs(lib.get_material_instance_scalar_parameter_value(material,key)-stocking['character_material']['pbr']['params'][key])<1e-4
    undone=undo()
    assert undone['count']==1 and bindings(component)==before
    operations.append({'coverage':density,'apply':1,'save_reload':True,'undo':1,'other_48_slots_unchanged':True})
assert editor.load_level(receipt['render_assets']['level'])
assert bindings(current())==before
protected=read_json(folder/'protected_asset_hashes.json')
assert all(file_hash(p)==digest for p,digest in protected.items())
assert file_hash(manifest['model'])==manifest['source_sha256']
atomic_json(folder/'validation_undo_ledger.json',read_json(ledger))
if prior_ledger is None: ledger.unlink()
else: ledger.write_bytes(prior_ledger)
atomic_json(folder/'textile_edit_receipt.json',{'source_preserved':True,
    'previous_assets_and_project_preserved_files':len(protected),'active_materials':before,
    'checks':checks,'operations':operations,'prior_undo_ledger_restored':True})
print('PBR_TEXTILES_APPLY_UNDO_COMPLETE',slot,len(checks))
