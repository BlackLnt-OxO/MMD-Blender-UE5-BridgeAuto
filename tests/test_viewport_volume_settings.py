import ast
from pathlib import Path
from types import SimpleNamespace


def test_viewport_uses_mrq_volume_values_and_restores_previous_project_values():
    # Run the shared native console boundary without importing editor-only classes.
    tree = ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_appearance.py').read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id in (
            '_VIEWPORT_VOLUME_DEFAULTS','VOLUME_RENDER_CVARS','LUMEN_RENDER_CVARS') for t in node.targets)) or (
        isinstance(node, ast.FunctionDef) and node.name == 'viewport_volume_settings')]
    values = {}
    def execute(world,command):
        name,value = command.split()
        values[name] = float(value)
    namespace = {'__name__': 'mmd_bridge.ue_appearance', 'u':SimpleNamespace(SystemLibrary=SimpleNamespace(
        get_console_variable_float_value=lambda name:values.get(name,7), execute_console_command=execute))}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'native-volume-boundary','exec'),namespace)
    set_viewport = namespace['viewport_volume_settings']
    set_viewport(True)
    assert values == namespace['VOLUME_RENDER_CVARS']
    set_viewport(True)
    set_viewport(False)
    assert set(values.values()) == {7}
    set_viewport(True, 'lumen', 'compatible')
    assert values['r.Lumen.HardwareRayTracing'] == values['r.RayTracing.Shadows'] == 0
    assert values['r.RayTracing.Geometry.StaticMeshes'] == 0
    set_viewport(False)
    assert set(values.values()) == {7}
    set_viewport(True, 'lumen', 'balanced')
    assert values['r.Lumen.HardwareRayTracing'] == 1
    assert values['r.RayTracing.Geometry.StaticMeshes'] == 7
    set_viewport(False)
    assert set(values.values()) == {7}
    assert not namespace['_VIEWPORT_VOLUME_DEFAULTS']
    set_viewport(True,'lumen')
    assert values['ShowFlag.PathTracing'] == 0
    assert values['r.HeterogeneousVolumes.MaxStepCount'] == 256
    assert values['r.Lumen.TranslucencyVolume.Enable'] == 1
    set_viewport(False)
    assert set(values.values()) == {7}


def test_ocio_restores_when_active_viewport_changes_in_same_world(tmp_path):
    tree = ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_appearance.py').read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'restore_viewport_look']
    content = tmp_path/'Content'
    source = content/'Generated/Source/Scene'
    source.mkdir(parents=True)
    (source/'package.json').write_text('{}')
    class Stage:
        def get_editor_property(self, name):
            return SimpleNamespace(file_path=str(source/'root.usda'))
    world = SimpleNamespace(get_path_name=lambda:'/Game/Generated/Maps/Preview.Preview')
    active = ['Viewport1']
    editor = SimpleNamespace(get_editor_world=lambda:world)
    level = SimpleNamespace(get_active_viewport_config_key=lambda:active[0])
    applied = []
    volumes = []
    restore_args = []
    def restore_volume_settings(enabled,pipeline=None,*args):
        volumes.append(enabled)
        restore_args.append((enabled,pipeline,args))
    native = SimpleNamespace(
        UnrealEditorSubsystem=object(), LevelEditorSubsystem=object(), UsdStageActor=Stage,
        PostProcessVolume=type('PostProcessVolume',(),{}),
        Paths=SimpleNamespace(project_content_dir=lambda:str(content)),
        EditorAssetLibrary=SimpleNamespace(load_asset=lambda path:'OCIO'),
        OpenColorIOEditorBlueprintLibrary=SimpleNamespace(set_active_viewport_configuration=applied.append))
    native.get_editor_subsystem = lambda cls: editor if cls is native.UnrealEditorSubsystem else level
    native.load_asset = native.EditorAssetLibrary.load_asset
    namespace = {'u':native, 'Path':Path, '_ACTIVE_LOOK':None,
                 'read_json':lambda path:{'render_look':{'config':'Appearance/config.ocio'},
                                         'viewport_render_settings':{'ue_render_memory_profile':'balanced',
                                                                     'foliage_distance_scale':3}},
                 'color_configuration':lambda asset,look:'Source Filmic',
                 'validate_color_configuration':lambda display:None,
                 'restore_stage_components':lambda actor:None,
                 'viewport_volume_settings':restore_volume_settings}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'native-viewport-boundary','exec'),namespace)
    restore = namespace['restore_viewport_look']
    actors = SimpleNamespace(get_all_level_actors=lambda:[Stage()])
    for viewport in ('Viewport1','Viewport1','Viewport2','Viewport2','Viewport1'):
        active[0] = viewport
        restore(actors)
    assert applied == ['Source Filmic']*5
    assert volumes == [False,True]*3
    assert all(args == (None,3) for enabled,pipeline,args in restore_args if enabled)
    active[0] = 'None'
    restore(actors)
    assert len(applied) == 5
