"""Independent uniform SVT fixture using the production USD field mapping."""
import os,sys
from pathlib import Path
import numpy as np
import openvdb as vdb
from pxr import Usd,UsdGeom
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.blender_appearance import write_volumes
out=Path(os.environ['MMDBRIDGE_FOG_TEST_OUT']);fixture=read_json(out/'fixtures.json')
root=out/'svt';root.mkdir(exist_ok=True)
grids=[];shape=(128,128,128);step=.06
for name,value in [('extinction_'+s,.00012) for s in 'rgb']+[('albedo_'+s,1.) for s in 'rgb']+[('phase',0.)]:
    grid=vdb.FloatGrid();grid.name=name
    transform=vdb.createLinearTransform(voxelSize=step);transform.postTranslate((-3.81,)*3);grid.transform=transform
    grid.copyFromArray(np.full(shape,value,dtype='float32'));grids.append(grid)
file=root/'box.vdb';vdb.write(str(file),grids=grids)
volume=dict(id='svt_box',name='Uniform source',kind='local_grid',status='converted',path=str(file),
    bounds=[[-3.84]*3,[3.84]*3],voxel_size=step,coefficient_units='per_voxel',resolution=list(shape),
    matrix=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
    uniform_source_proof=dict(method='constant_expressions',coefficients=dict(
        extinction=.002,albedo=[1.,1.,1.],emission=[0.,0.,0.],anisotropy=0.),container='closed_svt_mask'))
stage=Usd.Stage.CreateNew(str(root/'box.usda'));UsdGeom.SetStageMetersPerUnit(stage,1.)
UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.Xform.Define(stage,'/World');stage.SetDefaultPrim(stage.GetPrimAtPath('/World'));stage.GetRootLayer().Save()
write_volumes(root/'box.usda',[volume],root,fixture['root'],'FogSvt')
atomic_json(out/'svt_source.json',dict(root_layer=str(root/'box.usda'),volume=volume,asset_root=fixture['root']+'/A_FogSvt'))
print('SVT_SOURCE_READY')
