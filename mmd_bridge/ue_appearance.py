"""Preserved path-tracing appearance baseline: editable fog, lighting and OCIO.

Lumen reuses extracted source data; its rendering and acceptance remain separate.
"""
import math
from pathlib import Path

import unreal as u
from .ue_foliage import restore_stage_components
from .core import read_json,stable_id,file_hash
from .scene_appearance import (physical_light, RADIANCE_TO_UE, classify_appearance_reviews,
                               cached_scene_package, volume_sampling_review, fog_shadow_review)
from .ue_materials import prop

_ACTIVE_LOOK = None
_VIEWPORT_VOLUME_DEFAULTS = {}
VOLUME_RENDER_CVARS = {
    'r.HeterogeneousVolumes':1, 'r.HeterogeneousVolumes.Shadows':1,
    'r.HeterogeneousVolumes.DownsampleFactor':1, 'r.HeterogeneousVolumes.Preshading':0,
    'r.HeterogeneousVolumes.IndirectLighting':1, 'r.HeterogeneousVolumes.IndirectLighting.Mode':2,
    'r.Translucency.HeterogeneousVolumes':1,
}
LUMEN_RENDER_CVARS = {
    'r.DynamicGlobalIlluminationMethod':1, 'r.ReflectionMethod':1,
    'r.RayTracing.Shadows':1, 'r.Lumen.HardwareRayTracing':1,
    'r.Lumen.HardwareRayTracing.LightingMode':1,
    'r.Lumen.TranslucencyVolume.Enable':1,
    'r.VolumetricFog':1, 'r.VolumetricFog.GridPixelSize':8, 'r.VolumetricFog.GridSizeZ':128,
    'r.HeterogeneousVolumes.MaxStepCount':256,
    'r.HeterogeneousVolumes.Shadows.Resolution':512,
    'r.HeterogeneousVolumes.Shadows.Precision':0,
}


def configure_mrq_defaults(config, pipeline, memory_profile='balanced', foliage_distance_scale=2.,
                           fog_occlusion_quality='legacy', volumes=()):
    """Shared defaults for saved presets and single-frame reference jobs."""
    from .ue_render_policy import memory_cvars, lighting_cvars, OWNED_MEMORY_CVARS, foliage_distance_cvars, ensure_mrq_image_output
    ensure_mrq_image_output(config, pipeline, u)
    values = memory_cvars(memory_profile) if pipeline == 'lumen' else {}
    cvars = config.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    cvars.add_or_update_console_variable('ShowFlag.PathTracing', float(pipeline == 'path_tracing'))
    # Reusing a preset must not retain overrides from a previous memory profile.
    for name in OWNED_MEMORY_CVARS | set(foliage_distance_cvars()):
        cvars.remove_console_variable(name, True)
    if pipeline == 'lumen':
        for name, value in lighting_cvars(memory_profile).items():
            cvars.add_or_update_console_variable(name, float(value))
    for name, value in values.items():
        cvars.add_or_update_console_variable(name, float(value))
    if pipeline == 'lumen':
        for name, value in foliage_distance_cvars(foliage_distance_scale).items():
            cvars.add_or_update_console_variable(name, float(value))
    overrides = config.find_or_add_setting_by_class(u.MoviePipelineGameOverrideSetting)
    overrides.set_editor_property('soft_game_mode_override', u.MoviePipelineGameMode)
    overrides.cinematic_quality_settings = True
    overrides.texture_streaming = u.MoviePipelineTextureStreamingMethod.NONE
    overrides.use_lod_zero = False
    overrides.disable_hlods = False
    overrides.use_high_quality_shadows = False
    overrides.override_view_distance_scale = False
    if pipeline == 'lumen':
        overrides.flush_grass_streaming = memory_profile == 'quality'
        aa = config.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
        aa.spatial_sample_count, aa.temporal_sample_count = 1, 1
        aa.override_anti_aliasing = True
        aa.anti_aliasing_method = u.AntiAliasingMethod.AAM_TSR
    from .ue_fog_occlusion import configure
    configure(config,fog_occlusion_quality,pipeline,volumes)
    return overrides


def viewport_volume_settings(enabled, pipeline=None, memory_profile=None, foliage_distance_scale=2.,
                             fog_occlusion_quality='legacy', volumes=()):
    """Match volume quality while leaving the viewport render mode selectable."""
    from .fog_occlusion import TemporaryOverrides, decision
    controller = _VIEWPORT_VOLUME_DEFAULTS.setdefault('controller',TemporaryOverrides())
    read = u.SystemLibrary.get_console_variable_float_value
    write = lambda name,value: u.SystemLibrary.execute_console_command(None,name+' '+str(value))
    if not enabled:
        if _VIEWPORT_VOLUME_DEFAULTS.get('native_active'):
            from .ue_fog_froxel import release_native_streaming
            release_native_streaming()
        from .ue_fog_occlusion import restore_components
        restore_components()
        controller.end(read,write)
        _VIEWPORT_VOLUME_DEFAULTS.clear()
        return
    # 2 relinquishes the global show-flag override to each viewport's UI.
    # Explicit MRQ jobs retain their renderer-specific 0/1 in the transient preset.
    values = dict(VOLUME_RENDER_CVARS, **{'ShowFlag.PathTracing': 2})
    if pipeline == 'lumen':
        values.update(LUMEN_RENDER_CVARS)
        if memory_profile is not None:
            from .ue_render_policy import memory_cvars, lighting_cvars
            values.update(lighting_cvars(memory_profile))
            values.update(memory_cvars(memory_profile))
        from .ue_render_policy import foliage_distance_cvars
        values.update(foliage_distance_cvars(foliage_distance_scale))
    result=decision(fog_occlusion_quality,pipeline,volumes=volumes,
        engine=u.SystemLibrary.get_engine_version() if fog_occlusion_quality!='legacy' else None)
    from .native_fog_candidate import NATIVE_PROFILES
    if fog_occlusion_quality in NATIVE_PROFILES:
        from .ue_fog_occlusion import restore_components
        from .ue_fog_froxel import restore_native
        restore_components()
        world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        stages=u.GameplayStatics.get_all_actors_of_class(world,u.UsdStageActor) if world else []
        if world and stages:
            native=restore_native(world,stages,pipeline,result.get('effective') in NATIVE_PROFILES,volumes)
            _VIEWPORT_VOLUME_DEFAULTS['native_active']=native['renderer']=='volumetric_fog'
        else:
            result=dict(result,cvars={},status='limited',reviews=['等待原生雾及源 USD 组件加载'])
    elif fog_occlusion_quality in ('vsm_finer','vsm_nocache'):
        from .ue_fog_occlusion import apply_world
        world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
        stages=u.GameplayStatics.get_all_actors_of_class(world,u.UsdStageActor) if world else []
        if stages:
            result=apply_world(world,stages,volumes,fog_occlusion_quality,pipeline)
        else:
            result=dict(result,cvars={},status='limited',reviews=['等待源 USD 雾组件加载后应用实验补充方案'])
    else:
        from .ue_fog_occlusion import restore_components
        restore_components()
    values.update(result['cvars'])
    controller.apply(values,read,write)
    if result['reviews'] and _VIEWPORT_VOLUME_DEFAULTS.get('fog_status')!=result:
        u.log_warning('MMDBridge 雾光遮挡: '+ '; '.join(result['reviews']))
    _VIEWPORT_VOLUME_DEFAULTS['fog_status']=result


