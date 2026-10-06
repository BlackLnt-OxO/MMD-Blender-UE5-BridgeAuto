"""Repair only the isolated validation scene's managed material permutations."""
import sys
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials,BACKEND_VERSION
OUT=ROOT/'work/material_restoration/full_scene'
job=Path(read_json(OUT/'job_path.json')['job']);data=read_json(job/'job.json');report=read_json(job/'ue_report.json')
root=report['assets']['level'].split('/Maps/')[0]
assert root.startswith('/Game/MMDBridgeValidation/MyGO3/')
converter=Materials(root,data['asset_id'],report,data['settings'])
seen=set();rows=[]
for path in report['assets']['materials'].values():
    instance=u.load_asset(path);parent=instance.parent
    converter.verify_asset(instance)
    if parent.get_path_name() not in seen:
        converter.verify_asset(parent)
        parent.set_editor_property('used_with_instanced_static_meshes',True)
        if parent.blend_mode in (u.BlendMode.BLEND_OPAQUE,u.BlendMode.BLEND_MASKED):
            parent.set_editor_property('used_with_nanite',True)
        errors=u.MaterialEditingLibrary.recompile_material(parent)
        assert not errors,str(errors)
        assert u.EditorAssetLibrary.save_loaded_asset(parent)
        converter.protect_asset(parent);seen.add(parent.get_path_name())
        rows.append(dict(path=parent.get_path_name(),nanite=parent.used_with_nanite,instancing=parent.used_with_instanced_static_meshes))
    u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.BackendVersion',str(BACKEND_VERSION))
    assert u.EditorAssetLibrary.save_loaded_asset(instance);converter.protect_asset(instance)
report.setdefault('checks',{})['material_usage_repair']=rows
atomic_json(job/'ue_report.json',report)
atomic_json(OUT/'usage_repair.json',dict(complete=True,parents=rows,backend=BACKEND_VERSION,code_sha256=file_hash(ROOT/'mmd_bridge/ue_materials.py')))
print('USAGE_REPAIR',len(rows),flush=True)
