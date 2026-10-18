"""Save/reload and undo v7 thickness, rosiness and the retained water switch."""
from pathlib import Path
import runpy,json
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
state=runpy.run_path(str(ROOT/'scripts/validate_character_pbr_textiles_ue.py'))
folder,receipt,manifest,editor,lib,current,bindings=[state[k] for k in (
    'folder','receipt','manifest','editor','lib','current','bindings')]
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_character_pbr_edit import apply,undo
component=current();before=bindings(component)
ledger=Path(u.Paths.project_saved_dir())/'MMDBridge/character_pbr_undo.json'
prior_ledger=ledger.read_bytes() if ledger.is_file() else None
rows={r['name']:r for r in manifest['materials']}
operations=[]
commands=(('stocking',{'HosieryThickness':0.}),('stocking',{'HosieryThickness':2.5}),
          ('Face',{'SkinRosiness':0.,'WetEnabled':0.}),
          ('Face',{'SkinRosiness':.6,'WetEnabled':1.}))
for name,params in commands:
    record=rows[name];slot=before.index(receipt['assets']['materials'][record['id']])
    cfg={'character_material_overrides':{manifest['source_sha256']:{str(record['pmx_index']):{'params':params}}}}
    result=apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
                  'pmx_index':record['pmx_index'],'settings':cfg})
    assert result['count']==1 and not result['report']['failures']
    assert [i for i,(a,b) in enumerate(zip(before,bindings(component))) if a!=b]==[slot]
    assert editor.load_level(receipt['render_assets']['level'])
    component=current();material=component.get_material(slot)
    for key,value in params.items():
        assert abs(lib.get_material_instance_scalar_parameter_value(material,key)-value)<1e-5
    if name=='Face':
        assert lib.get_material_instance_scalar_parameter_value(material,'Use_sss_mask')==1.
        assert lib.get_material_instance_scalar_parameter_value(material,'SSSDetailPreservation')==1.
        assert abs(lib.get_material_instance_scalar_parameter_value(material,'SSSStrength')-.3)<1e-5
        profile=material.get_editor_property('parent').get_editor_property('subsurface_profile')
        assert abs(profile.get_editor_property('settings').get_editor_property('mean_free_path_distance')-.18)<1e-5
    else:
        for key in ('DetailTiling','FabricDirection','Opacity','HosieryCoverage'):
            assert abs(lib.get_material_instance_scalar_parameter_value(material,key)-record['character_material']['pbr']['params'][key])<1e-4
    restored=undo()
    assert restored['count']==1 and bindings(component)==before
    operations.append({'name':name,'params':params,'apply':1,'saved_reloaded':True,'undo':1,
                       'other_48_slots_unchanged':True})
assert editor.load_level(receipt['render_assets']['level'])
assert bindings(current())==before
actual=[]
for record in manifest['materials']:
    pbr=record['character_material']['pbr'];kind=pbr['type']
    if kind not in ('cloth','satin','hosiery','skin','face'):continue
    material=u.load_asset(receipt['assets']['materials'][record['id']])
    names={str(n) for n in lib.get_scalar_parameter_names(material.get_editor_property('parent'))}
    keys={'cloth':('Roughness','Fuzz','FuzzBrightness','FabricRoughnessVariation'),
          'satin':('Roughness','Specular','Anisotropy'),
          'hosiery':('HosieryThickness','HosieryCoverage'),
          'skin':('SkinRosiness','WetEnabled'), 'face':('SkinRosiness','WetEnabled')}
    for key in keys[kind]:
        assert key in names
        assert abs(lib.get_material_instance_scalar_parameter_value(material,key)-pbr['params'][key])<1e-5
    assert lib.get_material_instance_scalar_parameter_value(material,'EmissiveStrength')==0.
    actual.append({'name':record['name'],'type':kind,'saved_params':{k:lib.get_material_instance_scalar_parameter_value(material,k) for k in keys[kind]}})
protected=read_json(folder/'protected_asset_hashes.json')
assert all(file_hash(p)==digest for p,digest in protected.items())
assert file_hash(manifest['model'])==manifest['source_sha256']
atomic_json(folder/'fabric_rosy_validation_ledger.json',read_json(ledger))
if prior_ledger is None:ledger.unlink()
else:ledger.write_bytes(prior_ledger)
atomic_json(folder/'fabric_rosy_edit_receipt.json',{'source_preserved':True,
    'protected_previous_files':len(protected),'prior_undo_ledger_restored':True,
    'active_materials':before,'operations':operations,'saved_material_checks':actual})
print('PBR_FABRIC_ROSY_APPLY_UNDO_COMPLETE',len(operations),len(actual))
