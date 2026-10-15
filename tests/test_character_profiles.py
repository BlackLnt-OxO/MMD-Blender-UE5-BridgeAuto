import math
from pathlib import Path

import pytest

from mmd_bridge.character_profiles import (load_profiles, resolve_profile, source_values,
    material_role, channel_value, fingerprint, discover_sources, validate_expression)
from mmd_bridge.face_sdf import direction_grid, atlas_neighbors, edt_1d


def builtin():
    return load_profiles(Path(__file__).resolve().parents[1])['ZZZ_Render_PMX']


def test_exact_parent_preserves_unregistered_material():
    profiles = {'ZZZ_Render_PMX': builtin()}
    assert resolve_profile(profiles, '/Game/User/M_RenderMaster') is None
    assert resolve_profile(profiles, '/Game/Character/MasterMaterial/M_RenderMaster')['id'] == 'ZZZ_Render_PMX'
    with pytest.raises(ValueError):
        resolve_profile(profiles, '/Game/User/Material', 'ZZZ_Render_PMX')


def test_official_toon_ignores_custom_parent_and_registered_profiles(tmp_path):
    from mmd_bridge.character_profiles import requested_character_parent
    from mmd_bridge.character_resources import install
    from mmd_bridge.core import Settings
    profile = builtin()
    profiles = {profile['id']: profile}
    settings = vars(Settings(character_master_material=profile['parents'][0],
                             eye_master_material=profile['default_eye_parent']))
    assert settings['character_material_profile'] == 'ue_official_toon'
    for name in ('Body', 'Face', 'eyes'):
        assert requested_character_parent(settings, {'name':name}, profiles) == ''
    assert resolve_profile(profiles, profile['parents'][0], 'ue_official_toon') is None
    assert install(settings, Path(__file__).resolve().parents[1], tmp_path/'Missing.uproject', None) is False


@pytest.mark.parametrize('mode,g,b', [(0,0,0),(1,1,0),(2,0,1),(3,0,0)])
def test_sphere_modes_are_not_all_additive(mode, g, b):
    values = source_values({'name': '服', 'mmd': {'sphere_texture_type': str(mode)},
                            'textures': {'sphere': {'path': 'metal.png'}}}, builtin())
    assert (values['sphere_g_weight'], values['sphere_b_weight']) == (g,b)
    assert values['sphere_gate'] == 1


def test_curve_codes_are_measured_not_sorted():
    codes = builtin()['region_codes']
    assert codes['skin'] == 0
    assert codes['cloth_2'] < codes['cloth_1'] < codes['cloth_3']


def test_pmx_ramp_lit_gain_preserves_color_and_relative_shadow():
    from mmd_bridge.character_profiles import ramp_lit_gain
    lit=[.1145833358]*3
    gain=ramp_lit_gain(lit)
    assert [a*b for a,b in zip(lit,gain)]==pytest.approx([1,1,1])
    shadow=[.0486111,.0565653,.0729167]
    assert [a*b for a,b in zip(shadow,gain)]==pytest.approx([a/b for a,b in zip(shadow,lit)])
    assert ramp_lit_gain([1,1,1])==[1,1,1,0]
    for invalid in ([0,.1,.1],[float('nan'),1,1],[-1,1,1]):
        with pytest.raises(ValueError):ramp_lit_gain(invalid)


def test_channels_and_lightmap_alpha_are_explicit():
    assert channel_value({'source':'mask','channel':'a','invert':True,'scale':.5}, {}, {'mask':[.1,.2,.3,.4]}) == pytest.approx(.3)
    assert channel_value(builtin()['packed_textures']['lightmap']['channels']['a'], source_values({},builtin())) == 1
    with pytest.raises(ValueError):
        validate_expression({'value': float('nan')})
    with pytest.raises(ValueError):
        validate_expression({'source':'mask','channel':'x'})


def test_face_detection_excludes_eye_lashes_and_body_skin():
    assert material_role({'name':'面1'}) == 'face'
    assert material_role({'name':'肌'}) == 'skin'
    assert material_role({'name':'eyelash'}) != 'face'
    assert material_role({'name':'睫毛'}) != 'face'
    assert material_role({'name':'FaceBrow'}) != 'face'
    assert material_role({'name':'目'}) == 'eye'


