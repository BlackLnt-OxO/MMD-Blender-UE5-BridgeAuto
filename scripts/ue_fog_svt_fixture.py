"""Add hidden native SVT references and the general fog kernel to test maps."""
import os
from pathlib import Path
import unreal as u
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_appearance import volume_materials,native_property
from mmd_bridge.ue_fog_froxel import prepare_material,prepare_actors
from mmd_bridge.ue_usd import _asset_cache
out=Path(os.environ['MMDBRIDGE_FOG_TEST_OUT']);data=read_json(out/'fixtures.json');source=read_json(out/'svt_source.json')
assert data['root'].startswith('/Game/FogOcclusion_')
manager=Materials(source['asset_root'],data['owner'],{'checks':{},'assets':{}})
volume_materials({'volumes':[source['volume']]},manager,{'fog_density_scale':1.})
parent=u.load_asset(source['asset_root']+'/Materials/M_Volume_svt_box')
u.MaterialEditingLibrary.set_material_usage(parent,u.MaterialUsage.MATUSAGE_HETEROGENEOUS_VOLUMES)
u.MaterialEditingLibrary.recompile_material(parent);u.EditorAssetLibrary.save_loaded_asset(parent)
manager.protect_asset(parent)
kernel=prepare_material(manager)
level=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
for fixture in data['fixtures']:
    if fixture['volume_kind']!='grid':continue
    assert level.load_level(fixture['level'])
    for old in actors.get_all_level_actors():
        if any(str(t)=='FogTest.SvtSource' or str(t).startswith('MMDBridge.FogProxy=') or
               str(t)=='MMDBridge.ExperimentalFogHost' for t in old.tags):actors.destroy_actor(old)
    actor=actors.spawn_actor_from_class(u.UsdStageActor,u.Vector())
    cache=_asset_cache(source['asset_root'],data['owner'])
    assert cache is not None
    actor.set_usd_asset_cache(cache)
    actor.set_root_layer(source['root_layer'])
    comp=actor.get_generated_component(source['volume']['prim_path']);assert isinstance(comp,u.HeterogeneousVolumeComponent)
    assert comp.get_material(0)
    print('SVT_MATERIAL',comp.get_material(0).get_path_name(),comp.get_material(0).get_class().get_name())
    print('SVT_ASSETS',[(a.get_path_name(),a.get_class().get_name()) for a in actor.get_generated_assets(source['volume']['prim_path'])])
    material=comp.get_material(0)
    if isinstance(material,u.MaterialInstance):
        print('SVT_PARAMETERS',material.get_editor_property('sparse_volume_texture_parameter_values'))
    origin,extent,radius=u.SystemLibrary.get_component_bounds(comp)
    print('SVT_TRANSFORM',comp.get_world_transform(),comp.get_editor_property('frame_transform'),native_property(comp,'volume_resolution','VolumeResolution'))
    # UE stores the VDB index-space frame separately. Production imports use
    # source voxel spacing to configure it; the fixture sets the same spacing.
    if max(abs(getattr(extent,a)-64.) for a in 'xyz')<.1:
        comp.set_world_scale3d(u.Vector(6.,6.,6.))
    origin,extent,radius=u.SystemLibrary.get_component_bounds(comp)
    assert max(abs(getattr(extent,a)-384.) for a in 'xyz')<.1,(origin,extent)
    actor.set_actor_location(u.Vector(50-origin.x,-origin.y,100-origin.z),False,False)
    comp.set_visibility(False);actor.tags=[u.Name('FogTest.SvtSource')]
    for asset in actor.get_generated_assets(source['volume']['prim_path']):
        assert u.EditorAssetLibrary.save_loaded_asset(asset)
    assert u.EditorAssetLibrary.save_loaded_asset(cache)
    prepare_actors(actors,[actor],[source['volume']],kernel)
    assert level.save_current_level()
    fixture['svt']=dict(source,actor=actor.get_name(),kernel=kernel)
data['version']=5;atomic_json(out/'fixtures.json',data)
print('SVT_NATIVE_FIXTURES_READY')
