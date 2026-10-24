import copy
from pathlib import Path

import pytest

from mmd_bridge.character_pbr import classify, classification_evidence, plan, discover, override_for, validate_settings
from mmd_bridge.core import Settings


def source(tmp_path, sphere_mode=0):
    base=tmp_path/'Body.png'; base.write_bytes(b'base')
    sphere=tmp_path/'cap.spa'; sphere.write_bytes(b'sphere')
    return {'name':'Body','source_name':'Body','asset_name':'M_Body','pmx_index':0,
            'mmd':{'diffuse_color':[.8,.5,.4],'diffuse_alpha':1.,'texture_rel_path':'Body.png',
                   'sphere_texture_type':sphere_mode,'sphere_texture_rel_path':'cap.spa'},
            'textures':{'base_color':{'path':str(base)},'sphere':{'path':str(sphere)}},
            'character_material':{'alpha_mode':'opaque'}}


@pytest.mark.parametrize('name,kind', [('Hairpin','plastic'),('Eyelashes','lash'),
    ('EyeShadow','eye_shadow'),('EyeLights','eye_highlight'),('白目','sclera'),
    ('Pupils','iris'),('Body','skin'),('Face2','face'),('ShortsMetal','metal'),
    ('Hair02','hair'),('stocking','hosiery'),('丝袜','hosiery'),('socks','generic'),
    ('Tie','satin'),('ShoeTies','cloth'),('unknown_part','generic')])
def test_categories_keep_eye_layers_and_hair_accessories_separate(name,kind):
    assert classify({'name':name})==kind


@pytest.mark.parametrize('name,english,kind', [
    ('Ribbon','N00_000_Hair_00_HAIR_03 (Instance)','cloth'),
    ('RibbonTail','N00_000_Hair_00_HAIR_03 (Instance)','cloth'),
    ('FaceBrow','N00_000_00_FaceBrow_00_FACE (Instance)','lash'),
    ('安全裤','N00_000_00_Body_00_SKIN (Instance)','cloth'),
    ('白い目','eyeball','sclera'),('上着','Jacket','cloth'),('クローク','cape','cloth'),
    ('白い ショーツ','white short','cloth'),('Ｈａｉｒ０２','','hair'),
    ('髪影','HairShadow','eye_shadow'),('GARTERButtons','','generic'),
    ('ShortsMetal','cloth','metal'),('Shoes','','generic'),
    ('Shoes','N00_000_Shoes_00_CLOTH (Instance)','generic'),
    ('unknown_part','N00_000_Hair_00_HAIR_04 (Instance)','hair')])
def test_original_part_names_precede_exporter_tags(name,english,kind):
    record={'name':name,'mmd':{'name_j':name,'name_e':english}}
    assert classify(record)==kind


def test_classification_evidence_and_manual_type_are_preserved(tmp_path):
    record=source(tmp_path);record.update(name='Ribbon',source_name='Ribbon')
    record['mmd']['name_e']='N00_000_Hair_00_HAIR_03 (Instance)'
    evidence=classification_evidence(record)
    assert evidence['type']=='cloth' and evidence['field']=='name'
    assert any(item['type']=='hair' and item['field']=='mmd.name_e' for item in evidence['conflicts'])
    cfg={'character_material_overrides':{'model':{'0':{'type':'satin'}}}}
    selected=plan(record,cfg,'model')
    assert selected['type']=='satin' and selected['classification']['method']=='manual_override'
    assert selected['classification']['automatic_type']=='cloth'
    assert plan(record,cfg,'other')['type']=='cloth'
    assert classification_evidence({'name':'unknown_part'})['requires_review']


def test_original_japanese_field_and_specific_metal_rule_keep_priority():
    record={'name':'unknown_part','mmd':{'name_j':'リボン','name_e':'N00_Hair (Instance)'}}
    evidence=classification_evidence(record)
    assert evidence['field']=='mmd.name_j' and evidence['type']=='cloth'
    assert any(row['type']=='hair' for row in evidence['conflicts'])
    assert classify({'name':'ShortsMetal','mmd':{'name_e':'Hair'}})=='metal'