def lumen_post_process(settings):
    for name,value in {
        'dynamic_global_illumination_method':u.DynamicGlobalIlluminationMethod.LUMEN,
        'reflection_method':u.ReflectionMethod.LUMEN,
        'lumen_front_layer_translucency_reflections':True,
        'lumen_scene_lighting_quality':2., 'lumen_final_gather_quality':2.,
        'lumen_reflection_quality':2.,
    }.items():
        prop(settings,'override_'+name,True)
        prop(settings,name,value)


def restore_volume_materials(actor):
    """Restore only empty managed USD volume slots after regeneration / -game load."""
    from .core import read_json
    file = Path(actor.get_editor_property('root_layer').file_path)
    if not file.is_absolute():
        file = Path(u.Paths.project_dir()) / file
    file = file.resolve()
    content = Path(u.Paths.project_content_dir()).resolve()
    package = file.parent / 'package.json'
    if not file.is_relative_to(content) or not package.is_file():
        return []
    record = cached_scene_package(package)
    if not record.get('asset_id'):
        return []
    root = '/Game/' + file.parent.parent.parent.relative_to(content).as_posix()
    bindings = record.get('ue_volume_material_bindings')
    if bindings is None:
        # Compatibility for managed packages created before explicit bindings.
        bindings = [{'prim_path':v['prim_path'],
                     'material':root+'/Scene/Generated/MI_V_'+v['id']}
                    for v in record.get('volumes',[])
                    if v.get('status')=='converted' and v.get('prim_path')]
    restored=[]
    for binding in bindings:
        component=actor.get_generated_component(binding['prim_path'])
        if component is None:continue  # An unloaded USD prim is not a material slot.
        if not isinstance(component,u.HeterogeneousVolumeComponent):
            raise RuntimeError('体积恢复目标不是原生体积组件: '+binding['prim_path'])
        if component.get_material(0) is not None:continue
        path=binding['material']
        if not path.startswith(root+'/Scene/Generated/'):
            raise RuntimeError('体积恢复材质不属于当前受管理场景: '+path)
        material=u.load_asset(path)
        if material is None:
            raise RuntimeError('体积恢复材质资源缺失: '+path)
        component.set_material(0,material)
        if component.get_material(0) is None:
            raise RuntimeError('体积材质恢复失败: '+binding['prim_path'])
        restored.append(dict(prim_path=binding['prim_path'],material=material.get_path_name()))
    return restored


def preflight(scene):
    if not scene.get('render_look'):
        return
    missing = [name for name in ('OpenColorIOConfiguration','OpenColorIOEditorBlueprintLibrary',
                                'HeterogeneousVolumeComponent','MaterialExpressionSparseVolumeTextureSampleParameter')
               if not hasattr(u,name)]
    if missing:
        raise RuntimeError('转换前需要部署/启用 UE USDImporter、OpenColorIO 并重启编辑器；缺少 '+', '.join(missing))


