"""Thin BSDF unit/closure semantics; native rendering covers visual compatibility."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def thin_method():
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_materials.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Materials')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='thin_transmission')
    ns={'u':SimpleNamespace(MaterialSubSurfaceType=SimpleNamespace(
        MSS_DIFFUSION='diffusion',MSS_TWO_SIDED_WRAP='two_sided_wrap')),
        'prop':lambda obj,key,value:obj.__setitem__(key,value)}
    exec(compile(ast.Module(body=[method],type_ignores=[]),'thin_transmission','exec'),ns)
    return ns['thin_transmission']


@pytest.mark.parametrize('mode',['two_sided_wrap','legacy_diffusion'])
def test_thin_color_units_and_explicit_fallback(mode):
    nodes=[];links=[];slab=({},'');color=('color','')
    builder=SimpleNamespace(cfg={'thin_surface_scattering':mode},
        node=lambda kind:(nodes.append(kind) or kind,''),
        op=lambda kind,value:(kind,value),connect=lambda *args:links.append(args))
    thin_method()(builder,slab,color)
    if mode=='two_sided_wrap':
        assert slab[0]['sub_surface_type']=='two_sided_wrap'
        assert nodes==['SubstrateTransmittanceToMFP']
        assert links[-1][0]==('SubstrateTransmittanceToMFP','')
        assert links[-1][2]=='SSS MFP'
        assert all(link[0]!=color for link in links)
    else:
        assert slab[0]['sub_surface_type']=='diffusion'
        assert not nodes and links==[(color,slab,'SSS MFP')]


def test_invalid_scattering_mode_cannot_silently_change_semantics():
    with pytest.raises(ValueError,match='散射模式'):
        thin_method()(SimpleNamespace(cfg={'thin_surface_scattering':'unknown'}),({},''),('color',''))


def test_settings_keep_performance_fallback_and_reject_unknown_mode():
    from dataclasses import asdict
    from mmd_bridge.core import Settings
    cfg=Settings()
    assert asdict(cfg)['thin_surface_scattering']=='legacy_diffusion'
    cfg.thin_surface_scattering='unknown'
    with pytest.raises(ValueError,match='未知叶片透光方案'):
        cfg.validate()
