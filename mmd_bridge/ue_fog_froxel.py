"""Source SVT -> native Volumetric Fog, plus an isolated uniform experiment.

Reuses the authored SVT mask and its native frame transform. A source constant
proof is mandatory; the actor/material names never select this renderer.
Native auxiliary actors are prepared during native fog import. Runtime
only changes the loaded copy, original components remain available for PT.
"""
import math
import unreal as u
from .fog_occlusion import TemporaryOverrides
from .ue_appearance import native_property

_STATES={}
_STREAMERS={}


def prepare_material(materials):
    m=materials.create('M_FogOcclusion_Froxel_SVT_v1',u.Material,u.MaterialFactoryNew())
    materials.material=m;materials.clear_expressions(m)
    from .ue_materials import prop
    prop(m,'material_domain',u.MaterialDomain.MD_VOLUME)
    prop(m,'blend_mode',u.BlendMode.BLEND_ADDITIVE)
    empty=u.load_asset('/Engine/EngineResources/EmptySparseVolumeTexture')
    sample=materials.node('SparseVolumeTextureSampleParameter',parameter_name='Volume',sparse_volume_texture=empty)
    ext=materials.node('ComponentMask',r=True,g=True,b=True,a=False)
    materials.connect((sample[0],'Attributes A'),ext,'Input')
    # HVC integrates per voxel. Fog voxelization integrates per metre and
    # explicitly multiplies by 1/100 in UE 5.8's voxelization shader.
    ext=materials.op('Divide',ext,materials.scalar('Source voxel metres',1.))
    ext=materials.op('Multiply',ext,materials.scalar('Fog density',1.))
    alb=materials.node('ComponentMask',r=True,g=True,b=True,a=False)
    materials.connect((sample[0],'Attributes B'),alb,'Input')
    materials.lib.connect_material_property(ext[0],ext[1],u.MaterialProperty.MP_SUBSURFACE_COLOR)
    materials.lib.connect_material_property(alb[0],alb[1],u.MaterialProperty.MP_BASE_COLOR)
    if u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
        bsdf=materials.node('SubstrateVolumetricFogCloudBSDF',emissive_only=False)
        materials.connect(ext,bsdf,'Extinction');materials.connect(alb,bsdf,'Albedo')
        assert materials.lib.connect_material_property(bsdf[0],bsdf[1],u.MaterialProperty.MP_FRONT_MATERIAL)
    assert not list(materials.lib.recompile_material(m) or [])
    assert u.EditorAssetLibrary.save_loaded_asset(m);materials.protect_asset(m)
    return m.get_path_name()


def _texture(component, parameter='Volume'):
    seen=set()
    for initial in (component.get_material(0),native_property(component,'material_instance_dynamic','MaterialInstanceDynamic')):
        material=initial
        while isinstance(material,u.MaterialInstance) and material not in seen:
            seen.add(material)
            rows=material.get_editor_property('sparse_volume_texture_parameter_values')
            for row in rows:
                if str(row.parameter_info.name)==parameter and row.parameter_value:
                    return row.parameter_value
            material=material.get_editor_property('parent')
    return None


def _source_frame(texture):
    if texture is None:raise ValueError('missing_source_svt_frame')
    controller=_STREAMERS.get(texture)
    if controller is None:
        controller=u.new_object(u.AnimatedSparseVolumeTextureController)
        controller.set_editor_property('sparse_volume_texture',texture)
        controller.set_editor_property('mip_level',0)
        controller.set_editor_property('blocking_streaming_requests',True)
        _STREAMERS[texture]=controller
    frame=controller.get_frame_by_index(0)
    if frame is None:raise ValueError('source_svt_frame_unavailable')
    return frame


def refresh_native_streaming():
    # Hidden HVC carriers no longer issue streaming requests. Keep the actual
    # source frames resident for the native Volume-domain samples instead.
    for texture in list(_STREAMERS):_source_frame(texture)


def release_native_streaming():
    _STREAMERS.clear()


