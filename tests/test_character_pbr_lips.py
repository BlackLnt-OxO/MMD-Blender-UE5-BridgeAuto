import copy

import pytest

from mmd_bridge.character_pbr import classify, inherit_lip_face_defaults, plan


@pytest.mark.parametrize('name', ['唇', '嘴唇', 'くちびる', 'リップ', 'Lip', 'Lips', 'UpperLip', 'Mouth_Lips'])
def test_lips_use_face_material(name):
    assert classify({'name': name}) == 'face'


@pytest.mark.parametrize('name,kind', [('口腔', 'mouth'), ('舌', 'mouth'), ('Tongue', 'mouth'),
                                    ('歯', 'teeth'), ('Teeth', 'teeth'),
                                    ('Lipstick', 'generic'), ('LipGloss', 'generic'), ('唇膏', 'generic')])
def test_inner_mouth_and_cosmetics_are_separate(name, kind):
    assert classify({'name': name}) == kind


def record(tmp_path, name, index, cfg, *, alpha=1., texture='face.png'):
    image = tmp_path / texture
    image.write_bytes(texture.encode())
    row = {'name': name, 'pmx_index': index, 'mmd': {'diffuse_alpha': alpha},
           'textures': {'base_color': {'path': str(image)}},
           'character_material': {'alpha_mode': 'masked' if alpha == 0 else 'opaque'}}
    row['character_material']['pbr'] = plan(row, cfg, 'model')
    return row


def test_lip_defaults_inherit_effective_face_values_and_preserve_colour_and_overrides(tmp_path):
    cfg = {'character_skin_scattering': 'balanced_v1', 'character_material_overrides': {'model': {
        '0': {'params': {'SkinRosiness': .42, 'DetailTiling': 17.}},
        '1': {'params': {'WetEnabled': 1.}}}}}
    face = record(tmp_path, 'Face', 0, cfg)
    lips = record(tmp_path, '唇', 1, cfg, texture='lips.png')
    source = face['character_material']['pbr']
    source['generated_defaults'] = {'DetailTiling': 98., 'WetTiling': 7., 'WetRotation': 33.}
    source['params'].update(WetTiling=7., WetRotation=33.)
    before = copy.deepcopy(face)
    inherit_lip_face_defaults([face, lips], cfg, 'model')
    actual = lips['character_material']['pbr']
    assert actual['params'] == dict(source['params'], WetEnabled=1.)
    assert actual['textures']['base_color']['path'].endswith('lips.png')
    assert actual['face_parameter_source']['pmx_index'] == 0
    assert face == before
    edited = plan(lips, cfg, 'model')
    assert edited['params'] == actual['params']
    assert plan(lips, {}, 'model')['skin_scattering'] == 'balanced_v1'
    assert plan(lips, {}, 'other')['params']['SkinRosiness'] == .45


def test_hidden_lips_and_manual_type_are_not_overwritten(tmp_path):
    cfg = {'character_material_overrides': {'model': {'2': {'type': 'mouth'}}}}
    face = record(tmp_path, 'Face', 0, cfg)
    hidden = record(tmp_path, '唇', 1, cfg, alpha=0)
    manual = record(tmp_path, '唇', 2, cfg)
    inherit_lip_face_defaults([face, hidden, manual], cfg, 'model')
    assert hidden['character_material']['pbr']['params']['Opacity'] == 0
    assert hidden['character_material']['pbr']['alpha_mode'] == 'masked'
    assert manual['character_material']['pbr']['type'] == 'mouth'
    assert 'face_parameter_source' not in manual['character_material']['pbr']


def test_ambiguous_face_defaults_are_reported_instead_of_guessed(tmp_path):
    a = record(tmp_path, 'Face0', 0, {})
    b = record(tmp_path, 'Face1', 1, {})
    b['character_material']['pbr']['params']['Roughness'] = .8
    lips = record(tmp_path, 'Lips', 2, {})
    inherit_lip_face_defaults([a, b, lips], {}, 'model')
    pbr = lips['character_material']['pbr']
    assert pbr['classification']['face_defaults'] == 'ambiguous_face_parameters'
    assert pbr['classification']['requires_review']
    assert 'face_parameter_source' not in pbr


def test_lip_cannot_reuse_a_cached_nose_scattering_mask(tmp_path):
    lips = record(tmp_path, '唇', 0, {})
    lips['character_material']['pbr']['textures']['sss_mask'] = {
        'path': str(tmp_path/'old_nose.png'), 'kind': 'generated'}
    updated = plan(lips, {}, 'model')
    assert 'sss_mask' not in updated['textures']


def test_matching_atlas_shares_face_mask_without_generating_a_lip_nose(tmp_path):
    face = record(tmp_path, 'Face', 0, {})
    lips = record(tmp_path, 'Lips', 1, {})
    mask = tmp_path/'face_mask.png'
    mask.write_bytes(b'mask')
    source = face['character_material']['pbr']
    source['textures']['sss_mask'] = {'path': str(mask), 'kind': 'generated'}
    face['textures']['sss_mask'] = {'path': str(mask)}
    inherit_lip_face_defaults([face, lips], {}, 'model')
    actual = lips['character_material']['pbr']
    assert actual['textures']['sss_mask'] == source['textures']['sss_mask']
    assert actual['face_scattering_source']['method'] == 'shared_face_atlas_mask'
    assert plan(lips, {}, 'model')['textures']['sss_mask']['path'] == str(mask)


def test_opaque_lips_can_use_shared_masked_face_parent_but_manual_alpha_wins(tmp_path):
    face = record(tmp_path, 'Face', 0, {})
    face['character_material']['pbr']['alpha_mode'] = 'masked'
    lips = record(tmp_path, 'Lips', 1, {})
    inherit_lip_face_defaults([face, lips], {}, 'model')
    assert lips['character_material']['pbr']['alpha_mode'] == 'masked'
    assert plan(lips, {}, 'model')['alpha_mode'] == 'masked'
    cfg = {'character_material_overrides': {'model': {'1': {'alpha_mode': 'opaque'}}}}
    lips['character_material']['pbr'] = plan(lips, cfg, 'model')
    inherit_lip_face_defaults([face, lips], cfg, 'model')
    assert lips['character_material']['pbr']['alpha_mode'] == 'opaque'
