"""Isolated fixture variants; changes live components and transient MRQ only."""
from pathlib import Path
import unreal as u
from mmd_bridge import ue_standalone_render as runtime
from mmd_bridge.ue_appearance import native_property

prepare=runtime.prepare_job
record=runtime.record


def prepare_variant(queue):
    job=prepare(queue);cfg=job.get_configuration();variant=runtime._REQUEST['fog_test_variant'].removesuffix('_unoccluded').removesuffix('_no_fog')
    cv=cfg.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    values={'r.VolumetricFog.InjectRaytracedLights':int(variant=='rt_inject')}
    if variant=='vsm_nocache':values['r.HeterogeneousVolumes.LightingCache']=0
    if variant=='vsm_no_indirect':values['r.HeterogeneousVolumes.IndirectLighting']=0
    if variant=='vsm_nocache':values['r.HeterogeneousVolumes.Scalability']=3
    if variant=='rt_inject_fine':values.update({'r.VolumetricFog.InjectRaytracedLights':1,'r.VolumetricFog.GridPixelSize':2})
    if variant.startswith('shadowmap'):values['r.Shadow.Virtual.Enable']=0
    if variant.startswith('froxel_box'):
        values['r.VolumetricFog.InjectRaytracedLights']=int('rt' in variant)
        if 'fine' in variant:values['r.VolumetricFog.GridPixelSize']=4
    if variant.startswith('froxel_svt'):values['r.VolumetricFog.InjectRaytracedLights']=1
    if runtime._REQUEST['pipeline']=='path_tracing':
        aa=cfg.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
        aa.spatial_sample_count=128;aa.temporal_sample_count=1
        aa.override_anti_aliasing=True;aa.anti_aliasing_method=u.AntiAliasingMethod.AAM_NONE
        values.update({'r.PathTracing.HeterogeneousVolumes':1,'r.PathTracing.EnableAtmosphere':1,'r.PathTracing.Denoiser':1})
    for name,value in values.items():cv.add_or_update_console_variable(name,value)
    record(fog_test_console_overrides=values)
    return job


