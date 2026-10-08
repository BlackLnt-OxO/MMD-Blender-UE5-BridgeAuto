import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


def test_managed_usd_save_temporarily_saves_layers_and_restores_user_policy_on_error(tmp_path):
    root='/Game/test/A_Character';owner='Character'
    world=SimpleNamespace(get_path_name=lambda:root+'/Maps/MMD_Preview.MMD_Preview')
    class Stage:
        tags=['MMDBridge']
        def get_editor_property(self,name):
            return SimpleNamespace(file_path=str(tmp_path/'Content/test/A_Character/Source/Scene/Scene.usda'))
    values={'show_save_layers_dialog_when_saving':'prompt'}
    settings=SimpleNamespace(get_editor_property=values.get,set_editor_property=values.__setitem__)
    actors=[Stage()]
    native=SimpleNamespace(UsdProjectSettings=object,UsdSaveDialogBehavior=SimpleNamespace(ALWAYS_SAVE='always'),
        UsdStageActor=Stage,EditorActorSubsystem=object,
        get_editor_subsystem=lambda kind:SimpleNamespace(get_all_level_actors=lambda:actors),
        get_default_object=lambda kind:settings,
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda *args:owner),
        Paths=SimpleNamespace(project_dir=lambda:str(tmp_path),convert_relative_path_to_full=lambda path:path))
    source=Path(__file__).parents[1]/'mmd_bridge/ue_host.py'
    method=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='save_generated_level')
    namespace={'u':native,'Path':Path}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),namespace)
    def save():
        assert values['show_save_layers_dialog_when_saving']=='always'
        raise RuntimeError('Disk save failed')
    with pytest.raises(RuntimeError,match='Disk save failed'):
        namespace['save_generated_level'](SimpleNamespace(save_current_level=save),world,root,owner)
    assert values['show_save_layers_dialog_when_saving']=='prompt'
    actors[0].tags=[]
    calls=[]
    def user_save():calls.append(values['show_save_layers_dialog_when_saving']);return True
    assert namespace['save_generated_level'](SimpleNamespace(save_current_level=user_save),world,root,owner)
    assert calls==['prompt']


def test_stage_checkpoint_saves_mesh_display_material_without_changing_graph():
    root='/Game/test/Scene';owner='Scene'
    class Asset:
        def __init__(self,path):self.path=path
        def get_path_name(self):return self.path
    class Mesh(Asset):
        def get_editor_property(self,name):
            assert name=='static_materials'
            return [SimpleNamespace(get_editor_property=lambda name,material=m:material) for m in materials]
    materials=[Asset(root+'/Scene/Generated/MI_DisplayColor.MI_DisplayColor'),
               Asset('/Engine/EngineMaterials/DefaultMaterial.DefaultMaterial')]
    mesh=Mesh(root+'/Scene/Generated/SM_Mesh.SM_Mesh');saved=[];tags=[]
    native=SimpleNamespace(StaticMesh=Mesh,load_asset=lambda path:None,
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda *args:'',
            set_metadata_tag=lambda asset,key,value:tags.append((asset.path,value)),
            save_loaded_asset=lambda asset:saved.append(asset.path) or True))
    source=Path(__file__).parents[1]/'mmd_bridge/ue_usd.py'
    method=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(n,ast.FunctionDef) and n.name=='save_stage_assets')
    namespace={'u':native,'_generated_assets':lambda *args:([mesh],{})}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),namespace)
    result=namespace['save_stage_assets'](object(),{},root,owner)
    assert result==saved==[mesh.path,materials[0].path]
    assert tags==[(mesh.path,owner),(materials[0].path,owner)]
