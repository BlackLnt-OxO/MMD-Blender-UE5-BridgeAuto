import copy
from dataclasses import asdict

import pytest

from mmd_bridge.character_pbr import plan, SKIN_SCATTERING
from mmd_bridge.core import Settings


def record(name='Face', index=0):
    return {'name':name,'pmx_index':index,'mmd':{'diffuse_alpha':1.,'sphere_texture_type':0},
            'character_material':{'alpha_mode':'opaque'},'textures':{}}


@pytest.mark.parametrize('name,kind,expected', [('Face','face',.27),('Body','skin',.35)])
def test_new_import_midpoint_scattering_and_explicit_override(name,kind,expected):
    settings=asdict(Settings())
    result=plan(record(name),settings,'model')
    assert result['skin_scattering']=='balanced_v1'
    assert result['params']['SSSStrength']==expected
    assert result['params']['SkinRosiness']==.3
    settings['character_material_overrides']={'model':{'0':{'params':{'SSSStrength':.73125}}}}
    assert plan(record(name),settings,'model')['params']['SSSStrength']==.73125
    assert plan(record(name),settings,'other')['params']['SSSStrength']==expected


def test_soft_shadow_switch_does_not_change_midpoint_skin_or_other_materials():
    enabled=asdict(Settings());disabled=dict(enabled,character_soft_shadows=False)
    for name in ('Face','Body','Hair','Shirt','stocking','Metal'):
        assert plan(record(name),enabled,'model')==plan(record(name),disabled,'model')
    assert Settings().character_soft_shadows is True


def test_legacy_settings_and_cached_records_keep_original_scattering():
    source=record();old=plan(source,{},'model')
    assert 'skin_scattering' not in old
    assert old['params']['SSSStrength']==.3
    source['character_material']['pbr']=copy.deepcopy(old)
    assert plan(source,{},'model')==old
    legacy=asdict(Settings(character_skin_scattering='legacy_v8'))
    assert plan(record(),legacy,'model')==old


def test_midpoint_is_in_parameter_space_and_distances_stay_independent():
    old=SKIN_SCATTERING['legacy_v8'];mid=SKIN_SCATTERING['balanced_v1']
    for key,right in {'face':.24,'skin':.30}.items():
        assert mid[key]==pytest.approx((old[key]+right)/2)
    for key,right in {'surface_albedo':(.8,.68,.64),'mean_free_path_color':(1.,.78,.70)}.items():
        assert mid[key]==pytest.approx(tuple((a+b)/2 for a,b in zip(old[key],right)))


def test_import_controls_default_off_roundtrip_and_legacy_restore(monkeypatch,tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen');monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setattr(Window,'scan',lambda self:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window()
    try:
        assert window.settings().character_soft_shadows is True
        assert window.settings().character_subdivision_enabled is False
        geometry=window.inputs['character_geometry_mode']
        assert geometry.currentData()=='weld_weighted'
        saved_geometry=asdict(window.settings())
        geometry.setCurrentIndex(geometry.findData('all_subdivision'))
        window.apply_settings(saved_geometry,existing_job=True)
        assert window.settings().character_subdivision_enabled is False
        assert geometry.currentData()=='weld_weighted'
        assert window.settings().character_skin_scattering=='balanced_v1'
        window.inputs['character_soft_shadows'].setChecked(False)
        saved=asdict(window.settings());assert saved['character_soft_shadows'] is False
        window.inputs['character_soft_shadows'].setChecked(True)
        window.apply_settings(saved,existing_job=True)
        assert window.settings().character_soft_shadows is False
        window.apply_settings({},existing_job=True)
        assert window.settings().character_subdivision_enabled is False
        assert window.settings().character_skin_scattering=='legacy_v8'
        window.apply_settings(asdict(Settings()))
        assert window.settings().character_soft_shadows is True
        assert window.settings().character_skin_scattering=='balanced_v1'
        assert window.settings().character_subdivision_enabled is False
        combo=window.inputs['character_material_profile'];combo.setCurrentIndex(combo.findData('generic'))
        assert not window.inputs['character_soft_shadows'].isEnabled()
        combo.setCurrentIndex(combo.findData('character_pbr'))
        assert window.inputs['character_soft_shadows'].isEnabled()
    finally:window.timer.stop();window.close()
