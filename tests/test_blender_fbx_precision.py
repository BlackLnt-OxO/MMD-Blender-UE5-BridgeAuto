from types import ModuleType, SimpleNamespace as N
from pathlib import Path
import ast
import sys

import pytest

from mmd_bridge.blender_fbx_precision import animation_scale_precision


@pytest.fixture
def exporter(monkeypatch):
    class Wrapper:
        is_bone = True

        def fbx_object_tx(self, scale, *args, **kwargs):
            return (self.position, self.rotation, scale, self.matrix, self.rotation_matrix)

    package = ModuleType('io_scene_fbx')
    module = ModuleType('io_scene_fbx.fbx_utils')
    module.ObjectWrapper = Wrapper
    monkeypatch.setitem(sys.modules, 'io_scene_fbx', package)
    monkeypatch.setitem(sys.modules, 'io_scene_fbx.fbx_utils', module)
    wrapper = Wrapper()
    wrapper.position, wrapper.rotation, wrapper.matrix, wrapper.rotation_matrix = [object() for _ in range(4)]
    return Wrapper, wrapper


def test_precision_removes_numeric_scale_noise_only_and_restores_exporter(exporter):
    cls, wrapper = exporter
    original = cls.fbx_object_tx
    scale = [1.0000274181, .99999988, 1.0]
    with animation_scale_precision('animation') as report:
        values = wrapper.fbx_object_tx(scale, rest=False)
        assert values[2] == [1., 1., 1.]
        assert scale == [1.0000274181, .99999988, 1.0]
        assert values[0] is wrapper.position and values[1] is wrapper.rotation
        assert values[3] is wrapper.matrix and values[4] is wrapper.rotation_matrix
        assert report['samples_changed'] == 1
        assert report['max_scale_correction'] == pytest.approx(.0000274181)
    assert cls.fbx_object_tx is original


@pytest.mark.parametrize('scale,is_bone', [([1.001, 1., 1.], True), ([-1., 1., 1.], True),
                                         ([0., 1., 1.], True), ([1.00001, 1., 1.], False)])
def test_precision_preserves_scaled_mirrored_hidden_and_object_transforms(exporter, scale, is_bone):
    cls, wrapper = exporter
    wrapper.is_bone = is_bone
    with animation_scale_precision('animation') as report:
        assert wrapper.fbx_object_tx(scale)[2] is scale
        assert report['samples_changed'] == 0


def test_nested_export_and_exception_restore_the_previous_hook(exporter):
    cls, wrapper = exporter
    original = cls.fbx_object_tx
    with pytest.raises(RuntimeError):
        with animation_scale_precision('animation') as outer:
            with animation_scale_precision('animation') as inner:
                assert outer is inner
                wrapper.fbx_object_tx([1., .99999, 1.])
            assert cls.fbx_object_tx is not original
            raise RuntimeError('export failed')
    assert cls.fbx_object_tx is original
    assert outer['samples_changed'] == 1


@pytest.mark.parametrize('kind', ['model', 'camera', None])
def test_other_exports_never_load_or_patch_the_fbx_addon(monkeypatch, kind):
    monkeypatch.setitem(sys.modules, 'io_scene_fbx.fbx_utils', None)
    with animation_scale_precision(kind) as report:
        assert report is None


def _load_function(path, name, scope):
    tree = ast.parse((Path(__file__).parents[1] / path).read_text(encoding='utf-8-sig'))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), path, 'exec'), scope)
    return scope[name]


@pytest.mark.parametrize('manual', [False, True])
def test_automatic_and_manual_export_use_and_record_the_same_precision_rule(exporter, tmp_path, manual):
    cls, wrapper = exporter
    original = cls.fbx_object_tx
    records = []

    def save(*args, **kwargs):
        assert wrapper.fbx_object_tx([1.00002, 1., 1.])[2] == [1., 1., 1.]
        return {'FINISHED'}

    active = N(kinds=['animation'], index=0, finished=False, manual=manual,
               job=N(status=lambda *a, **kw: None), manifest={'files': {}},
               set_kind=lambda kind: None, path_for=lambda kind: tmp_path / 'animation.fbx',
               record=lambda *a, **kw: records.append(kw), complete=lambda: None)
    scope = dict(animation_scale_precision=animation_scale_precision,
                 host=N(ACTIVE=active, export_options=lambda kind: {'bake_anim': True}),
                 original_save=save, advance=lambda: None)
    if manual:
        run = _load_function('mmd_bridge/blender_ui.py', 'tracked_save', scope)
        run(None, None, filepath='animation.fbx', bake_anim=True)
    else:
        scope.update(Path=Path, check_stop=lambda job: None, export_options=lambda kind: {'bake_anim': True},
                     bpy=N(ops=N(export_scene=N(fbx=save))))
        run = _load_function('mmd_bridge/blender_host.py', 'export_all', scope)
        run(active)
    assert cls.fbx_object_tx is original
    assert len(records) == 1
    assert records[0]['transform_precision']['samples_changed'] == 1
