"""Inject an incorrect UV only into a generated validation fixture."""
import sys
from pathlib import Path
import numpy as np
from pxr import Usd, UsdGeom, Sdf, Vt
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
job=Path(sys.argv[sys.argv.index('--')+1]).resolve()
assert job.is_relative_to((ROOT/'work/material_restoration/workflow_native_v1/Transfer/MMDBridge_Jobs').resolve())
manifest=read_json(job/'manifest.json');usd=manifest['scene']['usd'];root=Path(usd['root_layer'])
stage=Usd.Stage.Open(str(root));stage.OverridePrim('/World/Objects/RepairTarget').SetInstanceable(False)
path='/World/Objects/RepairTarget/Mesh';prim=stage.GetPrimAtPath(path)
prior=UsdGeom.PrimvarsAPI(prim).GetPrimvar('st');values=np.asarray(prior.ComputeFlattened(),dtype=np.float32)
name=prior.GetAttr().GetCustomDataByKey('MMDBridge:sourceName') or 'UVMap'
over=stage.OverridePrim(path);var=UsdGeom.PrimvarsAPI(over).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.faceVarying)
var.Set(Vt.Vec2fArray.FromNumpy(values+.125));var.GetAttr().SetCustomDataByKey('MMDBridge:sourceName',name)
stage.GetRootLayer().Save()
for record in usd['files']:
    if Path(record['path']).resolve()==root.resolve():record.update(sha256=file_hash(root),size=root.stat().st_size)
atomic_json(job/'manifest.json',manifest)
atomic_json(job/'uv_fault.json',{'fixture_only':True,'path':path,'source_uv_retained':True})