def _pose(component):
    texture=_texture(component);frame=_source_frame(texture)
    frame_transform=native_property(frame,'transform','Transform')
    if frame_transform is None:raise ValueError('source_svt_frame_transform_unreadable')
    transform=u.MathLibrary.compose_transforms(frame_transform,component.get_world_transform())
    resolution=native_property(texture,'volume_resolution','VolumeResolution')
    half=u.Vector(resolution.x/2.,resolution.y/2.,resolution.z/2.)
    center=u.Vector() if native_property(component,'pivot_at_centroid','bPivotAtCentroid') else half
    origin=u.MathLibrary.transform_location(transform,center);scale=transform.scale3d
    size=u.Vector(half.x*scale.x/50.,half.y*scale.y/50.,half.z*scale.z/50.)
    pose=u.Transform(location=origin,rotation=u.MathLibrary.quat_rotator(transform.rotation),scale=size)
    distance=math.sqrt((half.x*scale.x)**2+(half.y*scale.y)**2+(half.z*scale.z)**2)*4
    return pose,distance


def _same_pose(a,b):
    return all(abs(getattr(getattr(a,k),axis)-getattr(getattr(b,k),axis))<1e-3
               for k,axes in (('translation','xyz'),('scale3d','xyz'),('rotation','xyzw')) for axis in axes)


def prepare_actors(actor_sub, stages, sources, kernel_path, meters_per_unit=1.):
    """Explicit editor preparation only; no scene-name or mesh-name rules."""
    from .fog_occlusion import froxel_eligibility
    parent=u.load_asset(kernel_path);created=[];distance=0.
    if not parent:raise ValueError('missing_froxel_kernel')
    for volume in sources:
        if not froxel_eligibility(volume,meters_per_unit)['eligible']:continue
        component=next((s.get_generated_component(volume['prim_path']) for s in stages
            if s.get_generated_component(volume['prim_path']) is not None),None)
        if not isinstance(component,u.HeterogeneousVolumeComponent):continue
        pose,reach=_pose(component);distance=max(distance,reach)
        actor=actor_sub.spawn_actor_from_class(u.StaticMeshActor,pose.translation)
        actor.set_actor_transform(pose,False,False)
        mesh=actor.static_mesh_component;mesh.set_mobility(u.ComponentMobility.MOVABLE)
        mesh.set_static_mesh(u.load_asset('/Engine/BasicShapes/Cube'))
        mesh.set_collision_enabled(u.CollisionEnabled.NO_COLLISION);mesh.set_cast_shadow(False)
        mesh.set_material(0,parent);mesh.set_visibility(False);actor.set_actor_hidden_in_game(True)
        actor.tags=[u.Name('MMDBridge.FogProxy='+volume['id'])];created.append(actor.get_path_name())
    if created:
        host=actor_sub.spawn_actor_from_class(u.ExponentialHeightFog,u.Vector())
        c=host.get_component_by_class(u.ExponentialHeightFogComponent)
        for name,value in dict(fog_density=.04,fog_height_falloff=0.,fog_max_opacity=1.,
            start_distance=1e9,volumetric_fog_extinction_scale=0.,enable_volumetric_fog=True,
            volumetric_fog_scattering_distribution=0.,volumetric_fog_distance=max(2000.,distance)).items():
            c.set_editor_property(name,value)
        c.set_visibility(False);host.set_actor_hidden_in_game(True)
        host.tags=[u.Name('MMDBridge.ExperimentalFogHost')];created.append(host.get_path_name())
    return created


