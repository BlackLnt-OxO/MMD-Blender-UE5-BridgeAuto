from mmd_bridge.material_policy import main_transmission_attempt


def test_only_dense_small_props_are_excluded_by_count():
    assert not main_transmission_attempt(candidate=True,size_m=[.5,.2,.3],usage_count=16)['eligible']
    assert main_transmission_attempt(candidate=True,size_m=[2,.2,.3],usage_count=100)['eligible']
    assert main_transmission_attempt(candidate=True,size_m=[.5,.2,.3],usage_count=15)['eligible']


def test_foliage_cutout_characters_and_opaque_are_not_upgraded():
    for options in ({'names':['Dead_Leaves']},{'thin_cutout':True},{'character':True}):
        assert not main_transmission_attempt(candidate=True,**options)['eligible']
    assert not main_transmission_attempt(candidate=False)['eligible']
    assert main_transmission_attempt(candidate=True,names=['Glass_Lampshade'])['eligible']
