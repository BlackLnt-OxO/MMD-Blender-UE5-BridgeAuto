"""Tiny ordinary plus PointInstancer fixture, independent of production geometry."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pxr import Usd,UsdGeom,UsdShade,Sdf,Gf
out=ROOT/'work/material_functions_implementation/shader_data';out.mkdir(parents=True,exist_ok=True)
stage=Usd.Stage.CreateNew(str(out/'native_data.usda'))
UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.SetStageMetersPerUnit(stage,.01)
material=UsdShade.Material.Define(stage,'/World/Material')
shader=UsdShade.Shader.Define(stage,'/World/Material/UnrealShader')
shader.SetSourceAsset(Sdf.AssetPath('/Game/MMDBridgeValidation/FunctionsV4/Data/Materials/MI_M_Data.MI_M_Data'),'unreal')
material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out',Sdf.ValueTypeNames.Token))
def quad(path):
    mesh=UsdGeom.Mesh.Define(stage,path)
    mesh.CreatePointsAttr([(-40,-40,0),(40,-40,0),(40,40,0),(-40,40,0)])
    mesh.CreateFaceVertexCountsAttr([4]);mesh.CreateFaceVertexIndicesAttr([0,1,2,3])
    mesh.CreateSubdivisionSchemeAttr('none')
    # Source slots 0 and 6 are the only ones used after conversion. Slot 6 is
    # physically packed into UE slot 1; the distinct slot-0 value detects drift.
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,'faceVarying').Set([(0.9,0)]*4)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st1',Sdf.ValueTypeNames.TexCoord2fArray,'faceVarying').Set([(0.2,0)]*4)
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return mesh
ordinary=UsdGeom.Xform.Define(stage,'/World/Ordinary');ordinary.AddTranslateOp().Set((0,100,0));quad('/World/Ordinary/Mesh')
quad('/World/Prototype/Mesh')
instancer=UsdGeom.PointInstancer.Define(stage,'/World/Instancer')
instancer.CreatePrototypesRel().SetTargets(['/World/Prototype'])
instancer.CreateProtoIndicesAttr([0]);instancer.CreatePositionsAttr([(0,-100,0)])
instancer.CreateOrientationsAttr([Gf.Quath(1,Gf.Vec3h(0))]);instancer.CreateScalesAttr([(1,1,1)])
stage.GetRootLayer().Save()
print('SHADER_DATA_USD_READY',flush=True)
