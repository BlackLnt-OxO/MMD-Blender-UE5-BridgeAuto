"""Material review views shared by arbitrary PMX characters and posed props."""
from pathlib import Path
import os,sys,copy,re
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_pbr import build
from mmd_bridge.ue_host import bind_float

folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']);report=read_json(folder/'ue_receipt.json')
manifest=read_json(folder/'manifest.json');root=report['root'];key=root.rsplit('/',1)[-1]
assert report['status']=='ue_assets_created'
manager=Materials(root,'pbr_1017_'+key,report,manifest['settings'])
manager.assets=dict(report['assets']['materials'])
editor=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
assert editor.load_level(report['render_assets']['level'])
actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
component=actor.skeletal_mesh_component
sun=next(a for a in actors.get_all_level_actors() if isinstance(a,u.DirectionalLight))
sky=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkyLight))
camera=next(a for a in actors.get_all_level_actors() if isinstance(a,u.CineCameraActor))
camera.set_actor_label('PBR_Portrait')
filmback=camera.get_cine_camera_component().get_editor_property('filmback')
filmback.sensor_width=36.;filmback.sensor_height=20.25
camera.get_cine_camera_component().set_editor_property('filmback',filmback)
origin,extent=actor.get_actor_bounds(False);height=max(20.,extent.z*2)
is_character=manifest.get('fixture_kind','character')=='character'
framing_excluded=set()
head=(component.get_socket_location(next(b['exported'] for b in manifest['bones']
      if b['original'] in ('頭','头','Head','head')))+u.Vector(0,0,-1)) if is_character else origin


def owned(name,kind,factory):
    result=u.load_asset(root+'/'+name)
    if result:manager.verify_asset(result)
    else:result=u.AssetToolsHelpers.get_asset_tools().create_asset(name,root,kind,factory)
    assert result
    manager.managed(result)
    return result


def variant(material,label,parameters):
    target=material.get_path_name().split('.')[0]+'_'+label
    result=u.load_asset(target)
    if result:manager.verify_asset(result)
    else:result=u.EditorAssetLibrary.duplicate_asset(material.get_path_name(),target)
    manager.managed(result)
    for name,value in parameters.items():manager.lib.set_material_instance_scalar_parameter_value(result,name,value)
    manager.lib.update_material_instance(result)
    assert u.EditorAssetLibrary.save_loaded_asset(result,only_if_is_dirty=False)
    manager.protect_asset(result)
    return result


def new_camera(label,target,distance,lateral=0.,focal=50.):
    location=target+u.Vector(distance,lateral,distance*.025)
    result=next((a for a in actors.get_all_level_actors() if a.get_actor_label()==label),None)
    if result is None:result=actors.spawn_actor_from_class(u.CineCameraActor,location)
    result.set_actor_label(label);result.set_actor_location(location,False,False)
    result.set_editor_property('tags',[u.Name('MMDBridgePBRFixture')])
    result.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location,target),False)
    cine=result.get_cine_camera_component();source=camera.get_cine_camera_component()
    for name in ('post_process_settings','filmback','focus_settings'):
        cine.set_editor_property(name,source.get_editor_property(name))
    cine.set_editor_property('current_focal_length',focal)
    return result


def source_bounds(record):
    data=manifest.get('material_bounds',{}).get(record['id'])
    if not data:return origin,extent
    low,high=data['min'],data['max'];center=[(a+b)*.5 for a,b in zip(low,high)]
    size=[b-a for a,b in zip(low,high)]
    # Standard Blender MMD (-Y front, Z up) -> FBX-imported UE (+X front, Z up).
    return u.Vector(-center[1]*100,-center[0]*100,center[2]*100),u.Vector(size[1]*50,size[0]*50,size[2]*50)


def choose(kinds,preferred=''):
    records=[r for r in manifest['materials'] if r['id'] not in framing_excluded and r['character_material']['pbr']['type'] in kinds
             and r['character_material']['pbr']['params'].get('Opacity',1)>0 and r['id'] in manifest.get('material_bounds',{})]
    records.sort(key=lambda r:kinds.index(r['character_material']['pbr']['type']))
    return next((r for r in records if preferred and re.search(preferred,r['name'],re.I)),records[0] if records else None)


