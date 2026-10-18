"""Import the optional finished mesh; reuse identical compiled baseline shaders."""
from pathlib import Path
import copy,json,os,runpy,sys
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json,file_hash
from mmd_bridge.ue_host import import_fbx
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_pbr import signature

base=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']).resolve()
folder=base.parent/'WeldWeighted'
source=read_json(base/'ue_receipt.json');manifest=read_json(folder/'manifest.json')
original_manifest=read_json(base/'manifest.json')
assert source['status']=='ue_assets_created'
assert manifest['source_sha256']==original_manifest['source_sha256']
assert manifest['mesh_finish']['mode']=='weld_weighted'
suffix=os.environ.get('MMDBRIDGE_PBR_COMPARE_SUFFIX','WeldWeighted')
assert suffix.replace('_','').isalnum()
root=source['root'].rsplit('_',1)[0]+'_'+suffix;owner='pbr_1017_'+root.rsplit('/',1)[-1]
previous=read_json(folder/'ue_receipt.json') if (folder/'ue_receipt.json').is_file() else {}
editor=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
assert editor.load_level(source['render_assets']['level'])
matched={}
for actor in actors.get_all_level_actors():
    if not isinstance(actor,(u.CineCameraActor,u.RectLight)):continue
    location=actor.get_actor_location();rotation=actor.get_actor_rotation()
    row={'location':[location.x,location.y,location.z],
         'rotation':[rotation.pitch,rotation.yaw,rotation.roll]}
    if isinstance(actor,u.CineCameraActor):
        camera=actor.get_cine_camera_component()
        filmback=camera.get_editor_property('filmback');focus=camera.get_editor_property('focus_settings')
        row.update(focal=float(camera.current_focal_length),aperture=float(camera.current_aperture),
                   filmback=[float(filmback.sensor_width),float(filmback.sensor_height)],
                   focus_method=focus.focus_method,focus_distance=float(focus.manual_focus_distance))
    else:
        row.update(width=actor.light_component.get_editor_property('source_width'),
                   height=actor.light_component.get_editor_property('source_height'))
    matched[actor.get_actor_label()]=row
