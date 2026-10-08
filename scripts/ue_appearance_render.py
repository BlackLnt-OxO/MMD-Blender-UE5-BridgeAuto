"""Start from UE -ExecCmds=\"py absolute_script_path\"; native MRQ owns the asynchronous render."""
import sys,time,traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json,Job
from mmd_bridge import process
from mmd_bridge.ue_appearance import restore_viewport_look

request=read_json(ROOT/'work/appearance_render_request.json')
if request.get('surface_graph_smoke'):
    import runpy
    runpy.run_path(str(ROOT/'scripts/ue_surface_graph_smoke.py'))
    if not read_json(ROOT/'work/surface_graph_smoke/ue_result.json').get('complete'):
        u.SystemLibrary.quit_editor()
        raise RuntimeError('原生闭包检查失败，停止场景导入')
folder=Path(request['job'])
job=Job(folder)
cfg=job.data['settings']
out=folder/'visual'
out.mkdir(exist_ok=True)
atomic_json(out/'ue_render.json',{'complete':False,'state':'starting'})
state={'started':False,'tick':None,'executor':None,'paused':False,'check_at':0.,'pie_checked':False,'normal_overrides':{},'color_overrides':{},'diagnostic_errors':[]}

def job_started(render_job):
    """Diagnostic variants affect only the transient PIE world, before MRQ initialization."""
    state['pie_checked']=False
    variant=read_json_text(render_job.get_editor_property('user_data'))
    changed=[]
    replacements=state['normal_overrides'] if variant.get('disable_closure_normals') else state['color_overrides'] if variant.get('material_colors') else {}
    if replacements:
        world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world()
        if not world:
            raise RuntimeError('诊断渲染缺少 PIE 世界')
        for actor in u.GameplayStatics.get_all_actors_of_class(world,u.Actor):
            for component in actor.get_components_by_class(u.StaticMeshComponent):
                for index in range(component.get_num_materials()):
                    material=component.get_material(index)
                    replacement=replacements.get(material.get_path_name()) if material else None
                    if replacement:
                        component.set_material(index,u.load_asset(replacement))
                        changed.append(component.get_path_name())
        if not changed:
            state['diagnostic_errors'].append('材质诊断未匹配任何组件: '+variant.get('output_name','ue'))
    if variant.get('disable_environment'):
        world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world()
        for actor in u.GameplayStatics.get_all_actors_of_class(world,u.Actor):
            sky=actor.get_component_by_class(u.SkyLightComponent)
            if sky:
                sky.set_intensity(0.)
                changed.append(actor.get_path_name())
            if actor.get_actor_label()=='Source sky background':
                actor.set_actor_hidden_in_game(True)
                changed.append(actor.get_path_name())
    atomic_json(out/('diagnostic_'+variant.get('output_name','ue')+'.json'),{'variant':variant,'material_overrides':changed,'complete':not replacements or bool(changed)})

def read_json_text(value):
    import json
    return json.loads(value or '{}')

def done(executor,success):
    if state['paused']:
        state['executor']=None
        job.status('ue_wait_vram','渲染已暂停并释放运行实例，等待 GPU 内存降至恢复阈值')
        return
    from mmd_bridge import ue_appearance,ue_materials
    from mmd_bridge.core import file_hash
    atomic_json(out/'ue_render.json',{'complete':success and not state['diagnostic_errors'],'diagnostic_errors':state['diagnostic_errors'],'gpu':process.gpu_memory_info(),
                'appearance_code':ue_appearance.__file__,'appearance_code_sha256':file_hash(ue_appearance.__file__),
                'materials_code':ue_materials.__file__,'materials_code_sha256':file_hash(ue_materials.__file__)})
    job.status('needs_review' if success and not state['diagnostic_errors'] else 'failed',
               '原生参考帧渲染完成；画面差异需复核' if success else '原生参考帧渲染失败',
               render_report=str(out/'ue_render.json'))
    u.SystemLibrary.quit_editor()

def start_render():
    executor=u.MoviePipelinePIEExecutor()
    executor.on_executor_finished_delegate.add_callable_unique(done)
    executor.on_individual_job_started_delegate.add_callable_unique(job_started)
    state['executor']=executor
    u.get_editor_subsystem(u.MoviePipelineQueueSubsystem).render_queue_with_executor_instance(executor)