def sequence(name,selected_camera,yaw=145.,dark=False,fill=0.):
    result=owned(name,u.LevelSequence,u.LevelSequenceFactoryNew())
    for binding in list(result.get_bindings()):binding.remove()
    for track in list(result.get_tracks()):result.remove_track(track)
    result.set_display_rate(u.FrameRate(30,1));result.set_playback_start(0);result.set_playback_end(241)
    if is_character:
        section=result.add_possessable(actor).add_track(u.MovieSceneSkeletalAnimationTrack).add_section()
        section.set_range(0,241)
        params=section.get_editor_property('params');params.animation=u.load_asset(report['animation_asset']);params.force_custom_mode=True
        section.set_editor_property('params',params)
    binding=result.add_possessable(selected_camera)
    cut=result.add_track(u.MovieSceneCameraCutTrack).add_section();cut.set_range(0,241)
    identity=u.MovieSceneObjectBindingID();identity.set_editor_property('guid',binding.get_id());cut.set_camera_binding_id(identity)
    for light,value in ((sun,0. if dark else 10.),(sky,0. if dark else .4),(softbox,0. if dark else fill)):
        binding=result.add_possessable(light)
        bind_float(result,binding,light.light_component,'Intensity','Intensity',[(0,value),(240,value)])
        if light==sun:
            section=binding.add_track(u.MovieScene3DTransformTrack).add_section();section.set_range(0,241)
            for channel,value in zip(section.get_all_channels(),(0.,0.,300.,0.,-25.,yaw,1.,1.,1.)):channel.set_default(value)
    if backlight is not None:
        value=250. if not dark and selected_camera.get_actor_label()=='PBR_Accessories' else 0.
        bind_float(result,result.add_possessable(backlight),backlight.light_component,'Intensity','Intensity',[(0,value),(240,value)])
    assert u.EditorAssetLibrary.save_loaded_asset(result,only_if_is_dirty=False)
    manager.protect_asset(result)
    return result.get_path_name()