baseline_bytes={str(p):file_hash(p) for p in (Path(u.Paths.project_content_dir())/source['root'][6:]).rglob('*') if p.is_file()}
result={'status':'running','source_root':source['root'],'root':root,'shader_comparison':'same_compiled_parents_and_parameters'}
try:
    u.SystemLibrary.execute_console_command(None,'Interchange.FeatureFlags.Import.FBX 0')
    imported=import_fbx(manifest['files']['model']['path'],root+'/Meshes','SK_WeldWeighted','model',owner=owner)
    mesh=next(a for a in imported if isinstance(a,u.SkeletalMesh))
    report=copy.deepcopy(source);report.update(root=root,mesh=mesh.get_path_name())
    report['assets']={'materials':{}};report['checks']={}
    report['render_assets']={'level':root+'/L_PBR_Review'}
    report.pop('dark_level_sha256',None);report.pop('review_levels',None)
    if previous.get('root')==root and previous.get('dark_level_sha256'):
        report['dark_level_sha256']=previous['dark_level_sha256']
    manager=Materials(root,owner,report,manifest['settings'])
    lib=manager.lib;report['matcap_off_overrides']={}
    for record,original_record in zip(manifest['materials'],original_manifest['materials']):
        assert (record['id'],record['pmx_index'])==(original_record['id'],original_record['pmx_index'])
        pbr=record['character_material']['pbr'];old=original_record['character_material']['pbr']
        assert pbr['type']==old['type'] and pbr['params']==old['params'] and pbr['alpha_mode']==old['alpha_mode']
        assert {key:file_hash(value['path']) for key,value in record['textures'].items()}=={
            key:file_hash(value['path']) for key,value in original_record['textures'].items()}
        material=u.load_asset(source['assets']['materials'][record['id']])
        target=root+'/Materials/'+material.get_name()
        instance=u.load_asset(target)
        if instance:manager.verify_asset(instance)
        else:
            instance=u.EditorAssetLibrary.duplicate_asset(material.get_path_name(),target)
            assert instance
            u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.Owner',owner)
        assert instance.get_editor_property('parent')==material.get_editor_property('parent')
        for tag,value in {'PBRRecord':json.dumps(record,ensure_ascii=False),'PBRRoot':root,
                          'PBRBinding':signature(record)}.items():
            u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.'+tag,value)
        assert u.EditorAssetLibrary.save_loaded_asset(instance);manager.protect_asset(instance)
        manager.assets[record['id']]=instance.get_path_name()
    manager.assign(mesh,manifest['materials'])
    report['assets']['materials']=manager.assets
    names=[str(m.get_name()) for m in mesh.get_editor_property('morph_targets')]
    expected={m['exported'] for m in manifest['morph_probe']}
    assert expected <= set(names),('Exported morph target names changed',sorted(expected-set(names)),names[:15])
    report['morph_target_names']=names
    animations=import_fbx(manifest['files']['animation']['path'],root+'/Animations','A_HeadEyes','animation',skeleton=mesh.skeleton,owner=owner)
    animation=next(a for a in animations if isinstance(a,u.AnimSequence))
    report['animation_asset']=animation.get_path_name()
    level=report['render_assets']['level']
    assert (editor.load_level(level) if u.EditorAssetLibrary.does_asset_exist(level)
            else editor.new_level_from_template(level,source['render_assets']['level']))
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actor.set_actor_label('PBR_'+root.rsplit('/',1)[-1]+'_Review')
    component=actor.skeletal_mesh_component;component.set_skeletal_mesh_asset(mesh)
    for index,slot in enumerate(mesh.materials):component.set_material(index,slot.material_interface)
    assert editor.save_current_level()
    report['checks'].update(slots=len(mesh.materials),reload_slots=len(mesh.materials))
    assert len(mesh.materials)==len(manifest['materials'])
    atomic_json(folder/'ue_receipt.json',report)
    os.environ['MMDBRIDGE_PBR_FIXTURE']=str(folder)
    runpy.run_path(str(ROOT/'scripts/prepare_character_pbr_cross_views_ue.py'),run_name='__main__')
    # Fix cameras and area light geometry to the original branch exactly.
    # Sequence lighting and exposure are identical; only mesh normals differ.
    report=read_json(folder/'ue_receipt.json');assert editor.load_level(level)
    report['render_assets'].update(sequence=report['review_views']['portrait'],preset=report['portrait_preset'])
    copied=[]
    for actor in actors.get_all_level_actors():
        label=actor.get_actor_label()
        if label not in matched:continue
        row=matched[label];actor.set_actor_location(u.Vector(*row['location']),False,False)
        actor.set_actor_rotation(u.Rotator(pitch=row['rotation'][0],yaw=row['rotation'][1],roll=row['rotation'][2]),False)
        if isinstance(actor,u.CineCameraActor):
            camera=actor.get_cine_camera_component()
            camera.set_editor_property('current_focal_length',row['focal']);camera.set_editor_property('current_aperture',row['aperture'])
            filmback=camera.get_editor_property('filmback');filmback.sensor_width,filmback.sensor_height=row['filmback']
            camera.set_editor_property('filmback',filmback)
            focus=camera.get_editor_property('focus_settings');focus.focus_method=row['focus_method'];focus.manual_focus_distance=row['focus_distance']
            camera.set_editor_property('focus_settings',focus)
        else:
            actor.light_component.set_editor_property('source_width',row['width']);actor.light_component.set_editor_property('source_height',row['height'])
        copied.append(label)
    assert editor.save_current_level();assert editor.load_level(level)
    camera_contract={}
    for actor in actors.get_all_level_actors():
        label=actor.get_actor_label()
        if label not in matched:continue
        row=matched[label];location=actor.get_actor_location();rotation=actor.get_actor_rotation()
        assert max(abs(a-b) for a,b in zip((location.x,location.y,location.z),row['location']))<1e-4
        assert max(abs(a-b) for a,b in zip((rotation.pitch,rotation.yaw,rotation.roll),row['rotation']))<1e-4
        if isinstance(actor,u.CineCameraActor):
            camera=actor.get_cine_camera_component();filmback=camera.get_editor_property('filmback')
            assert abs(camera.current_focal_length-row['focal'])<1e-5
            assert max(abs(a-b) for a,b in zip((filmback.sensor_width,filmback.sensor_height),row['filmback']))<1e-5
            camera_contract[label]={key:value for key,value in row.items() if key!='focus_method'}
    component=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor)).skeletal_mesh_component
    assert component.get_editor_property('skeletal_mesh_asset')==mesh
    actual=[m.get_path_name() for m in component.get_materials()]
    assert actual==[mesh.materials[i].material_interface.get_path_name() for i in range(len(mesh.materials))]
    assert all(file_hash(p)==digest for p,digest in baseline_bytes.items()),'The baseline branch was modified'
    result.update(status='complete',slot_count=len(actual),morph_targets=len(names),expected_probe_morphs=sorted(expected),
        matched_cameras_and_lights=copied,baseline_files_preserved=len(baseline_bytes),
        material_bindings=actual,saved_reloaded=True,matched_camera_settings=camera_contract,
        mesh=mesh.get_path_name(),animation=animation.get_path_name())
    report['mesh_comparison']=result;atomic_json(folder/'ue_receipt.json',report)
    print('WELD_WEIGHTED_NATIVE_COMPLETE',folder.parent.name,len(actual),len(names),flush=True)
except Exception:
    import traceback
    result.update(status='failed',error=traceback.format_exc());raise
finally:
    os.environ['MMDBRIDGE_PBR_FIXTURE']=str(base)
    atomic_json(folder/'ue_mesh_comparison_receipt.json',result)
