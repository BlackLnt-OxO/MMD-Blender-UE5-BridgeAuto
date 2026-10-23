import copy
import math
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.character_pbr import (parameter_defaults, skin_highlight_profile,
                                      validate_override, plan)


def test_outer_softness_changes_only_the_outer_lobe():
    base=parameter_defaults('skin')
    original=skin_highlight_profile(base)
    wider=skin_highlight_profile(dict(base,SkinHighlightSoftness=1.))
    assert wider['roughness0']==original['roughness0']
    assert wider['lobe_mix']==original['lobe_mix']
    assert original['roughness1']<wider['roughness1']==2.
    collapsed=skin_highlight_profile(dict(base,SkinHighlightSoftness=0.))
    assert collapsed['roughness0']==collapsed['roughness1']


def test_blend_changes_weight_without_moving_either_lobe():
    base=parameter_defaults('face')
    original=skin_highlight_profile(base)
    mixed=skin_highlight_profile(dict(base,SkinHighlightBlend=.85))
    assert mixed['roughness0']==original['roughness0']
    assert mixed['roughness1']==original['roughness1']
    assert mixed['lobe_mix']==.85


def test_profile_ranges_fit_native_engine_limits_at_all_control_corners():
    for width in (.5,2.):
        for softness in (0.,1.):
            for blend in (.1,.9):
                profile=skin_highlight_profile({'SkinHighlightWidth':width,
                    'SkinHighlightSoftness':softness,'SkinHighlightBlend':blend})
                assert .5<=profile['roughness0']<=profile['roughness1']<=2.
                assert .1<=profile['lobe_mix']<=.9


@pytest.mark.parametrize('params',[{'SkinHighlightWidth':.49},
    {'SkinHighlightSoftness':1.01},{'SkinHighlightBlend':.91},
    {'SkinHighlightWidth':math.nan}])
def test_invalid_highlight_parameters_are_rejected(params):
    with pytest.raises(ValueError):validate_override({'params':params})


def test_gui_controls_roundtrip_by_source_and_slot(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets as W
    from mmd_bridge.character_pbr_dialog import CharacterPBRDialog
    app=W.QApplication.instance() or W.QApplication([])
    rows=[]
    for index,name in enumerate(('Body','Shirt')):
        record={'name':name,'pmx_index':index,'mmd':{'diffuse_alpha':1.},'textures':{}}
        rows.append(dict(record,pbr=plan(record,{},'model')))
    snapshot={'model':'missing-fixture.pmx','sha256':'model','materials':rows,'texture_count':0}
    dialog=CharacterPBRDialog(snapshot,{})
    try:
        dialog.select(0)
        dialog.alpha.setCurrentIndex(dialog.alpha.findData('translucent'))
        assert not dialog.form.isRowVisible(dialog.param_rows['SkinHighlightWidth'])
        dialog.alpha.setCurrentIndex(dialog.alpha.findData('opaque'))
        for key,value in {'SkinHighlightWidth':1.4,'SkinHighlightSoftness':.8,'SkinHighlightBlend':.75}.items():
            assert dialog.form.isRowVisible(dialog.param_rows[key])
            dialog.params[key].setValue(value)
        dialog.select(1)
        assert not dialog.form.isRowVisible(dialog.param_rows['SkinHighlightWidth'])
        saved=copy.deepcopy(dialog.overrides)
    finally:dialog.close()
    restored=CharacterPBRDialog(snapshot,{'character_material_overrides':saved})
    try:
        restored.select(0)
        assert restored.params['SkinHighlightWidth'].value()==1.4
        assert restored.params['SkinHighlightSoftness'].value()==.8
        assert restored.params['SkinHighlightBlend'].value()==.75
        other=plan(rows[0],{'character_material_overrides':saved},'other')
        assert other['params']['SkinHighlightWidth']==.6
    finally:restored.close()


def test_profile_guard_protects_manual_bindings_and_checks_asset_changes():
    path=Path(__file__).parents[1]/'mmd_bridge/ue_character_pbr.py'
    tree=ast.parse(path.read_text(encoding='utf8'))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='verify_skin_profile')
    scope={'u':SimpleNamespace(EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda *a:'/Game/Slot/Profile.Profile'))}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),scope)
    profile=SimpleNamespace(get_path_name=lambda:'/Game/Slot/Profile.Profile')
    values={'override_subsurface_profile':True,'subsurface_profile':profile}
    instance=SimpleNamespace(get_editor_property=values.get)
    verified=[];manager=SimpleNamespace(verify_asset=verified.append)
    scope['verify_skin_profile'](manager,instance)
    assert verified==[profile]
    values['override_subsurface_profile']=False
    with pytest.raises(RuntimeError,match='用户修改'):scope['verify_skin_profile'](manager,instance)
    assert verified==[profile]
