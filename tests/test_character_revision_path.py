from mmd_bridge.material_contract import character_revision_root,surface_revision_root,asset_name_for_path


def test_long_revision_keeps_identity_in_a_short_owned_namespace():
    content='C:/P/Content'
    root='/Game/AutoMMDFullTest_20261021/FailureQA_01/MultiRole_OneInvalid/A_Character_DeepSeek_01'
    first=character_revision_root(root,'Character_01','123456abcdef',content)
    assert first.startswith('/Game/AutoMMDFullTest_20261021/PBRRevisions/C_')
    assert first!=character_revision_root(root+'2','Character_01','123456abcdef',content)
    name=asset_name_for_path(first+'/Materials','M_'+'x'*120,content)
    assert len(name)>=24 and len(first+'/Materials/'+name)<=144
    again=character_revision_root(first,'Character_01','abcdef123456',content)
    assert again.rsplit('/',1)[0]==first.rsplit('/',1)[0]


def test_short_existing_revision_layout_is_retained():
    assert character_revision_root('/Game/MMD/A_01','owner','123456abcdef','C:/P/Content')=='/Game/MMD/A_01/PBRRevisions/R_123456abcdef'


def test_surface_revisions_fit_without_moving_original_scene_assets():
    root='/Game/AutoMMDFullTest_20261021/FailureQA_01/InvalidCamera/DeepSeaStage_White_v03_01'
    result=surface_revision_root(root,'owner','S_20261022_140941_dc96c067','C:/P/Content')
    assert result.startswith(root+'/S_')
    assert result!=surface_revision_root(root+'2','owner','S_20261022_140941_dc96c067','C:/P/Content')
    name=asset_name_for_path(result+'/Materials','M_'+'x'*120,'C:/P/Content')
    assert len(name)>=24 and len(result+'/Materials/'+name)<=144
