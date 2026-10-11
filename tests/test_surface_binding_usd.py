"""Run in Blender's USD runtime; desktop Python may not include pxr."""
import tempfile
import unittest
from pathlib import Path
try:
    from pxr import Usd, UsdShade, Sdf
except ImportError:
    raise unittest.SkipTest('Requires Blender USD runtime')
from mmd_bridge.core import atomic_json, read_json, file_hash
from mmd_bridge.blender_surface_layer import run


class SurfaceBindings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder/'Scene.usda'
        self.stage = Usd.Stage.CreateNew(str(self.root))
        self.stage.GetRootLayer().subLayerPaths = []
        parent = self.stage.DefinePrim('/World/Group', 'Xform')
        self.meshes = [self.stage.DefinePrim('/World/Group/'+name, 'Mesh') for name in ('A','B','C')]
        old = UsdShade.Material.Define(self.stage, '/World/Materials/Old')
        shader = UsdShade.Shader.Define(self.stage, str(old.GetPath())+'/UnrealShader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        shader.GetPrim().CreateAttribute('info:unreal:sourceAsset', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath('/Game/Old.Old'))
        old.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out',Sdf.ValueTypeNames.Token))
        for mesh in self.meshes: UsdShade.MaterialBindingAPI.Apply(mesh).Bind(old)
        collection = Usd.CollectionAPI.Apply(parent, 'unrealOverrides')
        collection.CreateExpansionRuleAttr().Set(Usd.Tokens.explicitOnly)
        collection.CreateIncludesRel().SetTargets([m.GetPath() for m in self.meshes])
        UsdShade.MaterialBindingAPI.Apply(parent).Bind(collection,old,'unrealOverrides',UsdShade.Tokens.strongerThanDescendants)
        self.stage.GetRootLayer().Save()
        self.before = self.root.read_bytes()

    def asset(self, mesh, purpose='unreal'):
        mesh = self.stage.GetPrimAtPath(mesh.GetPath())
        material,_ = UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial(purpose)
        shader = self.stage.GetPrimAtPath(str(material.GetPath())+'/UnrealShader')
        return shader.GetAttribute('info:unreal:sourceAsset').Get().path

    def switch(self, batch, indices, asset):
        folder = self.folder/'MaterialSurfaces'/batch;folder.mkdir(parents=True)
        before = self.root.read_bytes()
        rows = [{'prim_path':str(self.meshes[i].GetPath()),'slot':0,'label':str(i),
                 'original_asset':self.asset(self.meshes[i]),'candidate_id':'shared', 'candidate_asset':asset} for i in indices]
        atomic_json(folder/'build.json', {'root_layer':str(self.root),'root_hash':file_hash(self.root),'targets':rows})
        run(folder)
        self.assertEqual(self.root.read_bytes(), before)
        self.stage.GetRootLayer().Reload(True)
        self.root.write_bytes((folder/'root_candidate.usda').read_bytes())
        self.stage.GetRootLayer().Reload(True)
        for i in indices:
            for purpose in ('','unreal'): self.assertEqual(self.asset(self.meshes[i],purpose),asset)
        return before

    def test_strong_shared_override_changes_only_selected_mesh_and_restores_exactly(self):
        self.switch('S_First',[0],'/Game/New.New')
        self.assertEqual([self.asset(m) for m in self.meshes],['/Game/New.New','/Game/Old.Old','/Game/Old.Old'])
        self.root.write_bytes(self.before);self.stage.GetRootLayer().Reload(True)
        self.assertEqual([self.asset(m) for m in self.meshes],['/Game/Old.Old']*3)
        self.assertEqual(self.root.read_bytes(),self.before)

    def test_multi_select_repeated_switch_and_two_undos_do_not_retarget_older_material(self):
        self.switch('S_First',[0],'/Game/First.First')
        first = self.switch('S_Second',[1,2],'/Game/Second.Second')
        self.assertEqual(self.asset(self.meshes[0]),'/Game/First.First')
        second = self.switch('S_Third',[0],'/Game/Third.Third')
        self.assertEqual(self.asset(self.meshes[1]),'/Game/Second.Second')
        self.root.write_bytes(second);self.stage.GetRootLayer().Reload(True)
        self.assertEqual(self.asset(self.meshes[0]),'/Game/First.First')
        self.root.write_bytes(first);self.stage.GetRootLayer().Reload(True)
        self.assertEqual([self.asset(m) for m in self.meshes],['/Game/First.First','/Game/Old.Old','/Game/Old.Old'])
        self.root.write_bytes(self.before);self.stage.GetRootLayer().Reload(True)
        self.assertEqual([self.asset(m) for m in self.meshes],['/Game/Old.Old']*3)

    def test_root_direct_strong_binding_is_scoped_to_one_descendant(self):
        parent=self.stage.GetPrimAtPath('/World/Group')
        api=UsdShade.MaterialBindingAPI(parent);api.UnbindCollectionBinding('unrealOverrides')
        api.Bind(UsdShade.Material(self.stage.GetPrimAtPath('/World/Materials/Old')),UsdShade.Tokens.strongerThanDescendants)
        self.stage.GetRootLayer().Save()
        self.switch('S_Direct',[0],'/Game/New.New')
        self.assertEqual(self.asset(self.meshes[1]),'/Game/Old.Old')

    def test_material_subset_leaves_other_slot_untouched(self):
        api=UsdShade.MaterialBindingAPI(self.stage.GetPrimAtPath('/World/Group'))
        api.UnbindCollectionBinding('unrealOverrides')
        mesh_api=UsdShade.MaterialBindingAPI(self.meshes[0])
        first=mesh_api.CreateMaterialBindSubset('First',[0])
        second=mesh_api.CreateMaterialBindSubset('Second',[1])
        old=UsdShade.Material(self.stage.GetPrimAtPath('/World/Materials/Old'))
        other=UsdShade.Material.Define(self.stage,'/World/Materials/Other')
        shader=UsdShade.Shader.Define(self.stage,str(other.GetPath())+'/UnrealShader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        shader.GetPrim().CreateAttribute('info:unreal:sourceAsset',Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath('/Game/Other.Other'))
        other.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out',Sdf.ValueTypeNames.Token))
        UsdShade.MaterialBindingAPI.Apply(first.GetPrim()).Bind(old)
        UsdShade.MaterialBindingAPI.Apply(second.GetPrim()).Bind(other)
        self.stage.GetRootLayer().Save()
        folder=self.folder/'MaterialSurfaces/S_Subset';folder.mkdir(parents=True)
        atomic_json(folder/'build.json',{'root_layer':str(self.root),'root_hash':file_hash(self.root),'targets':[
            {'prim_path':str(self.meshes[0].GetPath()),'slot':0,'label':'First','original_asset':'/Game/Old.Old',
             'candidate_id':'shared','candidate_asset':'/Game/New.New'}]})
        before=self.root.read_bytes();run(folder)
        self.assertEqual(self.root.read_bytes(),before)
        self.root.write_bytes((folder/'root_candidate.usda').read_bytes());self.stage.GetRootLayer().Reload(True)
        self.assertEqual(self.asset(first.GetPrim()),'/Game/New.New')
        self.assertEqual(self.asset(second.GetPrim()),'/Game/Other.Other')
        self.root.write_bytes(before);self.stage.GetRootLayer().Reload(True)
        self.assertEqual(self.asset(first.GetPrim()),'/Game/Old.Old')

    def test_instance_proxy_breaks_only_selected_instance_and_undo_restores_it(self):
        self.stage.DefinePrim('/World/Prototype','Xform')
        mesh=self.stage.DefinePrim('/World/Prototype/Mesh','Mesh')
        material=UsdShade.Material.Define(self.stage,'/World/Prototype/Material')
        shader=UsdShade.Shader.Define(self.stage,str(material.GetPath())+'/UnrealShader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        shader.GetPrim().CreateAttribute('info:unreal:sourceAsset',Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath('/Game/Old.Old'))
        material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out',Sdf.ValueTypeNames.Token))
        UsdShade.MaterialBindingAPI.Apply(mesh).Bind(material)
        for name in ('I1','I2'):
            instance=self.stage.DefinePrim('/World/Group/'+name,'Xform')
            instance.GetReferences().AddInternalReference('/World/Prototype');instance.SetInstanceable(True)
        self.meshes[0]=self.stage.GetPrimAtPath('/World/Group/I1/Mesh')
        self.stage.GetRootLayer().Save();before=self.root.read_bytes()
        self.switch('S_Instance',[0],'/Game/New.New')
        self.assertFalse(self.stage.GetPrimAtPath('/World/Group/I1').IsInstance())
        self.assertTrue(self.stage.GetPrimAtPath('/World/Group/I2').IsInstance())
        self.assertEqual(self.asset(self.stage.GetPrimAtPath('/World/Group/I2/Mesh')),'/Game/Old.Old')
        self.root.write_bytes(before);self.stage.GetRootLayer().Reload(True)
        self.assertTrue(self.stage.GetPrimAtPath('/World/Group/I1').IsInstance())


if __name__ == '__main__': unittest.main()
