import ast,sys
from pathlib import Path
from types import SimpleNamespace as N
import pytest


def load_reuse(file,scope):
    scope.update(__name__='mmd_bridge.'+file.removesuffix('.py'),__package__='mmd_bridge')
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge'/file).read_text(encoding='utf-8-sig'))
    fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='reuse')
    helpers=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='verify_skin_profile']
    exec(compile(ast.Module(body=[*helpers,fn],type_ignores=[]),file,'exec'),scope)
    return scope['reuse']


@pytest.mark.parametrize('profile',[None,{'id':'explicit-profile'}])
def test_generic_and_registered_cache_restore_route_evidence(monkeypatch,profile):
    parent=N(get_path_name=lambda:'/Game/Materials/ActualParent.ActualParent')
    instance=N(get_editor_property=lambda key:parent)
    route='character_profile' if profile else 'character_master'
    tags={'MMDBridge.ParentMaterial':'/Game/Materials/Requested',
          'MMDBridge.CharacterBinding':'binding','MMDBridge.GenericCharacterBinding':'binding',
          'MMDBridge.MaterialRoute':route}
    lib=N(get_metadata_tag=lambda obj,key:tags.get(key,'') if obj is instance else '')
    u=N(load_asset=lambda path:instance if '/MI_' in path else None,EditorAssetLibrary=lib,
        Paths=N(project_content_dir=lambda:'C:/P/Content'))
    monkeypatch.setitem(sys.modules,'mmd_bridge.ue_character_materials',N(
        profile_for=lambda *a:profile,dependency_signature=lambda *a:'dependency',ADAPTER_VERSION=19))
    monkeypatch.setattr('mmd_bridge.character_profiles.binding_signature',lambda *a:'source-binding')
    monkeypatch.setattr('mmd_bridge.character_profiles.fingerprint',lambda *a:'binding')
    checked=[]
    manager=N(cfg={'character_backend':'mmdbridge_fbx','character_material_profile':'generic'},owner='owner',
        material_folder=lambda row:'/Game/QA/Materials',character_master=lambda row:(parent,'/Game/Materials/Requested',route),
        verify_asset=lambda obj:checked.append(obj),assets={},report={})
    record={'id':'slot','asset_name':'M_Slot','pmx_index':0}
    assert load_reuse('ue_materials.py',{'u':u})(manager,record)
    actual=manager.report['material_routes']['slot']
    assert actual['route']==route and actual['reused'] and actual['binding_hash']=='binding'
    assert actual['parent']==parent.get_path_name() and checked==[instance]
    if profile:assert actual['profile']=='explicit-profile'


def test_pbr_cache_retains_its_material_route_in_the_new_receipt():
    parent=N(get_path_name=lambda:'/Game/QA/Materials/M_PBR.M_PBR')
    instance=N(get_editor_property=lambda key:parent,get_path_name=lambda:'/Game/QA/Materials/MI_Skin.MI_Skin')
    u=N(EditorAssetLibrary=N(get_metadata_tag=lambda obj,key:'binding' if key=='MMDBridge.PBRBinding' else ''))
    manager=N(verify_asset=lambda asset:None,assets={},report={})
    record={'id':'skin','character_material':{'pbr':{'type':'skin','alpha_mode':'opaque'}}}
    assert load_reuse('ue_character_pbr.py',{'u':u,'signature':lambda row:'binding'})(manager,record,instance)
    assert manager.report['material_routes']['skin']['route']=='character_pbr'
    assert manager.report['material_routes']['skin']['reused']


@pytest.mark.parametrize('backend', ['mmdbridge_fbx', 'fbx_legacy'])
def test_explicit_custom_record_uses_custom_cache_for_both_blender_import_routes(monkeypatch, backend):
    instance = object()
    u = N(load_asset=lambda path: instance if '/MI_' in path else None,
          EditorAssetLibrary=N(get_metadata_tag=lambda *a: ''),
          Paths=N(project_content_dir=lambda: 'C:/P/Content'))
    seen = []
    monkeypatch.setitem(sys.modules, 'mmd_bridge.ue_custom_materials', N(
        reuse=lambda manager, record, value: seen.append(value) or True))
    manager = N(cfg={'character_backend': backend}, material_folder=lambda row: '/Game/QA/Materials')
    record = {'id': 'slot', 'asset_name': 'M_Slot', 'pmx_index': 0, 'character_material': {'custom': {'version': 1}}}
    assert load_reuse('ue_materials.py', {'u': u})(manager, record)
    assert seen == [instance]
