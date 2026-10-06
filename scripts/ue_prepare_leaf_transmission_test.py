"""Copy only affected shader assets for A/B; preserve the current scene and originals."""
import sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import Job,read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_appearance import configure_mrq_defaults

OUT=ROOT/'work/material_functions_implementation/leaf_transmission';OUT.mkdir(parents=True,exist_ok=True)
result={'complete':False,'original_assets_modified':False,'materials':{},'functions':{},'parents':{}}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02'
    job=Job(read_json(ROOT/'work/material_functions_implementation/full_scene/job_path.json')['job'])
    report=read_json(job.folder/'ue_report.json');scene=read_json(job.folder/'manifest.json')['scene']
    root=report['root']+'/LeafTransmissionV8';builder=Materials(root,job.data['asset_id'],{'warnings':[]},
        dict(job.data['settings'],thin_surface_scattering='two_sided_wrap'))
    sources={};function_cache={};parent_cache={}
    def snapshot(asset):
        sources[asset.get_path_name()]=file_hash(builder.asset_file(asset))
    def duplicate(asset,path):
        if u.EditorAssetLibrary.does_asset_exist(path):
            raise RuntimeError('测试路径已存在，保留已有资产: '+path)
        snapshot(asset)
        copy=u.EditorAssetLibrary.duplicate_asset(asset.get_path_name(),path)
        assert copy,path
        builder.managed(copy)
        return copy
    def save(asset):
        assert u.EditorAssetLibrary.save_loaded_asset(asset,False)
        builder.protect_asset(asset)
    def function(asset):
        key=asset.get_path_name()
        if key in function_cache:return function_cache[key]
        expressions=u.MaterialEditingLibrary.get_material_function_expressions(asset)
        calls={e.get_path_name():function(e.get_editor_property('material_function')) for e in expressions
               if isinstance(e,u.MaterialExpressionMaterialFunctionCall)}
        def transmitting(e):
            if not isinstance(e,u.MaterialExpressionSubstrateSlabBSDF):return False
            names=list(u.MaterialEditingLibrary.get_material_expression_input_names(e))
            inputs=u.MaterialEditingLibrary.get_inputs_for_material_function_expression(asset,e)
            return inputs[names.index('SSS MFP')] is not None
        affected=any(transmitting(e) for e in expressions) or any(
            calls[e.get_path_name()]!=e.get_editor_property('material_function') for e in expressions
            if isinstance(e,u.MaterialExpressionMaterialFunctionCall))
        if not affected:
            function_cache[key]=asset;return asset
        copy=duplicate(asset,root+'/Materials/Functions/'+asset.get_name())
        for e in u.MaterialEditingLibrary.get_material_function_expressions(copy):
            if isinstance(e,u.MaterialExpressionMaterialFunctionCall):
                e.set_editor_property('material_function',function(e.get_editor_property('material_function')))
            elif isinstance(e,u.MaterialExpressionSubstrateSlabBSDF):
                names=list(u.MaterialEditingLibrary.get_material_expression_input_names(e))
                inputs=u.MaterialEditingLibrary.get_inputs_for_material_function_expression(copy,e)
                color=inputs[names.index('SSS MFP')]
                if color:
                    builder.material=copy
                    output=u.MaterialEditingLibrary.get_input_node_output_name_for_material_expression(e,color)
                    builder.thin_transmission((e,''),(color,output or ''))
                    assert e.get_editor_property('sub_surface_type')==u.MaterialSubSurfaceType.MSS_TWO_SIDED_WRAP
        u.MaterialEditingLibrary.update_material_function(copy);save(copy)
        function_cache[key]=copy;result['functions'][key]=copy.get_path_name();return copy
    def parent(asset):
        key=asset.get_path_name()
        if key in parent_cache:return parent_cache[key]
        copy=duplicate(asset,root+'/Materials/'+asset.get_name())
        for e in u.MaterialEditingLibrary.get_material_expressions(copy):
            if isinstance(e,u.MaterialExpressionMaterialFunctionCall):
                e.set_editor_property('material_function',function(e.get_editor_property('material_function')))
        u.MaterialEditingLibrary.recompile_material(copy);save(copy)
        parent_cache[key]=copy;result['parents'][key]=copy.get_path_name();return copy
    for rec in scene['materials']:
        if not rec.get('thin_surface_transmission') or not rec.get('surface_graph'):continue
        source=u.load_asset(report['assets']['materials'][rec['id']]);assert isinstance(source,u.MaterialInstanceConstant)
        target=parent(source.get_editor_property('parent'))
        copy=duplicate(source,root+'/Materials/'+source.get_name())
        u.MaterialEditingLibrary.set_material_instance_parent(copy,target);save(copy)
        result['materials'][source.get_path_name()]=copy.get_path_name()
        atomic_json(OUT/'preparation.json',result)
    assert result['materials'] and result['functions']
    source=u.load_asset(report['assets'].get('render_presets',{}).get('lumen',report['assets']['render_preset']))
    preset=duplicate(source,root+'/Sequences/MRQ_LeafTransmission_AB')
    configure_mrq_defaults(preset,'lumen',job.data['settings'].get('ue_render_memory_profile','balanced'),2.)
    output=preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_resolution=u.IntPoint(2560,1080)
    output.use_custom_playback_range=True;output.custom_start_frame=20;output.custom_end_frame=100
    aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
    aa.engine_warm_up_count=60;aa.render_warm_up_count=8
    save(preset)
    for path,digest in sources.items():assert file_hash(builder.asset_file(u.load_asset(path)))==digest,path
    result.update(complete=True,preset=preset.get_path_name(),source_hashes=sources,root=root,
                  source_code_hash=file_hash(ROOT/'mmd_bridge/ue_materials.py'))
except Exception:
    result['error']=traceback.format_exc();u.log_error(result['error'])
atomic_json(OUT/'preparation.json',result)
print('LEAF_TRANSMISSION_PREPARED',result['complete'],result.get('error'),flush=True)
