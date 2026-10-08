"""UE Python commandlet: prepare owned presets without loading/rendering a scene.

Set MMDBRIDGE_MRQ_JOB to a converted job directory before running this script.
"""
import os
from pathlib import Path
import unreal as u
from mmd_bridge.core import read_json, atomic_json
from mmd_bridge.ue_appearance import configure_mrq_defaults
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_render_policy import memory_cvars, lighting_cvars, MEMORY_PROFILES

folder = Path(os.environ['MMDBRIDGE_MRQ_JOB']).resolve()
job = read_json(folder / 'job.json')
cfg = job['settings']
assert Path(u.Paths.get_project_file_path()).resolve() == Path(cfg['project']).resolve()
assert not u.get_editor_subsystem(u.MoviePipelineQueueSubsystem).is_rendering()
report = read_json(folder / 'ue_report.json')
source_path = report['assets'].get('render_presets', {}).get('lumen', report['assets']['render_preset'])
source = u.load_asset(source_path)
owner = u.EditorAssetLibrary.get_metadata_tag(source, 'MMDBridge.Owner')
assert owner, 'Source preset is not tool-owned'
root = report.get('root') or report['assets']['level'].split('/Maps/')[0]
materials = Materials(root, owner, report, cfg)
records = []
profiles = os.environ.get('MMDBRIDGE_MRQ_PROFILES', 'quality,balanced,low,compatible').split(',')
assert all(profile in MEMORY_PROFILES for profile in profiles)
suffix = os.environ.get('MMDBRIDGE_MRQ_PRESET_SUFFIX', '')
assert all(c.isalnum() or c == '_' for c in suffix)
for profile in profiles:
    preset = materials.create('MRQ_Lumen_' + profile.title() + suffix,
        u.MoviePipelinePrimaryConfig, u.MoviePipelinePrimaryConfigFactory())
    preset.copy_from(source)
    volumes=(read_json(folder/'manifest.json').get('scene',{}).get('volumes',[])
             if cfg.get('fog_occlusion_quality','legacy')=='detail' else [])
    overrides = configure_mrq_defaults(preset, 'lumen', profile, cfg.get('foliage_distance_scale', 2.), cfg.get('fog_occlusion_quality','legacy'),volumes)
    console = preset.find_or_add_setting_by_class(u.MoviePipelineConsoleVariableSetting)
    values = {entry.name: entry.value for entry in console.get_console_variables() if entry.is_enabled}
    assert all(values.get(name) == value for name, value in memory_cvars(profile).items())
    assert overrides.cinematic_quality_settings and not overrides.use_lod_zero
    assert all(values.get(name) == value for name, value in lighting_cvars(profile).items())
    output = preset.find_or_add_setting_by_class(u.MoviePipelineOutputSetting)
    output.output_directory = u.DirectoryPath(str(folder / 'renders' / profile))
    assert u.EditorAssetLibrary.save_loaded_asset(preset)
    records.append({'profile': profile, 'preset': preset.get_path_name(),
        'console_variables': values, 'cinematic_quality': overrides.cinematic_quality_settings,
        'flush_grass_streaming': overrides.flush_grass_streaming,
        'output': output.output_directory.path})
    report['assets'].setdefault('memory_presets', {})[profile] = preset.get_path_name()
updated_defaults = []
for path in (dict.fromkeys((source_path, report['assets']['render_preset']))
             if os.environ.get('MMDBRIDGE_MRQ_UPDATE_DEFAULTS', '1') == '1' else ()):
    preset = u.load_asset(path)
    assert u.EditorAssetLibrary.get_metadata_tag(preset, 'MMDBridge.Owner') == owner
    configure_mrq_defaults(preset, 'lumen', 'balanced', cfg.get('foliage_distance_scale', 2.), cfg.get('fog_occlusion_quality','legacy'))
    assert u.EditorAssetLibrary.save_loaded_asset(preset)
    updated_defaults.append(path)
atomic_json(folder / 'ue_report.json', report)
atomic_json(folder / 'memory_presets.json', {'complete': True, 'render_started': False,
    'engine': u.SystemLibrary.get_engine_version(), 'presets': records,
    'existing_presets_updated_to_balanced': updated_defaults})
print('MEMORY_PRESETS_SAVED_WITHOUT_RENDER')
