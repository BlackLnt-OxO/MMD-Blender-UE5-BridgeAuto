import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.character_profiles import fingerprint
from mmd_bridge.core import file_hash


def functions():
    path=Path(__file__).parents[1]/'mmd_bridge/ue_character_pbr.py'
    names={'_record_versions','signature','master'}
    tree=ast.parse(path.read_text(encoding='utf8'))
    scope={'VERSION':9,'SHADING_VERSION':11,'DEFAULTS_VERSION':5,'Path':Path,
           'file_hash':file_hash,'fingerprint':fingerprint}
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),str(path),'exec'),scope)
    return scope


def test_records_without_skin_revision_keep_the_previous_graph_contract():
    assert functions()['_record_versions']({'version':9})==(9,9,3)


def test_new_defaults_preserve_explicit_controls_and_slot_identity():
    from mmd_bridge.character_pbr import plan
    source={'name':'Body','pmx_index':0,'mmd':{'diffuse_alpha':.75},'textures':{}}
    explicit={'Roughness':.73,'Specular':.25,'DetailStrength':.33}
    cfg={'character_skin_scattering':'balanced_v1',
         'character_material_overrides':{'one':{'0':{'params':explicit}}}}
    edited=plan(source,cfg,'one')
    assert all(edited['params'][key]==value for key,value in explicit.items())
    assert edited['params']['Opacity']==.75
    assert edited['params']['SSSStrength']==.35
    assert edited['params']['SkinRosiness']==.55
    fresh=plan(dict(source,pmx_index=1),cfg,'one')
    assert fresh['params']['Roughness']==.34
    assert fresh['params']['Specular']==.42
    assert fresh['params']['DetailStrength']==.065
    assert fresh['shading_version']==11 and fresh['defaults_version']==5


def test_texture_changes_still_invalidate_the_saved_binding(tmp_path):
    path=tmp_path/'base.png';path.write_bytes(b'first source')
    record={'character_material':{'pbr':{'version':9,'shading_version':9,'defaults_version':3,'type':'skin'}},
            'textures':{'base_color':{'path':str(path)}}}
    signature=functions()['signature'];before=signature(record)
    path.write_bytes(b'changed source')
    assert signature(record)!=before


def test_existing_v9_binding_hash_survives_new_live_defaults():
    scope=functions();pbr={'version':9,'shading_version':9,'defaults_version':3,'type':'skin',
                           'params':{'Roughness':.48,'DetailStrength':.22}}
    record={'character_material':{'pbr':pbr},'mmd':{'diffuse_alpha':1},'textures':{}}
    original=fingerprint([9,9,3,pbr,record['mmd'],{},'explicit_color_compression_1'])
    assert scope['signature'](record)==original
    pbr.update(shading_version=10,defaults_version=4)
    assert scope['signature'](record)!=original


@pytest.mark.parametrize('shading,defaults',[(9,3),(10,4),(11,5)])
def test_master_path_uses_the_prepared_record_instead_of_live_defaults(shading,defaults):
    scope=functions();loaded=[];asset=object();verified=[]
    scope['u']=SimpleNamespace(load_asset=lambda p:loaded.append(p) or asset,
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda *a:'owned'))
    manager=SimpleNamespace(root='/Game/Fixture',owner='owned',_created_this_pass=set(),
                            verify_asset=lambda a:verified.append(a))
    record={'character_material':{'pbr':{'version':9,'shading_version':shading,
        'defaults_version':defaults,'type':'skin','alpha_mode':'opaque','skin_scattering':'balanced_v1'}},
        'mmd':{'two_sided':False}}
    assert scope['master'](manager,record) is asset
    assert loaded==[f'/Game/Fixture/Materials/M_CharacterPBR_skin_opaque_One_v9_S{shading}_D{defaults}_Balanced1']
    assert verified==[asset]
