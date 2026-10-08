import ast
import copy
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from mmd_bridge.core import atomic_json
from mmd_bridge.scene_appearance import cached_scene_package, fog_shadow_review, volume_sampling_review
from mmd_bridge.ue_render_policy import mrq_beauty_passes, ensure_mrq_image_output


def test_package_cache_reuses_reads_but_invalidates_edits_and_new_paths(tmp_path):
    path = tmp_path/'package.json'
    atomic_json(path,{'asset_id':'old','volumes':[]})
    first = cached_scene_package(path)
    assert cached_scene_package(path) is first
    atomic_json(path,{'asset_id':'new','volumes':[{'name':'fog'}]})
    assert cached_scene_package(path)['asset_id']=='new'
    other = tmp_path/'other.json'
    atomic_json(other,{'asset_id':'other'})
    assert cached_scene_package(other)['asset_id']=='other'
    path.unlink()
    with pytest.raises(FileNotFoundError):cached_scene_package(path)


def test_sampling_review_converts_units_and_preserves_source_for_all_scenes():
    volume = dict(kind='mesh_grid',voxel_size=20,sample_statistics={'phase_range':[.4,.7]},phase_approximation=True)
    before = copy.deepcopy(volume)
    review = volume_sampling_review(volume,.01,'lumen',2)
    assert review['voxel_size_m']==.2 and review['lighting_cell_estimate_m']==.4
    assert review['reasons']==['multiple_phase_functions_approximated','realtime_grid_phase_requires_review']
    assert volume==before and not review['automatically_changed']
    assert volume_sampling_review(volume,.01,'path_tracing')['reasons']==['multiple_phase_functions_approximated']
    assert volume_sampling_review(dict(kind='height_fog',anisotropy=.7),1,'lumen')['reasons']==[]
    assert volume_sampling_review(dict(kind='mesh_grid'),1,'lumen')['sample_statistics'] is None


def test_shadow_review_distinguishes_raster_rt_disabled_and_path_tracer():
    volume = dict(kind='mesh_grid',status='converted')
    light = dict(component='lamp',actual=dict(cast_shadows=True,cast_volumetric_shadow=True,
        cast_raytraced_shadow='CastRayTracedShadow.USE_PROJECT_SETTING'))
    cvars = {'r.RayTracing.Shadows':1,'r.HeterogeneousVolumes.HardwareRayTracing':0}
    assert fog_shadow_review('lumen',[volume],[light],cvars)['possible_surface_volume_shadow_path_mismatch']==['lamp']
    for changes in ({'r.RayTracing.Shadows':0},{'r.HeterogeneousVolumes.HardwareRayTracing':1}):
        assert not fog_shadow_review('lumen',[volume],[light],dict(cvars,**changes))['possible_surface_volume_shadow_path_mismatch']
    for mode in ('CastRayTracedShadow.DISABLED','None'):
        state=dict(light['actual'],cast_raytraced_shadow=mode)
        assert not fog_shadow_review('lumen',[volume],[dict(light,actual=state)],cvars)['possible_surface_volume_shadow_path_mismatch']
    assert not fog_shadow_review('path_tracing',[volume],[light],cvars)['possible_surface_volume_shadow_path_mismatch']
    assert not fog_shadow_review('lumen',[dict(volume,kind='height_fog')],[light],cvars)['possible_surface_volume_shadow_path_mismatch']
    inactive=dict(volume,sample_statistics={'occupied_voxels':0,'phase_range':None})
    assert not fog_shadow_review('lumen',[inactive],[light],cvars)['possible_surface_volume_shadow_path_mismatch']
    assert not fog_shadow_review('lumen',[volume],[light],cvars)['occlusion_verified']


