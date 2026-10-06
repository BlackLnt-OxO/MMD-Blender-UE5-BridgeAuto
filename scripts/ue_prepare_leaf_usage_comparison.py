"""Qualify both A/B shaders for the same regenerated USD/Nanite mesh usage."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json,Job,file_hash
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/material_functions_implementation/leaf_transmission'
result=read_json(OUT/'preparation.json');result['complete']=False
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
    builder=Materials(result['root'],job.data['asset_id'],{'warnings':[]},job.data['settings'])
    parents={};result['baseline_materials']={}
    def usage(parent):
        parent.set_editor_property('used_with_nanite',True)
        parent.set_editor_property('used_with_instanced_static_meshes',True)
        u.MaterialEditingLibrary.recompile_material(parent)
        assert u.EditorAssetLibrary.save_loaded_asset(parent,False)
        builder.protect_asset(parent)
    for path in result['parents'].values():
        asset=u.load_asset(path);builder.verify_asset(asset);usage(asset)
    for original,corrected in result['materials'].items():
        source=u.load_asset(original);old_parent=source.get_editor_property('parent')
        key=old_parent.get_path_name()
        if key not in parents:
            target=result['root']+'/Materials/Baseline/'+old_parent.get_name()
            assert not u.EditorAssetLibrary.does_asset_exist(target)
            copy=u.EditorAssetLibrary.duplicate_asset(key,target);assert copy
            builder.managed(copy);usage(copy);parents[key]=copy
        target=result['root']+'/Materials/Baseline/'+source.get_name()
        assert not u.EditorAssetLibrary.does_asset_exist(target)
        copy=u.EditorAssetLibrary.duplicate_asset(original,target);assert copy
        builder.managed(copy);u.MaterialEditingLibrary.set_material_instance_parent(copy,parents[key])
        assert u.EditorAssetLibrary.save_loaded_asset(copy,False);builder.protect_asset(copy)
        result['baseline_materials'][original]=copy.get_path_name()
    for path,digest in result['source_hashes'].items():
        assert file_hash(builder.asset_file(u.load_asset(path)))==digest,path
    result.update(complete=True,usage_qualified=True,baseline_parent_paths=[a.get_path_name() for a in parents.values()])
except Exception:
    result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(OUT/'preparation.json',result)
print('LEAF_USAGE_PREPARED',result['complete'],result.get('error'),flush=True)