def test_default_eye_parent_preserves_explicit_and_generic_choices():
    from mmd_bridge.character_profiles import requested_character_parent,validate
    profile=builtin();profiles={profile['id']:profile}
    settings={'character_master_material':profile['parents'][0],'character_material_profile':'auto','eye_master_material':''}
    for name in ('Eyes','Eyewhite','FaceEyeWhite','Pupils','sclera','瞳','眼白','白目','黒目','目玉'):
        assert requested_character_parent(settings,{'name':name},profiles)==profile['default_eye_parent']
    for name in ('Eyelashes','Eyebrows','FaceBrow','Coat','眼罩','眼罩金属','眼皮','Eyelid','Blindfold','EyeMask'):
        assert material_role({'name':name})!='eye'
    settings['eye_master_material']='/Game/User/Eye'
    assert requested_character_parent(settings,{'name':'eyes'},profiles)=='/Game/User/Eye'
    settings.update(eye_master_material='',character_material_profile='generic')
    assert requested_character_parent(settings,{'name':'eyes'},profiles)==''
    settings.update(character_master_material='/Game/User/Other',character_material_profile='auto')
    assert requested_character_parent(settings,{'name':'eyes'},profiles)==''
    profile['default_eye_parent']='bad'
    with pytest.raises(ValueError):validate(profile)


def test_only_unique_rule_binds_existing_extra_texture(tmp_path):
    (tmp_path/'Model.pmx').touch()
    (tmp_path/'Body_ao.png').touch()
    profile = {'source_rules':[{'semantic':'ao','pattern':r'{base}_ao\.png'}]}
    record = {'mmd':{'texture_rel_path':'tex/Body.png'}}
    bound, candidates = discover_sources(tmp_path/'Model.pmx', record, profile)
    assert bound['ao'] == str(tmp_path/'Body_ao.png')
    (tmp_path/'more').mkdir(); (tmp_path/'more/Body_ao.png').touch()
    bound, candidates = discover_sources(tmp_path/'Model.pmx', record, profile)
    assert not bound and candidates[0]['reason'] == 'ambiguous'


def test_profile_hash_tracks_packing_changes():
    profile = builtin(); previous = fingerprint(profile)
    profile['packed_textures']['lightmap']['channels']['g'] = {'value':.2}
    assert fingerprint(profile) != previous


def test_binding_tracks_changed_texture_at_same_path(tmp_path):
    from mmd_bridge.character_profiles import binding_signature
    image=tmp_path/'base.png';image.write_bytes(b'first')
    record={'textures':{'base_color':{'path':str(image),'id':'unchanged'}}}
    previous=binding_signature(record,builtin(),'/Game/M')
    image.write_bytes(b'second')
    assert binding_signature(record,builtin(),'/Game/M')!=previous


def test_full_sphere_grid_and_wrap_and_poles():
    assert len(direction_grid()) == 114
    a, b = atlas_neighbors(-1e-8, .2), atlas_neighbors(math.tau-1e-8, .2)
    assert a == b
    assert any(cell[0] == 0 for cell, weight in a)
    for pitch in (-math.pi/2, math.pi/2):
        neighbors = atlas_neighbors(1.,pitch)
        assert sum(w for _,w in neighbors) == pytest.approx(1)
        assert all(row == (0 if pitch<0 else 8) for (_,row),w in neighbors if w>0)


def test_exact_distance_envelope_against_brute_force():
    for values in ([math.inf,0,math.inf,math.inf,0], [3,0,2,math.inf], [math.inf]*4):
        expected = [min((i-j)**2+v for j,v in enumerate(values)) for i in range(len(values))]
        assert edt_1d(values) == expected


