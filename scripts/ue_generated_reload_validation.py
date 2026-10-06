"""Verify Generated UV carriers survive native USD import and saved-level reload."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import atomic_json,read_json
out=ROOT/'work/material_functions_implementation/generated'
report={'complete':False,'project':u.Paths.get_project_file_path(),'checks':[]}
try:
    assert Path(report['project']).stem=='TESTON1_1005_02'
    source=read_json(out/'result.json')
    from mmd_bridge.ue_materials import Materials
    converter=Materials('/Game/MMDBridgeValidation/FunctionsV3/A_fixture','FunctionValidation',report,
                        {'restore_scene_appearance':True,'use_scene_master_material':False})
    converter.build(source['material'],out)
    actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    world=u.EditorLoadingAndSavingUtils.new_blank_map(False)
    stage=actors.spawn_actor_from_class(u.UsdStageActor,u.Vector())
    stage.set_root_layer(str(out/'scene.usda'))
    def inspect(stage):
        component=stage.get_generated_component('/World/Generated/Mesh')
        assert isinstance(component,u.StaticMeshComponent),str(component)
        dynamic=u.DynamicMesh()
        u.GeometryScript_AssetUtils.copy_mesh_from_static_mesh(component.static_mesh,dynamic,
            u.GeometryScriptCopyMeshFromAssetOptions(),u.GeometryScriptMeshReadLOD())
        arrays=[]
        methods=[getattr(u.GeometryScript_UVs,n) for n in dir(u.GeometryScript_UVs)
                 if n.startswith('get_mesh_per_vertex')]
        assert len(methods)==1,[m.__name__ for m in methods]
        report['uv_api']=methods[0].__doc__
        for index,channel in source['indices']:
            result=methods[0](dynamic,index)
            assert result[2],str(result)
            values=u.GeometryScript_List.convert_uv_list_to_array(result[1])
            assert values
            scalar=[v.x if channel==0 else 1-v.y for v in values]
            assert all(min(abs(v),abs(v-1))<1e-5 for v in scalar)
            assert {round(v) for v in scalar}=={0,1}
            arrays.append(scalar)
        # All three independent scalars must reconstruct all cube corners.
        assert len({tuple(round(arrays[a][i]) for a in range(3))
                    for i in range(len(arrays[0]))})==8
        return arrays
    before=inspect(stage)
    level='/Game/MMDBridgeValidation/FunctionsV2/GeneratedReload'
    assert u.EditorLoadingAndSavingUtils.save_map(world,level)
    u.EditorLoadingAndSavingUtils.new_blank_map(False)
    assert u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(level)
    stages=[a for a in actors.get_all_level_actors() if isinstance(a,u.UsdStageActor)]
    assert len(stages)==1
    after=inspect(stages[0]);assert before==after
    report.update(complete=True,level=level,indices=source['indices'],uvs=after)
except Exception:report['error']=traceback.format_exc();u.log_error(report['error'])
atomic_json(out/'ue_reload_result.json',report)
print('GENERATED_RELOAD_PASS',report['complete'],flush=True)