def apply(world, stages, sources, kernel_path, meters_per_unit=1., density_scale=1.):
    """Caller applies only a validated/explicit experimental semantic plan."""
    parent=u.load_asset(kernel_path) if kernel_path else None
    if not parent:return dict(status='limited',reason='missing_froxel_kernel',changed=[])
    from .fog_occlusion import froxel_eligibility
    candidates=[];reviews=[]
    for volume in sources:
        check=froxel_eligibility(volume,meters_per_unit)
        if not check['eligible']:
            reviews.append(dict(id=volume.get('id'),reason=check['reason']));continue
        component=next((s.get_generated_component(volume['prim_path']) for s in stages
                        if s.get_generated_component(volume['prim_path']) is not None),None)
        if not isinstance(component,u.HeterogeneousVolumeComponent):
            reviews.append(dict(id=volume['id'],reason='missing_source_component'));continue
        texture=_texture(component)
        if texture is None:
            reviews.append(dict(id=volume['id'],reason='missing_source_svt'));continue
        candidates.append((volume,component,texture,check))
    if not candidates:return dict(status='limited',reviews=reviews,changed=[])
    token=tuple(c.get_path_name() for _,c,_,_ in candidates)
    previous=_STATES.get(token)
    if previous and all(previous['components'].get(c.get_path_name())==c for _,c,_,_ in candidates):
        return dict(status='experimental',changed=previous['changed'],reviews=reviews,visual_status='needs_review')
    # One phase parameter is shared by UE's froxel renderer. Preserve existing
    # height fog/other volumes when their phase cannot share the same value.
    phase=candidates[0][3]['phase_g']
    if any(abs(c[3]['phase_g']-phase)>1e-6 for c in candidates):
        return dict(status='limited',reason='multiple_phase_values',changed=[],reviews=reviews)
    hosts=[h for h in u.GameplayStatics.get_all_actors_of_class(world,u.ExponentialHeightFog)
           if 'MMDBridge.ExperimentalFogHost' not in [str(t) for t in h.tags] and
           h.get_component_by_class(u.ExponentialHeightFogComponent).is_visible()]
    for host in hosts:
        c=host.get_component_by_class(u.ExponentialHeightFogComponent)
        if abs(float(native_property(c,'volumetric_fog_scattering_distribution'))-phase)>1e-6:
            return dict(status='limited',reason='existing_height_fog_phase_mismatch',changed=[],reviews=reviews)
    state=dict(controller=TemporaryOverrides(),components={},changed=[],materials=[])
    try:
        distance=0.;desired={}
        proxies=u.GameplayStatics.get_all_actors_of_class(world,u.StaticMeshActor)
        for volume,component,texture,check in candidates:
            matches=[a for a in proxies if 'MMDBridge.FogProxy='+volume['id'] in [str(t) for t in a.tags]]
            if len(matches)!=1:raise ValueError('missing_or_ambiguous_prepared_proxy')
            actor=matches[0];pose,reach=_pose(component);distance=max(distance,reach)
            if not _same_pose(actor.get_actor_transform(),pose):
                raise ValueError('source_or_proxy_transform_changed')
            mesh=actor.get_component_by_class(u.StaticMeshComponent)
            if mesh.get_material(0)!=parent:raise ValueError('proxy_material_manually_changed')
            dynamic=mesh.create_dynamic_material_instance(0,parent)
            dynamic.set_sparse_volume_texture_parameter_value('Volume',texture)
            dynamic.set_scalar_parameter_value('Source voxel metres',check['voxel_metres'])
            dynamic.set_scalar_parameter_value('Fog density',density_scale)
            state['materials'].append((mesh,parent,dynamic))
            name=component.get_path_name();state['components'][name]=component
            state['components'][mesh.get_path_name()]=mesh
            desired[name]=False;desired[mesh.get_path_name()]=True
            state['changed'].append(dict(prim_path=volume['prim_path'],source_component=name,
                proxy=mesh.get_path_name(),source_svt=texture.get_path_name(),voxel_metres=check['voxel_metres'],
                container_preserved=True,source_material_changed=False))
        if not hosts:
            owned=[h for h in u.GameplayStatics.get_all_actors_of_class(world,u.ExponentialHeightFog)
                   if 'MMDBridge.ExperimentalFogHost' in [str(t) for t in h.tags]]
            if len(owned)!=1:raise ValueError('missing_or_ambiguous_prepared_host')
            host=owned[0]
            c=host.get_component_by_class(u.ExponentialHeightFogComponent)
            if abs(float(native_property(c,'volumetric_fog_scattering_distribution'))-phase)>1e-6:
                raise ValueError('prepared_host_phase_changed')
            if float(native_property(c,'volumetric_fog_extinction_scale'))!=0.:
                raise ValueError('prepared_host_density_changed')
            if float(native_property(c,'volumetric_fog_distance'))<distance-.01:
                raise ValueError('prepared_host_distance_insufficient')
            state['components'][c.get_path_name()]=c;desired[c.get_path_name()]=True
        else:
            # Changing an existing height fog's sampling distance changes its
            # rendering. Require its current distance to cover the containers.
            if any(float(native_property(h.get_component_by_class(u.ExponentialHeightFogComponent),
                'volumetric_fog_distance'))<distance for h in hosts):
                raise ValueError('existing_height_fog_distance_insufficient')
        def read(n):
            c=state['components'][n]
            return (c.is_visible(),bool(native_property(c.get_owner(),'hidden','bHidden')))
        def write(n,v):
            c=state['components'][n];c.set_visibility(v[0]);c.get_owner().set_actor_hidden_in_game(v[1])
        # Keep the source owner's hidden flag: USD may share its actor with
        # other prims. Auxiliary actors exclusively contain the fog component.
        targets={n:(v,read(n)[1] if not v else False) for n,v in desired.items()}
        state['controller'].apply(targets,read,write)
        _STATES[token]=state
        return dict(status='experimental',changed=state['changed'],reviews=reviews,
                    additional_dense_lighting_cache_bytes=0,visual_status='needs_review')
    except Exception:
        for mesh,parent,dynamic in state['materials']:
            if mesh.get_material(0)==dynamic:mesh.set_material(0,parent)
        raise


