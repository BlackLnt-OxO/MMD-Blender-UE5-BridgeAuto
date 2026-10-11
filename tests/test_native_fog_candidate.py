"""Offline native fog planner checks."""

def test_native_volume_plan_preserves_fields_and_reports_phase_limits():
    import copy,pytest
    from mmd_bridge.native_fog_candidate import native_volume_plan,NATIVE_PROFILES
    v=dict(id='v',name='local',status='converted',kind='local_grid',prim_path='/World/V',
        coefficient_units='per_voxel',voxel_size=.2,
        uniform_source_proof=dict(method='constant_expressions',coefficients=dict(anisotropy=.5)))
    before=copy.deepcopy(v)
    plan=native_volume_plan([v],2.)
    assert plan['phase_g']==.5 and plan['grids'][0]['voxel_metres']==.4 and v==before
    zero=dict(v,sample_statistics=dict(sampled_voxels=5,occupied_voxels=0))
    assert not native_volume_plan([zero])['grids']
    mixed=native_volume_plan([v,dict(v,id='v2',uniform_source_proof=None)])
    assert mixed['phase_g']==0 and mixed['reviews']
    with pytest.raises(ValueError):native_volume_plan([dict(v,time_policy='animated')])
    assert set(NATIVE_PROFILES)=={'native_balanced','native_quality','native_detail'}


def test_native_binding_validation_precedes_visibility_changes_and_pt_restores_source():
    import ast,json,pytest
    from pathlib import Path
    from types import SimpleNamespace as N
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_fog_froxel.py').read_text(encoding='utf-8-sig'))
    texture=N(get_path_name=lambda:'/Game/SVT')
    class Component:
        visible=True
        def get_path_name(self):return '/World/Source'
        def is_visible(self):return self.visible
        def set_visibility(self,value):self.visible=value
    source=Component();mesh=Component()
    mesh.get_material=lambda slot:N(get_path_name=lambda:'/Game/NativeMaterial')
    binding=dict(prim_path='/World/Fog',material='/Game/NativeMaterial',source_svt='/Game/SVT',source_visible=True)
    actor=N(tags=['MMDBridge.NativeFog='+json.dumps(binding)],static_mesh_component=mesh,
            get_actor_transform=lambda:'pose',hidden=False)
    actor.set_actor_hidden_in_game=lambda value:setattr(actor,'hidden',value)
    native=N(StaticMeshActor=object(),ExponentialHeightFog=object(),HeterogeneousVolumeComponent=Component)
    native.GameplayStatics=N(get_all_actors_of_class=lambda world,cls:[actor] if cls is native.StaticMeshActor else [])
    namespace=dict(__name__='mmd_bridge.ue_fog_froxel',u=native,_texture=lambda c,p='Volume':texture,_pose=lambda c:('pose',1),
                   _same_pose=lambda a,b:a==b,native_property=lambda c,n,cpp=None:c.hidden,
                   release_native_streaming=lambda:None)
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='restore_native']
    exec(compile(ast.Module(body=selected,type_ignores=[]),'native-binding','exec'),namespace)
    restore=namespace['restore_native'];stages=[N(get_generated_component=lambda path:source)]
    volume=dict(id='fog',name='Fog',prim_path='/World/Fog',status='converted',kind='world_grid',coefficient_units='per_voxel',voxel_size=.1)
    with pytest.raises(ValueError):restore('world',stages,sources=[])
    assert source.visible and mesh.visible and not actor.hidden
    result=restore('world',stages,sources=[volume])
    assert result['restored']==1 and not source.visible and mesh.visible
    restore('world',stages,'path_tracing',True,[volume])
    assert source.visible and not mesh.visible and actor.hidden
