"""Bind only changed original-v8 classifications for matched camera renders."""
from pathlib import Path
import os,sys
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_pbr import build
folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']);receipt=read_json(folder/'ue_receipt.json')
current=read_json(folder/'manifest.json');before=read_json(folder/'Before_v8/manifest.json')
assert current['source_sha256']==before['source_sha256']
assert len(current['materials'])==len(before['materials'])
report={'warnings':[],'failures':[],'checks':{}}
root=receipt['root']+'/ClassificationBefore_v8'
manager=Materials(root,'cross_before_'+folder.name,report,before['settings'])
overrides={};changes=[]
for now,old in zip(current['materials'],before['materials']):
    assert now['pmx_index']==old['pmx_index']
    a,b=now['character_material']['pbr'],old['character_material']['pbr']
    if a['type']==b['type']:continue
    candidate=manager.asset(old['id']) if manager.reuse(old) else build(manager,old)
    overrides[receipt['assets']['materials'][now['id']]]=candidate.get_path_name()
    changes.append({'slot':old['pmx_index'],'name':old['name'],'before_type':b['type'],'after_type':a['type'],
                    'before_material':candidate.get_path_name(),'after_material':receipt['assets']['materials'][now['id']]})
assert not report['failures'],report
atomic_json(folder/'classification_before_overrides.json',overrides)
atomic_json(folder/'classification_comparison_receipt.json',{'status':'complete','changes':changes,
    'same_mesh_camera_lighting':True,'classifier_before':8,'classifier_after':9,'shading_equations':8,'native_build':report})
print('CLASSIFICATION_COMPARISON_READY',folder.name,len(changes))
