"""Add four complete railway MRQ presets in Sequences; preserve legacy assets."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_appearance import configure_mrq_defaults

out=ROOT/'work/material_functions_implementation/railway_presets';out.mkdir(parents=True,exist_ok=True)
result={'complete':False,'render_started':False,'existing_maps_materials_modified':False,'presets':[]}

def snapshot(config):
    settings=config.get_all_settings()
    return {'path':config.get_path_name(),'settings':[
        {'class':s.get_class().get_name(),'enabled':s.is_enabled()} for s in settings],
        'renderers':[s.get_class().get_name() for s in settings
                     if isinstance(s,u.MoviePipelineRenderPass) and s.is_enabled()],
        'writers':[s.get_class().get_name() for s in settings
                   if isinstance(s,u.MoviePipelineOutputBase) and s.is_enabled()]}

try:
    job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
    cfg=job.data['settings'];report=read_json(job.folder/'ue_report.json');root=report['root']
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    assert root=='/Game/MMDBridgeValidation/FullSceneV3/A_Scene_railway_01_9ca2172b'
    source=u.load_asset(report['assets']['render_preset']);assert isinstance(source,u.MoviePipelinePrimaryConfig)
    cypress=None
    for path in ('/Game/MMDBridgeValidation/FunctionsV2/TreeRender/Sequences/MRQ_Cypress',
                 '/Game/MMDBridgeValidation/FunctionsV2/TreeRender/Materials/MRQ_Cypress'):
        cypress=u.load_asset(path)
        if cypress:break
    assert isinstance(cypress,u.MoviePipelinePrimaryConfig),'Working Cypress preset missing'
    originals={'source_look':snapshot(source),'cypress':snapshot(cypress)}
    if not (out/'original_configs.json').exists():atomic_json(out/'original_configs.json',originals)
    # Snapshot in transient configs before touching any target. Resume must not
    # copy a config onto itself or overwrite any old asset.
    source_copy=u.new_object(u.MoviePipelinePrimaryConfig);source_copy.copy_from(source)
    cypress_copy=u.new_object(u.MoviePipelinePrimaryConfig);cypress_copy.copy_from(cypress)
    builder=Materials(root,job.data['asset_id'],report,cfg)
    choices=[('MRQ_SourceLook',cfg.get('scene_render_pipeline','lumen'),source_copy),
             ('MRQ_Lumen','lumen',source_copy),('MRQ_PathTracing','path_tracing',source_copy),
             ('MRQ_Cypress','lumen',cypress_copy)]
    for name,pipeline,template in choices:
        preset=builder.create(name,u.MoviePipelinePrimaryConfig,u.MoviePipelinePrimaryConfigFactory())
        assert preset.get_path_name().startswith(root+'/Sequences/')
        preset.copy_from(template)
        game=configure_mrq_defaults(preset,pipeline,cfg.get('ue_render_memory_profile','balanced'),
                                    cfg.get('foliage_distance_scale',2.))
        output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
        output.output_resolution=u.IntPoint(int(cfg['width']),int(cfg['height']))
        output.output_directory=u.DirectoryPath(str(Path(u.Paths.project_saved_dir())/'MovieRenders'/'railway'/name))
        output.file_name_format='{sequence_name}.{frame_number}'
        output.use_custom_playback_range=False;output.output_frame_step=1;output.override_existing_output=False
        aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
        aa.engine_warm_up_count=60;aa.render_warm_up_count=8
        if pipeline=='path_tracing':
            aa.spatial_sample_count=32;aa.temporal_sample_count=1
            aa.override_anti_aliasing=True;aa.anti_aliasing_method=u.AntiAliasingMethod.AAM_NONE
        assert u.EditorAssetLibrary.save_loaded_asset(preset,False)
        builder.protect_asset(preset)
        saved=snapshot(preset)
        assert len(saved['renderers'])==1 and 'MoviePipelineImageSequenceOutput_PNG' in saved['writers'],saved
        selected=game.get_editor_property('soft_game_mode_override')
        saved.update(pipeline=pipeline,game_mode=u.get_default_object(selected).get_class().get_path_name(),
          resolution=[output.output_resolution.x,output.output_resolution.y],output=output.output_directory.path,
          file_sha256=file_hash(builder.asset_file(preset)))
        assert saved['game_mode']=='/Script/MovieRenderPipelineCore.MoviePipelineGameMode'
        result['presets'].append(saved)
        atomic_json(out/'result.json',result)
    report['assets']['render_preset']=result['presets'][0]['path']
    report['assets'].setdefault('render_presets',{}).update(
        lumen=result['presets'][1]['path'],path_tracing=result['presets'][2]['path'],cypress=result['presets'][3]['path'])
    report.setdefault('checks',{})['saved_mrq_presets']={'status':'configured',
      'location':root+'/Sequences','renderer_and_png_present':True,'visual_validation':'not_run',
      'presets':[r['path'] for r in result['presets']]}
    atomic_json(job.folder/'ue_report.json',report)
    result.update(complete=True,source_look_original_had_render_pass=bool(originals['source_look']['renderers']),
                  project=u.Paths.get_project_file_path(),job=str(job.folder),root=root+'/Sequences')
except Exception:
    result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(out/'result.json',result)
print('RAILWAY_PRESETS_PASS',result['complete'],result.get('error'),flush=True)
