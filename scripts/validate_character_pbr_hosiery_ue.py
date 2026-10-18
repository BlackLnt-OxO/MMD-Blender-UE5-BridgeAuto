"""Verify continuous UE scalars, parameter UI and isolated save/reload/undo."""
from pathlib import Path
import runpy
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
state=runpy.run_path(str(ROOT/'scripts/validate_character_pbr_textiles_ue.py'))
folder,receipt,manifest,editor,lib,current,bindings=[state[k] for k in (
    'folder','receipt','manifest','editor','lib','current','bindings')]
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_character_pbr_edit import apply,undo
component=current();before=bindings(component)
record=next(r for r in manifest['materials'] if r['character_material']['pbr']['type']=='hosiery')
slot=before.index(receipt['assets']['materials'][record['id']])
material=component.get_material(slot);parent=material.get_editor_property('parent')
nodes=[node for node in u.ObjectIterator(u.MaterialExpressionScalarParameter)
       if node.get_outer()==parent and str(node.get_editor_property('parameter_name'))=='HosieryThickness']
assert len(nodes)==1,len(nodes)
parameter=nodes[0]
ui={key:str(parameter.get_editor_property(key)) if key=='group' else parameter.get_editor_property(key)
    for key in ('group','slider_min','slider_max','sort_priority','default_value')}
assert ui['group']=='Hosiery' and ui['slider_min']==0. and ui['slider_max']==3.
dynamic=u.MaterialLibrary.create_dynamic_material_instance(component,material)
assert dynamic
runtime_values=[]
for value in (.35,.73125,1.23456,1.85,2.5):
    dynamic.set_scalar_parameter_value('HosieryThickness',value)
    actual=dynamic.get_scalar_parameter_value('HosieryThickness')
    assert abs(actual-value)<1e-5
    runtime_values.append({'requested':value,'actual':actual})
ledger=Path(u.Paths.project_saved_dir())/'MMDBridge/character_pbr_undo.json'
prior=ledger.read_bytes() if ledger.is_file() else None
operations=[]
try:
    for value in (0.,.65,1.23456,1.85,2.5,3.):
        cfg={'character_material_overrides':{manifest['source_sha256']:{str(record['pmx_index']):{
            'params':{'HosieryThickness':value}}}}}
        result=apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
                      'pmx_index':record['pmx_index'],'settings':cfg})
        assert result['count']==1 and not result['report']['failures']
        assert [i for i,(a,b) in enumerate(zip(before,bindings(component))) if a!=b]==[slot]
        assert editor.load_level(receipt['render_assets']['level'])
        component=current();changed=component.get_material(slot)
        actual=lib.get_material_instance_scalar_parameter_value(changed,'HosieryThickness')
        assert abs(actual-value)<1e-5,(actual,value)
        for key in ('DetailTiling','FabricDirection','Opacity','HosieryCoverage'):
            assert abs(lib.get_material_instance_scalar_parameter_value(changed,key)-record['character_material']['pbr']['params'][key])<1e-4
        restored=undo()
        assert restored['count']==1 and bindings(component)==before
        operations.append({'requested':value,'saved_value':actual,'apply':1,'save_reload':True,
                           'undo':1,'other_48_slots_unchanged':True})
    assert editor.load_level(receipt['render_assets']['level']) and bindings(current())==before
    old=read_json(ROOT/'work/character_pbr_20261018/FabricRosy_v7/manifest.json')
    old_rows={r['id']:r for r in old['materials']}
    unchanged=[]
    for row in manifest['materials']:
        prior_pbr=old_rows[row['id']]['character_material']['pbr'];pbr=row['character_material']['pbr']
        assert pbr['params']==prior_pbr['params'],row['name']
        assert pbr['alpha_mode']==prior_pbr['alpha_mode'] and pbr['matcap']==prior_pbr['matcap']
        mat=u.load_asset(receipt['assets']['materials'][row['id']])
        assert lib.get_material_instance_scalar_parameter_value(mat,'EmissiveStrength')==0.
        unchanged.append(row['name'])
    protected=read_json(folder/'protected_asset_hashes.json')
    assert all(file_hash(path)==digest for path,digest in protected.items())
    assert file_hash(manifest['model'])==manifest['source_sha256']
finally:
    if prior is None:
        if ledger.is_file():ledger.unlink()
    else:ledger.write_bytes(prior)
atomic_json(folder/'hosiery_edit_receipt.json',{'source_preserved':True,'active_materials':before,
    'parameter_ui':ui,'operations':operations,'other_material_defaults_unchanged':unchanged,
    'dynamic_instance_continuous_values':runtime_values,
    'protected_previous_files':len(protected),'prior_undo_ledger_restored':True})
print('PBR_HOSIERY_CONTINUOUS_APPLY_UNDO_COMPLETE',len(operations),ui)