def test_pre_v9_cached_record_does_not_require_new_classification_evidence(tmp_path):
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    record=source(tmp_path);old=plan(record,{},'model')
    old.pop('classification');old['version']=8
    old['params']['SkinRosiness']=.55
    snapshot={'model':'test.pmx','sha256':'model','texture_count':0,
              'materials':[{'pmx_index':0,'name':'Body','pbr':old}]}
    dialog=CharacterPBRDialog(snapshot,{})
    try:
        assert dialog.kind.currentData()=='auto'
        assert dialog.params['SkinRosiness'].value()==old['params']['SkinRosiness']
        dialog.store()
        assert dialog.overrides['model']['0']['type']=='auto'
    finally:dialog.close()


def test_ambiguous_eye_uses_actual_uv_region_and_respects_explicit_iris_name():
    note={'uv_color_statistics':{'samples':51073,'bright_neutral_ratio':1.,'std':[.001,.003,.005]}}
    assert classify({'name':'Eyes','character_material':note})=='sclera'
    assert classify({'name':'Pupils','character_material':note})=='iris'
    assert classify({'name':'Eyes'})=='iris'
    note['uv_color_statistics']['bright_neutral_ratio']=.22
    assert classify({'name':'Eyes','character_material':note})=='iris'


def test_manual_base_texture_does_not_reuse_old_uv_classification(tmp_path):
    record=source(tmp_path);record['name']='Eyes'
    record['character_material']['uv_color_statistics']={'samples':1000,'bright_neutral_ratio':1.,'std':[0,0,0]}
    assert plan(record,{},'model')['type']=='sclera'
    cfg={'character_material_overrides':{'model':{'0':{'textures':{
        'base_color':{'path':record['textures']['base_color']['path'],'channel':'rgb'}}}}}}
    assert plan(record,cfg,'model')['type']=='iris'


def test_packaged_fallback_normal_has_no_surface_variation():
    import struct,zlib
    data=(Path(__file__).resolve().parents[1]/'resources/character_materials/Character_PBR/flat_normal.png').read_bytes()
    offset=8;compressed=b''
    while offset<len(data):
        length=struct.unpack('>I',data[offset:offset+4])[0];kind=data[offset+4:offset+8]
        if kind==b'IDAT':compressed+=data[offset+8:offset+8+length]
        offset+=12+length
    rows=zlib.decompress(compressed)
    assert rows==(b'\0'+bytes((128,128,255,255))*4)*4


@pytest.mark.parametrize('mode,enabled', [(0,False),(1,True),(2,True),(3,False)])
def test_matcap_tracks_active_pmx_mode_not_folder_presence(tmp_path,mode,enabled):
    result=plan(source(tmp_path,mode),{},'model')
    assert result['matcap']['enabled'] is enabled
    if mode==3: assert result['status']=='white'


def test_missing_pbr_maps_keep_base_and_intentional_solid_colors(tmp_path):
    record=source(tmp_path)
    result=plan(record,{},'model')
    assert result['status']=='ready' and result['params']['Metallic']==0
    record['mmd']['texture_rel_path']=''; record['textures']={}
    assert plan(record,{},'model')['status']=='ready'
    record['mmd']['texture_rel_path']='Missing.png'
    assert plan(record,{},'model')['status']=='white'


def test_missing_source_matcap_preserves_base_and_reports_incomplete_result(tmp_path):
    record = source(tmp_path, 1)
    del record['textures']['sphere']
    result = plan(record, {}, 'model')
    assert result['status'] == 'needs_review'
    assert result['textures']['base_color']['path'] == record['textures']['base_color']['path']
    assert result['matcap'] == {'enabled': False, 'mode': 1, 'selection': 'source_auto',
                                'source_requested': True, 'available': False}
    assert result['missing_effects'][0]['reference'] == 'cap.spa'
    assert result['issues'] and result['missing_effects'][0]['fallback'] == 'preserve_base_color'
    del record['textures']['base_color']
    assert plan(record, {}, 'model')['status'] == 'white'
    record = source(tmp_path, 2)
    cfg = {'character_pbr_options': {'textures': {'sphere': {'path': str(tmp_path/'absent.png')}}}}
    assert plan(record, cfg, 'model')['status'] == 'white'


