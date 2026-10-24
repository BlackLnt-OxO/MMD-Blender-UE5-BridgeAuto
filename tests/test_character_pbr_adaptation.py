"""User-visible material correction, texture discovery and override isolation."""
import copy
from pathlib import Path

import pytest

from mmd_bridge.character_pbr import classify, plan, inspect_model, review_reasons, analysis_record


@pytest.mark.parametrize('name,expected', [('齒','teeth'), ('Eyehi','eye_highlight'),
    ('EyeHi2','eye_highlight'), ('Trousers','cloth'), ('スウェット','cloth'),
    ('スウェット袖','cloth'), ('スーツ','generic'), ('ShortsMetal','metal'),
    ('スウェット金属','metal'), ('スウェットパーツ(メタル)','metal'),
    ('レザーコート','leather'), ('ゴム底','rubber'), ('ガラス','glass'),
    ('プラスチック','plastic'), ('SuitMetal','metal'), ('EyeHinge','iris')])
def test_verified_aliases_keep_physical_material_priority(name, expected):
    assert classify({'name':name}) == expected


def test_mechanical_context_does_not_force_body_into_skin_or_metal():
    record={'name':'身体','pmx_index':0,'textures':{}}
    automatic=plan(record,{'model':'local/机器人.pmx'},'robot')
    assert automatic['type']=='generic' and review_reasons(automatic)
    assert plan(record,{'model':'local/角色.pmx'},'human')['type']=='skin'
    selected={'model':'local/机器人.pmx','character_material_overrides':{'robot':{'0':{'type':'plastic'}}}}
    assert not review_reasons(plan(record,selected,'robot'))
    assert plan(record,selected,'other')['type']=='generic'
    assert plan(dict(record,name='身体Metal'),{'model':'机器人.pmx'},'robot')['type']=='metal'


def test_eye_defaults_preserve_source_alpha_and_explicit_emission(tmp_path):
    image=tmp_path/'Eye.png';image.write_bytes(b'eye')
    record={'name':'Eye','pmx_index':4,'textures':{'base_color':{'path':str(image)}},
            'mmd':{'diffuse_alpha':.6},'character_material':{'alpha_mode':'translucent'}}
    result=plan(record,{},'one')
    assert result['alpha_mode']=='translucent' and result['params']['Opacity']==.6
    assert result['params']['EmissiveStrength'] == .35
    assert result['params']['ClearCoat']==1 and result['params']['IrisDepth']==0
    settings={'character_material_overrides':{'one':{'4':{'params':{'EmissiveStrength':0}}}}}
    assert plan(record,settings,'one')['params']['EmissiveStrength']==0
    assert plan(record,settings,'other')['params']['EmissiveStrength']>0
    record['name']='EyeWhite'
    assert plan(record,{},'one')['params']['EmissiveStrength'] == .2
    record['name']='Face'
    assert plan(record,{},'one')['params']['EmissiveStrength']==0


def test_restored_sss_floor_keeps_midpoint_skin_and_manual_choices():
    record={'name':'Face','pmx_index':0,'textures':{}}
    settings={'character_skin_scattering':'balanced_v1'}
    result=plan(record,settings,'one')
    assert result['params']['SSSMaskFloor']==.9
    assert result['params']['SSSStrength']==.27 and result['params']['SkinRosiness']==.35
    assert result['defaults_version']==6
    settings['character_material_overrides']={'one':{'0':{'params':{'SSSMaskFloor':.7}}}}
    assert plan(record,settings,'one')['params']['SSSMaskFloor']==.7
    assert plan(record,settings,'other')['params']['SSSMaskFloor']==.9
    record['name']='EyeHighlight'
    assert plan(record,{},'one')['params']['EmissiveStrength']==.3