try:
    if is_character:
        feet=[component.get_socket_location(b['exported']).z for b in manifest['bones']
              if b['original'] in ('左足首','右足首','左つま先','右つま先','LeftFoot','RightFoot','左脚踝','右脚踝')]
        if feet:
            foot=min(feet);body_span=max(20.,head.z-foot)
            if height>body_span*3:
                nearby=[]
                for record in manifest['materials']:
                    if record['id'] not in manifest.get('material_bounds',{}):continue
                    center,size=source_bounds(record)
                    if center.z+size.z<foot-body_span*.35 or center.z-size.z>head.z+body_span*.7:
                        framing_excluded.add(record['id'])
                    elif record['character_material']['pbr']['params'].get('Opacity',1)>0:
                        nearby.append((center,size))
                assert nearby,'No character geometry remains in the head/foot framing region'
                low=u.Vector(*(min(getattr(c,axis)-getattr(s,axis) for c,s in nearby) for axis in ('x','y','z')))
                high=u.Vector(*(max(getattr(c,axis)+getattr(s,axis) for c,s in nearby) for axis in ('x','y','z')))
                origin=(low+high)*.5;extent=(high-low)*.5;height=max(20.,extent.z*2)
                location=head+u.Vector(height*.525,0,0)
                camera.set_actor_location(location,False,False)
                camera.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location,head),False)
                report['character_framing']={'method':'head_foot_gated_source_geometry',
                    'height_cm':height,'excluded_pmx_slots':[r['pmx_index'] for r in manifest['materials'] if r['id'] in framing_excluded],
                    'geometry_preserved':True,'excluded_only_from_camera_framing':True}
    report['wet_on_overrides']={}
    for record in manifest['materials']:
        pbr=record['character_material']['pbr']
        if pbr['type'] not in ('face','skin') or pbr['status']=='white':continue
        material=manager.asset(record['id']);wet=variant(material,'WetOn',{'WetEnabled':1.})
        report['wet_on_overrides'][material.get_path_name()]=wet.get_path_name()
    atomic_json(folder/'wet_on.json',report['wet_on_overrides'])
    if not is_character:
        visible=[source_bounds(r) for r in manifest['materials'] if r['character_material']['pbr']['params'].get('Opacity',1)>0
                 and r['id'] in manifest.get('material_bounds',{})]
        if visible:
            low=u.Vector(*(min(getattr(c,axis)-getattr(s,axis) for c,s in visible) for axis in ('x','y','z')))
            high=u.Vector(*(max(getattr(c,axis)+getattr(s,axis) for c,s in visible) for axis in ('x','y','z')))
            origin=(low+high)*.5;extent=(high-low)*.5;height=max(20.,extent.z*2);head=origin
        report['visible_fixture_bounds']={'origin':[origin.x,origin.y,origin.z],'extent':[extent.x,extent.y,extent.z]}
    softbox=next((a for a in actors.get_all_level_actors() if a.get_actor_label()=='PBR_Softbox'),None)
    light_size=height if is_character else max(height,extent.y*2)
    location=head+u.Vector(light_size*.6,-light_size*.4,light_size*.15)
    if softbox is None:softbox=actors.spawn_actor_from_class(u.RectLight,location)
    softbox.set_actor_label('PBR_Softbox');softbox.set_actor_location(location,False,False)
    softbox.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location,head),False)
    softbox.set_editor_property('tags',[u.Name('MMDBridgePBRFixture')])
    softbox.light_component.set_mobility(u.ComponentMobility.MOVABLE)
    for name,value in {'intensity_units':u.LightUnits.LUMENS,'intensity':0.,'source_width':light_size*.4,'source_height':light_size*.6}.items():
        softbox.light_component.set_editor_property(name,value)
    full=new_camera('PBR_FullBody',origin,max(height*2.9,extent.y*3.4))
    backlight=next((a for a in actors.get_all_level_actors() if a.get_actor_label()=='PBR_BackAccessorySoftbox'),None)
    if is_character:
        cloth_record=choose(('cloth','satin'),r'^(shirt|上衣|シャツ|top|衣服)$')
        cloth_target=(source_bounds(cloth_record)[0] if cloth_record else head-u.Vector(0,0,height*.24))
        if cloth_record and source_bounds(cloth_record)[1].z*2>height*.35:
            cloth_target.z=max(cloth_target.z,head.z-height*.22)
        cloth=new_camera('PBR_Clothing',cloth_target,height*.67,-height*.06,60.)
        accessory=choose(('glass','metal','plastic','rubber'))
        accessory_target,accessory_size=source_bounds(accessory) if accessory else (head,extent*.1)
        accessory_distance=max(height*.23,min(height*.8,max(accessory_size.z*6,accessory_size.y*4)))
        if accessory_target.x<origin.x-extent.x*.2:accessory_distance=-accessory_distance
        detail=new_camera('PBR_Accessories',accessory_target,accessory_distance,-height*.08,60.)
        if accessory_distance<0:
            position=accessory_target+u.Vector(-height*.45,-height*.18,height*.18)
            if backlight is None:backlight=actors.spawn_actor_from_class(u.RectLight,position)
            backlight.set_actor_label('PBR_BackAccessorySoftbox');backlight.set_actor_location(position,False,False)
            backlight.set_actor_rotation(u.MathLibrary.find_look_at_rotation(position,accessory_target),False)
            backlight.light_component.set_mobility(u.ComponentMobility.MOVABLE)
            for name,value in {'intensity_units':u.LightUnits.LUMENS,'intensity':0.,'source_width':height*.25,'source_height':height*.35}.items():
                backlight.light_component.set_editor_property(name,value)
        side=new_camera('PBR_HairSide',head,height*.5,-height*.3)
        report['review_views']={'full_body':sequence('S_Cross_FullBody',full,fill=50.),
            'portrait':sequence('S_Cross_Portrait',camera),'hair_side':sequence('S_Cross_HairSide',side,40.,fill=250.),
            'clothing':sequence('S_Cross_Clothing',cloth,fill=500.),'accessories':sequence('S_Cross_Accessories',detail,fill=250.),
            'dark':sequence('S_Cross_Dark',camera,dark=True)}
        report['camera_materials']={'clothing':cloth_record['id'] if cloth_record else None,
                                    'accessories':accessory['id'] if accessory else None}
        rubber=choose(('rubber',))
        if rubber:
            target,size=source_bounds(rubber)
            feet=new_camera('PBR_RubberDetail',target,max(size.z*6,size.y*4,height*.25),-height*.1,60.)
            report['review_views']['rubber']=sequence('S_Cross_Rubber',feet,fill=250.)
            report['camera_materials']['rubber']=rubber['id']
    else:
        leather=choose(('leather',));assert leather
        target,size=source_bounds(leather)
        detail=new_camera('PBR_LeatherDetail',target,max(size.z*5,size.y*4,15.),-max(size.y,3.),65.)
        side=new_camera('PBR_PropSide',origin,max(height*2.7,extent.y*3),-max(height,extent.y*2),50.)
        report['review_views']={'full_body':sequence('S_Cross_WholeProp',full,fill=50.),
            'leather':sequence('S_Cross_Leather',detail,fill=250.),'side':sequence('S_Cross_PropSide',side,45.,fill=250.)}
        report['camera_materials']={'leather':leather['id']}
    preset=owned('MRQ_Cross_Portrait2K',u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
    preset.find_or_add_setting_by_class(u.MoviePipelineDeferredPassBase)
    preset.find_or_add_setting_by_class(u.MoviePipelineImageSequenceOutput_PNG)
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting);output.output_resolution=u.IntPoint(2560,1440)
    output.file_name_format='{frame_number}'
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.spatial_sample_count=16;aa.temporal_sample_count=1;aa.engine_warm_up_count=64;aa.render_warm_up_count=32;aa.render_warm_up_frames=True
    console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    for name,value in {'r.MotionBlurQuality':0.,'r.AnisotropicMaterials':1.,'r.SSS.Quality':1.,'r.SSS.HalfRes':0.,
                       'r.TextureStreaming':1.,'r.TemporalAASamples':16.,'r.SSS.Burley.NumSamplesOverride':128.,
                       'r.SSS.Checkerboard':0.,'r.SSS.SampleSet':2.,'r.RayTracing.Shadows.SamplesPerPixel':4.}.items():
        console.add_or_update_console_variable(name,value)
    assert u.EditorAssetLibrary.save_loaded_asset(preset,only_if_is_dirty=False);manager.protect_asset(preset)
    report['portrait_preset']=preset.get_path_name();report['cross_views_ready']=True
    report['lighting']={'sun_lux':10.,'sky_intensity':.4,'portrait_softbox_lumens':0.,'clothing_softbox_lumens':500.,
                        'hair_side_softbox_lumens':250.,'dark_all_lights':0.}
    report['camera_filmback_mm']=[36.,20.25]
    assert editor.save_current_level()
    if is_character:
        # A separately saved zero-light world prevents the lit level's initial
        # skylight capture/Lumen history from contaminating a frame-0 off test.
        base_level=report['render_assets']['level'];dark_level=root+'/L_Cross_Dark'
        dark_file=Path(u.Paths.project_content_dir())/(dark_level[6:]+'.umap')
        if dark_file.is_file():
            assert report.get('dark_level_sha256')==file_hash(dark_file),'Dark review level has later edits'
            assert editor.load_level(dark_level)
        else:assert editor.new_level_from_template(dark_level,base_level)
        all_actors=actors.get_all_level_actors()
        actor=next(a for a in all_actors if isinstance(a,u.SkeletalMeshActor))
        camera=next(a for a in all_actors if a.get_actor_label()=='PBR_Portrait')
        sun=next(a for a in all_actors if isinstance(a,u.DirectionalLight))
        sky=next(a for a in all_actors if isinstance(a,u.SkyLight))
        softbox=next(a for a in all_actors if a.get_actor_label()=='PBR_Softbox')
        backlight=next((a for a in all_actors if a.get_actor_label()=='PBR_BackAccessorySoftbox'),None)
        dark_lights=[sun,sky,softbox]+([backlight] if backlight is not None else [])
        for light in dark_lights:light.light_component.set_intensity(0.)
        report['review_views']['dark']=sequence('S_Cross_Dark',camera,dark=True)
        assert editor.save_current_level()
        report['review_levels']={'dark':dark_level}
        report['dark_level_sha256']=file_hash(dark_file)
        report['dark_level_lights']={a.get_actor_label():a.light_component.get_editor_property('intensity')
                                     for a in dark_lights}
        assert all(value==0 for value in report['dark_level_lights'].values())
        assert editor.load_level(base_level)
    print('CROSS_MATERIAL_VIEWS_READY',key,len(report['review_views']))
except Exception:
    import traceback
    report['cross_views_error']=traceback.format_exc();raise
finally:atomic_json(folder/'ue_receipt.json',report)
