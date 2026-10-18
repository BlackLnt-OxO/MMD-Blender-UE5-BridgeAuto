"""Refresh PBR preparation from the saved first-character fixture, without FBX reimport."""
import copy
import os
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'vendor/python',ROOT/'vendor/blender_mmd_tools-4.5.13'):
    sys.path.insert(0,str(path))
import bpy
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.blender_host import enable_mmd
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_character_pbr import prepare
from mmd_bridge.character_pbr import VERSION
source=ROOT/'work/character_pbr_20261017/GenZ_Flow'
folder=Path(os.environ.get('MMDBRIDGE_PBR_FIXTURE', str(ROOT/'work/character_pbr_20261018'/('Refined_v'+str(VERSION)))))
folder.mkdir(parents=True,exist_ok=True)
manifest=read_json(source/'manifest.json')
assert manifest['source_sha256']==file_hash(manifest['model'])
enable_mmd()
bpy.ops.wm.open_mainfile(filepath=str(source/'Character.blend'))
worker=SimpleNamespace(cfg=manifest['settings'],data={'settings':manifest['settings']},bundle=folder/'Data',
    meshes=[o for o in bpy.context.scene.objects if o.type=='MESH'],
    armature=next(o for o in bpy.context.scene.objects if o.type=='ARMATURE'))
worker.materials=Materials(worker.bundle,manifest['model'])
worker.materials.records={r['id']:copy.deepcopy(r) for r in manifest['materials']}
prepare(worker)
manifest['materials']=list(worker.materials.records.values())
assert not [r['name'] for r in manifest['materials'] if r['character_material']['pbr']['status']=='white']
atomic_json(folder/'manifest.json',manifest)
atomic_json(folder/'blender_receipt.json',{'source_preserved':file_hash(manifest['model'])==manifest['source_sha256'],
    'materials':len(manifest['materials']),'eyes_type':next(r['character_material']['pbr']['type'] for r in manifest['materials'] if r['name']=='Eyes')})
print('REFRESHED_PBR',len(manifest['materials']))
