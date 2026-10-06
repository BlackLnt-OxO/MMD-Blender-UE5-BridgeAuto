import ast
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest


def test_source_optics_apply_only_to_matching_render_camera(monkeypatch):
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_reference.py').read_text(encoding='utf-8-sig'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='reference_camera')
    optics={'current_focal_length':85.,'current_aperture':8.,
            'focus_settings':SimpleNamespace(focus_method='Disabled',manual_focus_distance=0.)}
    component=SimpleNamespace(get_editor_property=optics.__getitem__)
    camera=SimpleNamespace(get_actor_label=lambda:'Source',get_path_name=lambda:'PIE:Source',
                           get_component_by_class=lambda cls:component)
    unrelated=SimpleNamespace(get_actor_label=lambda:'User camera')
    cameras=[unrelated,camera]
    configured=[]
    def configure(target,source,scene):
        configured.append(target)
        optics.update(current_focal_length=source['lens'],current_aperture=source['dof']['fstop'],
                      focus_settings=SimpleNamespace(focus_method='Manual',manual_focus_distance=source['dof']['focus_distance']*100))
    monkeypatch.setitem(sys.modules,'mmd_bridge.ue_appearance',SimpleNamespace(configure_camera=configure))
    native=SimpleNamespace(CineCameraActor=object(),CineCameraComponent=object(),
                           GameplayStatics=SimpleNamespace(get_all_actors_of_class=lambda world,cls:cameras))
    namespace={'u':native,'__package__':'mmd_bridge'}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'reference-camera-boundary','exec'),namespace)
    scene={'objects':[{'name':'Source','camera':{'active':True,'lens':50.,
             'dof':{'enabled':True,'fstop':.8,'focus_distance':7.}}}]}
    result=namespace['reference_camera']('temporary world',scene)
    assert configured==[component]
    assert result['aperture']==.8 and result['focus_distance_cm']==700.
    assert result['focus_method']=='Manual' and result['actor']=='PIE:Source'
    cameras[:]=[unrelated,unrelated]
    with pytest.raises(ValueError,match='唯一'):
        namespace['reference_camera']('temporary world',scene)
    assert configured==[component]


def test_geometry_validation_excludes_only_requested_hidden_objects():
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_reference.py').read_text(encoding='utf-8-sig'))
    function=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='reference_scene_geometry')
    class Mesh:
        def get_editor_property(self,name):return object()
        def get_children_components(self,recursive):return []
        def get_path_name(self):return 'PIE:Tree'
    mesh=Mesh()
    stage=SimpleNamespace(get_generated_component=lambda path:mesh if path=='/Tree' else None)
    native=SimpleNamespace(UsdStageActor=object(),StaticMeshComponent=Mesh,
        GameplayStatics=SimpleNamespace(get_all_actors_of_class=lambda world,cls:[stage]))
    namespace={'u':native,'restore_stage_components':lambda actor:{'components_checked':1}}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'reference-scene-hidden','exec'),namespace)
    scene={'instances':[{'name':'Glass','prim_path':'/Glass'},{'name':'Tree','prim_path':'/Tree'}]}
    result=namespace['reference_scene_geometry'](None,scene,['Glass'])
    assert result['expected_instances']==2 and result['renderable_instances']==1
    assert result['intentionally_hidden_instances']==1
    assert result['foliage_distance']['components_checked']==1
    with pytest.raises(ValueError,match='USD'):
        namespace['reference_scene_geometry'](None,scene)
