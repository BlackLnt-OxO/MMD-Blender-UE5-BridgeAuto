"""Build an opt-in, isolated soft-light study from a saved PBR fixture.

The existing shader graphs, mesh topology and source assets are read-only.
Skin profiles, instances, mesh bindings, lights, sequences and maps are copied
under the explicitly supplied new /Game/MMDBridgePBR/SoftShadow_* root.
"""
from pathlib import Path
import copy, os, sys
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json, read_json, file_hash, stable_id
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_host import bind_float

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']).resolve()
config=read_json(folder/'soft_shadow_config.json')
source_folder=Path(config['source_fixture']);source=read_json(source_folder/'ue_receipt.json')
manifest=read_json(folder/'manifest.json');root=config['root']
assert root.startswith('/Game/MMDBridgePBR/SoftShadow_') and root!=source['root']
assert manifest['source_sha256']==read_json(source_folder/'manifest.json')['source_sha256']
owner='pbr_soft_shadow_'+stable_id(root)
report=copy.deepcopy(source)
for key in ('review_views','review_levels','dark_level_sha256','mesh_comparison','matcap_off_overrides','wet_on_overrides'):
    report.pop(key,None)
report.update(root=root,status='building',assets={'materials':{}},checks={})
report['render_assets']={'level':root+'/L_SoftShadow_Review'}
manager=Materials(root,owner,report,manifest['settings']);lib=manager.lib
editor=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
profile_paths={};parent_paths={};material_profiles={};modified_assets=[]
world_ledger=read_json(folder/'world_hashes.json') if (folder/'world_hashes.json').is_file() else {}
result={'status':'running','source_root':source['root'],'root':root,'config':config,
    'shader_equations':'unchanged v8; duplicated skin parents use independent Subsurface Profiles',
    'mesh':'copied with original topology, weighted normals, weights and morph targets'}


def save(asset):
    assert asset.get_path_name().startswith(root+'/')
    u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.Owner',owner)
    u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.SoftShadowBranch','1')
    assert u.EditorAssetLibrary.save_loaded_asset(asset,only_if_is_dirty=False)
    if isinstance(asset,u.World):
        path=Path(u.Paths.project_content_dir())/(asset.get_path_name().split('.')[0][6:]+'.umap')
        assert path.is_file();world_ledger[asset.get_path_name()]=file_hash(path)
        atomic_json(folder/'world_hashes.json',world_ledger)
    else:manager.protect_asset(asset)
    if asset.get_path_name() not in modified_assets:modified_assets.append(asset.get_path_name())
    return asset


def verify_world(path):
    world=u.load_asset(path);assert isinstance(world,u.World)
    assert u.EditorAssetLibrary.get_metadata_tag(world,'MMDBridge.Owner')==owner
    filename=Path(u.Paths.project_content_dir())/(path[6:]+'.umap')
    assert file_hash(filename)==world_ledger.get(world.get_path_name()),'Preserve later edits to '+path


def duplicate(asset,path):
    assert path.startswith(root+'/')
    result=u.load_asset(path)
    if result:
        assert u.EditorAssetLibrary.get_metadata_tag(result,'MMDBridge.Owner')==owner
        manager.verify_asset(result)
    else:
        result=u.EditorAssetLibrary.duplicate_asset(asset.get_path_name(),path)
        assert result
        # Duplicates inherit the source ownership tag; only this new path changes.
        u.EditorAssetLibrary.set_metadata_tag(result,'MMDBridge.Owner',owner)
        save(result)
    return result


def create(name,kind,factory):
    asset=u.load_asset(root+'/'+name)
    if asset:manager.verify_asset(asset)
    else:asset=u.AssetToolsHelpers.get_asset_tools().create_asset(name,root,kind,factory)
    assert asset
    return asset


def vector(value):return [float(value.r),float(value.g),float(value.b)]


