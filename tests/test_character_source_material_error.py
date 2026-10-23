import sys
from types import SimpleNamespace
import pytest
from mmd_bridge.core import Cancelled
from mmd_bridge.blender_character_pbr import analyze


def test_source_error_preserves_hidden_slot_and_original_alpha(tmp_path,monkeypatch):
    # The native Blender exception type is the only runtime dependency here.
    monkeypatch.setitem(sys.modules,'mmd_bridge.blender_materials',SimpleNamespace(MissingTextureError=type('MissingTextureError',(ValueError,),{})))
    source=tmp_path/'model.pmx'
    mm=SimpleNamespace(name_j='hidden',name_e='hidden',alpha=0.,diffuse_color=(.2,.3,.4),
        is_double_sided=True,texture_rel_path='missing.png',sphere_texture_rel_path='',sphere_texture_type='0')
    material=SimpleNamespace(name='hidden',mmd_material=mm)
    record={'id':'source-id','channels':{},'textures':{},'source_name':'hidden'}
    def broken(*args,**kwargs):raise ValueError('source texture missing')
    materials=SimpleNamespace(source=str(source),_active_record=record,records={},analyze=broken)
    actual=analyze(materials,material,25)
    assert actual['pmx_index']==25 and actual['mmd']['diffuse_alpha']==0
    assert actual['mmd']['texture_rel_path']=='missing.png'
    assert actual['material_error']=='source texture missing' and actual['requires_visual_review']
    assert actual['textures']=={} and materials.records['source-id'] is actual
    assert material.name==actual['slot_name']==actual['asset_name']


def test_cancelled_source_analysis_still_stops_the_batch(tmp_path):
    def cancelled(*args,**kwargs):raise Cancelled('user stopped')
    with pytest.raises(Cancelled):
        analyze(SimpleNamespace(analyze=cancelled),SimpleNamespace(name='slot'),0)
