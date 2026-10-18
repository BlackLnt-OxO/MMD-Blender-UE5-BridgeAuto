"""Create isolated parameter A/B tests; leave the accepted baseline assets intact."""
import copy
import os
import sys
from pathlib import Path
import unreal as u

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials

baseline=ROOT/'work/character_pbr_20261017/GenZ_Flow'
folder=ROOT/'work/character_pbr_20261018/Diagnostics'
folder.mkdir(parents=True,exist_ok=True)
receipt=read_json(baseline/'ue_receipt.json')
manifest=read_json(baseline/'manifest.json')
report={'warnings':[],'failures':[],'checks':{}}
source_manager=Materials(receipt['root'],'pbr_1017_GenZ_v7',report,manifest['settings'])
root='/Game/MMDBridgePBR/GenZ_Lookdev_1018/DiagnosticsA'
manager=Materials(root,'pbr_1018_diag',report,manifest['settings'])
diagnostics=[]
for label in ('no_detail','no_flow','no_aniso','no_sss','sss_full','no_specular','flat_normal'):
    mapping={}
    for record in manifest['materials']:
        kind=record['character_material']['pbr']['type']
        params=({'DetailStrength':0.} if label=='no_detail' and kind in ('skin','face','hair') else
                {'Use_direction':0.} if label=='no_flow' and kind=='hair' else
                {'Anisotropy':0.} if label=='no_aniso' and kind=='hair' else
                {'SSSStrength':0.} if label=='no_sss' and kind in ('skin','face') else
                {'SSSStrength':1.} if label=='sss_full' and kind in ('skin','face') else
                {'Specular':0.} if label=='no_specular' and kind in ('skin','face','hair') else {})
        if not params and label!='flat_normal':continue
        source=u.load_asset(receipt['assets']['materials'][record['id']])
        source_manager.verify_asset(source)
        path=root+'/'+label+'/'+source.get_name()
        candidate=u.load_asset(path)
        if candidate:
            manager.verify_asset(candidate)
        else:
            candidate=u.EditorAssetLibrary.duplicate_asset(source.get_path_name(),path)
        manager.managed(candidate)
        if label=='flat_normal':
            manager.lib.set_material_instance_texture_parameter_value(candidate,'normal_Tex',
                u.load_asset('/Engine/EngineMaterials/FlatNormal'))
        for name,value in params.items():
            names=[str(x) for x in manager.lib.get_scalar_parameter_names(candidate.get_editor_property('parent'))]
            assert name in names,(record['name'],name,names)
            manager.lib.set_material_instance_scalar_parameter_value(candidate,name,value)
            assert abs(manager.lib.get_material_instance_scalar_parameter_value(candidate,name)-value)<1e-5
        manager.lib.update_material_instance(candidate)
        u.EditorAssetLibrary.save_loaded_asset(candidate);manager.protect_asset(candidate)
        mapping[source.get_path_name()]=candidate.get_path_name()
    atomic_json(folder/(label+'.json'),mapping)
    diagnostics.append({'label':label,'changed':len(mapping)})
preset_path=root+'/MRQ_Compare16'
preset=u.load_asset(preset_path)
if preset:manager.verify_asset(preset)
else:preset=u.EditorAssetLibrary.duplicate_asset(receipt['portrait_preset'],preset_path)
manager.managed(preset)
aa=preset.find_or_add_setting_by_class(u.MoviePipelineAntiAliasingSetting)
aa.spatial_sample_count=16;aa.temporal_sample_count=1
aa.engine_warm_up_count=64;aa.render_warm_up_count=32
console=preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
console.add_or_update_console_variable('r.TemporalAASamples',16.)
console.add_or_update_console_variable('r.SSS.Burley.NumSamplesOverride',64.)
u.EditorAssetLibrary.save_loaded_asset(preset,only_if_is_dirty=False);manager.protect_asset(preset)
out=copy.deepcopy(receipt)
out['portrait_preset']=preset.get_path_name()
atomic_json(folder/'manifest.json',manifest)
atomic_json(folder/'ue_receipt.json',out)
atomic_json(folder/'diagnostics.json',{'variants':diagnostics,'preset':preset.get_path_name()})
for name in ('DefaultNormal','FlatNormal'):
    task=u.AssetExportTask()
    task.object=u.load_asset('/Engine/EngineMaterials/'+name)
    task.filename=str(folder/(name+'.tga'))
    task.automated=True;task.prompt=False;task.replace_identical=True
    task.exporter=u.TextureExporterTGA()
    assert u.Exporter.run_asset_export_task(task),name
print('PBR_DIAGNOSTICS_READY',diagnostics,preset.get_path_name())