def skin_parent(parent):
    identity=parent.get_path_name()
    if identity in parent_paths:return u.load_asset(parent_paths[identity])
    old_profile=parent.get_editor_property('subsurface_profile');assert old_profile
    profile=duplicate(old_profile,root+'/Materials/SSP_Soft_'+stable_id(identity))
    settings=profile.get_editor_property('settings')
    original_settings=old_profile.get_editor_property('settings')
    before={name:vector(original_settings.get_editor_property(name)) for name in ('surface_albedo','mean_free_path_color','tint','transmission_tint_color')}
    before['mean_free_path_distance']=original_settings.get_editor_property('mean_free_path_distance')
    for name,value in {'surface_albedo':u.LinearColor(*config['surface_albedo'],1),
                       'mean_free_path_color':u.LinearColor(*config['mean_free_path_color'],1)}.items():
        settings.set_editor_property(name,value)
    # Preserve the already validated short distances and the nose mask.
    settings.set_editor_property('enable_burley',True);settings.set_editor_property('enable_mean_free_path',True)
    profile.set_editor_property('settings',settings);save(profile)
    copied=duplicate(parent,root+'/Materials/M_Soft_'+stable_id(identity))
    copied.set_editor_property('subsurface_profile',profile)
    shaders=[node for node in lib.get_material_expressions(copied)
             if isinstance(node,u.MaterialExpressionSubstrateShadingModels)]
    assert len(shaders)==1
    shaders[0].set_editor_property('subsurface_profile',profile)
    errors=lib.recompile_material(copied);assert not errors,errors
    save(copied)
    parent_paths[identity]=copied.get_path_name();profile_paths[identity]=profile.get_path_name()
    result.setdefault('profiles',[]).append({'source_parent':identity,'parent':copied.get_path_name(),
        'profile':profile.get_path_name(),'source':before,
        'mean_free_path_color':config['mean_free_path_color'],'surface_albedo':config['surface_albedo'],
        'mean_free_path_distance_preserved':before['mean_free_path_distance']})
    return copied


def variant(material,label,values=None,parent=None):
    asset=duplicate(material,root+'/Materials/'+material.get_name()+'_'+label)
    if parent:lib.set_material_instance_parent(asset,parent)
    lib.update_material_instance(asset)
    for name,value in (values or {}).items():
        set_scalar(asset,name,value)
    lib.update_material_instance(asset);save(asset)
    return asset


def set_scalar(asset,name,value):
    names=[str(n) for n in lib.get_scalar_parameter_names(asset)]
    assert name in names,(asset.get_path_name(),name,names)
    lib.set_material_instance_scalar_parameter_value(asset,name,float(value))
    lib.update_material_instance(asset)
    actual=lib.get_material_instance_scalar_parameter_value(asset,name)
    assert abs(actual-value)<1e-6,(asset.get_path_name(),name,value,actual)


def camera_values(actor):
    location=actor.get_actor_location();rotation=actor.get_actor_rotation();camera=actor.get_cine_camera_component()
    filmback=camera.get_editor_property('filmback')
    return {'location':[location.x,location.y,location.z],'rotation':[rotation.pitch,rotation.yaw,rotation.roll],
        'focal':float(camera.current_focal_length),'filmback':[filmback.sensor_width,filmback.sensor_height]}


def sequence(name,camera,animation,lighting,dark=False):
    seq=create(name,u.LevelSequence,u.LevelSequenceFactoryNew())
    for binding in list(seq.get_bindings()):binding.remove()
    for track in list(seq.get_tracks()):seq.remove_track(track)
    seq.set_display_rate(u.FrameRate(30,1));seq.set_playback_start(0);seq.set_playback_end(241)
    section=seq.add_possessable(actor).add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
    section.set_range(0,241);params=section.get_editor_property('params');params.animation=animation;params.force_custom_mode=True
    section.set_editor_property('params',params)
    binding=seq.add_possessable(camera)
    cut=seq.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,241)
    identity=u.MovieSceneObjectBindingID();identity.set_editor_property('guid',binding.get_id());cut.set_camera_binding_id(identity)
    for light in lights:
        value=0. if dark else lighting.get(light.get_actor_label(),0.)
        binding=seq.add_possessable(light)
        bind_float(seq,binding,light.light_component,'Intensity','Intensity',[(0,value),(240,value)])
        if isinstance(light,u.DirectionalLight):
            section=binding.add_track(u.MovieScene3DTransformTrack).add_section();section.set_range(0,241)
            for channel,value in zip(section.get_all_channels(),(0.,0.,300.,0.,-25.,145.,1.,1.,1.)):channel.set_default(value)
    return save(seq).get_path_name()


