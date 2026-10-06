import math
import pytest
from mmd_bridge.scene_appearance import grid_layout, physical_light
from mmd_bridge.core import Settings


def test_static_appearance_units_and_defaults():
    white={'type':'POINT','energy':120,'color':[1,1,1]}
    assert physical_light(white)['intensity'] == pytest.approx(81960)
    red=physical_light(dict(white,color=[2,0,0]))
    assert red['intensity']*red['color'][0] == pytest.approx(81960*2)
    assert red['luminous_flux'] == pytest.approx(81960*2*.2126)
    assert physical_light(dict(white,type='SUN'))['units'] == 'lux'
    spot=physical_light(dict(white,type='SPOT',spot_size=math.pi/3))
    assert spot['intensity']/(2*math.pi*(1-math.cos(math.pi/6))) == pytest.approx(81960/(4*math.pi))
    disk=dict(white,type='AREA',normalize=False,shape='DISK',size=2,size_y=2)
    assert physical_light(disk)['intensity'] == pytest.approx(81960*math.pi)
    assert physical_light(dict(white,meters_per_unit=.01))['intensity'] == pytest.approx(8.196)
    shape,step,origin=grid_layout([[0,0,0],[2,1,.5]],256)
    assert shape == [256,128,64] and step == 2/256 and origin == [step/2]*3
    with pytest.raises(ValueError):
        grid_layout([[0,0,0],[0,1,1]],256)
    cfg=Settings()
    assert cfg.restore_scene_appearance and cfg.volume_resolution==256 and cfg.scene_light_scale==1
