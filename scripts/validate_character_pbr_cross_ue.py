"""Native save/reload/apply/undo checks using actual slots of each fixture."""
from pathlib import Path
import os,sys,copy,json
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_character_pbr_edit import apply,undo,_ledger

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']);receipt=read_json(folder/'ue_receipt.json')
manifest=read_json(folder/'manifest.json');editor=u.get_editor_subsystem(u.LevelEditorSubsystem)
actors=u.get_editor_subsystem(u.EditorActorSubsystem);lib=u.MaterialEditingLibrary
level=receipt['render_assets']['level'];result={'status':'running','samples':[],'slots':len(manifest['materials'])}
ledger=_ledger();previous_ledger=ledger.read_bytes() if ledger.is_file() else None
pending_undo=False
if previous_ledger is not None:(folder/'undo_ledger_before_validation.json').write_bytes(previous_ledger)

def component(reload=False):
    if reload:assert editor.load_level(level)
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actors.set_selected_level_actors([actor]);return actor.skeletal_mesh_component

def paths(comp):return [m.get_path_name() for m in comp.get_materials()]

def disk(path):return Path(u.Paths.project_content_dir())/(path.split('.')[0][6:]+'.uasset')

try:
    comp=component(True);original=paths(comp)
    assert len(original)==len(manifest['materials'])
    hashes={path:file_hash(disk(path)) for path in original}
    sample_directory=disk(receipt['mesh']).parents[1]
    other_assets={str(p):file_hash(p) for p in sample_directory.parent.rglob('*')
                  if p.is_file() and sample_directory not in p.parents}
    result['bindings_before']=original
    jobs=[(('face','skin'),'WetEnabled',1.),(('hair',),'Anisotropy',.15),(('cloth',),'Fuzz',.45)]
    if os.environ.get('MMDBRIDGE_PBR_CHECK_ROSINESS')=='1':
        jobs.append((('face','skin'),'SkinRosiness',.73125))
    if manifest.get('fixture_kind','character')=='prop':jobs=[(('leather',),'Roughness',.73)]
    for kinds,parameter,value in jobs:
        candidates=[r for r in manifest['materials'] if r['character_material']['pbr']['type'] in kinds
                    and r['character_material']['pbr']['status']!='white'
                    and r['character_material']['pbr']['params'].get('Opacity',1)>0]
        supported=[]
        for candidate in candidates:
            parent=comp.get_material(candidate['pmx_index']).get_editor_property('parent')
            if parameter in map(str,lib.get_scalar_parameter_names(parent)):
                supported.append(candidate)
            else:
                result.setdefault('skipped_incompatible_parameters',[]).append({
                    'slot':candidate['pmx_index'],'name':candidate['name'],'parameter':parameter,
                    'alpha_mode':candidate['character_material']['pbr']['alpha_mode'],
                    'reason':'parameter_not_exposed_by_actual_parent'})
        assert supported,('No compatible visible material for parameter',kinds,parameter)
        record=supported[0]
        # Keep the tested source's options and all other manual overrides.
        cfg=copy.deepcopy(manifest['settings'])
        model_overrides=cfg.setdefault('character_material_overrides',{}).setdefault(manifest['source_sha256'],{})
        settings=model_overrides.setdefault(str(record['pmx_index']),{})
        settings.setdefault('params',{})[parameter]=value
        previous_value=lib.get_material_instance_scalar_parameter_value(comp.get_material(record['pmx_index']),parameter)
        applied=apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
                       'pmx_index':record['pmx_index'],'settings':cfg})
        pending_undo=True
        assert not applied['report'].get('failures'),applied
        after=paths(comp);changed=[i for i,(a,b) in enumerate(zip(original,after)) if a!=b]
        assert changed==[record['pmx_index']],changed
        native=comp.get_material(changed[0])
        assert parameter in map(str,lib.get_scalar_parameter_names(native.get_editor_property('parent'))),parameter
        assert abs(lib.get_material_instance_scalar_parameter_value(native,parameter)-value)<1e-5
        comp=component(True);assert paths(comp)==after
        assert abs(lib.get_material_instance_scalar_parameter_value(comp.get_material(changed[0]),parameter)-value)<1e-5
        undone=undo();assert undone['count']==1
        pending_undo=False
        comp=component(True);assert paths(comp)==original
        assert all(file_hash(disk(path))==digest for path,digest in hashes.items())
        assert all(file_hash(path)==digest for path,digest in other_assets.items())
        result['samples'].append({'type':record['character_material']['pbr']['type'],'slot':record['pmx_index'],
            'name':record['name'],'parameter':parameter,'previous_value':previous_value,'value':value,
            'changed_slots':changed,'candidate':after[changed[0]],'saved_reloaded':True,'undo_reloaded':True,
            'other_slots_and_original_material_bytes_unchanged':True,
            'other_sample_files_unchanged':len(other_assets),'revision':applied['revision']})
    hidden=[];compiled=[]
    for record in manifest['materials']:
        material=u.load_asset(receipt['assets']['materials'][record['id']]);parent=material.get_editor_property('parent')
        pbr=record['character_material']['pbr']
        if pbr['params'].get('Opacity',1)==0:
            assert lib.get_material_instance_scalar_parameter_value(material,'Opacity')==0
            hidden.append(record['pmx_index'])
        compiled.append({'slot':record['pmx_index'],'type':pbr['type'],'parent':parent.get_path_name(),
            'white':pbr['status']=='white','hidden':pbr['params'].get('Opacity',1)==0,
            'build_state':u.EditorAssetLibrary.get_metadata_tag(parent,'MMDBridge.BuildState')})
        assert compiled[-1]['build_state']=='complete'
    preset=u.load_asset(receipt['portrait_preset']);output=preset.find_setting_by_class(u.MoviePipelineOutputSetting)
    assert (output.output_resolution.x,output.output_resolution.y)==(2560,1440)
    result.update(status='complete',hidden_slots_preserved=hidden,compiled_slots=compiled,
                  native_resolution=[2560,1440],bindings_restored=paths(comp)==original,
                  other_sample_files_unchanged=len(other_assets))
    receipt['checks']['cross_character_edit']=result
    atomic_json(folder/'ue_receipt.json',receipt)
    print('CROSS_EDIT_RELOAD_UNDO_COMPLETE',folder.name,len(result['samples']))
except Exception:
    import traceback
    result.update(status='failed',error=traceback.format_exc());raise
finally:
    if pending_undo:
        try:
            undo()
            result['failed_step_bindings_restored']=paths(component(True))==original
        except Exception as exc:
            result['failed_step_rollback_error']=str(exc)
            if ledger.is_file():
                (folder/'failed_step_undo_ledger.json').write_bytes(ledger.read_bytes())
    # Retain any user's previous live undo state exactly, including absence.
    if previous_ledger is not None:ledger.write_bytes(previous_ledger)
    elif ledger.is_file():ledger.unlink()
    result['previous_undo_ledger_restored']=((ledger.read_bytes()==previous_ledger) if previous_ledger is not None else not ledger.exists())
    atomic_json(folder/'cross_edit_receipt.json',result)