def volume_materials(scene, materials, cfg):
    preflight(scene)
    for volume in scene.get('volumes',[]):
        if volume.get('status') != 'converted' or volume['kind'] == 'height_fog':
            continue
        m = materials.create('M_Volume_'+volume['id'],u.Material,u.MaterialFactoryNew())
        materials.material = m
        materials.clear_expressions(m)
        prop(m,'blend_mode',u.BlendMode.BLEND_ADDITIVE)
        prop(m,'material_domain',u.MaterialDomain.MD_VOLUME)
        prop(m,'shading_model',u.MaterialShadingModel.MSM_DEFAULT_LIT)
        empty=u.load_asset('/Engine/EngineResources/EmptySparseVolumeTexture')
        sample = materials.node('SparseVolumeTextureSampleParameter',parameter_name='Volume',sparse_volume_texture=empty)
        ext = materials.node('ComponentMask')
        materials.connect((sample[0],'Attributes A'),ext,'Input')
        for channel, enabled in (('r',True),('g',True),('b',True),('a',False)):
            prop(ext[0],channel,enabled)
        scaled = materials.op('Multiply',ext,materials.scalar('Fog density',cfg.get('fog_density_scale',1)))
        # Volume-domain Extinction is the RGB SubsurfaceColor input in UE 5.8.
        if not materials.lib.connect_material_property(scaled[0],scaled[1],u.MaterialProperty.MP_SUBSURFACE_COLOR):
            raise RuntimeError('无法连接体积消光')
        albedo = materials.node('ComponentMask')
        materials.connect((sample[0],'Attributes B'),albedo,'Input')
        for channel, enabled in (('r',True),('g',True),('b',True),('a',False)):
            prop(albedo[0],channel,enabled)
        materials.lib.connect_material_property(albedo[0],albedo[1],u.MaterialProperty.MP_BASE_COLOR)
        substrate=None
        if u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
            substrate=materials.node('SubstrateVolumetricFogCloudBSDF',emissive_only=False)
            materials.connect(scaled,substrate,'Extinction')
            materials.connect(albedo,substrate,'Albedo')
            if not materials.lib.connect_material_property(substrate[0],substrate[1],u.MaterialProperty.MP_FRONT_MATERIAL):
                raise RuntimeError('无法连接 Substrate 体积 BSDF')
        phase = materials.node('ComponentMask')
        materials.connect((sample[0],'Attributes A'),phase,'Input')
        for channel, enabled in (('r',False),('g',False),('b',False),('a',True)):
            prop(phase[0],channel,enabled)
        advanced = materials.node('VolumetricAdvancedMaterialOutput',gray_scale_material=False,
                                  per_sample_phase_evaluation=True)
        materials.connect(phase,advanced,'Phase G')
        if volume.get('emission_path'):
            emission = materials.node('SparseVolumeTextureSampleParameter',parameter_name='Emission',sparse_volume_texture=empty)
            mask = materials.node('ComponentMask')
            materials.connect((emission[0],'Attributes A'),mask,'Input')
            for channel, enabled in (('r',True),('g',True),('b',True),('a',False)):
                prop(mask[0],channel,enabled)
            mask=materials.op('Multiply',mask,materials.constant(RADIANCE_TO_UE))
            materials.lib.connect_material_property(mask[0],mask[1],u.MaterialProperty.MP_EMISSIVE_COLOR)
            if substrate:
                materials.connect(mask,substrate,'Emissive Color')
        errors = list(materials.lib.recompile_material(m) or [])
        if errors:
            raise RuntimeError('体积材质最终编译失败: ' + '; '.join(map(str,errors)))
        materials.report.setdefault('checks', {}).setdefault('volume_material_compile', []).append(
            {'material': m.get_path_name(), 'errors': []})
        if volume.get('phase_approximation'):
            materials.report.setdefault('checks',{}).setdefault('volume_phase',[]).append(dict(
                name=volume['name'],source_components=volume.get('source_components',[]),
                exported_model=volume.get('phase_model'),field_context=volume.get('field_context'),
                runtime_model='isotropic' if cfg.get('scene_render_pipeline')=='lumen' else 'path_tracer_requires_review',
                dual_hg_enabled=False,visual_status='needs_review',
                reason='当前 UE 异质体积 Lumen 路径使用各向同性散射；云节点双 HG 不等于此路径支持'))
        u.EditorAssetLibrary.save_loaded_asset(m)
        materials.protect_asset(m)


def configure_light(component, light, cfg, materials=None):
    # Converted scenes have no Lightmass bake; static lights disappear in runtime renders.
    component.set_mobility(u.ComponentMobility.MOVABLE)
    # The material converter emits foliage/subsurface shading models. UE lights
    # default to no transmission, which prevents their supported RT response.
    component.set_transmission(True)
    converted = physical_light(light,cfg.get('scene_light_scale',1))
    component.set_light_color(u.LinearColor(*converted['color'],1))
    if light['type'] != 'SUN':
        prop(component,'intensity_units',u.LightUnits.LUMENS)
    component.set_intensity(converted['intensity'])
    prop(component,'cast_shadows',light['cast_shadow'])
    prop(component,'cast_volumetric_shadow',light['cast_shadow'])
    from .native_fog_candidate import NATIVE_PROFILES
    if cfg.get('fog_occlusion_quality') in NATIVE_PROFILES:
        raytraced=light['cast_shadow'] and cfg.get('ue_render_memory_profile')!='compatible'
        component.set_cast_raytraced_shadows(u.CastRayTracedShadow.ENABLED if raytraced else u.CastRayTracedShadow.DISABLED)
    prop(component,'volumetric_scattering_intensity',light['volume_factor'])
    prop(component,'specular_scale',light['specular_factor'])
    prop(component,'diffuse_scale',light['diffuse_factor'])
    prop(component,'use_temperature',light['use_temperature'])
    prop(component,'temperature',light['temperature'])
    if light['type'] == 'SUN':
        prop(component,'light_source_angle',math.degrees(light['sun_angle']))
    elif light['type'] in ('SPOT','POINT'):
        prop(component,'source_radius',light['radius']*100)
    if light['type'] == 'SPOT':
        outer = math.degrees(light['spot_size']/2)
        prop(component,'outer_cone_angle',outer)
        # Blender's blend controls the cosine-space inner cone.
        inner = math.acos(min(1,math.cos(light['spot_size']/2)*(1-light['spot_blend'])+light['spot_blend']))
        prop(component,'inner_cone_angle',math.degrees(inner))
    elif light['type'] == 'AREA':
        prop(component,'source_width',light['size']*100)
        prop(component,'source_height',light['size_y']*100)
    if light.get('directional'):
        if materials is None:raise ValueError('方向性灯光缺少受管理的资源上下文')
        configure_directional_light(component,light['directional'],materials)
    return converted


