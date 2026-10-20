from mmd_bridge.character_geometry_roles import purpose, bone_role, validate_overrides
import pytest


def record(name,kind='generic'):
    return {'name':name,'character_material':{'pbr':{'type':kind}}}


def evidence(**values):
    return dict(humanoid_rig=True,head_weight_ratio=.95,body_weight_ratio=.95,
                leg_weight_ratio=.8,near_leg_ratio=.9,**values)


def test_roles_use_names_and_measured_anatomy_not_skin_type_or_slot():
    for name,kind,role in [('Body','skin','body'),('Face2','face','face'),('Lip','face','face'),
                           ('stocking','hosiery','legwear'),('スーツ','generic','legwear'),('Garter','cloth','legwear')]:
        assert purpose(record(name,kind),evidence())['role']==role
    for name,kind in [('Nails','skin'),('FaceBrow','face'),('Eyes','iris'),('Hair','hair'),
                      ('pocket','cloth'),('Skirt','cloth'),('Shoes','generic'),('ShortsMetal','metal'),('目','generic'),('目白','generic')]:
        assert not purpose(record(name,kind),evidence())['local_selected']
    assert purpose(record('Material_041','skin'),evidence())['needs_review']
    assert purpose(record('Body','skin'),{})['needs_review']


def test_weak_or_conflicting_evidence_is_not_silently_selected():
    assert purpose(record('Face'),dict(humanoid_rig=True,head_weight_ratio=.1))['needs_review']
    assert purpose(record('pants'),dict(humanoid_rig=True,leg_weight_ratio=.8,near_leg_ratio=.2))['needs_review']
    robot=record('Body','generic');robot['model_name']='Robot'
    assert not purpose(robot,evidence())['local_selected']
    assert purpose(record('Body','generic'),dict(evidence(),model_name_context='機械 Robot'))['needs_review']
    assert purpose(record('無名'),dict(humanoid_rig=True,attached_host=7))['role']=='attached_layer'
    assert purpose(record('Body','plastic'),evidence())['needs_review']
    assert purpose(record('Body','skin'),dict(evidence(),mixed_purposes=['body','mechanical']))['needs_review']
    assert purpose(record('面1','face'),evidence())['role']=='face'
    assert not purpose(record('blush','face'),evidence())['local_selected']


def test_geometry_override_is_separate_and_explicit():
    assert purpose(record('Nails','skin'),{}, {'local':'include'})['local_selected']
    assert not purpose(record('Face'),evidence(),{'local':'exclude'})['local_selected']
    validate_overrides({'model':{'0':{'local':'include','host':1}}})
    for invalid in [{'model':{'0':{'host':0}}},{'model':{'0':{'type':'cloth'}}}]:
        with pytest.raises(ValueError):validate_overrides(invalid)


def test_analysis_uses_effective_physical_type_for_geometry_purpose(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from mmd_bridge import character_pbr,pmx_reader,pmx_geometry
    materials=[{'name':name,'pmx_index':i,'name_e':'','texture_index':-1,'sphere_index':-1,
                'diffuse':[1.,1.,1.,1.],'sphere_mode':0}
               for i,name in enumerate(('Body','UnknownLayer'))]
    monkeypatch.setattr(pmx_reader,'read_pmx',lambda path:SimpleNamespace(
        encoded_name='Humanoid',materials=materials,textures=[]))
    monkeypatch.setattr(pmx_geometry,'read_geometry',lambda path:{i:evidence() for i in range(2)})
    monkeypatch.setattr(character_pbr,'file_hash',lambda path:'model')
    cfg={'character_material_overrides':{'model':{'0':{'type':'plastic'},'1':{'type':'iris'}}}}
    rows=character_pbr.inspect_model(tmp_path/'model.pmx',cfg)['materials']
    assert rows[0]['pbr']['type']=='plastic'
    assert rows[0]['geometry_purpose']['needs_review']
    assert not rows[0]['geometry_purpose']['local_selected']
    assert rows[1]['pbr']['type']=='iris'
    assert rows[1]['geometry_purpose']['role']=='excluded'


@pytest.mark.parametrize('name,role',[('左足','thigh'),('右ひざ','knee'),('LeftFoot','foot'),
                                   ('左足D','thigh'),('J_Bip_C_Head','head'),('上半身2','trunk'),('左足ＩＫ',None)])
def test_bone_aliases(name,role):
    assert bone_role(name)==role
