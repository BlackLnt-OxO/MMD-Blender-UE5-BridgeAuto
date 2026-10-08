"""Experimental bounded uniform SVT -> Volumetric Fog adapter.

Reuses the authored SVT mask and its native frame transform. A source constant
proof is mandatory; the actor/material names never select this renderer.
Hidden auxiliary actors are prepared by explicit experimental import. Runtime
only changes the loaded copy, original components remain available for PT.
"""
import math
import unreal as u
from .fog_occlusion import TemporaryOverrides
from .ue_appearance import native_property

_STATES={}


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


def _texture(component):
    seen=set()
    for initial in (component.get_material(0),native_property(component,'material_instance_dynamic','MaterialInstanceDynamic')):
        material=initial
        while isinstance(material,u.MaterialInstance) and material not in seen:
            seen.add(material)
            rows=material.get_editor_property('sparse_volume_texture_parameter_values')
            for row in rows:
                if str(row.parameter_info.name)=='Volume' and row.parameter_value:
                    return row.parameter_value
            material=material.get_editor_property('parent')
    return None


def _pose(component):
    transform=u.MathLibrary.compose_transforms(component.get_editor_property('frame_transform'),component.get_world_transform())
    resolution=native_property(component,'volume_resolution','VolumeResolution')
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
