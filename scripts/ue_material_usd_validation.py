"""Native USD material/color reload proof, confined to validation assets."""
import sys,traceback,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json
out=ROOT/'work/material_functions_implementation'
report={'complete':False,'project':u.Paths.get_project_file_path(),'checks':[]}
try:
    assert Path(report['project']).stem=='TESTON1_1005_02'
    assert hasattr(u,'UsdStageActor'), 'USDImporter must be enabled for this process'
    world=u.EditorLoadingAndSavingUtils.new_blank_map(False)
    actors=u.get_editor_subsystem(u.EditorActorSubsystem)
    source=read_json(out/'blender_fixture/report.json')
    stage=actors.spawn_actor_from_class(u.UsdStageActor,u.Vector())
    stage.set_render_context(u.Name('unreal'))
    stage.set_root_layer(str(out/'blender_fixture/scene.usda'))
    component=stage.get_generated_component('/World/ColoredMesh/Mesh')
    assert isinstance(component,u.StaticMeshComponent),str(component)
    material=component.get_material(0)
    expected='/Game/MMDBridgeValidation/FunctionsV2/A_fixture/Materials/MI_'+source['materials'][0]['asset_name']
    assert material and material.get_path_name().split('.')[0]==expected,str(material)
    mesh=component.get_editor_property('static_mesh')
    libraries=[getattr(u,n) for n in dir(u) if 'GeometryScript' in n and hasattr(getattr(u,n),'copy_mesh_from_static_mesh')]
    report['geometry_libraries']=[n for n in dir(u) if 'GeometryScript' in n]
    assert libraries,'GeometryScript mesh-copy API must be loaded'
    library=libraries[0]
    report['geometry_api']=library.copy_mesh_from_static_mesh.__doc__
    dynamic=u.DynamicMesh()
    copied=library.copy_mesh_from_static_mesh(mesh,dynamic,
        u.GeometryScriptCopyMeshFromAssetOptions(),u.GeometryScriptMeshReadLOD())
    colors=u.GeometryScript_VertexColors.get_mesh_per_vertex_colors(dynamic)
    report['color_result']=str(colors)
    report['color_api']=u.GeometryScript_VertexColors.get_mesh_per_vertex_colors.__doc__
    assert colors[2], 'Imported USD vertex colors must exist'
    color_list=colors[1]
    values=u.GeometryScript_List.convert_color_list_to_array(color_list)
    report['colors']=[[c.r,c.g,c.b,c.a] for c in values]
    decode=lambda x:x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4
    for color in values:
        assert max(abs(a-b) for a,b in zip((decode(color.r),decode(color.g),decode(color.b),color.a),(.2,.4,.6,1)))<.01, str(color)
    report['color_encoding']='linear displayColor imported as sRGB vertex values, decoded by material'
    level='/Game/MMDBridgeValidation/FunctionsV2/ColorReload'
    assert u.EditorLoadingAndSavingUtils.save_map(world,level)
    u.EditorLoadingAndSavingUtils.new_blank_map(False)
    assert u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(level)
    stages=[a for a in actors.get_all_level_actors() if isinstance(a,u.UsdStageActor)]
    assert len(stages)==1
    restored=stages[0].get_generated_component('/World/ColoredMesh/Mesh')
    assert restored and restored.get_material(0).get_path_name().split('.')[0]==expected
    report.update(complete=True,level=level,mesh=mesh.get_path_name(),material=material.get_path_name())
except Exception:
    report['error']=traceback.format_exc();u.log_error(report['error'])
atomic_json(out/'ue_usd_result.json',report)
u.log('USD_COLOR_VALIDATION='+json.dumps({'complete':report['complete'],'error':report.get('error')}))
