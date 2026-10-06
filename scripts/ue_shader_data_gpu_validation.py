"""Read actual raster pixels for ordinary CPD and non-Nanite instance data."""
import sys,traceback,copy,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
out=ROOT/'work/material_functions_implementation/shader_data'
report={'complete':False,'project':u.Paths.get_project_file_path()}
try:
    assert Path(report['project']).stem=='TESTON1_1005_02'
    converter=Materials('/Game/MMDBridgeValidation/FunctionsV4/Data','DataValidation',report,
                        {'restore_scene_appearance':True,'use_scene_master_material':False})
    record=copy.deepcopy(read_json(ROOT/'work/material_functions_implementation/generated/result.json')['material'])
    record.update(id='data_gpu',asset_name='M_Data',name='DataPixelFixture',water=False)
    record.pop('surface_graph',None);record.pop('function_definitions',None)
    from mmd_bridge.material_graph import FunctionLibrary
    from mmd_bridge.material_uv import used_channels,relocate_record
    library=FunctionLibrary()
    carrier=library.intern({'op':'mesh_scalar','uv_index':6,'component':0},{})
    record['channels']['base_color']={'op':'combine','mode':'RGB','components':[
        {'op':'object_random','primitive_index':0,'instance_index':0},
        carrier, {'op':'constant','value':.3}]}
    assert used_channels([record],library)==[0,6]
    record=relocate_record(record,{0:0,6:1},library)
    instance=converter.build(record,out);parent=instance.get_editor_property('parent')
    converter.lib.set_material_usage(parent,u.MaterialUsage.MATUSAGE_INSTANCED_STATIC_MESHES)
    parent.set_editor_property('two_sided',True)
    converter.lib.recompile_material(parent);assert u.EditorAssetLibrary.save_loaded_asset(parent);converter.protect_asset(parent)
    actors=u.get_editor_subsystem(u.EditorActorSubsystem);world=u.EditorLoadingAndSavingUtils.new_blank_map(False)
    from mmd_bridge.ue_shader_data import restore,asset_dependencies
    destination=Path(u.Paths.project_content_dir())/'MMDBridgeValidation/FunctionsV4/Data/Source'
    destination.mkdir(parents=True,exist_ok=True);shutil.copy2(out/'native_data.usda',destination/'native_data.usda')
    records=[{'prim_path':'/World/Ordinary','object_random':.1},{'prim_path':'/World/Instancer','instance_index':0,'object_random':.8}]
    atomic_json(destination/'package.json',{'shader_data':records})
    stage=actors.spawn_actor_from_class(u.UsdStageActor,u.Vector())
    stage.set_render_context(u.Name('unreal'));stage.set_root_layer(str(destination/'native_data.usda'))
    report['initial_restore']=restore(stage)
    level='/Game/MMDBridgeValidation/FunctionsV4/Data/Maps/DataReload'
    assert u.EditorLoadingAndSavingUtils.save_map(world,level)
    u.EditorLoadingAndSavingUtils.new_blank_map(False)
    assert u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(level)
    world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    stage=next(a for a in actors.get_all_level_actors() if isinstance(a,u.UsdStageActor))
    report['reloaded_restore']=restore(stage)
    report['source_packages']=asset_dependencies([instance.get_path_name()])
    ordinary=stage.get_generated_component('/World/Ordinary/Mesh')
    instanced=stage.get_generated_component('/World/Instancer')
    report['components']=[str(ordinary),str(instanced)]
    if not isinstance(instanced,u.InstancedStaticMeshComponent):
        children=instanced.get_children_components(True)
        instanced=next(c for c in children if isinstance(c,u.InstancedStaticMeshComponent))
    assert isinstance(ordinary,u.StaticMeshComponent) and instanced.get_instance_count()==1
    assert ordinary.get_material(0)==instance and instanced.get_material(0)==instance,[str(ordinary.get_material(0)),str(instanced.get_material(0))]
    capture=actors.spawn_actor_from_class(u.SceneCapture2D,u.Vector(0,0,500),u.Rotator(pitch=-90,yaw=0,roll=0))
    center,extent,radius=u.SystemLibrary.get_component_bounds(ordinary)
    other,_,_=u.SystemLibrary.get_component_bounds(instanced)
    target_position=(center+other)*.5
    capture.set_actor_location(target_position+u.Vector(0,0,500),False,True)
    capture.set_actor_rotation(u.Rotator(pitch=-90,yaw=0,roll=0),True)
    report['bounds']=[str(center),str(other)]
    report['camera']=[str(capture.get_actor_location()),str(capture.get_actor_rotation())]
    component=capture.get_component_by_class(u.SceneCaptureComponent2D)
    target=u.RenderingLibrary.create_render_target2d(world,64,64,u.TextureRenderTargetFormat.RTF_RGBA32F)
    component.set_editor_property('texture_target',target)
    component.set_editor_property('projection_type',u.CameraProjectionMode.ORTHOGRAPHIC)
    component.set_editor_property('ortho_width',400.)
    component.set_editor_property('capture_source',u.SceneCaptureSource.SCS_BASE_COLOR)
    component.capture_scene()
    report['capture_api']=component.capture_scene.__doc__
    pixels=[u.RenderingLibrary.read_render_target_raw_pixel(world,target,x,32,False) for x in (16,48)]
    report['pixels']=[[c.r,c.g,c.b,c.a] for c in pixels]
    assert max(abs(a-b) for a,b in zip(sorted(c.r for c in pixels),(.1,.8)))<.02,report['pixels']
    assert all(abs(c.g-.2)<.02 and abs(c.b-.3)<.02 for c in pixels),report['pixels']
    report.update(complete=True,scope='ordinary_cpd_and_non_nanite_instance_raster_pixels')
except Exception:report['error']=traceback.format_exc();u.log_error(report['error'])
atomic_json(out/'ue_gpu_result.json',report)
print('SHADER_DATA_GPU_PASS',report['complete'],flush=True)