def configure_directional_light(component, graph, materials):
    key=stable_id('world_light_direction_v1',graph)
    if graph['op']=='ies':
        if file_hash(graph['path'])!=graph['sha256']:raise ValueError('IES 源文件已变化')
        path=materials.root+'/Textures/T_IES_'+key
        texture=u.load_asset(path)
        if texture:materials.verify_asset(texture)
        else:
            task=u.AssetImportTask()
            for name,value in dict(filename=graph['path'],destination_path=materials.root+'/Textures',destination_name='T_IES_'+key,automated=True,save=True).items():prop(task,name,value)
            materials.tools.import_asset_tasks([task]);texture=u.load_asset(path)
            if not isinstance(texture,u.TextureLightProfile):raise RuntimeError('IES 未生成原生 TextureLightProfile')
            materials.managed(texture);u.EditorAssetLibrary.save_loaded_asset(texture);materials.protect_asset(texture)
        prop(component,'ies_texture',texture);prop(component,'use_ies_brightness',False)
        materials.report.setdefault('checks',{}).setdefault('directional_lights',[]).append(dict(kind='ies',asset=texture.get_path_name(),runtime_validation='needs_review'))
        return
    material=materials.create('M_LightFunction_'+key,u.Material,u.MaterialFactoryNew())
    previous=materials.material
    try:
        materials.material=material;materials.clear_expressions(material)
        prop(material,'material_domain',u.MaterialDomain.MD_LIGHT_FUNCTION)
        prop(material,'shading_model',u.MaterialShadingModel.MSM_UNLIT)
        def field(g):
            if g['op']=='constant':return materials.constant(g['value'])
            if g['op']=='incoming':
                vector=materials.node('LightVector');n=materials.node('Custom',code='return float3(-V.y,-V.x,V.z);',output_type=u.CustomMaterialOutputType.CMOT_FLOAT3)
                port=u.CustomInput();port.set_editor_property('input_name','V')
                n[0].set_editor_property('inputs',[port]);materials.connect(vector,n,'V');return n
            if g['op']=='component':
                n=materials.node('ComponentMask');prop(n[0],('r','g','b')[g['axis']],True)
                materials.connect(field(g['value']),n,'Input');return n
            if g['op']=='math':
                args=[field(a) for a in g['args']]
                kind={'ADD':'Add','SUBTRACT':'Subtract','MULTIPLY':'Multiply','DIVIDE':'Divide','MINIMUM':'Min','MAXIMUM':'Max','ABSOLUTE':'Abs','SINE':'Sine','COSINE':'Cosine','POWER':'Power'}[g['mode']]
                n=materials.node(kind)
                ports=['Input'] if kind in ('Abs','Sine','Cosine') else ['Base','Exp'] if kind=='Power' else ['A','B']
                for value,port in zip(args,ports):materials.connect(value,n,port)
                if kind in ('Sine','Cosine'):prop(n[0],'period',2*math.pi)
                if g.get('clamp'):n=materials.op('Clamp',n)
                return n
            raise ValueError('未支持的光函数表达式: '+g['op'])
        result=field(graph)
        assert materials.lib.connect_material_property(result[0],result[1],u.MaterialProperty.MP_EMISSIVE_COLOR)
        errors=list(materials.lib.recompile_material(material) or [])
        if errors:raise RuntimeError('Light Function 编译失败: '+str(errors))
        u.EditorAssetLibrary.save_loaded_asset(material);materials.protect_asset(material)
        component.set_light_function_material(material)
        materials.report.setdefault('checks',{}).setdefault('directional_lights',[]).append(dict(kind='light_function',asset=material.get_path_name(),errors=[],runtime_validation='needs_review'))
    finally:materials.material=previous


def light_snapshot(component, light, cfg):
    expected = physical_light(light, cfg.get('scene_light_scale', 1))
    color = component.get_light_color()
    actual = {'intensity': float(component.get_editor_property('intensity')),
        'color': [color.r, color.g, color.b],
        'use_temperature': component.get_editor_property('use_temperature'),
        'temperature': component.get_editor_property('temperature')}
    for name in ('cast_shadows','cast_volumetric_shadow','volumetric_scattering_intensity',
                 'specular_scale','diffuse_scale','cast_raytraced_shadow'):
        actual[name] = native_property(component, name)
    for name in ('ies_texture','light_function_material'):
        asset = native_property(component, name)
        actual[name] = asset.get_path_name() if asset is not None else None
    actual['cast_raytraced_shadow'] = str(actual['cast_raytraced_shadow'])
    expected.update(cast_shadows=light['cast_shadow'], cast_volumetric_shadow=light['cast_shadow'],
        volumetric_scattering_intensity=light['volume_factor'], specular_scale=light['specular_factor'],
        diffuse_scale=light['diffuse_factor'], use_temperature=light['use_temperature'],
        temperature=light['temperature'])
    errors = []
    for name in ('cast_shadows','cast_volumetric_shadow','volumetric_scattering_intensity','specular_scale','diffuse_scale'):
        if actual[name] is None or abs(float(actual[name])-float(expected[name])) > 1e-5:
            errors.append(name)
    angular = light.get('directional', {})
    if angular and not actual['ies_texture' if angular.get('op')=='ies' else 'light_function_material']:
        errors.append('angular_distribution')
    if abs(actual['intensity'] - expected['intensity']) > max(1e-4, abs(expected['intensity']) * 1e-5):
        errors.append('intensity')
    # UE stores light color as sRGB FColor; allow one encoded byte of quantization.
    if any(abs(a - b) > .01 for a, b in zip(actual['color'], expected['color'])):
        errors.append('color')
    if actual['use_temperature'] != light['use_temperature'] or abs(actual['temperature'] - light['temperature']) > .1:
        errors.append('temperature')
    return {'component': component.get_path_name(), 'source': light, 'expected': expected,
        'actual': actual, 'parameter_match': not errors, 'mismatches': errors}


def native_property(component, name, native_name=None):
    """Optional diagnostics must not discard otherwise valid native resources."""
    try:
        return component.get_editor_property(name)
    except Exception:
        if native_name is not None:
            try:
                # -game can omit Python aliases for non-Blueprint properties.
                return component.get_editor_property(native_name)
            except Exception:
                pass
        return None


def volume_snapshot(component, volume, meters_per_unit, pipeline):
    material = component.get_material(0) if component is not None else None
    properties = {name: native_property(component, name, native_name) for name,native_name in (
        ('step_factor','StepFactor'),('shadow_step_factor','ShadowStepFactor'),
        ('shadow_bias_factor','ShadowBiasFactor'),('lighting_downsample_factor','LightingDownsampleFactor'))}
    return dict(prim_path=volume.get('prim_path'), name=volume.get('name'),
        kind=volume.get('kind'), status=volume.get('status'), sample_statistics=volume.get('sample_statistics'),
        component=component.get_path_name() if component is not None else None,
        material=material.get_path_name() if material else None,
        binding_valid=component is not None and material is not None,
        properties=properties, sampling=volume_sampling_review(volume, meters_per_unit,
            pipeline, properties['lighting_downsample_factor']))


