"""Probe the actual Interchange return paths before a full scene retry."""
import sys,traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/material_restoration/asset_paths';OUT.mkdir(parents=True,exist_ok=True)
report={'complete':False,'warnings':[],'checks':{},'textures':[]}
try:
    converter=Materials('/Game/MMDBridgeValidation/AssetPathProbe/A_Scene_It_sMyGO_ParkingLots_03_ffffffff',
        'AssetPathProbe',report)
    data=read_json(ROOT/'work/material_restoration/full_scene/jobs/Scene_It_sMyGO_ParkingLots_02/manifest.json')
    images={}
    def walk(value):
        if isinstance(value,dict):
            if value.get('op')=='texture':images[value['image']['path']]=value['image']
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(data['scene']['materials'])
    selected=[next(i for i in images.values() if '604fe26' in i['path']),
              next(i for i in images.values() if 'Leaf_Aesculus_hippocastanum_Diffuse' in i['path'])]
    for image in selected:
        asset=converter.texture(image,data=True)
        assert asset and u.EditorAssetLibrary.does_asset_exist(asset.get_path_name())
        converter.verify_asset(asset)
        assert converter.texture(image,data=True)==asset
        report['textures'].append(asset.get_path_name())
    report['complete']=True
except Exception:report['error']=traceback.format_exc();u.log_error(report['error'])
atomic_json(OUT/'result.json',report)