def restore():
    for state in _STATES.values():
        def read(name):
            try:
                c=state['components'][name];return (c.is_visible(),bool(native_property(c.get_owner(),'hidden','bHidden')))
            except Exception:return None
        def write(name,value):
            c=state['components'][name];c.set_visibility(value[0]);c.get_owner().set_actor_hidden_in_game(value[1])
        state['controller'].end(read,write)
        for mesh,parent,dynamic in state['materials']:
            try:
                if mesh.get_material(0)==dynamic:mesh.set_material(0,parent)
            except Exception:pass  # A destroyed/reloaded world already owns teardown.
    _STATES.clear()


def _native_material(materials, volume, component, voxel_metres, density):
    """Editable native fog material, using the original sparse fields and mask."""
    from .ue_materials import prop
    from .scene_appearance import RADIANCE_TO_UE
    texture=_texture(component)
    if texture is None:raise ValueError('missing_native_fog_source_svt')
    m=materials.create('M_NativeFog_'+volume['id'],u.Material,u.MaterialFactoryNew())
    materials.material=m;materials.clear_expressions(m)
    prop(m,'material_domain',u.MaterialDomain.MD_VOLUME)
    prop(m,'blend_mode',u.BlendMode.BLEND_ADDITIVE)
    sample=materials.node('SparseVolumeTextureSampleParameter',parameter_name='Volume',sparse_volume_texture=texture)
    rgb=materials.node('ComponentMask',r=True,g=True,b=True,a=False)
    materials.connect((sample[0],'Attributes A'),rgb,'Input')
    # VF has scalar extinction. The mean preserves neutral extinction exactly;
    # RGB absorption cannot be represented and is reported by native_volume_plan.
    ext=materials.op('DotProduct',rgb,materials.constant([1./3]*3))
    ext=materials.op('Divide',ext,materials.constant(voxel_metres))
    ext=materials.op('Multiply',ext,materials.scalar('Fog density',density))
    alb=materials.node('ComponentMask',r=True,g=True,b=True,a=False)
    materials.connect((sample[0],'Attributes B'),alb,'Input')
    em=materials.constant([0.,0.,0.])
    emission_texture=None
    if volume.get('emission_path'):
        emission_texture=_texture(component,'Emission')
        if emission_texture is None:raise ValueError('missing_native_fog_emission_svt')
        _source_frame(emission_texture)
        e=materials.node('SparseVolumeTextureSampleParameter',parameter_name='Emission',sparse_volume_texture=emission_texture)
        mask=materials.node('ComponentMask',r=True,g=True,b=True,a=False)
        materials.connect((e[0],'Attributes A'),mask,'Input')
        em=materials.op('Multiply',mask,materials.constant(RADIANCE_TO_UE/voxel_metres))
    for value,property in ((ext,u.MaterialProperty.MP_SUBSURFACE_COLOR),
                           (alb,u.MaterialProperty.MP_BASE_COLOR),(em,u.MaterialProperty.MP_EMISSIVE_COLOR)):
        assert materials.lib.connect_material_property(value[0],value[1],property)
    if u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
        bsdf=materials.node('SubstrateVolumetricFogCloudBSDF',emissive_only=False)
        materials.connect(ext,bsdf,'Extinction');materials.connect(alb,bsdf,'Albedo')
        materials.connect(em,bsdf,'Emissive Color')
        assert materials.lib.connect_material_property(bsdf[0],bsdf[1],u.MaterialProperty.MP_FRONT_MATERIAL)
    errors=list(materials.lib.recompile_material(m) or [])
    if errors:raise RuntimeError('原生体积雾材质编译失败: '+str(errors))
    assert u.EditorAssetLibrary.save_loaded_asset(m);materials.protect_asset(m)
    return m,texture,emission_texture


