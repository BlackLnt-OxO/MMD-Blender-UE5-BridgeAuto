"""Add a bounded uniform Volume-domain mesh alternative to isolated fixtures.

Original HVC remains the reference. Both alternatives start hidden; no accepted
scene is loaded. The box mask retains the original 768cm container boundary.
"""
import os
from pathlib import Path
import unreal as u
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials,prop
out=Path(os.environ['MMDBRIDGE_FOG_TEST_OUT']);data=read_json(out/'fixtures.json')
assert data['root'].startswith('/Game/FogOcclusion_')
level=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
manager=Materials(data['root'],data['owner'],{'checks':{},'assets':{}})
update=os.environ.get('MMDBRIDGE_FROXEL_UPDATE')=='1'
material=manager.create('M_FroxelBox_Corrected' if update else 'M_FroxelBox',u.Material,u.MaterialFactoryNew())
manager.material=material;manager.clear_expressions(material)
prop(material,'material_domain',u.MaterialDomain.MD_VOLUME);prop(material,'blend_mode',u.BlendMode.BLEND_ADDITIVE)
position=manager.node('WorldPosition')
p=manager.op('Subtract',position,manager.constant([50.,0.,100.]))
p=manager.op('Divide',manager.op('Abs',p),manager.constant([384.]*3))
channels=[]
for name in 'rgb':
    mask=manager.node('ComponentMask',r=name=='r',g=name=='g',b=name=='b',a=False)
    manager.connect(p,mask,'Input');channels.append(mask)
maximum=manager.op('Max',manager.op('Max',channels[0],channels[1]),channels[2])
inside=manager.node('Step');manager.connect(manager.constant(1.),inside,'X');manager.connect(maximum,inside,'Y')
# VolumetricFogVoxelization.usf explicitly scales material extinction by 1/100.
# The original HVC is .00012 per 6cm voxel: both equal .002 per metre.
ext=manager.op('Multiply',inside,manager.constant(.002))
alb=manager.constant([1.]*3)
manager.lib.connect_material_property(ext[0],ext[1],u.MaterialProperty.MP_SUBSURFACE_COLOR)
manager.lib.connect_material_property(alb[0],alb[1],u.MaterialProperty.MP_BASE_COLOR)
if u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
    bsdf=manager.node('SubstrateVolumetricFogCloudBSDF',emissive_only=False)
    manager.connect(ext,bsdf,'Extinction');manager.connect(alb,bsdf,'Albedo')
    assert manager.lib.connect_material_property(bsdf[0],bsdf[1],u.MaterialProperty.MP_FRONT_MATERIAL)
assert not list(manager.lib.recompile_material(material) or [])
assert u.EditorAssetLibrary.save_loaded_asset(material);manager.protect_asset(material)
for fixture in data['fixtures']:
    if fixture['volume_kind']!='grid':continue
    assert level.load_level(fixture['level'])
    if update:
        boxes=[a for a in actors.get_all_level_actors() if any(str(t)=='FogTest.FroxelBox' for t in a.tags)]
        assert len(boxes)==1
        boxes[0].static_mesh_component.set_material(0,material)
        fixture['froxel']['material']=material.get_path_name()
        assert level.save_current_level()
        continue
    box=actors.spawn_actor_from_class(u.StaticMeshActor,u.Vector(50,0,100))
    box.tags=[u.Name('FogTest.FroxelBox')];box.set_actor_scale3d(u.Vector(7.68,7.68,7.68))
    box.static_mesh_component.set_static_mesh(u.load_asset('/Engine/BasicShapes/Cube'))
    box.static_mesh_component.set_material(0,material);box.static_mesh_component.set_cast_shadow(False)
    box.static_mesh_component.set_collision_enabled(u.CollisionEnabled.NO_COLLISION)
    box.set_actor_hidden_in_game(True);box.static_mesh_component.set_visibility(False)
    host=actors.spawn_actor_from_class(u.ExponentialHeightFog,u.Vector())
    host.tags=[u.Name('FogTest.FroxelHost')];c=host.get_component_by_class(u.ExponentialHeightFogComponent)
    prop(c,'fog_density',.04);prop(c,'fog_height_falloff',0.);prop(c,'fog_max_opacity',1.)
    # Registration requires nonzero analytic density. The analytic fog starts
    # beyond this fixture; its volumetric contribution is explicitly zero.
    prop(c,'start_distance',1000000.);prop(c,'volumetric_fog_extinction_scale',0.)
    prop(c,'enable_volumetric_fog',True);prop(c,'volumetric_fog_distance',2000.)
    prop(c,'volumetric_fog_scattering_distribution',0.)
    host.set_actor_hidden_in_game(True);c.set_visibility(False)
    assert level.save_current_level()
    fixture['froxel']=dict(box=box.get_name(),host=host.get_name(),material=material.get_path_name(),
        bounds=[[-334.,-384.,-284.],[434.,384.,484.]],extinction_per_metre=.002,
        global_volumetric_density=0.,phase_g=0.,albedo=[1.,1.,1.],emission=[0.,0.,0.])
data['version']=4;atomic_json(out/'fixtures.json',data)
print('FROXEL_BOX_FIXTURES_READY')