def test_yellow_review_type_switch_autobinds_and_preserves_manual_maps(monkeypatch,tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets as W,QtCore as C
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app=W.QApplication.instance() or W.QApplication([])
    for name in ('Model.pmx','Suit.png','Suit_Normal.png','Suit_Roughness.png','Custom.png'):
        (tmp_path/name).write_bytes(b'fixture')
    base=tmp_path/'Suit.png';custom=tmp_path/'Custom.png'
    record={'name':'スーツ','pmx_index':11,'textures':{'base_color':{'path':str(base)}},
            'mmd':{'texture_rel_path':'Suit.png','diffuse_alpha':.95}}
    row={'name':'スーツ','pmx_index':11,'pbr':plan(record,{},'one'),'source_record':copy.deepcopy(record),
         'sphere_mode':0,'diffuse':[1,1,1,.95]}
    snapshot={'model':str(tmp_path/'Model.pmx'),'sha256':'one','texture_count':4,'materials':[row]}
    untouched={'two':{'11':{'type':'metal','params':{'Roughness':.2}}}}
    dialog=CharacterPBRDialog(snapshot,{'character_material_overrides':untouched})
    try:
        assert '待确认' in dialog.slots.item(0).text()
        assert dialog.slots.item(0).foreground().color().name()=='#d8b45a'
        assert dialog.texture_fields['normal'][0].placeholderText().endswith('Suit_Normal.png')
        dialog.texture_fields['roughness'][0].setText(str(custom))
        dialog.texture_fields['roughness'][1].setCurrentText('g')
        dialog.texture_fields['roughness'][2].setChecked(True)
        dialog.kind.setCurrentIndex(dialog.kind.findData('cloth'))
        assert '待确认' not in dialog.slots.item(0).text()
        assert 'cloth_normal.png' in dialog.auto_maps.text()
        assert dialog.texture_fields['base_color'][0].placeholderText()==str(base)
        assert dialog.texture_fields['roughness'][0].text()==str(custom)
        assert dialog.texture_fields['roughness'][1].currentText()=='g'
        assert dialog.texture_fields['roughness'][2].isChecked()
        dialog.store();saved=copy.deepcopy(dialog.overrides)
        assert saved['two']==untouched['two']
        assert saved['one']['11']['textures']=={'roughness':{'path':str(custom),'channel':'g','invert':True}}
        assert saved['one']['11']['params']['Roughness']==.84
        dialog.reset_current()
        assert '待确认' in dialog.slots.item(0).text()
    finally:dialog.close()
    restored=CharacterPBRDialog(snapshot,{'character_material_overrides':saved})
    try:
        assert restored.kind.currentData()=='cloth'
        assert '待确认' not in restored.slots.item(0).text()
        assert restored.texture_fields['roughness'][1].currentText()=='g'
    finally:restored.close()


def test_manual_type_does_not_hide_missing_textures(tmp_path):
    record={'name':'unknown','pmx_index':0,'mmd':{'texture_rel_path':'missing.png'},'textures':{}}
    original=plan(record,{},'one')
    row={'name':'unknown','pmx_index':0,'pbr':original}
    reconstructed=analysis_record(row)
    updated=plan(reconstructed,{'character_material_overrides':{'one':{'0':{'type':'cloth'}}}},'one')
    assert updated['status']=='white' and 'missing.png' in '\n'.join(review_reasons(updated))


def test_old_snapshot_keeps_original_texture_when_selecting_type(tmp_path):
    texture=tmp_path/'Old.png';texture.write_bytes(b'old')
    record={'name':'unknown','pmx_index':0,'textures':{'base_color':{'path':str(texture)}}}
    pbr=plan(record,{},'one');pbr.pop('source_textures');pbr.pop('source_references')
    reconstructed=analysis_record({'name':'unknown','pmx_index':0,'pbr':pbr})
    selected=plan(reconstructed,{'character_material_overrides':{'one':{'0':{'type':'cloth'}}}},'one')
    assert selected['textures']['base_color']['path']==str(texture)
    assert selected['textures']['base_color']['sha256']==pbr['textures']['base_color']['sha256']
