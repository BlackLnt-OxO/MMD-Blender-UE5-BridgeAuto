import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.core import file_hash
from mmd_bridge.material_policy import texture_role


class Properties:
    def __init__(self, **values):
        self.values = values

    def get_editor_property(self, name):
        return self.values[name]

    def set_editor_property(self, name, value):
        self.values[name] = value


def cached_import(tmp_path, *, space='sRGB', normal=False, data=False, hdr=False, modified=False):
    source = tmp_path / ('source.exr' if hdr else 'source.png')
    source.write_bytes(b'unchanged source pixels')
    colors = Properties(encoding_override='none', color_space='authored primaries')
    texture = Properties(srgb=not (normal or data or hdr or space == 'Non-Color'),
                         compression_settings=None, source_color_settings=colors)
    events = []
    native = SimpleNamespace(
        load_asset=lambda path: texture,
        Paths=SimpleNamespace(project_content_dir=lambda: str(tmp_path / 'Content')),
        TextureSourceEncoding=SimpleNamespace(TSE_S_RGB='srgb', TSE_LINEAR='linear'),
        TextureCompressionSettings=SimpleNamespace(TC_HALF_FLOAT='half', TC_HDR='hdr',
            TC_NORMALMAP='normal', TC_VECTOR_DISPLACEMENTMAP='lookup', TC_BC7='bc7', TC_DEFAULT='color'),
        EditorAssetLibrary=SimpleNamespace(
            get_metadata_tag=lambda asset, key: 'owner' if key == 'MMDBridge.Owner' else file_hash(source),
            save_loaded_asset=lambda asset: events.append('save') or True))
    path = Path(__file__).parents[1] / 'mmd_bridge/ue_materials.py'
    cls = next(n for n in ast.parse(path.read_text(encoding='utf8')).body if isinstance(n, ast.ClassDef))
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'texture')
    scope = {'__name__': 'mmd_bridge.ue_materials', '__package__': 'mmd_bridge',
             'u': native, 'Path': Path, 'file_hash': file_hash, 'texture_role': texture_role,
             'prop': lambda obj, key, value: obj.set_editor_property(key, value)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)

    def verify(asset):
        events.append('verify')
        if modified:
            raise RuntimeError('preserve user edits')

    builder = SimpleNamespace(root='/Game/Check', owner='owner', textures={}, texture_hashes={},
        report={}, cfg={}, verify_asset=verify, managed=lambda asset: None,
        protect_asset=lambda asset: events.append('protect'))
    image = {'id': 'source', 'path': str(source), 'color_space': space}
    call = lambda: scope['texture'](builder, image, normal=normal, data=data)
    return call, builder, texture, colors, events, source


@pytest.mark.parametrize('space,normal,data,hdr,encoding', [
    ('sRGB', False, False, False, 'srgb'),
    ('sRGB', False, True, False, 'linear'),
    ('sRGB', True, False, False, 'linear'),
    ('Non-Color', False, False, False, 'linear'),
    ('Linear Rec.709', False, False, False, 'linear'),
    ('sRGB', False, False, True, 'linear'),
])
def test_cached_textures_decode_source_by_semantics_and_keep_primaries(
        tmp_path, space, normal, data, hdr, encoding):
    call, builder, texture, colors, events, source = cached_import(
        tmp_path, space=space, normal=normal, data=data, hdr=hdr)
    digest = file_hash(source)
    assert call() is texture
    assert colors.get_editor_property('encoding_override') == encoding
    assert colors.get_editor_property('color_space') == 'authored primaries'
    assert texture.get_editor_property('srgb') == (encoding == 'srgb')
    assert events == ['verify', 'save', 'protect']
    events.clear()
    builder.textures.clear()
    assert call() is texture
    assert events == ['verify', 'protect']
    assert file_hash(source) == digest


def test_source_encoding_cannot_overwrite_a_later_user_edit(tmp_path):
    call, _, _, colors, events, _ = cached_import(tmp_path, modified=True)
    with pytest.raises(RuntimeError, match='preserve user edits'):
        call()
    assert colors.get_editor_property('encoding_override') == 'none'
    assert events == ['verify']
