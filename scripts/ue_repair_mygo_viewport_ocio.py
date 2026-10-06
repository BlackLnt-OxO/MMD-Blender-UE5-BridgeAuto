"""Repair only the existing managed MyGO OCIO asset, preserving the old scene."""
import sys,traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_appearance import repair_color_configuration,validate_color_configuration
OUT=ROOT/'work/material_restoration/viewport';OUT.mkdir(parents=True,exist_ok=True)
OLD=Path(r'D:\Codex_Projects_RebuildAutoMMD\MMDBridge_Jobs\Scene_It_sMyGO_ParkingLots_01')
receipt={'complete':False}
try:
    report=read_json(OLD/'ue_report.json');scene=read_json(OLD/'manifest.json')['scene']
    asset=u.load_asset(report['assets']['ocio']);assert asset
    path=asset.get_path_name();root=path.split('/Materials/')[0]
    owner=u.EditorAssetLibrary.get_metadata_tag(asset,'MMDBridge.Owner');assert owner
    protection=Materials(root,owner,{'warnings':[]});protection.verify_asset(asset)
    receipt['previous_config']=asset.configuration_file.file_path
    display=repair_color_configuration(asset,scene['render_look'],scene['render_look']['config'])
    assert u.EditorAssetLibrary.save_loaded_asset(asset);protection.protect_asset(asset)
    u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(display)
    receipt.update(complete=True,asset=path,config=asset.configuration_file.file_path,
        probe_color=validate_color_configuration(display),level=report['assets']['level'])
except Exception:receipt['error']=traceback.format_exc();u.log_error(receipt['error'])
atomic_json(OUT/'ocio_repair.json',receipt)