def runtime_world_snapshot(world, stages, pipeline, light_bindings=(), light_scale=1., expected_volume_paths=()):
    """Read live volume/light state after map load and again after MRQ overrides.

    Checks never rewrite density, materials, shadows, exposure or sampling. A
    missing expected resource fails technical validation; visual equivalence does
    not follow from resource or parameter checks.
    """
    volumes = []
    errors = []
    content = Path(u.Paths.project_content_dir()).resolve()
    for stage in stages:
        layer = Path(stage.get_editor_property('root_layer').file_path)
        if not layer.is_absolute():
            layer = Path(u.Paths.project_dir()) / layer
        layer = layer.resolve()
        package = layer.parent / 'package.json'
        if not layer.is_relative_to(content) or not package.is_file():
            continue
        scene = cached_scene_package(package)
        if not scene.get('asset_id'):
            continue
        for volume in scene.get('volumes', []):
            if volume.get('status') != 'converted' or not volume.get('prim_path'):
                continue
            component = stage.get_generated_component(volume['prim_path'])
            item = volume_snapshot(component, volume, scene.get('meters_per_unit',1.), pipeline)
            volumes.append(item)
            if not item['binding_valid']:
                errors.append('missing_volume_binding:' + volume['prim_path'])
    present_paths = {volume['prim_path'] for volume in volumes}
    errors.extend('missing_volume_component:' + path for path in expected_volume_paths if path not in present_paths)
    actors = {actor.get_name():actor for actor in u.GameplayStatics.get_all_actors_of_class(world,u.Light)}
    lights = []
    parameter_mismatches = []
    for binding in light_bindings:
        actor = actors.get(binding['actor_name'])
        component = actor.get_component_by_class(u.LightComponent) if actor else None
        if component is None:
            errors.append('missing_light:' + binding['actor_name'])
            continue
        item = light_snapshot(component, binding['source'], {'scene_light_scale':light_scale})
        lights.append(item)
        if not item['parameter_match']:
            parameter_mismatches.append(dict(actor=binding['actor_name'],fields=item['mismatches']))
    names = ('r.RayTracing.Shadows','r.HeterogeneousVolumes.HardwareRayTracing',
             'r.HeterogeneousVolumes.Shadows','r.HeterogeneousVolumes.IndirectLighting',
             'r.HeterogeneousVolumes.IndirectLighting.Mode','r.Shadow.Virtual.Enable')
    cvars = {name:u.SystemLibrary.get_console_variable_int_value(name) for name in names}
    return dict(resource_match=not errors, errors=errors, parameter_mismatches=parameter_mismatches,
        volumes=volumes, lights=lights,
        source_light_bindings_available=bool(light_bindings), cvars=cvars,
        fog_shadow_review=fog_shadow_review(pipeline,volumes,lights,cvars),
        visual_status='needs_review', parameters_changed=False)


def configure_camera(component, camera, scene):
    resolution = scene['reference_camera']['resolution']
    pixel_aspect = scene['reference_camera'].get('pixel_aspect',[1,1])
    aspect = resolution[0]*pixel_aspect[0]/(resolution[1]*pixel_aspect[1])
    filmback = component.get_editor_property('filmback')
    vertical = camera.get('sensor_fit') == 'VERTICAL' or (camera.get('sensor_fit') == 'AUTO' and aspect < 1)
    width = camera['sensor_height']*aspect if vertical else camera['sensor_width']
    filmback.sensor_width, filmback.sensor_height = width,width/aspect
    prop(component,'filmback',filmback)
    prop(component,'current_focal_length',camera['lens'])
    dof=camera.get('dof',{'enabled':False})
    focus=component.get_editor_property('focus_settings')
    focus.focus_method=u.CameraFocusMethod.MANUAL if dof['enabled'] else u.CameraFocusMethod.DISABLE
    if dof['enabled']:
        focus.manual_focus_distance=dof['focus_distance']*scene['meters_per_unit']*100
        lens=component.get_editor_property('lens_settings')
        lens.min_f_stop=min(lens.min_f_stop,dof['fstop'])
        lens.max_f_stop=max(lens.max_f_stop,dof['fstop'])
        lens.diaphragm_blade_count=max(4,dof['blades']) if dof['blades'] else 16
        prop(component,'lens_settings',lens)
        prop(component,'current_aperture',dof['fstop'])
    prop(component,'focus_settings',focus)
    prop(component,'override_custom_near_clipping_plane',True)
    prop(component,'custom_near_clipping_plane',camera.get('clip_start',.01)*scene['meters_per_unit']*100)
    if camera['projection'] == 'ORTHO':
        prop(component,'projection_mode',u.CameraProjectionMode.ORTHOGRAPHIC)
        prop(component,'ortho_width',camera.get('ortho_scale',1)*scene['meters_per_unit']*100)


def color_configuration(asset, record):
    source = u.OpenColorIOColorSpace(color_space_name=record['input'])
    destination = u.OpenColorIOColorSpace(color_space_name=record['output'])
    conversion = u.OpenColorIOColorConversionSettings(configuration_source=asset,
                        source_color_space=source,destination_color_space=destination)
    return u.OpenColorIODisplayConfiguration(is_enabled=True,color_configuration=conversion)


def validate_color_configuration(display):
    """An imported asset can exist even when the native OCIO processor failed."""
    result = u.OpenColorIOEditorBlueprintLibrary.apply_color_space_transform_to_color(
        display.color_configuration, u.LinearColor(.18, .18, .18, 1))
    # UE Python exposes the native bool as Optional[LinearColor] in 5.8;
    # older bindings expose (success, out_color).
    if isinstance(result, tuple):
        success, color = result
    else:
        success, color = result is not None, result
    if not success or not all(math.isfinite(v) for v in (color.r, color.g, color.b)):
        raise RuntimeError('OCIO 原生颜色处理器验证失败，不能标记外观还原成功')
    return [color.r, color.g, color.b]


def repair_color_configuration(asset, record, config_path):
    from .ocio_cache import portable_config
    config_path = portable_config(config_path, u.Paths.project_dir())
    prop(asset, 'configuration_file', u.FilePath(file_path=str(config_path)))
    prop(asset, 'desired_color_spaces', [u.OpenColorIOColorSpace(color_space_name=record['input']),
                                       u.OpenColorIOColorSpace(color_space_name=record['output'])])
    asset.reload_existing_colorspaces(True)
    display = color_configuration(asset, record)
    validate_color_configuration(display)
    return display


