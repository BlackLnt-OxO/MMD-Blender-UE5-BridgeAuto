"""Native UE compile/reload and OCIO processor validation in an isolated asset root."""
import sys, traceback
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork');sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_appearance import repair_color_configuration, validate_color_configuration
OUT=ROOT/'work/material_restoration/policy_v20';OUT.mkdir(parents=True,exist_ok=True)
report={'complete':False,'warnings':[],'failures':[],'materials':[],'checks':{},'project':u.Paths.get_project_file_path()}
try:
    assert Path(report['project']).stem=='TESTON1_1005_02'
    assert u.SystemLibrary.get_console_variable_int_value('r.Substrate')
    report['checks']['substrate_gbuffer']=u.SystemLibrary.get_console_variable_int_value('r.Substrate.ProjectGBufferFormat')
    converter=Materials('/Game/MMDBridgeValidation/ScenePolicyV20','ScenePolicyV20',report,
        {'restore_scene_appearance':True,'use_scene_master_material':False,'material_conversion_timeout':120})
    for record in read_json(OUT/'records.json'):
        try:
            instance=converter.build(record,OUT)
            parent=instance.get_editor_property('parent')
            assert u.EditorAssetLibrary.save_loaded_asset(parent)
            assert converter.reuse(record),'saved material cannot be reused: '+record['name']
            report['materials'].append({'id':record['id'],'name':record['name'],'path':instance.get_path_name(),
                'parent':parent.get_path_name(),'blend_mode':str(parent.blend_mode),
                'features':record.get('closure_features',[]),'source_objects':record.get('source_objects',[])})
            print('UE_MATERIAL_PASS',record['name'],flush=True)
        except Exception:
            report['failures'].append({'material':record['name'],'error':traceback.format_exc()})
            u.log_error(report['failures'][-1]['error'])
        atomic_json(OUT/'ue_result.json',report)
    old=read_json(Path(r'D:\Codex_Projects_RebuildAutoMMD\MMDBridge_Jobs\Scene_It_sMyGO_ParkingLots_01\manifest.json'))
    look=old['scene']['render_look']
    from mmd_bridge.ocio_cache import portable_config
    config_path=portable_config(look['config'],u.Paths.project_dir())
    ocio=u.load_asset(converter.root+'/Materials/OCIO_Blender')
    if not ocio:
        task=u.AssetImportTask()
        task.filename=str(config_path);task.destination_path=converter.root+'/Materials'
        task.destination_name='OCIO_Blender';task.automated=True;task.save=True
        task.factory=u.OpenColorIOConfigurationFactory()
        converter.tools.import_asset_tasks([task])
        ocio=u.load_asset(converter.root+'/Materials/OCIO_Blender')
        assert ocio,'OCIO import failed';converter.managed(ocio)
    display=repair_color_configuration(ocio,look,look['config'])
    assert u.EditorAssetLibrary.save_loaded_asset(ocio);converter.protect_asset(ocio)
    u.OpenColorIOEditorBlueprintLibrary.set_active_viewport_configuration(display)
    report['checks']['ocio']={'status':'validated','probe_color':validate_color_configuration(display),
        'asset':ocio.get_path_name(),'path':ocio.configuration_file.file_path}
    try:
        validate_color_configuration(u.OpenColorIODisplayConfiguration(is_enabled=True))
    except RuntimeError:
        report['checks']['invalid_ocio_rejected']=True
    else:
        raise AssertionError('invalid OCIO falsely passed validation')
    report['checks']['runtime_code_sha256']={name:file_hash(ROOT/'mmd_bridge'/name)
        for name in ('ue_materials.py','ue_appearance.py','material_contract.py')}
    report['complete']=not report['failures']
except Exception:
    report['failures'].append({'stage':'native_validation','error':traceback.format_exc()});u.log_error(report['failures'][-1]['error'])
atomic_json(OUT/'ue_result.json',report)
print('UE_MYGO_RESTORATION_RESULT',report['complete'],len(report['materials']),flush=True)
