from mmd_bridge.material_policy import main_transmission_attempt


def test_only_dense_small_props_are_excluded_by_count():
    assert not main_transmission_attempt(candidate=True,size_m=[.5,.2,.3],usage_count=64)['eligible']
    assert main_transmission_attempt(candidate=True,size_m=[2,.2,.3],usage_count=100)['eligible']
    assert main_transmission_attempt(candidate=True,size_m=[.5,.2,.3],usage_count=63)['eligible']
    assert main_transmission_attempt(candidate=True,size_m=[.5,.2,.3],usage_count=19)['eligible']
    assert main_transmission_attempt(candidate=True)['repeat_limit']==64


def test_foliage_cutout_characters_and_opaque_are_not_upgraded():
    for options in ({'names':['Dead_Leaves']},{'thin_cutout':True},{'character':True}):
        assert not main_transmission_attempt(candidate=True,**options)['eligible']
    assert not main_transmission_attempt(candidate=False)['eligible']
    assert main_transmission_attempt(candidate=True,names=['Glass_Lampshade'])['eligible']


def test_explicit_foliage_does_not_depend_on_name_size_or_repeat_count():
    for count in (1,19,64,1000):
        result=main_transmission_attempt(candidate=True,names=['Material.001'],
            size_m=[20,20,20],usage_count=count,foliage=True)
        assert not result['eligible']
        assert result['reason']=='explicit_foliage_keep_legacy'