def test_unsupported_format_and_atlas_settings_fail_explicitly():
    from mmd_bridge.character_profiles import validate
    for setting,value in (('format','unknown'),('gutter',0),('samples',0),('threshold',float('nan')),('resolution',1000),('interpolation','cubic'),('contrast',13),('smooth_sigma',-1),('edge_pixels',17),('nose_cutout',{'enabled':'yes'}),('cheek_triangle',{'enabled':'yes'}),('cheek_triangle',{'end_angle':float('nan')}),('cheek_triangle',{'end_angle':181})):
        profile=builtin();profile['sdf']['format']='threshold_lr';profile['sdf'][setting]=value
        with pytest.raises(ValueError): validate(profile)
    profile=builtin();profile['role_presets']={'skin':{'vectors':{'ShadowColor':[.4,.4,.4,0]}}}
    assert validate(profile)
    profile['role_presets']['skin']['vectors']['ShadowColor']=[0,float('nan'),0]
    with pytest.raises(ValueError): validate(profile)
    profile=builtin();profile['scalars']['opacity']['value']=float('inf')
    with pytest.raises(ValueError): validate(profile)


def test_single_threshold_and_existing_atlas_profiles_are_supported():
    from mmd_bridge.character_profiles import validate
    profile=builtin()
    assert profile['sdf']['format']=='none'
    assert profile['textures']['face_sdf']['parameter']=='SDF_Tex'
    assert profile['switches']['face_sdf']['parameter']=='UseSDF?'
    profile['sdf']['format']='threshold_lr'
    assert validate(profile)
    profile['sdf']['format']='direction_atlas'
    assert validate(profile)


def test_outline_contract_rejects_invalid_paths_and_widths():
    from mmd_bridge.character_profiles import validate
    for outline in ({'parent':'bad'}, {'parent':'/Game/../M'}, {'parent':'/Game/M','scale':float('nan')}, {'parent':'/Game/M','face_scale':-1}):
        profile=builtin();profile['outline']=outline
        with pytest.raises(ValueError): validate(profile)


def test_outline_width_flags_and_disabled_model_defaults():
    from mmd_bridge.character_profiles import outline_values
    record={'name':'Face','mmd':{'edge_color':[.1,.2,.3,1],'edge_weight':2,'enabled_toon_edge':False}}
    color,width=outline_values(record,builtin())
    assert color==[.1,.2,.3,0] and width==pytest.approx(.08)
    color,width=outline_values(record,builtin(),True)
    assert color==[0,0,0,1] and width==pytest.approx(.04)
    record['name']='eye';assert outline_values(record,builtin(),True)[0][3]==0
    record['name']='FaceBrow';assert outline_values(record,builtin(),True)[0][3]==0


def test_install_conflict_checks_all_assets_before_writing(tmp_path):
    import json
    from mmd_bridge.character_resources import install
    root=tmp_path/'source';folder=root/'profiles/character_materials';folder.mkdir(parents=True)
    profile={'version':1,'id':'Custom','parents':['/Game/M_Test'],'template':'Shared','sdf':{'format':'none'}}
    (folder/'test.json').write_text(json.dumps(profile))
    source=root/'resources/character_materials/Shared/Content';source.mkdir(parents=True)
    (source/'a.uasset').write_bytes(b'new');(source/'b.uasset').write_bytes(b'original')
    project=tmp_path/'project/Test.uproject';project.parent.mkdir();project.write_text('{}')
    content=project.parent/'Content';content.mkdir();(content/'b.uasset').write_bytes(b'user')
    settings={'character_master_material':'/Game/M_Test','character_material_profile':'Custom'}
    with pytest.raises(ValueError): install(settings,root,project,None)
    assert not (content/'a.uasset').exists()
    assert (content/'b.uasset').read_bytes()==b'user'
    (content/'b.uasset').write_bytes(b'original')
    assert install(settings,root,project,None)
    assert install(settings,root,project,None)
    assert (content/'a.uasset').read_bytes()==b'new'


def test_saved_custom_profile_roundtrip_without_text_entry(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from mmd_bridge.app import Window
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path))
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=QApplication.instance() or QApplication([])
    window=Window()
    try:
        field = window.inputs['character_material_profile']
        assert not field.isEditable()
        window.apply_settings({'character_material_profile':'MyOtherShader'})
        settings=window.settings()
        assert settings.character_material_profile=='MyOtherShader'
        window.apply_settings({'character_material_profile':'SavedOtherShader',
                               'character_material_profiles_dir':str(tmp_path/'profiles')})
        settings=window.settings()
        assert settings.character_material_profile=='SavedOtherShader'
        assert settings.character_material_profiles_dir==str(tmp_path/'profiles')
        assert not field.isEditable()
    finally: window.close()
