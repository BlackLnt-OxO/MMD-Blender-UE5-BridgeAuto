"""Activate the qualified candidate by changing leaf MI parents; retain old graphs."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials,BACKEND_VERSION
OUT=ROOT/'work/material_functions_implementation/leaf_transmission'
result={'complete':False,'changed':[],'rollback_parents':{},'visual_status':'needs_review'}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    comparison=read_json(OUT/'comparison.json');assert comparison['complete']
    assert comparison['decision']=='use_corrected',comparison
    prep=read_json(OUT/'preparation.json');assert prep['complete']
    job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
    report=read_json(job.folder/'ue_report.json');cfg=dict(job.data['settings'],thin_surface_scattering='two_sided_wrap')
    old=Materials(report['root'],job.data['asset_id'],report,cfg)
    new=Materials(prep['root'],job.data['asset_id'],report,cfg)
    sources=[]
    for path,digest in prep['source_hashes'].items():
        assert file_hash(old.asset_file(u.load_asset(path)))==digest,path
    for source,target in prep['materials'].items():
        instance=u.load_asset(source);old.verify_asset(instance)
        replacement=u.load_asset(target);new.verify_asset(replacement)
        parent=replacement.get_editor_property('parent');new.verify_asset(parent)
        assert parent.get_editor_property('used_with_nanite')
        sources.append((instance,parent))
        result['rollback_parents'][source]=instance.get_editor_property('parent').get_path_name()
    atomic_json(OUT/'activation.json',result)
    for path in prep['parents'].values():
        parent=u.load_asset(path)
        u.EditorAssetLibrary.set_metadata_tag(parent,'MMDBridge.ThinSurfaceScattering','two_sided_wrap')
        assert u.EditorAssetLibrary.save_loaded_asset(parent,False);new.protect_asset(parent)
    for instance,parent in sources:
        u.MaterialEditingLibrary.set_material_instance_parent(instance,parent)
        u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.BackendVersion',str(BACKEND_VERSION))
        u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.ThinSurfaceScattering','two_sided_wrap')
        assert u.EditorAssetLibrary.save_loaded_asset(instance,False);old.protect_asset(instance)
        result['changed'].append(instance.get_path_name())
        atomic_json(OUT/'activation.json',result)
    report.setdefault('checks',{})['thin_surface_transmission']={
        'status':'converted','mode':'two_sided_wrap','materials':len(sources),
        'gpu_delta_percent':comparison['delta_percent'],'visual_status':'needs_review',
        'baseline_retained':True,'comparison':str(OUT/'comparison.json')}
    for route in report.get('material_routes',{}).values():
        if route.get('parent') in prep['parents']:
            route['parent']=prep['parents'][route['parent']]
            route['thin_surface_scattering']='two_sided_wrap'
    atomic_json(job.folder/'ue_report.json',report)
    data=read_json(job.path);data['settings']['thin_surface_scattering']='two_sided_wrap';atomic_json(job.path,data)
    result.update(complete=True,decision=comparison['decision'],gpu_delta_percent=comparison['delta_percent'])
except Exception:
    result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(OUT/'activation.json',result)
print('LEAF_TRANSMISSION_ACTIVATED',result['complete'],result.get('error'),flush=True)
