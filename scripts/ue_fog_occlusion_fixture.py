"""Create isolated geometry/fog fixtures. Never load or save accepted scenes."""
import os,sys,time,uuid
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json
from mmd_bridge.ue_materials import Materials,prop
from mmd_bridge.ue_appearance import lumen_post_process,configure_mrq_defaults

OUT=Path(os.environ['MMDBRIDGE_FOG_TEST_OUT']);OUT.mkdir(parents=True,exist_ok=True)
owner='FogOcclusion_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:4]
root='/Game/'+owner
level=u.get_editor_subsystem(u.LevelEditorSubsystem)
actors=u.get_editor_subsystem(u.EditorActorSubsystem)
report={'checks':{},'assets':{}}
materials=Materials(root,owner,report)
fixture=[]

for kind, light_type in (('grid','point'),('height','point'),('grid','spot'),('height','spot')):
    case_kind = kind if light_type=='point' else kind+'_spot'
    assert level.new_level(root+'/Maps/'+case_kind)
    world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    def mesh(name,position,scale,material):
        actor=actors.spawn_actor_from_class(u.StaticMeshActor,u.Vector(*position))
        actor.set_actor_label(name);actor.tags=[u.Name('FogTest.'+name)]
        actor.set_actor_scale3d(u.Vector(*scale));c=actor.static_mesh_component
        c.set_static_mesh(u.load_asset('/Engine/BasicShapes/Cube'));c.set_material(0,material)
        c.set_collision_enabled(u.CollisionEnabled.NO_COLLISION)
        return actor
    opaque=materials.create('M_Opaque_'+case_kind,u.Material,u.MaterialFactoryNew())
    materials.material=opaque;materials.clear_expressions(opaque)
    prop(opaque,'two_sided',True)
    materials.lib.connect_material_property(materials.constant([.04,.04,.04])[0],'',u.MaterialProperty.MP_BASE_COLOR)
    materials.lib.recompile_material(opaque);u.EditorAssetLibrary.save_loaded_asset(opaque);materials.protect_asset(opaque)
    # Different apparent angular widths; reference and realtime use identical meshes.
    for width,y in ((2.,-32.),(5.,0.),(10.,42.)):
        mesh('Bar_'+str(int(width)),(-100,y,100),(.02,width/100.,2.),opaque)
    masked=materials.create('M_Masked_'+case_kind,u.Material,u.MaterialFactoryNew())
    materials.material=masked;materials.clear_expressions(masked)
    prop(masked,'blend_mode',u.BlendMode.BLEND_MASKED);prop(masked,'two_sided',True)
    uv=materials.node('TextureCoordinate');mask=materials.node('ComponentMask',r=True,g=False,b=False,a=False)
    materials.connect(uv,mask,'Input');s=materials.op('Multiply',mask,materials.constant(4.))
    s=materials.op('Frac',s);s=materials.op('Subtract',s,materials.constant(.5));s=materials.op('Abs',s)
    masknode=materials.node('Step');materials.connect(materials.constant(.18),masknode,'X');materials.connect(s,masknode,'Y')
    materials.lib.connect_material_property(masknode[0],masknode[1],u.MaterialProperty.MP_OPACITY_MASK)
    materials.lib.connect_material_property(materials.constant([.04,.04,.04])[0],'',u.MaterialProperty.MP_BASE_COLOR)
    errors=list(materials.lib.recompile_material(masked) or []);assert not errors,errors
    u.EditorAssetLibrary.save_loaded_asset(masked);materials.protect_asset(masked)
    mesh('Masked',(-100,80,100),(.02,.3,2.),masked)
    mesh('Floor',(70,0,-10),(10,10,.2),opaque)
    light=actors.spawn_actor_from_class(u.SpotLight if light_type=='spot' else u.PointLight,u.Vector(-200,0,100))
    light.tags=[u.Name('FogTest.Light')];light.set_actor_label('FogTest Light')
    lc=light.spot_light_component if light_type=='spot' else light.point_light_component
    lc.set_mobility(u.ComponentMobility.MOVABLE)
    if light_type=='spot':
        light.set_actor_rotation(u.Rotator(0,0,0),False)
        prop(lc,'inner_cone_angle',40.);prop(lc,'outer_cone_angle',60.)
    lc.set_intensity(10000.);prop(lc,'intensity_units',u.LightUnits.LUMENS)
    prop(lc,'source_radius',0.);prop(lc,'attenuation_radius',1500.)
    prop(lc,'cast_shadows',True);prop(lc,'cast_volumetric_shadow',True)
    prop(lc,'volumetric_scattering_intensity',1.)
    lc.set_cast_raytraced_shadows(u.CastRayTracedShadow.USE_PROJECT_SETTING)
    if kind=='height':
        fog=actors.spawn_actor_from_class(u.ExponentialHeightFog,u.Vector())
        c=fog.get_component_by_class(u.ExponentialHeightFogComponent)
        prop(c,'fog_density',.04);prop(c,'fog_height_falloff',0.)
        prop(c,'fog_max_opacity',.001);prop(c,'enable_volumetric_fog',True)
        prop(c,'volumetric_fog_albedo',u.Color(255,255,255,255))
        prop(c,'volumetric_fog_distance',1200.)
        prop(c,'volumetric_fog_scattering_distribution',0.)
    else:
        fog=actors.spawn_actor_from_class(u.HeterogeneousVolume,u.Vector(50,0,100))
        c=fog.get_component_by_class(u.HeterogeneousVolumeComponent)
        prop(c,'pivot_at_centroid',True);c.set_volume_resolution(u.IntVector(128,128,128))
        fog.set_actor_scale3d(u.Vector(6,6,6))
        volume=materials.create('M_UniformVolume_'+case_kind,u.Material,u.MaterialFactoryNew())
        materials.material=volume;materials.clear_expressions(volume)
        prop(volume,'material_domain',u.MaterialDomain.MD_VOLUME);prop(volume,'blend_mode',u.BlendMode.BLEND_ADDITIVE)
        materials.lib.set_material_usage(volume,u.MaterialUsage.MATUSAGE_HETEROGENEOUS_VOLUMES)
        extinction=materials.constant([.00012]*3);albedo=materials.constant([1.,1.,1.])
        materials.lib.connect_material_property(extinction[0],extinction[1],u.MaterialProperty.MP_SUBSURFACE_COLOR)
        materials.lib.connect_material_property(albedo[0],albedo[1],u.MaterialProperty.MP_BASE_COLOR)
        if u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
            bsdf=materials.node('SubstrateVolumetricFogCloudBSDF',emissive_only=False)
            materials.connect(extinction,bsdf,'Extinction');materials.connect(albedo,bsdf,'Albedo')
            assert materials.lib.connect_material_property(bsdf[0],bsdf[1],u.MaterialProperty.MP_FRONT_MATERIAL)
        materials.node('VolumetricAdvancedMaterialOutput',gray_scale_material=False,per_sample_phase_evaluation=True)
        errors=list(materials.lib.recompile_material(volume) or []);assert not errors,errors
        u.EditorAssetLibrary.save_loaded_asset(volume);materials.protect_asset(volume);c.set_material(0,volume)
    fog.tags=[u.Name('FogTest.Volume')]
    camera=actors.spawn_actor_from_class(u.CineCameraActor,u.Vector(500,0,100))
    camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(camera.get_actor_location(),u.Vector(-100,0,100)),False)
    cam=camera.get_cine_camera_component();cam.set_editor_property('current_focal_length',35.)
    film=cam.get_editor_property('filmback');film.sensor_width=36.;film.sensor_height=20.25;cam.set_editor_property('filmback',film)
    focus=cam.get_editor_property('focus_settings');focus.focus_method=u.CameraFocusMethod.DISABLE;cam.set_editor_property('focus_settings',focus)
    post=actors.spawn_actor_from_class(u.PostProcessVolume,u.Vector());prop(post,'unbound',True)
    settings=post.get_editor_property('settings');lumen_post_process(settings)
    for name,value in dict(auto_exposure_method=u.AutoExposureMethod.AEM_MANUAL,
        auto_exposure_apply_physical_camera_exposure=False,auto_exposure_bias=0.,bloom_intensity=0.,
        vignette_intensity=0.,motion_blur_amount=0.).items():
        setattr(settings,'override_'+name,True);setattr(settings,name,value)
    post.set_editor_property('settings',settings)
    seq=materials.create('LS_'+case_kind,u.LevelSequence,u.LevelSequenceFactoryNew())
    seq.set_display_rate(u.FrameRate(30,1));seq.set_playback_start(0);seq.set_playback_end(48)
    binding=seq.add_possessable(camera);cut=seq.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,48)
    bid=u.MovieSceneObjectBindingID();bid.set_editor_property('guid',binding.get_id());cut.set_camera_binding_id(bid)
    u.EditorAssetLibrary.save_loaded_asset(seq);materials.protect_asset(seq)
    preset=materials.create('MRQ_'+case_kind,u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting);output.output_resolution=u.IntPoint(1280,720)
    configure_mrq_defaults(preset,'lumen','balanced',1.)
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting);aa.engine_warm_up_count=60;aa.render_warm_up_count=8
    cv=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for name,value in {'r.HeterogeneousVolumes':1,'r.HeterogeneousVolumes.Shadows':1,
        'r.HeterogeneousVolumes.DownsampleFactor':1,'r.HeterogeneousVolumes.Preshading':0,
        'r.HeterogeneousVolumes.LightingCache':2,'r.HeterogeneousVolumes.IndirectLighting':1,
        'r.HeterogeneousVolumes.IndirectLighting.Mode':2,'r.HeterogeneousVolumes.MaxStepCount':512,
        'r.Shadow.Virtual.Enable':1,'r.VolumetricFog':1,'r.VolumetricFog.GridPixelSize':8,
        'r.VolumetricFog.GridSizeZ':128,'r.PathTracing.HeterogeneousVolumes':1,
        'r.PathTracing.EnableAtmosphere':1}.items():cv.add_or_update_console_variable(name,value)
    u.EditorAssetLibrary.save_loaded_asset(preset);materials.protect_asset(preset)
    assert level.save_current_level()
    fixture.append(dict(kind=case_kind,volume_kind=kind,light_type=light_type,level=root+'/Maps/'+case_kind,sequence=seq.get_path_name(),render_preset=preset.get_path_name(),
        camera=dict(position=[camera.get_actor_location().x,camera.get_actor_location().y,camera.get_actor_location().z],rotation=[camera.get_actor_rotation().pitch,camera.get_actor_rotation().yaw,camera.get_actor_rotation().roll],focal_length=35,sensor_width=36),
        light=light.get_name(),volume=fog.get_name()))
atomic_json(OUT/'fixtures.json',dict(version=3,root=root,owner=owner,fixtures=fixture,report=report))
print('FOG_OCCLUSION_FIXTURES_READY')