def test_override_priority_and_model_isolation(tmp_path):
    record=source(tmp_path,1)
    cfg={'character_pbr_options':{'params':{'Roughness':.6}},
         'character_material_overrides':{'one':{'0':{'matcap_mode':'off','params':{'Roughness':.2}}}}}
    assert plan(record,cfg,'one')['params']['Roughness']==.2
    assert plan(record,cfg,'one')['matcap']['enabled'] is False
    assert plan(record,cfg,'two')['params']['Roughness']==.6
    assert plan(record,cfg,'two')['matcap']['enabled'] is True
    assert cfg['character_pbr_options']=={'params':{'Roughness':.6}}


def test_missing_effect_map_is_visible_in_configuration(monkeypatch,tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app=W.QApplication.instance() or W.QApplication([])
    record=source(tmp_path,2);del record['textures']['sphere']
    row={'name':'Body','pmx_index':0,'sphere_mode':2,'diffuse':[1,1,1,1],
         'pbr':plan(record,{},'model')}
    dialog=CharacterPBRDialog({'sha256':'model','model':'model.pmx','materials':[row],'texture_count':1},{})
    try:
        assert '缺效果图' in dialog.slots.item(0).text()
        assert 'cap.spa' in dialog.evidence.toPlainText()
        assert dialog.matcap.currentData()=='source_auto'
    finally:dialog.close()


def test_changed_texture_bytes_change_binding_without_renaming(tmp_path):
    record=source(tmp_path)
    before=plan(record,{},'model')['signature']
    Path(record['textures']['base_color']['path']).write_bytes(b'new base')
    assert plan(record,{},'model')['signature']!=before


def test_clearing_override_restores_original_source_binding(tmp_path):
    record=source(tmp_path)
    changed=tmp_path/'Other.png';changed.write_bytes(b'other')
    cfg={'character_material_overrides':{'model':{'0':{'textures':{
        'base_color':{'path':str(changed),'channel':'rgb'}}}}}}
    edited=plan(record,cfg,'model')
    record['character_material']['pbr']=edited
    record['textures']['base_color']={'path':str(changed)}
    restored=plan(record,{},'model')
    assert Path(restored['textures']['base_color']['path']).name=='Body.png'


def test_ambiguous_packed_maps_are_not_guessed(tmp_path):
    first=tmp_path/'a'/'Body_Normal.png'; second=tmp_path/'b'/'Body_Normal.png'
    first.parent.mkdir();second.parent.mkdir()
    first.touch();second.touch()
    packed=tmp_path/'Body_LightMap.png';packed.touch()
    bound,candidates=discover('Body.png',[first,second,packed])
    assert 'normal' not in bound
    assert {item['reason'] for item in candidates}=={'ambiguous','unknown_packed_channels'}


def test_explicit_channel_and_alpha_zero_survive(tmp_path):
    record=source(tmp_path);record['mmd']['diffuse_alpha']=0
    cfg={'character_material_overrides':{'one':{'0':{'alpha_mode':'translucent','textures':{
        'roughness':{'path':record['textures']['base_color']['path'],'channel':'g','invert':True}}}}}}
    result=plan(record,cfg,'one')
    assert result['textures']['roughness']['channel']=='g'
    assert result['textures']['roughness']['invert'] is True
    assert result['alpha_mode']=='masked' and result['params']['Opacity']==0


def test_invalid_overrides_reject_nonfinite_or_unknown_values():
    with pytest.raises(ValueError):
        validate_settings({}, {'one':{'0':{'params':{'Roughness':float('nan')}}}})
    with pytest.raises(ValueError):
        validate_settings({}, {'one':{'0':{'textures':{'metallic':{'path':'a','channel':'q'}}}}})


def test_wet_skin_is_optional_and_isolated_by_model_and_slot(tmp_path):
    record = source(tmp_path)
    assert plan(record, {}, 'one')['params']['WetEnabled'] == 0
    settings = {'character_material_overrides': {'one': {'0': {'params': {
        'WetEnabled': 1., 'WetAmount': .6, 'WetRotation': 20.}}}}}
    wet = plan(record, settings, 'one')
    assert wet['params']['WetEnabled'] == 1 and wet['params']['WetAmount'] == .6
    assert plan(record, settings, 'two')['params']['WetEnabled'] == 0
    second = dict(record, pmx_index=1)
    assert plan(second, settings, 'one')['params']['WetEnabled'] == 0
    with pytest.raises(ValueError):
        validate_settings({'params': {'WetEnabled': 2.}}, {})


def test_uv_scale_survives_live_replan_but_explicit_controls_win(tmp_path):
    record = source(tmp_path)
    previous = plan(record, {}, 'one')
    previous['generated_defaults'] = {'DetailTiling': 115., 'WetTiling': 8.625, 'WetRotation': 10.}
    record['character_material']['pbr'] = previous
    settings = {'character_pbr_options': {'params': {'WetTiling': 3.}}}
    updated = plan(record, settings, 'one')
    assert updated['params']['DetailTiling'] == 115.
    assert updated['params']['WetTiling'] == 3.
    assert updated['params']['WetRotation'] == 10.
    changed = plan(record, {'character_pbr_options': {'type': 'cloth'}}, 'one')
    assert changed['generated_defaults'] == {}
    assert changed['params']['DetailTiling'] != 115.


def test_generated_face_mask_survives_live_wet_edits_and_manual_mask_wins(tmp_path):
    record = source(tmp_path); record['name'] = 'Face'
    previous = plan(record, {}, 'one')
    mask = {'path': record['textures']['base_color']['path'], 'channel': 'r', 'kind': 'generated'}
    previous['textures']['sss_mask'] = mask
    previous['face_scattering_source'] = {'method': 'original_frontal_nose_gaussian_uv_mask'}
    record['character_material']['pbr'] = previous
    updated = plan(record, {'character_pbr_options': {'params': {'WetEnabled': 1.}}}, 'one')
    assert updated['textures']['sss_mask']['kind'] == 'generated'
    assert updated['face_scattering_source'] == previous['face_scattering_source']
    manual = {'character_pbr_options': {'textures': {'sss_mask': dict(mask, channel='g')}}}
    updated = plan(record, manual, 'one')
    assert updated['textures']['sss_mask']['kind'] == 'manual'
    assert updated['textures']['sss_mask']['channel'] == 'g'
    assert 'face_scattering_source' not in updated
    assert 'sss_mask' not in plan(record, {'character_pbr_options': {'type': 'cloth'}}, 'one')['textures']


def test_builtin_details_separate_pores_weave_and_water():
    from mmd_bridge.character_pbr import detail_bindings
    skin, cloth, leather = [detail_bindings(k) for k in ('skin', 'cloth', 'leather')]
    assert len({x['detail_normal']['sha256'] for x in (skin, cloth, leather)}) == 3
    assert {'wet_normal', 'wet_mask'} <= skin.keys()
    assert 'wet_normal' not in cloth and 'wet_mask' not in leather


def test_wet_checkbox_roundtrip_and_explicit_off(monkeypatch, tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app = W.QApplication.instance() or W.QApplication([])
    record = source(tmp_path)
    row = {'name': 'Body', 'pmx_index': 0, 'sphere_mode': 0, 'diffuse': [1, 1, 1, 1],
           'pbr': plan(record, {}, 'model')}
    snapshot = {'sha256': 'model', 'model': 'model.pmx', 'materials': [row], 'texture_count': 1}
    dialog = CharacterPBRDialog(snapshot, {})
    try:
        assert not dialog.wet.isChecked()
        dialog.wet.setChecked(True); dialog.params['WetAmount'].setValue(.6); dialog.store()
        saved = copy.deepcopy(dialog.overrides)
        assert saved['model']['0']['params']['WetEnabled'] == 1.
    finally:
        dialog.close()
    restored = CharacterPBRDialog(snapshot, {'character_material_overrides': saved})
    try:
        assert restored.wet.isChecked() and restored.params['WetAmount'].value() == .6
        restored.wet.setChecked(False); restored.store()
        assert restored.overrides['model']['0']['params']['WetEnabled'] == 0.
    finally:
        restored.close()


def test_hosiery_preserves_source_visibility_and_generated_fiber_scale(tmp_path):
    from mmd_bridge.character_pbr import detail_bindings
    record = source(tmp_path); record['name'] = 'stocking'
    record['mmd']['diffuse_alpha'] = .9
    record['character_material']['alpha_mode'] = 'translucent'
    planned = plan(record, {}, 'one')
    assert planned['type'] == 'hosiery' and planned['params']['Opacity'] == .9
    assert planned['alpha_mode'] == 'translucent'
    assert detail_bindings('hosiery')['detail_normal']['sha256'] != detail_bindings('cloth')['detail_normal']['sha256']
    assert 'hosiery_mask' in detail_bindings('hosiery')
    planned['generated_defaults'] = {'DetailTiling':133., 'FabricDirection':-75.}
    record['character_material']['pbr'] = planned
    cfg = {'character_material_overrides':{'one':{'0':{'params':{'HosieryCoverage':.3}}}}}
    updated = plan(record, cfg, 'one')
    assert updated['params']['HosieryCoverage'] == .3
    assert updated['params']['DetailTiling'] == 133. and updated['params']['FabricDirection'] == -75.
    assert plan(record, cfg, 'two')['params']['HosieryCoverage'] == .5
    record['mmd']['diffuse_alpha'] = 0.
    hidden = plan(record, cfg, 'one')
    assert hidden['params']['Opacity'] == 0. and hidden['alpha_mode'] == 'masked'


def test_hosiery_gui_controls_persist_without_changing_cloth(monkeypatch, tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app = W.QApplication.instance() or W.QApplication([])
    rows = []
    for i, name in enumerate(('stocking','Top')):
        record = source(tmp_path); record.update(name=name, pmx_index=i)
        rows.append({'name':name,'pmx_index':i,'sphere_mode':0,'diffuse':[1,1,1,1],
                     'pbr':plan(record,{},'model')})
    snapshot = {'sha256':'model','model':'model.pmx','materials':rows,'texture_count':1}
    dialog = CharacterPBRDialog(snapshot,{})
    try:
        assert dialog.form.isRowVisible(dialog.params['HosieryCoverage'])
        dialog.params['HosieryCoverage'].setValue(.3)
        dialog.slots.setCurrentRow(1)
        assert not dialog.form.isRowVisible(dialog.params['HosieryCoverage'])
        dialog.slots.setCurrentRow(0)
        assert dialog.params['HosieryCoverage'].value() == .3
        dialog.store()
        assert dialog.overrides['model']['0']['params']['HosieryCoverage'] == .3
        assert 'HosieryCoverage' not in dialog.overrides['model']['1']['params']
    finally:
        dialog.close()


def test_fabric_rosiness_and_hosiery_thickness_overrides_are_independent(tmp_path):
    record=source(tmp_path); record['name']='stocking'
    record['mmd']['diffuse_alpha']=.9
    cfg={'character_material_overrides':{'one':{'0':{'params':{'HosieryThickness':2.5}}}}}
    thick=plan(record,cfg,'one')
    assert thick['params']['HosieryThickness']==2.5 and thick['params']['Opacity']==.9
    for value in (0.,.35,.65,1.,1.23456,1.85,2.5,3.):
        cfg['character_material_overrides']['one']['0']['params']['HosieryThickness']=value
        assert plan(record,cfg,'one')['params']['HosieryThickness']==value
    assert plan(record,cfg,'two')['params']['HosieryThickness']==1.
    assert plan(dict(record,pmx_index=1),cfg,'one')['params']['HosieryThickness']==1.
    old={'character_material_overrides':{'one':{'0':{'params':{'HosieryCoverage':.7}}}}}
    assert plan(record,old,'one')['params']['HosieryThickness']==1.
    record['name']='Face'
    original=plan(record,{},'one')
    pink=plan(record,{'character_material_overrides':{'one':{'0':{'params':{'SkinRosiness':.5}}}}},'one')
    assert pink['params']['SkinRosiness']==.5
    for key in ('SSSStrength','SSSDetailPreservation','WetEnabled','Opacity'):
        assert pink['params'][key]==original['params'][key]
    for key,value in (('HosieryThickness',3.1),('SkinRosiness',1.1),('FuzzBrightness',-1.)):
        with pytest.raises(ValueError):validate_settings({'params':{key:value}},{})


def test_thickness_presets_and_custom_values_roundtrip(monkeypatch,tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app=W.QApplication.instance() or W.QApplication([])
    rows=[]
    for i,name in enumerate(('stocking','Face','Top')):
        record=source(tmp_path);record.update(name=name,pmx_index=i)
        rows.append({'name':name,'pmx_index':i,'sphere_mode':0,'diffuse':[1,1,1,1],
                     'pbr':plan(record,{},'model')})
    snapshot={'sha256':'model','model':'model.pmx','materials':rows,'texture_count':1}
    # A cached pre-v7 analysis may not contain the new fields yet.
    for row in rows:
        for key in ('HosieryThickness','SkinRosiness','FuzzBrightness','FabricRoughnessVariation'):
            row['pbr']['params'].pop(key)
    settings={'character_material_overrides':{'model':{'0':{'params':{'HosieryThickness':.73125}}}}}
    dialog=CharacterPBRDialog(snapshot,settings)
    try:
        assert dialog.form.isRowVisible(dialog.param_rows['HosieryThickness'])
        assert set(dialog.thickness_presets)=={'薄','中','厚'}
        assert '自定义' in dialog.thickness_value.text()
        dialog.store();assert dialog.overrides['model']['0']['params']['HosieryThickness']==.73125
        dialog.thickness_presets['薄'].click()
        assert dialog.params['HosieryThickness'].value()==.35 and '薄' in dialog.thickness_value.text()
        dialog.thickness_presets['厚'].click()
        assert dialog.params['HosieryThickness'].value()==2.5 and '厚' in dialog.thickness_value.text()
        dialog.thickness_presets['中'].click()
        assert dialog.params['HosieryThickness'].value()==1.
        dialog.slots.setCurrentRow(1)
        assert not dialog.form.isRowVisible(dialog.param_rows['HosieryThickness'])
        assert dialog.form.isRowVisible(dialog.param_rows['SkinRosiness'])
        dialog.params['SkinRosiness'].setValue(.42)
        dialog.slots.setCurrentRow(2)
        assert dialog.form.isRowVisible(dialog.param_rows['FuzzBrightness'])
        assert not dialog.form.isRowVisible(dialog.param_rows['SkinRosiness'])
        dialog.slots.setCurrentRow(0);dialog.params['HosieryThickness'].setValue(0);dialog.store()
        saved=copy.deepcopy(dialog.overrides)
        assert saved['model']['0']['params']['HosieryThickness']==0.
        assert 'Opacity' not in saved['model']['0']['params']
        assert saved['model']['1']['params']['SkinRosiness']==.42
    finally:dialog.close()
    restored=CharacterPBRDialog(snapshot,{'character_material_overrides':saved})
    try:
        assert restored.params['HosieryThickness'].value()==0
        restored.store(); assert restored.overrides['model']['0']['params']['HosieryThickness']==0.
        restored.slots.setCurrentRow(1)
        assert restored.params['SkinRosiness'].value()==.42
    finally:restored.close()


def test_new_gui_default_and_old_job_profile_roundtrip(monkeypatch,tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()
    try:
        combo=window.inputs['character_material_profile']
        assert combo.itemData(0)=='ue_official_toon'
        assert combo.itemData(1)=='character_pbr'
        assert combo.currentData()=='character_pbr'
        data={'character_material_profile':'ZZZ_Render_PMX','character_material_overrides':{
            'one':{'2':{'type':'metal','params':{'Roughness':.3}}}}}
        window.apply_settings(copy.deepcopy(data))
        assert window.settings().character_material_profile=='ZZZ_Render_PMX'
        assert window.settings().character_material_overrides==data['character_material_overrides']
        window.apply_settings({},existing_job=True)
        assert combo.currentData()=='auto'
    finally:
        window.timer.stop();window.close()
