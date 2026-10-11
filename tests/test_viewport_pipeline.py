import ast
import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest
from mmd_bridge.controller import install_viewport_helper,install_ue_bridge


def test_native_viewport_reader_changes_fog_route_without_changing_post_process(tmp_path):
    module=Path(__file__).parents[1]/'mmd_bridge/ue_appearance.py'
    tree=ast.parse(module.read_text(encoding='utf-8-sig'))
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='restore_viewport_look')
    (tmp_path/'package.json').write_text('{}')
    class Stage:
        def get_editor_property(self,name):return N(file_path=str(tmp_path/'Scene.usda'))
    class Look:
        def get_actor_label(self):return 'Source render look'
        def get_editor_property(self,name):return N(dynamic_global_illumination_method=1)
    class Proxy:
        tags=['MMDBridge.NativeFog={}']
    mode=['raster'];routes=[]
    world=N(get_path_name=lambda:'/Game/Test.Test')
    editor=N(get_editor_world=lambda:world)
    level=N(get_active_viewport_config_key=lambda:'Viewport1')
    native=N(UnrealEditorSubsystem=object(),LevelEditorSubsystem=object(),StaticMeshActor=Proxy,
        UsdStageActor=Stage,PostProcessVolume=Look,DynamicGlobalIlluminationMethod=N(LUMEN=1),
        MMDBridgeViewportLibrary=N(get_active_viewport_pipeline=lambda:mode[0]))
    native.get_editor_subsystem=lambda cls:editor if cls is native.UnrealEditorSubsystem else level
    record=dict(viewport_render_settings=dict(auto_ocio=False,fog_occlusion_quality='native_balanced'),volumes=[])
    scope=dict(u=native,Path=Path,_ACTIVE_LOOK=None,read_json=lambda p:record,
               restore_stage_components=lambda a:None,
               viewport_volume_settings=lambda enabled,pipeline=None,*args:routes.append((enabled,pipeline)))
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'viewport-mode','exec'),scope)
    actors=N(get_all_level_actors=lambda:[Look(),Stage(),Proxy()])
    for value in ['raster','raster','path_tracing','path_tracing','raster']:
        mode[0]=value;scope['restore_viewport_look'](actors)
    assert [pipeline for enabled,pipeline in routes if enabled]==['lumen','path_tracing','lumen']


@pytest.mark.parametrize('bundle_id,engine_id,installed',[('test','test',True),('test','other',False),(None,None,False)])
def test_editor_helper_requires_matching_engine_build(tmp_path,bundle_id,engine_id,installed):
    source=tmp_path/'resources/plugins/unreal/MMDBridgeViewport'
    (source/'Binaries/Win64').mkdir(parents=True)
    (source/'Binaries/Win64/UnrealEditor.modules').write_text(json.dumps(dict(BuildId=bundle_id)))
    (source/'MMDBridgeViewport.uplugin').write_text('{}')
    engine=tmp_path/'engine/UnrealEditor.exe';engine.parent.mkdir()
    engine.with_name('UnrealEditor.modules').write_text(json.dumps(dict(BuildId=engine_id)))
    project=tmp_path/'project/Test.uproject';project.parent.mkdir()
    assert install_viewport_helper(project,tmp_path/'resources',engine)==installed
    target=project.parent/'Plugins/MMDBridgeViewport'
    assert target.exists()==installed
    if installed:assert (target/'.mmd_bridge_managed').is_file()


def test_engine_change_disables_previously_managed_viewport_binary(tmp_path,monkeypatch):
    project=tmp_path/'Test.uproject'
    project.write_text(json.dumps(dict(Plugins=[dict(Name='MMDBridgeViewport',Enabled=True)])))
    marker=tmp_path/'Plugins/MMDBridgeViewport/.mmd_bridge_managed';marker.parent.mkdir(parents=True);marker.touch()
    monkeypatch.setattr('mmd_bridge.controller.install_viewport_helper',lambda *args:False)
    install_ue_bridge(project,tmp_path/'home')
    entry=next(p for p in json.loads(project.read_text())['Plugins'] if p['Name']=='MMDBridgeViewport')
    assert entry['Enabled'] is False