def prepare_native(actor_sub, stages, sources, materials, cfg, meters_per_unit=1.):
    """Import once. Quality tiers reuse these same actors, SVTs and materials."""
    import json
    from .native_fog_candidate import native_volume_plan
    plan=native_volume_plan(sources,meters_per_unit)
    bindings=[];reach=0.
    for row in plan['grids']:
        found=[s.get_generated_component(row['prim_path']) for s in stages]
        found=list({c.get_path_name():c for c in found if c is not None}.values())
        if len(found)!=1 or not isinstance(found[0],u.HeterogeneousVolumeComponent):
            raise ValueError('原生体积雾源组件无法唯一定位: '+row['prim_path'])
        component=found[0];volume=next(v for v in sources if v['id']==row['id'])
        material,texture,emission=_native_material(materials,volume,component,
            row['voxel_metres'],cfg.get('fog_density_scale',1.))
        pose,distance=_pose(component);reach=max(reach,distance)
        actor=actor_sub.spawn_actor_from_class(u.StaticMeshActor,pose.translation)
        actor.set_actor_transform(pose,False,False);actor.set_actor_label('Native fog: '+volume['name'])
        mesh=actor.static_mesh_component;mesh.set_mobility(u.ComponentMobility.MOVABLE)
        mesh.set_static_mesh(u.load_asset('/Engine/BasicShapes/Cube'))
        mesh.set_collision_enabled(u.CollisionEnabled.NO_COLLISION);mesh.set_cast_shadow(False)
        mesh.set_material(0,material)
        binding=dict(id=row['id'],prim_path=row['prim_path'],proxy=actor.get_name(),
            material=material.get_path_name(),source_svt=texture.get_path_name(),
            emission_svt=emission.get_path_name() if emission else None,
            source_visible=component.is_visible(),voxel_metres=row['voxel_metres'])
        actor.tags=[u.Name('MMDBridge'),u.Name('MMDBridge.NativeFog='+json.dumps(binding,separators=(',',':')))]
        bindings.append(binding)
    hosts=[a for a in actor_sub.get_all_level_actors() if isinstance(a,u.ExponentialHeightFog)
           and 'MMDBridge' in [str(t) for t in a.tags]]
    if bindings and not hosts:
        host=actor_sub.spawn_actor_from_class(u.ExponentialHeightFog,u.Vector())
        host.tags=[u.Name('MMDBridge'),u.Name('MMDBridge.NativeFogHost')]
        host.set_actor_label('Native local fog host')
        c=host.get_component_by_class(u.ExponentialHeightFogComponent)
        # Nonzero analytic density registers the host; only local SVTs scatter.
        for name,value in dict(fog_density=.04,fog_height_falloff=0.,fog_max_opacity=1.,
            start_distance=1e9,volumetric_fog_extinction_scale=0.,enable_volumetric_fog=True,
            volumetric_fog_scattering_distribution=plan['phase_g'],
            volumetric_fog_distance=max(2000.,reach)).items():c.set_editor_property(name,value)
        hosts=[host]
    for host in hosts:
        c=host.get_component_by_class(u.ExponentialHeightFogComponent)
        c.set_editor_property('volumetric_fog_scattering_distribution',plan['phase_g'])
        if bindings:c.set_editor_property('volumetric_fog_distance',max(reach,float(native_property(c,'volumetric_fog_distance'))))
    result=dict(plan,bindings=bindings,renderer='volumetric_fog',quality=cfg['fog_occlusion_quality'],
                source_fields_preserved=True,quality_requires_reimport=False)
    world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    result['runtime']=restore_native(world,stages,cfg.get('scene_render_pipeline','lumen'),True)
    return result


