"""Run with Blender's bundled Python to validate real USD composition."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:
    Gf = None
from mmd_bridge.usd_scene_optimizer import compact_geometry, prepare_import_compaction
from mmd_bridge.core import atomic_json, read_json, file_hash


@unittest.skipIf(Gf is None, 'Requires native USD Python (Blender runtime)')
class NativeScatterTests(unittest.TestCase):
    def test_import_preparation_preserves_middle_files_and_color_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();job_folder=root/'job';job_folder.mkdir()
            stage=Usd.Stage.CreateNew(str(source/'payload.usda'))
            UsdGeom.Xform.Define(stage,'/Tree')
            UsdGeom.Mesh.Define(stage,'/Tree/Mesh');stage.GetRootLayer().Save()
            geometry=Usd.Stage.CreateNew(str(source/'Geometry.usda'))
            UsdGeom.Xform.Define(geometry,'/Parent')
            instances=[]
            for i in range(20):
                path='/Parent/P'+str(i);prim=UsdGeom.Xform.Define(geometry,path)
                prim.AddTransformOp().Set(Gf.Matrix4d(1))
                prim.GetPrim().GetReferences().AddReference('./payload.usda','/Tree')
                prim.GetPrim().SetInstanceable(True)
                instances.append({'prim_path':path,'name':'Leaf '+str(i)})
            geometry.GetRootLayer().Save()
            scene_layer=Usd.Stage.CreateNew(str(source/'Scene.usda'));scene_layer.GetRootLayer().Save()
            atomic_json(source/'package.json',{'actor_labels':{i['prim_path']:i['name'] for i in instances}})
            original_hashes={p.name:file_hash(p) for p in source.iterdir()}
            colors=[{'channels':{'base_color':{'op':'hsv','hue':.5}}}]
            manifest={'scene':{'route':'usd_stage','instances':instances,'prototypes':[],
                'hierarchy':[],'materials':colors,'usd':{'root_layer':str(source/'Scene.usda'),
                'files':[{'relative':name,'path':str(source/name),'sha256':original_hashes[name]}
                         for name in ('Geometry.usda','package.json')]}}}
            atomic_json(job_folder/'manifest.json',manifest)
            job=SimpleNamespace(folder=job_folder,data={'settings':{'optimize_usd_scatter':True}},status=lambda *args:None)
            prepare_import_compaction(job)
            result=read_json(job_folder/'manifest.json')['scene']
            assert result['materials']==colors
            assert result['import_compaction']['version']==2
            assert len(result['instances'])==20 and len(result['scatter_batches'])==1
            assert read_json(job_folder/'manifest_before_import_compaction.json')==manifest
            assert {p.name:file_hash(p) for p in source.iterdir()}==original_hashes
            exported=read_json(job_folder/'import_scene/package.json')
            assert len(exported['actor_labels'])==1
            assert file_hash(result['import_compaction']['geometry'])==result['import_compaction']['geometry_sha256']

    def test_preserves_reference_geometry_material_and_transforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            payload = Usd.Stage.CreateNew(str(folder / 'payload.usda'))
            proto = UsdGeom.Xform.Define(payload, '/Tree')
            proto.AddTranslateOp().Set((10,2,3))
            mesh = UsdGeom.Mesh.Define(payload, '/Tree/Mesh')
            mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
            mesh.CreateFaceVertexCountsAttr([3]); mesh.CreateFaceVertexIndicesAttr([0,1,2])
            material = UsdShade.Material.Define(payload, '/Tree/Material')
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
            payload.GetRootLayer().Save()
            original = Usd.Stage.CreateNew(str(folder / 'Geometry.usda'))
            UsdGeom.Xform.Define(original, '/Parent')
            matrices = {}
            for i in range(32):
                path = '/Parent/P' + str(i)
                xform = UsdGeom.Xform.Define(original, path)
                transform = Gf.Transform()
                transform.SetTranslation(Gf.Vec3d(i*.72, i*.1, 0))
                transform.SetRotation(Gf.Rotation(Gf.Vec3d(0,0,1), i*7.5))
                transform.SetScale(Gf.Vec3d(1))
                matrices[path] = transform.GetMatrix()
                xform.AddTransformOp().Set(matrices[path])
                xform.GetPrim().GetReferences().AddReference('./payload.usda','/Tree')
                xform.GetPrim().SetInstanceable(True)
            # Individual visibility must never be lost when optimizing.
            UsdGeom.Imageable(original.GetPrimAtPath('/Parent/P31')).CreateVisibilityAttr().Set('invisible')
            original.GetPrimAtPath('/Parent/P30').SetCustomDataByKey('User:editable', True)
            original.GetRootLayer().Save()
            checksum = hashlib.sha256((folder/'payload.usda').read_bytes()).hexdigest()
            result = compact_geometry(folder/'Geometry.usda', folder/'Optimized.usda',
                                      protected_paths=['/Parent/P29'])
            assert result['batched_instances'] == 29 and result['generated_batches'] == 1
            stage = Usd.Stage.Open(str(folder/'Optimized.usda'))
            batch = UsdGeom.PointInstancer(stage.GetPrimAtPath(result['batches'][0]['prim_path']))
            transforms = batch.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default(),
                                                               UsdGeom.PointInstancer.IncludeProtoXform)
            for old, item in result['mapping'].items():
                actual = transforms[item['instance_index']]
                assert max(abs(actual[r][c]-matrices[old][r][c]) for r in range(4) for c in range(4)) < .002
                assert not stage.GetPrimAtPath(old)
            retained = stage.GetPrimAtPath('/Parent/P31')
            assert retained and UsdGeom.Imageable(retained).GetVisibilityAttr().Get() == 'invisible'
            assert stage.GetPrimAtPath('/Parent/P29') and stage.GetPrimAtPath('/Parent/P30')
            prototype = stage.GetPrimAtPath(result['batches'][0]['prototype_path']+'/Mesh')
            assert UsdGeom.Mesh(prototype).GetFaceVertexIndicesAttr().Get() == [0,1,2]
            assert UsdShade.MaterialBindingAPI(prototype).ComputeBoundMaterial()[0]
            assert hashlib.sha256((folder/'payload.usda').read_bytes()).hexdigest() == checksum
            assert './payload.usda' in (folder/'Optimized.usda').read_text()


if __name__ == '__main__':
    unittest.main()