def setup(scene, cfg, root, owner, actor_sub, tag, materials, report, sequence, actors):
    global _ACTIVE_LOOK
    if not scene.get('render_look'):
        return
    reviews,skipped=classify_appearance_reviews(scene)
    report['checks']['appearance_visual_review']=reviews
    report['skipped_items'].extend(skipped)
    look = scene['render_look']
    display = ocio = None
    if look.get('config'):
        # USD deployment provides the portable config beside all LUTs.
        source_dir = Path(u.Paths.project_content_dir())/root.removeprefix('/Game/')/'Source/Scene'
        from .ocio_cache import portable_config
        config_path = portable_config(source_dir/'Appearance/config.ocio', u.Paths.project_dir())
        ocio=u.load_asset(root+'/Materials/OCIO_Blender')
        if ocio:
            if u.EditorAssetLibrary.get_metadata_tag(ocio,'MMDBridge.Owner') != owner:
                raise RuntimeError('拒绝覆盖未受管理的 OCIO 配置')
            materials.verify_asset(ocio)
        else:
            task=u.AssetImportTask()
            for name,value in dict(filename=str(config_path),destination_path=root+'/Materials',destination_name='OCIO_Blender',
                                   automated=True,replace_existing=True,save=True,factory=u.OpenColorIOConfigurationFactory()).items():
                prop(task,name,value)
            materials.tools.import_asset_tasks([task])
            ocio=u.load_asset(root+'/Materials/OCIO_Blender')
            if not ocio:
                raise RuntimeError('原生 OpenColorIO 配置导入失败')
            materials.managed(ocio)
            u.EditorAssetLibrary.save_loaded_asset(ocio)
        prop(ocio,'configuration_file',u.FilePath(file_path=str(config_path)))
        prop(ocio,'desired_color_spaces',[u.OpenColorIOColorSpace(color_space_name=look['input']),
                                         u.OpenColorIOColorSpace(color_space_name=look['output'])])
        ocio.reload_existing_colorspaces(True)
        u.EditorAssetLibrary.save_loaded_asset(ocio)
        materials.protect_asset(ocio)
        display = color_configuration(ocio,look)
        probe_color = validate_color_configuration(display)
        report['checks']['ocio'] = {'status':'validated','config':str(config_path),'probe_color':probe_color}
        if cfg.get('scene_viewport_ocio',True):
            u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(display)
        _ACTIVE_LOOK = (root,display)
    post = tag(actor_sub.spawn_actor_from_class(u.PostProcessVolume,u.Vector()),'Source render look')
    if ocio:
        import json
        post.tags = list(post.tags) + ['MMDBridge.OCIO=' + ocio.get_path_name(),
            'MMDBridge.AutoOCIO=' + str(int(cfg.get('scene_viewport_ocio',True))),
            'MMDBridge.RenderLook=' + json.dumps(look, ensure_ascii=True, separators=(',', ':'))]
    prop(post,'unbound',True)
    settings = post.get_editor_property('settings')
    if cfg.get('scene_render_pipeline', 'path_tracing') == 'lumen':
        lumen_post_process(settings)
    lens=u.SystemLibrary.get_console_variable_float_value('r.EyeAdaptation.LensAttenuation')
    extended=u.SystemLibrary.get_console_variable_int_value('r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange')
    luminance_max=.78/max(lens,.01) if extended else 1.
    calibration=math.log2(luminance_max/RADIANCE_TO_UE)
    for name,value in {'auto_exposure_method':u.AutoExposureMethod.AEM_MANUAL,
                       'auto_exposure_bias':look.get('exposure',0)+cfg.get('scene_exposure_offset',0)+calibration,
                       'auto_exposure_apply_physical_camera_exposure':False,
                       'motion_blur_amount':0.,'vignette_intensity':0.,'bloom_intensity':0.}.items():
        prop(settings,'override_'+name,True)
        prop(settings,name,value)
    if look.get('bloom'):
        prop(settings,'bloom_intensity',look['bloom']['strength'])
        prop(settings,'override_bloom_threshold',True)
        prop(settings,'bloom_threshold',look['bloom']['threshold']*2**(look.get('exposure',0)+cfg.get('scene_exposure_offset',0)))
    prop(post,'settings',settings)
    for volume in scene.get('volumes',[]):
        if volume.get('status') != 'converted' or volume['kind'] != 'height_fog':
            continue
        fog = tag(actor_sub.spawn_actor_from_class(u.ExponentialHeightFog,u.Vector()),volume['name'])
        comp = fog.get_component_by_class(u.ExponentialHeightFogComponent)
        prop(comp,'fog_height_falloff',0.)
        # UE's native voxelization multiplies its density by 0.5 (VolumetricFog.usf).
        prop(comp,'fog_density',volume['extinction']/scene['meters_per_unit']*20)
        prop(comp,'enable_volumetric_fog',True)
        # ponytail: cap the extra analytic fog at 0.1%; zero prevents UE scene registration.
        prop(comp,'fog_max_opacity',.001)
        prop(comp,'volumetric_fog_extinction_scale',cfg.get('fog_density_scale',1))
        rgb=[max(0,min(1,v)) for v in volume['albedo']]
        # FColor fog albedo is sRGB-encoded; coefficients are scene-linear.
        prop(comp,'volumetric_fog_albedo',u.Color(*[round((12.92*v if v<=.0031308 else 1.055*v**(1/2.4)-.055)*255) for v in rgb],255))
        emission=[RADIANCE_TO_UE*v/max(volume['extinction'],1e-20) for v in volume['emission']]
        prop(comp,'volumetric_fog_emissive',u.LinearColor(*emission,1))
        prop(comp,'volumetric_fog_scattering_distribution',volume['anisotropy'])
        prop(comp,'volumetric_fog_distance',volume['distance']*100)
    from .fog_occlusion import decision, NATIVE_PROFILES
    native_decision=decision(cfg.get('fog_occlusion_quality','legacy'),'lumen',
                            engine=u.SystemLibrary.get_engine_version(),volumes=scene.get('volumes',[]))
    if native_decision.get('effective') in NATIVE_PROFILES:
        from .ue_fog_froxel import prepare_native
        stages=[a for a in actor_sub.get_all_level_actors() if isinstance(a,u.UsdStageActor)]
        report['checks']['native_fog']=prepare_native(actor_sub,stages,scene.get('volumes',[]),materials,cfg,
                                                     scene.get('meters_per_unit',1.))
    environment = scene.get('environment',{})
    if environment.get('surface'):
        image = {'id':'environment','asset_name':'T_Environment','path':environment['path'],'hdr':True,'color_space':'Linear Rec.709'}
        texture = materials.texture(image,data=True)
        if not isinstance(texture,u.TextureCube):
            raise RuntimeError('世界 HDR 导入未生成 TextureCube，不能当作天空光验收')
        sky = tag(actor_sub.spawn_actor_from_class(u.SkyLight,u.Vector()),'Source environment')
        comp = sky.get_component_by_class(u.SkyLightComponent)
        comp.set_mobility(u.ComponentMobility.MOVABLE)
        prop(comp,'source_type',u.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
        comp.set_cubemap(texture)
        comp.set_intensity(RADIANCE_TO_UE)
        prop(comp,'lower_hemisphere_is_black',False)
        lighting = report.setdefault('checks', {}).setdefault('world_lighting', {})
        lighting.update({'environment_texture': texture.get_path_name(),
            'hdr_sha256': environment['sha256'], 'source_world': environment.get('source_world', {}),
            'sky_intensity_expected': RADIANCE_TO_UE,
            'sky_intensity_actual': float(comp.get_editor_property('intensity')),
            'lower_hemisphere_is_black': comp.get_editor_property('lower_hemisphere_is_black'),
            'visual_acceptance': 'requires_render_comparison'})
        background=materials.create('M_Environment',u.Material,u.MaterialFactoryNew())
        materials.material=background
        materials.clear_expressions(background)
        prop(background,'shading_model',u.MaterialShadingModel.MSM_UNLIT)
        prop(background,'two_sided',True)
        prop(background,'is_sky',True)
        ray=materials.op('Multiply',materials.node('CameraVectorWS'),materials.constant(-1))
        background_texture=texture
        if environment.get('background_path'):
            background_texture=materials.texture(dict(image,id='environment_background',asset_name='T_EnvironmentBackground',path=environment['background_path']),data=True)
            lighting['background_texture']=background_texture.get_path_name()
            lighting['background_hdr_sha256']=environment['background_sha256']
            lighting['lighting_context']=environment.get('lighting_context')
        sample=materials.node('TextureSample',texture=background_texture,sampler_type=u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
        materials.connect(ray,sample,'Coordinates')
        sample=materials.op('Multiply',(sample[0],'RGB'),materials.constant(RADIANCE_TO_UE))
        materials.lib.connect_material_property(sample[0],sample[1],u.MaterialProperty.MP_EMISSIVE_COLOR)
        materials.lib.recompile_material(background)
        u.EditorAssetLibrary.save_loaded_asset(background)
        materials.protect_asset(background)
        sphere=tag(actor_sub.spawn_actor_from_class(u.StaticMeshActor,u.Vector()),'Source sky background')
        sphere.static_mesh_component.set_static_mesh(u.load_asset('/Engine/BasicShapes/Sphere'))
        sphere.static_mesh_component.set_material(0,background)
        prop(sphere.static_mesh_component,'cast_shadow',False)
        sphere.static_mesh_component.set_collision_enabled(u.CollisionEnabled.NO_COLLISION)
        sphere.set_actor_scale3d(u.Vector(10000,10000,10000))
        active_camera=next((d for d in scene['objects'] if d.get('camera',{}).get('active')),None)
        if active_camera and active_camera['id'] in actors:
            sphere.set_actor_location(actors[active_camera['id']].get_actor_location(),False,False)
        if environment.get('sun'):
            from .ue_host import scene_transform
            sun=tag(actor_sub.spawn_actor_from_class(u.DirectionalLight,u.Vector()),'Source sky sun')
            sun.set_actor_transform(scene_transform(environment['sun']['matrix'],scene['meters_per_unit'],True),False,False)
            configure_light(sun.get_component_by_class(u.LightComponent),environment['sun']['light'],cfg)
            lighting['sun'] = light_snapshot(sun.get_component_by_class(u.LightComponent), environment['sun']['light'], cfg)
    preset = materials.create('MRQ_SourceLook',u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    output = preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution = u.IntPoint(int(cfg['width']),int(cfg['height']))
    cvars=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for name,value in VOLUME_RENDER_CVARS.items():
        cvars.add_or_update_console_variable(name,float(value))
    if cfg.get('scene_render_pipeline', 'path_tracing') == 'lumen':
        for name,value in LUMEN_RENDER_CVARS.items():
            cvars.add_or_update_console_variable(name,float(value))
    configure_mrq_defaults(preset, cfg.get('scene_render_pipeline', 'path_tracing'),
                           cfg.get('ue_render_memory_profile', 'balanced'), cfg.get('foliage_distance_scale', 2.),
                           cfg.get('fog_occlusion_quality','legacy'),scene.get('volumes',[]))
    viewport_volume_settings(True,cfg.get('scene_render_pipeline', 'path_tracing'),
                             cfg.get('ue_render_memory_profile', 'balanced'), cfg.get('foliage_distance_scale', 2.),
                             cfg.get('fog_occlusion_quality','legacy'),scene.get('volumes',[]))
    color = preset.find_or_add_setting_by_class(u.MoviePipelineColorSetting)
    if display:
        prop(color,'ocio_configuration',display)
    prop(color,'disable_tone_curve',bool(display))
    u.EditorAssetLibrary.save_loaded_asset(preset)
    materials.protect_asset(preset)
    active = next((d for d in scene['objects'] if d.get('camera',{}).get('active')),None)
    if active and active['id'] in actors:
        binding = sequence.add_possessable(actors[active['id']])
        track = sequence.add_track(u.MovieSceneCameraCutTrack)
        section = track.add_section()
        section.set_range(sequence.get_playback_start(),sequence.get_playback_end())
        camera_id=u.MovieSceneObjectBindingID()
        camera_id.set_editor_property('guid',binding.get_id())
        section.set_camera_binding_id(camera_id)
    report['assets']['render_preset'] = preset.get_path_name()
    if ocio:
        report['assets']['ocio'] = ocio.get_path_name()
    from .fog_occlusion import decision
    report['checks']['fog_occlusion'] = decision(cfg.get('fog_occlusion_quality','legacy'),
        cfg.get('scene_render_pipeline','path_tracing'),engine=u.SystemLibrary.get_engine_version(),
        volumes=scene.get('volumes',[]))
    report['checks']['appearance'] = {'status':'needs_review' if scene.get('needs_review') else 'converted',
        'pipeline':cfg.get('scene_render_pipeline', 'path_tracing'),
        'visual_acceptance':'not_run',
        'volume_count':len(scene.get('volumes',[])),'look_applied_once':bool(display),'exposure':look.get('exposure',0)+cfg.get('scene_exposure_offset',0),
        'unit_calibration_ev':calibration,'radiance_to_ue':RADIANCE_TO_UE}


def restore_viewport_look(actor_sub):
    """Restore each active viewport's look; OCIO configurations are per viewport."""
    global _ACTIVE_LOOK
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        return
    viewport = str(u.get_editor_subsystem(u.LevelEditorSubsystem).get_active_viewport_config_key())
    if viewport in ('', 'None'):
        return
    reader = getattr(u,'MMDBridgeViewportLibrary',None)
    viewport_pipeline = str(reader.get_active_viewport_pipeline()) if reader else ''
    # The same map path can be reloaded as a new UWorld with new USD components.
    key = (world.get_path_name(), viewport, str(world), viewport_pipeline)
    if _ACTIVE_LOOK and _ACTIVE_LOOK[0] == key:
        # UE owns viewport clients and may recreate/reset them under the same key.
        # There is no native getter in UE 5.8, so reapply the validated managed look.
        if _ACTIVE_LOOK[1] is not None:
            u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(_ACTIVE_LOOK[1])
        return
    viewport_volume_settings(False)
    actors = actor_sub.get_all_level_actors()
    # Native map copies can share an older USD package without a fog profile.
    native_prepared = any(any(str(tag).startswith('MMDBridge.NativeFog=') for tag in actor.tags)
                          for actor in actors if isinstance(actor,u.StaticMeshActor))
    pipeline = next(('lumen' for actor in actors if isinstance(actor,u.PostProcessVolume)
                     and actor.get_actor_label() == 'Source render look'
                     and actor.get_editor_property('settings').dynamic_global_illumination_method
                         == u.DynamicGlobalIlluminationMethod.LUMEN),None)
    if viewport_pipeline == 'path_tracing':
        pipeline = 'path_tracing'
    managed_settings = False
    for actor in actors:
        if not isinstance(actor,u.UsdStageActor):
            continue
        file = Path(actor.get_editor_property('root_layer').file_path)
        if not file.is_absolute():
            file = Path(u.Paths.project_dir())/file
        file = file.resolve()
        package = file.parent/'package.json'
        if not package.is_file():
            continue
        record = read_json(package)
        restore_stage_components(actor)
        if record.get('render_look') or record.get('viewport_render_settings'):
            view_cfg = record.get('viewport_render_settings', {})
            # Opening a viewport must not replace its caches/residency with an
            # MRQ memory budget. Only explicit import/render actions do that.
            viewport_volume_settings(True,pipeline,None,
                                     view_cfg.get('foliage_distance_scale', 2.),
                                     view_cfg.get('fog_occlusion_quality',
                                                  'native_balanced' if native_prepared else 'legacy'),
                                     record.get('volumes',[]))
            managed_settings = True
        look = record.get('render_look')
        if not record.get('viewport_render_settings',{}).get('auto_ocio',True):
            _ACTIVE_LOOK = (key,None)
            return
        if not look or not look.get('config'):
            continue
        # Source/Scene belongs to the generated asset root.
        content = Path(u.Paths.project_content_dir()).resolve()
        root = '/Game/'+file.parent.parent.parent.relative_to(content).as_posix()
        ocio = u.load_asset(root+'/Materials/OCIO_Blender')
        if ocio:
            display = color_configuration(ocio,look)
            try:
                validate_color_configuration(display)
            except RuntimeError:
                # Respect the same ownership/hash protection as scene import.
                from .ue_materials import Materials
                owner = u.EditorAssetLibrary.get_metadata_tag(ocio,'MMDBridge.Owner')
                if not owner:
                    raise RuntimeError('拒绝自动修复未受管理的 OCIO 配置')
                protection = Materials(root,owner,{'warnings':[]})
                protection.verify_asset(ocio)
                display = repair_color_configuration(ocio, look, file.parent/'Appearance/config.ocio')
                u.EditorAssetLibrary.save_loaded_asset(ocio)
                protection.protect_asset(ocio)
            u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(display)
            _ACTIVE_LOOK = (key,display)
            return
    # Saved source-look actor also restores non-USD scenes and maps whose Stage is unloaded.
    import json
    for actor in actors:
        if not isinstance(actor, u.PostProcessVolume) or actor.get_actor_label() != 'Source render look':
            continue
        tags = {str(t).split('=',1)[0]:str(t).split('=',1)[1] for t in actor.tags if '=' in str(t)}
        if tags.get('MMDBridge.AutoOCIO') == '0':
            _ACTIVE_LOOK = (key,None)
            return
        if 'MMDBridge.OCIO' not in tags or 'MMDBridge.RenderLook' not in tags:
            continue
        ocio = u.load_asset(tags['MMDBridge.OCIO'])
        if ocio:
            display = color_configuration(ocio,json.loads(tags['MMDBridge.RenderLook']))
            validate_color_configuration(display)
            u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(display)
            _ACTIVE_LOOK = (key,display)
            return
    if _ACTIVE_LOOK:
        u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(u.OpenColorIODisplayConfiguration(is_enabled=False))
        _ACTIVE_LOOK = (key,None)
    elif managed_settings:
        _ACTIVE_LOOK = (key,None)