try:
    old_mesh=u.load_asset(source['mesh']);assert isinstance(old_mesh,u.SkeletalMesh)
    mesh=duplicate(old_mesh,root+'/Meshes/SK_SoftShadow')
    maps={key:{} for key in ('BaselineWarm','ScatterOff','RosinessOff','NeutralProfileOnly','WetOn')}
    rows=[]
    for record in manifest['materials']:
        old=u.load_asset(source['assets']['materials'][record['id']]);pbr=record['character_material']['pbr']
        instance=duplicate(old,root+'/Materials/'+old.get_name())
        skin=pbr['type'] in ('face','skin') and pbr['alpha_mode']!='translucent' and pbr['status']!='white'
        if skin:
            parent=skin_parent(old.parent);lib.set_material_instance_parent(instance,parent)
            lib.update_material_instance(instance)
            values={'SkinRosiness':config['rosiness'],'SSSStrength':config['face_scattering'] if pbr['type']=='face' else config['skin_scattering']}
            for name,value in values.items():set_scalar(instance,name,value)
            material_profiles[record['id']]=parent.get_editor_property('subsurface_profile').get_path_name()
        lib.update_material_instance(instance);save(instance)
        manager.assets[record['id']]=instance.get_path_name()
        if skin and pbr['params'].get('Opacity',1)>0:
            for label,values,parent in [('BaselineWarm',{},None),('ScatterOff',{'SSSStrength':0.},None),
                ('RosinessOff',{'SkinRosiness':0.},None),('NeutralProfileOnly',{},u.load_asset(parent_paths[old.parent.get_path_name()]))]:
                maps[label][instance.get_path_name()]=variant(old,label,values,parent).get_path_name()
            maps['WetOn'][instance.get_path_name()]=variant(instance,'WetOn',{'WetEnabled':1.}).get_path_name()
            rows.append({'pmx_index':record['pmx_index'],'id':record['id'],'type':pbr['type'],'instance':instance.get_path_name(),
                'profile':material_profiles[record['id']],'SSSStrength':lib.get_material_instance_scalar_parameter_value(instance,'SSSStrength'),
                'SkinRosiness':lib.get_material_instance_scalar_parameter_value(instance,'SkinRosiness')})
    manager.assign(mesh,manifest['materials']);save(mesh)
    report.update(mesh=mesh.get_path_name(),animation_asset=source['animation_asset'])
    report['assets']['materials']=dict(manager.assets);report['skin_controls']=rows
    for label,mapping in maps.items():atomic_json(folder/(label+'.json'),mapping)
    level=report['render_assets']['level']
    if u.EditorAssetLibrary.does_asset_exist(level):
        verify_world(level);assert editor.load_level(level)
    else:assert editor.new_level_from_template(level,source['render_assets']['level'])
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actor.set_actor_label('PBR_SoftShadow_'+config['sample'])
    actor.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)
    for index,slot in enumerate(mesh.materials):actor.skeletal_mesh_component.set_material(index,slot.material_interface)
    animation=u.load_asset(source['animation_asset'])
    cameras={a.get_actor_label():a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor)}
    portrait=cameras['PBR_Portrait'];camera_contract={key:camera_values(a) for key,a in cameras.items()}
    lights=[a for a in actors.get_all_level_actors() if isinstance(a,(u.DirectionalLight,u.SkyLight,u.RectLight))]
    sun=next(a for a in lights if isinstance(a,u.DirectionalLight));sky=next(a for a in lights if isinstance(a,u.SkyLight))
    softbox=next(a for a in lights if a.get_actor_label()=='PBR_Softbox')
    baseline_lighting={sun.get_actor_label():10.,sky.get_actor_label():.4,softbox.get_actor_label():0.}
    report['review_views']={'baseline':sequence('S_Baseline',portrait,animation,baseline_lighting)}
    sun.set_actor_rotation(u.Rotator(pitch=-25.,yaw=145.,roll=0.),False)
    assert editor.save_current_level()
    save(u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world())
    report.setdefault('review_levels',{})['baseline']=level
    # The baseline world keeps the source light geometry and angular size. Soft
    # lighting is saved in another world, so baseline and ablations stay matched.
    soft_level=root+'/L_SoftShadow_Lighting'
    if u.EditorAssetLibrary.does_asset_exist(soft_level):
        verify_world(soft_level);assert editor.load_level(soft_level)
    else:assert editor.new_level_from_template(soft_level,level)
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    cameras={a.get_actor_label():a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor)}
    lights=[a for a in actors.get_all_level_actors() if isinstance(a,(u.DirectionalLight,u.SkyLight,u.RectLight))]
    sun=next(a for a in lights if isinstance(a,u.DirectionalLight));sky=next(a for a in lights if isinstance(a,u.SkyLight))
    softbox=next(a for a in lights if a.get_actor_label()=='PBR_Softbox')
    sun.light_component.set_editor_property('light_source_angle',config['source_angle_degrees'])
    width,height=config['softbox_width_height_cm']
    softbox.light_component.set_editor_property('source_width',width);softbox.light_component.set_editor_property('source_height',height)
    soft_lighting={sun.get_actor_label():config['sun_lux'],sky.get_actor_label():config['sky_intensity'],softbox.get_actor_label():config['softbox_lumens']}
    for light in lights:light.light_component.set_intensity(soft_lighting.get(light.get_actor_label(),0.))
    report['lighting']={'baseline':baseline_lighting,'soft':soft_lighting,'source_angle_degrees':config['source_angle_degrees'],
        'softbox_width_height_cm':[width,height]}
    for view,label in (('portrait','PBR_Portrait'),('hair_side','PBR_HairSide'),('clothing','PBR_Clothing'),('full_body','PBR_FullBody')):
        report['review_views'][view]=sequence('S_Soft_'+view,cameras[label],animation,soft_lighting)
        report['review_levels'][view]=soft_level
    report['review_views']['dark']=sequence('S_Soft_Dark',cameras['PBR_Portrait'],animation,soft_lighting,dark=True)
    assert editor.save_current_level()
    world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world();save(world)
    # Stable zero-light initial state prevents skylight capture/history in an off test.
    dark_level=root+'/L_SoftShadow_Dark'
    if u.EditorAssetLibrary.does_asset_exist(dark_level):
        verify_world(dark_level);assert editor.load_level(dark_level)
    else:assert editor.new_level_from_template(dark_level,soft_level)
    for light in actors.get_all_level_actors():
        if isinstance(light,(u.DirectionalLight,u.SkyLight,u.RectLight)):light.light_component.set_intensity(0.)
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    lights=[a for a in actors.get_all_level_actors() if isinstance(a,(u.DirectionalLight,u.SkyLight,u.RectLight))]
    dark_camera=next(a for a in actors.get_all_level_actors() if a.get_actor_label()=='PBR_Portrait')
    report['review_views']['dark']=sequence('S_Soft_DarkBound',dark_camera,animation,soft_lighting,dark=True)
    assert editor.save_current_level();save(u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world())
    report['review_levels']['dark']=dark_level
    preset=duplicate(u.load_asset(source['portrait_preset']),root+'/MRQ_SoftShadow_1440p')
    console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for name,value in {'r.Shadow.Virtual.SMRT.RayCountDirectional':16.,'r.Shadow.Virtual.SMRT.RayCountLocal':16.,
        'r.Shadow.Virtual.SMRT.SamplesPerRayDirectional':8.,'r.Shadow.Virtual.SMRT.SamplesPerRayLocal':8.}.items():
        console.add_or_update_console_variable(name,value)
    save(preset);report['portrait_preset']=preset.get_path_name()
    report['render_assets'].update(level=soft_level,sequence=report['review_views']['portrait'],preset=preset.get_path_name())
    report['status']='ue_assets_created'
    assert editor.load_level(soft_level)
    current_cameras={a.get_actor_label():a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor)}
    assert {key:camera_values(a) for key,a in current_cameras.items()}==camera_contract
    reloaded=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor)).skeletal_mesh_component
    assert reloaded.get_editor_property('skeletal_mesh_asset')==mesh
    assert [m.get_path_name() for m in reloaded.get_materials()]==[m.material_interface.get_path_name() for m in mesh.materials]
    assert len(mesh.get_editor_property('morph_targets'))==len(old_mesh.get_editor_property('morph_targets'))
    # Actual single-instance editing and save/reload/undo, independent of the
    # global application undo ledger and every other material/character.
    probes=[]
    for row in rows:
        asset=u.load_asset(row['instance']);old=lib.get_material_instance_scalar_parameter_value(asset,'SSSStrength')
        others={path:file_hash(manager.asset_file(u.load_asset(path))) for path in manager.assets.values() if path!=row['instance']}
        try:
            set_scalar(asset,'SSSStrength',.173125)
            lib.update_material_instance(asset);save(asset)
            assert editor.save_current_level();assert editor.load_level(soft_level)
            loaded=u.load_asset(row['instance']);value=lib.get_material_instance_scalar_parameter_value(loaded,'SSSStrength')
            assert abs(value-.173125)<1e-6
            assert all(file_hash(manager.asset_file(u.load_asset(path)))==digest for path,digest in others.items())
            probes.append({'instance':row['instance'],'SSSStrength':value,'other_slots_unchanged':True})
        finally:
            set_scalar(asset,'SSSStrength',old)
            lib.update_material_instance(asset);save(asset)
        assert abs(lib.get_material_instance_scalar_parameter_value(u.load_asset(row['instance']),'SSSStrength')-old)<1e-6
    assert editor.save_current_level()
    save(u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world())
    result.update(status='complete',slot_count=len(mesh.materials),morph_targets=len(mesh.get_editor_property('morph_targets')),
        camera_contract=camera_contract,camera_saved_reloaded=True,skin_controls=rows,edit_reload_undo=probes,
        material_profiles=material_profiles,modified_assets=modified_assets)
    report['soft_shadow']=result
    print('SOFT_SHADOW_NATIVE_READY',config['sample'],len(mesh.materials),len(rows),flush=True)
except Exception:
    import traceback
    result.update(status='failed',error=traceback.format_exc());report.update(status='failed',error=result['error'])
    raise
finally:
    atomic_json(folder/'soft_shadow_receipt.json',result);atomic_json(folder/'ue_receipt.json',report)