def restore_native(world, stages, pipeline='lumen', enabled=True, sources=None):
    """Bind saved native proxies after USD regeneration and in isolated MRQ worlds.

    HVC remains the saved source carrier for PT. Never change its material or
    shared owner visibility, and never affect unrelated/manual fog actors.
    """
    import json
    use_native=enabled and pipeline=='lumen';rows=[];targets=[]
    for actor in u.GameplayStatics.get_all_actors_of_class(world,u.StaticMeshActor):
        tags=[str(t) for t in actor.tags]
        records=[t.split('=',1)[1] for t in tags if t.startswith('MMDBridge.NativeFog=')]
        if not records:continue
        if len(records)!=1:raise ValueError('ambiguous_native_fog_binding')
        binding=json.loads(records[0]);path=binding['prim_path']
        found=[s.get_generated_component(path) for s in stages]
        found=list({c.get_path_name():c for c in found if c is not None}.values())
        if len(found)!=1 or not isinstance(found[0],u.HeterogeneousVolumeComponent):
            raise ValueError('missing_native_fog_source:'+path)
        source=found[0];mesh=actor.static_mesh_component
        if mesh.get_material(0) is None or mesh.get_material(0).get_path_name()!=binding['material']:
            raise ValueError('native_fog_material_changed:'+path)
        texture=_texture(source)
        if texture is None or texture.get_path_name()!=binding['source_svt']:
            raise ValueError('native_fog_source_svt_changed:'+path)
        pose,_=_pose(source)
        if not _same_pose(actor.get_actor_transform(),pose):
            raise ValueError('native_fog_transform_changed:'+path)
        if binding.get('emission_svt'):
            emission=_texture(source,'Emission')
            if emission is None or emission.get_path_name()!=binding['emission_svt']:
                raise ValueError('native_fog_emission_svt_changed:'+path)
            _source_frame(emission)
        targets.append((source,mesh,actor,binding))
    if use_native and sources is not None:
        from .native_fog_candidate import native_volume_plan
        expected={v['prim_path'] for v in native_volume_plan(sources)['grids']}
        if {v[3]['prim_path'] for v in targets}!=expected:
            raise ValueError('原生雾绑定与源体积不一致，请重新导入，拒绝仅切换渲染参数')
    for source,mesh,actor,binding in targets:
        source.set_visibility(False if use_native else binding['source_visible'])
        mesh.set_visibility(use_native);actor.set_actor_hidden_in_game(not use_native)
        rows.append(dict(binding,source_component=source.get_path_name(),
            source_visible=source.is_visible(),native_visible=mesh.is_visible(),
            native_owner_hidden=bool(native_property(actor,'hidden','bHidden'))))
    for actor in u.GameplayStatics.get_all_actors_of_class(world,u.ExponentialHeightFog):
        if 'MMDBridge.NativeFogHost' in [str(t) for t in actor.tags]:
            actor.get_component_by_class(u.ExponentialHeightFogComponent).set_visibility(use_native)
            actor.set_actor_hidden_in_game(not use_native)
    if not use_native:release_native_streaming()
    return dict(renderer='volumetric_fog' if use_native else 'source',bindings=rows,
                restored=len(rows),visual_status='needs_review')