def test_native_pass_contract_switches_both_ways_and_rejects_unknown_renderers():
    class Pass:
        enabled=True
        def is_enabled(self):return self.enabled
        def set_is_enabled(self,value):self.enabled=value
        def get_class(self):return N(get_name=lambda:type(self).__name__)
    class Deferred(Pass):pass
    class PT(Deferred):pass
    class PNG(Pass):pass
    class Custom(Pass):pass
    native=N(MoviePipelineRenderPass=Pass,MoviePipelineDeferredPassBase=Deferred,
        MoviePipelineDeferredPass_PathTracer=PT,MoviePipelineImageSequenceOutput_PNG=PNG)
    # Writer is not actually a render pass.
    native.MoviePipelineRenderPass=(Deferred,Custom)
    settings=[Deferred(),PNG()]
    def add(cls):
        found=next((s for s in settings if type(s)==cls),None)
        if found is None:found=cls();settings.append(found)
        return found
    config=N(get_all_settings=lambda:settings,find_or_add_setting_by_class=add,remove_setting=settings.remove)
    assert mrq_beauty_passes(config,native)==['FinalImage']
    ensure_mrq_image_output(config,'path_tracing',native)
    assert mrq_beauty_passes(config,native)==['PathTracer']
    ensure_mrq_image_output(config,'lumen',native)
    assert mrq_beauty_passes(config,native)==['FinalImage']
    next(s for s in settings if type(s)==Deferred).set_is_enabled(False)
    with pytest.raises(ValueError):mrq_beauty_passes(config,native)
    settings.append(Custom())
    with pytest.raises(ValueError,match='Custom'):mrq_beauty_passes(config,native)
    next(s for s in settings if type(s)==Deferred).set_is_enabled(True)
    assert mrq_beauty_passes(config,native)==['FinalImage']


def test_native_light_snapshot_catches_lost_shadow_and_angular_asset():
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_appearance.py').read_text(encoding='utf-8'))
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('native_property','light_snapshot')]
    from mmd_bridge.scene_appearance import physical_light
    ns={'physical_light':physical_light}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'world-native-boundary','exec'),ns)
    source=dict(type='POINT',energy=1,color=[1,1,1],cast_shadow=True,volume_factor=1,
        specular_factor=1,diffuse_factor=1,use_temperature=False,temperature=6500,directional={'op':'ies'})
    values=dict(intensity=683.,use_temperature=False,temperature=6500,cast_shadows=True,
        cast_volumetric_shadow=True,volumetric_scattering_intensity=1.,specular_scale=1.,diffuse_scale=1.,
        cast_raytraced_shadow='USE_PROJECT_SETTING',ies_texture=N(get_path_name=lambda:'/Game/Profile'),light_function_material=None)
    component=N(get_path_name=lambda:'light',get_light_color=lambda:N(r=1,g=1,b=1),get_editor_property=values.__getitem__)
    assert ns['light_snapshot'](component,source,{})['parameter_match']
    values.update(cast_volumetric_shadow=False,ies_texture=None)
    assert ns['light_snapshot'](component,source,{})['mismatches']==['cast_volumetric_shadow','angular_distribution']


def test_game_property_alias_fallback_and_missing_volume_cannot_be_success():
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_appearance.py').read_text(encoding='utf-8'))
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('native_property','runtime_world_snapshot')]
    ns={'u':N(Paths=N(project_content_dir=lambda:'.'),Light=object,
        GameplayStatics=N(get_all_actors_of_class=lambda world,kind:[]),
        SystemLibrary=N(get_console_variable_int_value=lambda name:0)),
        'Path':Path,'fog_shadow_review':fog_shadow_review}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'game-world-boundary','exec'),ns)
    native=N(get_editor_property={'LightingDownsampleFactor':2.}.__getitem__)
    assert ns['native_property'](native,'lighting_downsample_factor','LightingDownsampleFactor')==2.
    report=ns['runtime_world_snapshot'](None,[],'lumen',expected_volume_paths=['/World/Fog'])
    assert not report['resource_match'] and report['errors']==['missing_volume_component:/World/Fog']