def tick(delta):
    if state['started']:
        if time.monotonic()<state['check_at']:
            return
        state['check_at']=time.monotonic()+2
        if (folder/'stop').exists():
            state['paused']=False
            if state['executor']:
                state['executor'].cancel_all_jobs()
            else:
                atomic_json(out/'ue_render.json',{'complete':False,'cancelled':True})
                u.SystemLibrary.quit_editor()
            job.status('stopped','渲染已停止，保留导出缓存及关卡')
            return
        usage=process.gpu_budget_percent(process.gpu_memory_info()) or 0
        if state['paused']:
            if state['executor'] is None and usage<cfg.get('ue_vram_resume_percent',80):
                state['paused']=False
                job.status('ue_render','GPU 内存已降至恢复阈值，重新渲染参考帧')
                start_render()
        elif cfg.get('render_vram_guard_enabled',False) and usage>=cfg.get('ue_vram_pause_percent',90):
            state['paused']=True
            job.status('ue_wait_vram','GPU 专用加共享内存达到暂停阈值，保留关卡并暂停渲染')
            if state['executor']:
                state['executor'].cancel_all_jobs()
        elif not state['pie_checked']:
            world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world()
            if world:
                actors=u.GameplayStatics.get_all_actors_of_class(world,u.Actor)
                details=[]
                for a in actors:
                    if isinstance(a,u.HeterogeneousVolume):
                        c=a.get_component_by_class(u.HeterogeneousVolumeComponent)
                        material=c.get_material(0)
                        details.append({'label':a.get_actor_label(),'bounds':str(a.get_actor_bounds(False)),
                            'resolution':str(c.get_editor_property('volume_resolution')),'tick_enabled':c.is_component_tick_enabled(),
                            'material':str(material),'registered':c.is_registered() if hasattr(c,'is_registered') else None,
                            'texture':str(u.MaterialEditingLibrary.get_material_instance_sparse_volume_texture_parameter_value(material,'Volume'))})
                lights=[]
                for a in actors:
                    c=a.get_component_by_class(u.LightComponent)
                    if c:
                        lights.append({'label':a.get_actor_label(),'mobility':str(c.get_editor_property('mobility')),
                                       'intensity':c.get_editor_property('intensity')})
                atomic_json(out/'pie.json',{'actors':[{'class':a.get_class().get_name(),'label':a.get_actor_label(),
                            'location':str(a.get_actor_location()),'rotation':str(a.get_actor_rotation())} for a in actors],
                            'volumes':details,'lights':lights})
                state['pie_checked']=True
        return
    if (u.AssetRegistryHelpers.get_asset_registry().is_loading_assets() or
            u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world() is None):
        return
    state['started']=True
    try:
        from mmd_bridge import ue_host
        ue_host.BUSY=True  # Prevent bridge tick reentry while the editor is changing worlds.
        if request.get('import_first'):
            from mmd_bridge.ue_host import import_job
            import_job(folder)
        report=read_json(folder/'ue_report.json')
        if 'level' not in report['assets']:
            status=read_json(folder/'status.json')
            atomic_json(out/'ue_render.json',{'complete':False,'import_state':status['state'],
                        'note':'导入检查点已保留；释放编辑器后按恢复阈值重试'})
            u.SystemLibrary.quit_editor()
            return
        subsystem=u.get_editor_subsystem(u.LevelEditorSubsystem)
        atomic_json(out/'ue_render.json',{'complete':False,'state':'loading_saved_level'})
        assert subsystem.load_level(report['assets']['level'])
        actor_sub=u.get_editor_subsystem(u.EditorActorSubsystem)
        if request.get('affine_smoke'):
            fixture=read_json(ROOT/'work/usd_affine_smoke/expected.json')
            probe=actor_sub.spawn_actor_from_class(u.UsdStageActor,u.Vector())
            try:
                probe.set_actor_rotation(u.Rotator(pitch=0,yaw=-90,roll=0),False)
                probe.set_root_layer(fixture['root_layer'])
                checks=[]
                for item in fixture['instances']:
                    component=probe.get_generated_component(item['prim_path']+'/Mesh')
                    assert isinstance(component,u.StaticMeshComponent),item['prim_path']
                    low,high=component.get_local_bounds()
                    transform=component.get_world_transform()
                    points=[u.MathLibrary.transform_location(transform,u.Vector(x,y,z)) for x in (low.x,high.x) for y in (low.y,high.y) for z in (low.z,high.z)]
                    actual=[[min(getattr(p,a) for p in points) for a in ('x','y','z')],
                            [max(getattr(p,a) for p in points) for a in ('x','y','z')]]
                    lo,hi=item['bounds']['min'],item['bounds']['max']
                    expected=[[-hi[1]*100,-hi[0]*100,lo[2]*100],[-lo[1]*100,-lo[0]*100,hi[2]*100]]
                    error=max(abs(actual[r][c]-expected[r][c]) for r in range(2) for c in range(3))
                    assert error<.02,(item['prim_path'],actual,expected,error)
                    checks.append({'prim_path':item['prim_path'],'bounds':actual,'error_cm':error,
                                   'affine_baked':item['affine_baked'],'uv_channels':u.EditorStaticMeshLibrary.get_num_uv_channels(component.get_editor_property('static_mesh'),0)})
                atomic_json(ROOT/'work/usd_affine_smoke/ue_result.json',{'complete':True,'checks':checks})
            finally:
                probe.set_root_layer('')
                actor_sub.destroy_actor(probe)
        ue_host.restore_scene_actor_labels()
        restore_viewport_look(actor_sub)
        records=read_json(folder/'manifest.json')['scene']['materials']
        color_probes={identity:color for variant in request.get('variants',[]) for identity,color in variant.get('material_colors',{}).items()}
        if request.get('rebuild_cutouts') or request.get('rebuild_glass') or color_probes or any(v.get('disable_closure_normals') for v in request.get('variants',[])):
            from mmd_bridge.ue_materials import Materials
            from mmd_bridge.material_policy import cutout_transmission
            import copy
            converter=Materials(report['root'],job.data['asset_id'],report,cfg)
            for record in records:
                if record['id'] in color_probes:
                    diagnostic=copy.deepcopy(record)
                    diagnostic.pop('surface_graph',None)
                    diagnostic['id'] += '_color_probe'
                    diagnostic['asset_name'] += '_ColorProbe'
                    color={'op':'constant','value':color_probes[record['id']]}
                    diagnostic['channels']['base_color']=color
                    diagnostic['channels']['emission']=color
                    diagnostic['channels']['emission_strength']={'op':'constant','value':1}
                    diagnostic['channels'].pop('normal',None)
                    original=u.load_asset(report['assets']['materials'][record['id']]).get_path_name()
                    state['color_overrides'][original]=converter.build(diagnostic,folder).get_path_name()
                if request.get('rebuild_cutouts') and cutout_transmission(record):
                    converter.build(record,folder)
                if request.get('rebuild_glass') and record.get('surface')=='glass':
                    converter.build(record,folder)
                if record.get('surface_graph') and any(v.get('disable_closure_normals') for v in request.get('variants',[])):
                    diagnostic=copy.deepcopy(record)
                    diagnostic['id'] += '_normal_probe'
                    diagnostic['asset_name'] += '_NormalProbe'
                    def strip_normals(value):
                        if isinstance(value,dict):
                            value.pop('normal',None)
                            for child in value.values():strip_normals(child)
                        elif isinstance(value,list):
                            for child in value:strip_normals(child)
                    strip_normals(diagnostic)
                    original=u.load_asset(report['assets']['materials'][record['id']]).get_path_name()
                    replacement=converter.build(diagnostic,folder).get_path_name()
                    state['normal_overrides'][original]=replacement
            cutout_checks=[]
            for record in records:
                if cutout_transmission(record):
                    material=u.load_asset(report['assets']['materials'][record['id']]).get_editor_property('parent')
                    mask=u.MaterialEditingLibrary.get_material_property_input_node(material,u.MaterialProperty.MP_OPACITY_MASK)
                    assert material.get_editor_property('blend_mode')==u.BlendMode.BLEND_MASKED and mask
                    cutout_checks.append({'source_id':record['id'],'material':material.get_path_name(),
                        'blend':str(material.get_editor_property('blend_mode')),'model':str(material.get_editor_property('shading_model')),'mask_connected':bool(mask)})
            atomic_json(out/'cutout_materials.json',{'complete':True,'materials':cutout_checks})
        glass_checks=[]
        for record in records:
            if record.get('surface')=='glass':
                material=u.load_asset(report['assets']['materials'][record['id']]).get_editor_property('parent')
                front=u.MaterialEditingLibrary.get_material_property_input_node(material,u.MaterialProperty.MP_FRONT_MATERIAL)
                inputs=u.MaterialEditingLibrary.get_inputs_for_material_expression(material,front)
                names=u.MaterialEditingLibrary.get_material_expression_input_names(front)
                attached={str(name):bool(value) for name,value in zip(names,inputs)}
                assert u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.GlassColor')=='3'
                assert attached.get('F0') and attached.get('F90'),attached
                glass_checks.append({'id':record['id'],'name':record['name'],'source_color':record['channels']['base_color'],
                                     'color_version':3,'two_sided':material.get_editor_property('two_sided'),
                                     'inputs':attached,'material':material.get_path_name()})
        atomic_json(out/'glass_color.json',{'complete':True,'materials':glass_checks})
        atomic_json(out/'ue_render.json',{'complete':False,'state':'checking_material_graphs'})
        graph_failures=[]
        compile_errors={}
        graph_ids={m['id'] for m in read_json(folder/'manifest.json')['scene']['materials'] if m.get('surface_graph')}
        for identity,path in report['assets'].get('materials',{}).items():
            instance=u.load_asset(path)
            parent=instance.get_editor_property('parent')
            if identity in graph_ids:
                atomic_json(out/'ue_render.json',{'complete':False,'state':'compiling_closure','material':identity})
            errors=u.MaterialEditingLibrary.recompile_material(parent) if identity in graph_ids else []
            if errors:
                compile_errors[parent.get_path_name()]=[str(e) for e in errors]
            pending=[u.MaterialEditingLibrary.get_material_property_input_node(parent,p) for p in
                     (u.MaterialProperty.MP_BASE_COLOR,u.MaterialProperty.MP_NORMAL,u.MaterialProperty.MP_EMISSIVE_COLOR,
                      u.MaterialProperty.MP_FRONT_MATERIAL)]
            visited=set()
            while pending:
                node=pending.pop()
                if not node or node.get_path_name() in visited:
                    continue
                visited.add(node.get_path_name())
                inputs=u.MaterialEditingLibrary.get_inputs_for_material_expression(parent,node)
                if node.get_class().get_name() in ('MaterialExpressionNormalize','MaterialExpressionComponentMask') and not any(inputs):
                    graph_failures.append(node.get_path_name())
                pending.extend(n for n in inputs if n)
        atomic_json(out/'material_graph.json',{'complete':not graph_failures and not compile_errors,
                                            'missing_inputs':graph_failures,'compile_errors':compile_errors})
        translucent=[]
        for identity,path in report['assets'].get('materials',{}).items():
            parent=u.load_asset(path).get_editor_property('parent')
            blend=parent.get_editor_property('blend_mode')
            if blend in (u.BlendMode.BLEND_TRANSLUCENT,u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE):
                lighting=parent.get_editor_property('translucency_lighting_mode')
                assert lighting==u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING, parent.get_path_name()
                translucent.append({'source_id':identity,'material':parent.get_path_name(),
                                    'lighting':str(lighting),'refraction':str(parent.get_editor_property('refraction_method'))})
        atomic_json(out/'translucent_materials.json',{'complete':True,'materials':translucent})
        if graph_failures or compile_errors:
            raise RuntimeError('最终材质未通过原生编译及输入检查，停止视觉验收: '+str(graph_failures)+str(compile_errors))
        native=[]
        for actor in actor_sub.get_all_level_actors():
            if isinstance(actor,u.UsdStageActor):
                for volume in read_json(folder/'manifest.json')['scene'].get('volumes',[]):
                    component=actor.get_generated_component(volume.get('prim_path',''))
                    if isinstance(component,u.HeterogeneousVolumeComponent):
                        material=component.get_material(0)
                        texture=u.MaterialEditingLibrary.get_material_instance_sparse_volume_texture_parameter_value(material,'Volume')
                        parent=material.get_editor_property('parent')
                        front=u.MaterialEditingLibrary.get_material_property_input_node(parent,u.MaterialProperty.MP_FRONT_MATERIAL)
                        native.append({'label':component.get_owner().get_actor_label(),
                                       'material':material.get_path_name() if material else None,
                                       'texture':texture.get_path_name() if texture else None,
                                       'texture_format_a':str(texture.get_editor_property('format_a')),
                                       'texture_format_b':str(texture.get_editor_property('format_b')),
                                       'emissive_only':front.get_editor_property('emissive_only') if front else None,
                                       'shading_model':str(parent.get_editor_property('shading_model')),
                                       'bounds':str(component.get_owner().get_actor_bounds(False)),
                                       'transform':str(component.get_owner().get_actor_transform())})
        histogram={}
        fogs=[]
        uv_channels={}
        from mmd_bridge.material_policy import cutout_transmission
        cutout_ids={m['id'] for m in records if cutout_transmission(m)}
        mesh_transforms={}
        stage=next((a for a in actor_sub.get_all_level_actors() if isinstance(a,u.UsdStageActor)),None)
        if stage:
            for proto in read_json(folder/'manifest.json')['scene']['prototypes']:
                if not (graph_ids|cutout_ids).intersection(proto.get('material_slots',[])):
                    continue
                component=stage.get_generated_component(proto['prim_path']+'/Mesh')
                if isinstance(component,u.StaticMeshComponent):
                    mesh=component.get_editor_property('static_mesh')
                    uv_channels[proto['prim_path']]={'mesh':mesh.get_path_name(),
                        'uv_count':u.EditorStaticMeshLibrary.get_num_uv_channels(mesh,0)}
                    low,high=component.get_local_bounds()
                    transform=component.get_world_transform()
                    world=[u.MathLibrary.transform_location(transform,u.Vector(x,y,z)) for x in (low.x,high.x) for y in (low.y,high.y) for z in (low.z,high.z)]
                    mesh_transforms[proto['prim_path']]={'local_bounds':[str(low),str(high)],'transform':str(transform),
                        'world_bounds':[[min(getattr(p,a) for p in world) for a in ('x','y','z')],
                                        [max(getattr(p,a) for p in world) for a in ('x','y','z')]]}
            if request.get('affine_smoke'):
                from pxr import Usd,UsdGeom
                scene=read_json(folder/'manifest.json')['scene']
                source_stage=Usd.Stage.Open(scene['usd']['root_layer'])
                bounds_cache=UsdGeom.BBoxCache(Usd.TimeCode.Default(),[UsdGeom.Tokens.default_,UsdGeom.Tokens.render])
                cutout_protos={p['id'] for p in scene['prototypes'] if cutout_ids.intersection(p.get('material_slots',[]))}
                checks=[]
                for item in scene['instances']:
                    if item['prototype_id'] not in cutout_protos:
                        continue
                    component=stage.get_generated_component(item['prim_path']+'/Mesh')
                    assert isinstance(component,u.StaticMeshComponent),item['prim_path']
                    low,high=component.get_local_bounds()
                    transform=component.get_world_transform()
                    points=[u.MathLibrary.transform_location(transform,u.Vector(x,y,z)) for x in (low.x,high.x) for y in (low.y,high.y) for z in (low.z,high.z)]
                    actual=[[min(getattr(p,a) for p in points) for a in ('x','y','z')],
                            [max(getattr(p,a) for p in points) for a in ('x','y','z')]]
                    box=bounds_cache.ComputeWorldBound(source_stage.GetPrimAtPath(item['prim_path']+'/Mesh')).ComputeAlignedRange()
                    lo,hi=box.GetMin(),box.GetMax()
                    unit=scene['meters_per_unit']*100
                    expected=[[-hi[1]*unit,-hi[0]*unit,lo[2]*unit],[-lo[1]*unit,-lo[0]*unit,hi[2]*unit]]
                    error=max(abs(actual[r][c]-expected[r][c]) for r in range(2) for c in range(3))
                    assert error<.1,(item['prim_path'],actual,expected,error)
                    checks.append({'prim_path':item['prim_path'],'error_cm':error,'bounds_cm':actual})
                atomic_json(out/'cutout_geometry.json',{'complete':True,'instances':len(checks),
                              'max_error_cm':max((v['error_cm'] for v in checks),default=0),'checks':checks})
        for actor in actor_sub.get_all_level_actors():
            name=actor.get_class().get_name()
            histogram[name]=histogram.get(name,0)+1
            if isinstance(actor,u.ExponentialHeightFog):
                c=actor.get_component_by_class(u.ExponentialHeightFogComponent)
                fogs.append({key:str(c.get_editor_property(key)) for key in
                    ('fog_density','enable_volumetric_fog','volumetric_fog_distance','volumetric_fog_scattering_distribution','volumetric_fog_albedo')})
        atomic_json(out/'reload.json',{'native_volumes':native,'native_fog':fogs,
                    'actor_count':len(actor_sub.get_all_level_actors()),'actor_classes':histogram,'closure_mesh_uvs':uv_channels,
                    'surface_mesh_transforms':mesh_transforms})
        if read_json(folder/'manifest.json')['scene'].get('volumes') and not native and any(v.get('prim_path') for v in read_json(folder/'manifest.json')['scene']['volumes']):
            raise RuntimeError('保存重开后没有原生体积组件，不能继续视觉验收')
        queue_sub=u.get_editor_subsystem(u.MoviePipelineQueueSubsystem)
        queue=queue_sub.get_queue()
        queue.delete_all_jobs()
        for overrides in request.get('variants',[{}]):
            variant=dict(request,**overrides)
            variant.setdefault('path_tracing', cfg.get('scene_render_pipeline', 'path_tracing') == 'path_tracing')
            render_job=queue.allocate_new_job(u.MoviePipelineExecutorJob)
            render_job.set_editor_property('user_data',__import__('json').dumps(variant))
            render_job.map=u.SoftObjectPath(report['assets']['level'])
            render_job.sequence=u.SoftObjectPath(report['assets']['sequence'])
            render_job.set_configuration(u.load_asset(report['assets']['render_preset']))
            config=render_job.get_configuration()
            output=config.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
            reference=read_json(folder/'manifest.json')['scene']['reference_camera']
            output.output_resolution=u.IntPoint(*reference['resolution'])
            output.output_directory=u.DirectoryPath(str(out))
            output.file_name_format=variant.get('output_name','ue')
            if variant.get('console_variables'):
                cvars=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
                for name,value in variant['console_variables'].items():
                    cvars.add_or_update_console_variable(name,float(value))
            output.use_custom_playback_range=True
            output.custom_start_frame=cfg.get('scene_frame',1)
            output.custom_end_frame=output.custom_start_frame+1
            if variant.get('path_tracing'):
                deferred=config.find_setting_by_class(u.MoviePipelineDeferredPassBase)
                if deferred:
                    config.remove_setting(deferred)
                config.find_or_add_setting_by_class(u.MoviePipelineDeferredPass_PathTracer)
                cvars=config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
                cvars.add_or_update_console_variable('r.PathTracing.HeterogeneousVolumes',1.)
            else:
                config.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase)
            config.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
            aa=config.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
            aa.spatial_sample_count=variant.get('samples',32 if variant.get('path_tracing') else 1)
            aa.temporal_sample_count=1
            aa.engine_warm_up_count=60
            aa.render_warm_up_count=8
            atomic_json(out/('render_pipeline_'+variant.get('output_name','ue')+'.json'), {
                'selected_pipeline': cfg.get('scene_render_pipeline','path_tracing'),
                'actual_passes': [setting.get_class().get_name() for setting in config.get_all_settings()
                                  if isinstance(setting,u.MoviePipelineDeferredPassBase)],
                'path_tracing': bool(variant.get('path_tracing')), 'samples': aa.spatial_sample_count})
        info=process.gpu_memory_info()
        if cfg.get('render_vram_guard_enabled',False) and (process.gpu_budget_percent(info) or 0)>=cfg.get('ue_vram_pause_percent',90):
            state['paused']=True
            job.status('ue_wait_vram','渲染前 GPU 内存达到暂停阈值，等待降至恢复阈值')
        else:
            start_render()
    except Exception:
        atomic_json(out/'ue_render.json',{'complete':False,'error':traceback.format_exc()})
        u.SystemLibrary.quit_editor()

state['tick']=u.register_slate_post_tick_callback(tick)