def record_variant(**values):
    if 'runtime_foliage_components' in values and not runtime._STATE.get('fog_test_applied'):
        prefix=runtime._REQUEST['level']+'.'
        actors=[a for a in u.ObjectIterator(u.Actor) if a.get_path_name().startswith(prefix) and ':PersistentLevel.' in a.get_path_name()]
        lights=[a for a in actors if isinstance(a,(u.PointLight,u.SpotLight))];assert len(lights)==1
        light=lights[0];comp=light.get_component_by_class(u.LocalLightComponent);variant=runtime._REQUEST['fog_test_variant']
        width=runtime._REQUEST.get('fog_test_width_cm',5)
        cameras=[a for a in actors if isinstance(a,u.CineCameraActor)];assert len(cameras)==1
        y={2:-32.,5:0.,10:42.,'masked':80.}[width]
        # Look from inside the selected geometric shadow cone. Reference and
        # candidates share this transform; never change light or fog parameters.
        camera=cameras[0];camera.set_actor_location(u.Vector(500,7*y,100),False,False)
        camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(camera.get_actor_location(),u.Vector(-100,y,100)),False)
        if variant.startswith(('vsm','shadowmap')):comp.set_cast_raytraced_shadows(u.CastRayTracedShadow.DISABLED)
        hidden=[]
        if variant.endswith('unoccluded'):
            for actor in actors:
                if any(str(tag).startswith('FogTest.Bar_') or str(tag)=='FogTest.Masked' for tag in actor.tags):
                    actor.static_mesh_component.set_cast_shadow(False);hidden.append(actor.get_name())
        # SVT fixtures add generated HVC actors. Only the named fixture actor
        # is the uniform grid under test; object iteration order is not stable.
        grid=[a for a in actors if isinstance(a,u.HeterogeneousVolume)
              and a.get_name()==runtime._REQUEST['fog_test_fixture'].get('volume')]
        if runtime._REQUEST['fog_test_fixture'].get('volume_kind')=='grid':
            assert len(grid)==1,'Expected fixture volume was not uniquely found'
        print('FOG_TEST_VOLUMES',[(a.get_name(),a.get_path_name(),
              a.get_component_by_class(u.HeterogeneousVolumeComponent).is_visible(),
              bool(native_property(a,'hidden','bHidden')))
              for a in actors if isinstance(a,u.HeterogeneousVolume)])
        base_variant=variant.removesuffix('_unoccluded').removesuffix('_no_fog')
        for stage in [a for a in actors if isinstance(a,u.UsdStageActor) and 'FogTest.SvtSource' in [str(t) for t in a.tags]]:
            source=runtime._REQUEST['fog_test_fixture']['svt']
            c=stage.get_generated_component(source['volume']['prim_path'])
            if c.get_material(0) is None:
                c.set_material(0,u.load_asset(source['asset_root']+'/Scene/Generated/MI_V_svt_box'))
            c.set_visibility(False)
        if base_variant.startswith(('froxel_svt','path_tracing_svt')):
            from mmd_bridge.ue_fog_froxel import apply
            source=runtime._REQUEST['fog_test_fixture']['svt']
            stages=[a for a in actors if isinstance(a,u.UsdStageActor) and 'FogTest.SvtSource' in [str(t) for t in a.tags]]
            assert len(stages)==1
            c=stages[0].get_generated_component(source['volume']['prim_path'])
            # Explicit fixture geometry: UE's USD SVT import uses index-space
            # units. Apply the same 6 cm native lattice in candidate and PT.
            c.set_world_scale3d(u.Vector(6.,6.,6.))
            origin,extent,radius=u.SystemLibrary.get_component_bounds(c)
            location=c.get_world_location()
            c.set_world_location(u.Vector(location.x+50-origin.x,location.y-origin.y,location.z+100-origin.z),False,False)
            if c.get_material(0) is None:
                # This isolated layer lives outside Content/package.json;
                # production restore_volume_materials supplies this binding.
                c.set_material(0,u.load_asset(source['asset_root']+'/Scene/Generated/MI_V_svt_box'))
            c.set_visibility(True)
            grid[0].get_component_by_class(u.HeterogeneousVolumeComponent).set_visibility(False)
            if base_variant.startswith('froxel_svt'):
                print('SVT_RUNTIME_MATERIAL',c.get_material(0),c.get_material(0).get_editor_property('sparse_volume_texture_parameter_values') if isinstance(c.get_material(0),u.MaterialInstance) else None)
                values['fog_test_svt_adapter']=apply(light,stages,[source['volume']],source['kernel'])
                assert values['fog_test_svt_adapter']['changed'],str(values['fog_test_svt_adapter'])
        if variant.startswith('froxel_box'):
            for actor in actors:
                if any(str(t) in ('FogTest.FroxelBox','FogTest.FroxelHost') for t in actor.tags):
                    actor.set_actor_hidden_in_game(False)
                    kind=u.StaticMeshComponent if isinstance(actor,u.StaticMeshActor) else u.ExponentialHeightFogComponent
                    actor.get_component_by_class(kind).set_visibility(True)
            assert len(grid)==1
            grid[0].set_actor_hidden_in_game(True)
            grid[0].get_component_by_class(u.HeterogeneousVolumeComponent).set_visibility(False)
            if 'vsm' in variant:comp.set_cast_raytraced_shadows(u.CastRayTracedShadow.DISABLED)
            values['fog_test_alternative']=runtime._REQUEST['fog_test_fixture']['froxel']
        volume=None
        if grid:
            c=grid[0].get_component_by_class(u.HeterogeneousVolumeComponent)
            if base_variant=='vsm_fine':
                for name,value in {'LightingDownsampleFactor':1.,'StepFactor':.5}.items():c.set_editor_property(name,value)
            if base_variant=='vsm_finer':
                for name,value in {'LightingDownsampleFactor':.5,'StepFactor':.25}.items():c.set_editor_property(name,value)
            if base_variant=='vsm_cache_fine':c.set_editor_property('LightingDownsampleFactor',.5)
            if base_variant=='vsm_cache_medium':c.set_editor_property('LightingDownsampleFactor',1.)
            if base_variant=='shadowmap_medium':c.set_editor_property('LightingDownsampleFactor',1.)
            if base_variant=='shadowmap_fine':c.set_editor_property('LightingDownsampleFactor',.5)
            if base_variant=='vsm_step_fine':c.set_editor_property('StepFactor',.25)
            volume={name:native_property(c,name,cpp) for name,cpp in (
                ('step_factor','StepFactor'),('lighting_downsample_factor','LightingDownsampleFactor'),('volume_resolution','VolumeResolution'))}
            volume['volume_resolution']=str(volume['volume_resolution'])
            volume['material']=c.get_material(0).get_path_name() if c.get_material(0) else None
            volume['actor']=grid[0].get_path_name()
            volume['visible']=c.is_visible()
            volume['owner_hidden']=bool(native_property(grid[0],'hidden','bHidden'))
            assert volume['material']
            if not base_variant.startswith(('froxel','path_tracing_svt')):
                assert volume['visible'] and not volume['owner_hidden'],'Fixture fog is hidden'
        if variant.endswith('_no_fog'):
            for actor in actors:
                for cls in (u.HeterogeneousVolumeComponent,u.ExponentialHeightFogComponent):
                    c=actor.get_component_by_class(cls)
                    if c is not None:c.set_visibility(False)
            values['fog_test_disabled']=True
        values.update(fog_test_applied=True,fog_test_variant=variant,
            fog_test_camera=dict(position=str(camera.get_actor_location()),rotation=str(camera.get_actor_rotation()),width_cm=width),
            fog_test_light={'cast_shadows':native_property(comp,'cast_shadows'),
                'cast_volumetric_shadow':native_property(comp,'cast_volumetric_shadow'),
                'cast_raytraced_shadow':str(native_property(comp,'cast_raytraced_shadow'))},
            fog_test_volume=volume,fog_test_shadow_disabled=hidden)
    if 'runtime_world_validation' in values:
        values['fog_test_actual_console']={name:u.SystemLibrary.get_console_variable_float_value(name) for name in (
            'r.VolumetricFog.InjectRaytracedLights','r.VolumetricFog.GridPixelSize','r.VolumetricFog.GridSizeZ',
            'r.HeterogeneousVolumes.LightingCache','r.HeterogeneousVolumes.IndirectLighting','sg.ShadingQuality',
            'r.HeterogeneousVolumes.Scalability','r.HeterogeneousVolumes.LightingCache.DownsampleFactor',
            'r.Shadow.Virtual.MarkCoarsePagesLocal','r.Shadow.Virtual.NonNanite.IncludeInCoarsePages')}
    return record(**values)


runtime.prepare_job=prepare_variant
runtime.record=record_variant
