import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.ue_project import ensure_render_defaults, RENDERER_DEFAULTS


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16'])
def test_project_defaults_preserve_sections_backup_encoding_and_repeated_use(tmp_path, encoding):
    project = tmp_path / 'Test.uproject'
    project.write_text('{}')
    path = tmp_path / 'Config/DefaultEngine.ini'
    path.parent.mkdir()
    original = ('; 保留注释\r\n[Other]\r\nr.RayTracing=False\r\n'
                '[/Script/Engine.RendererSettings]\r\nr.RayTracing=False\r\n'
                'r.RayTracing=False\r\nr.Custom=17\r\n[Another]\r\nKeep=Yes\r\n').encode(encoding)
    path.write_bytes(original)
    assert ensure_render_defaults(project)
    updated = path.read_bytes()
    text = updated.decode(encoding)
    assert '[Other]\r\nr.RayTracing=False' in text
    assert 'r.Custom=17\r\n' in text
    assert '[Another]\r\nKeep=Yes' in text
    assert text.count('r.RayTracing=True') == 2
    assert all(f'{key}={value}\r\n' in text for key, value in RENDERER_DEFAULTS.items())
    assert path.with_name(path.name + '.mmdb-render-backup').read_bytes() == original
    assert not ensure_render_defaults(project)
    assert path.read_bytes() == updated


def test_new_project_defaults_and_missing_project_boundary(tmp_path):
    project = tmp_path / 'New.uproject'
    with pytest.raises(ValueError):
        ensure_render_defaults(project)
    assert not (tmp_path / 'Config').exists()
    project.write_text('{}')
    assert ensure_render_defaults(project)
    assert not ensure_render_defaults(project)


def native_namespace(module, native, functions=None):
    tree = ast.parse((Path(__file__).parents[1] / 'mmd_bridge' / module).read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and (functions is None or node.name in functions)]
    namespace = {'u': native, '__name__': 'mmd_bridge.ue_appearance'}
    exec(compile(ast.Module(body=selected, type_ignores=[]), module, 'exec'), namespace)
    return namespace


def test_mrq_preset_and_reference_policy_overrides_inherited_expensive_settings(monkeypatch):
    class Deferred:
        def set_is_enabled(self, value): self.enabled = value
    class PathTracer(Deferred): pass
    class PNG:
        def set_is_enabled(self, value): self.enabled = value
    game, aa, console = object(), object(), object()
    values = {'r.CustomUserSetting': 17}
    settings = {game: SimpleNamespace(cinematic_quality_settings=False,
        texture_streaming='Disabled', use_lod_zero=True, disable_hlods=True,
        use_high_quality_shadows=True, override_view_distance_scale=True),
        aa: SimpleNamespace(spatial_sample_count=64, temporal_sample_count=8),
        console: SimpleNamespace(remove_console_variable=lambda name, all_instances=False: values.pop(name, None),
            add_or_update_console_variable=lambda name, value: values.update({name: value}))}
    native = SimpleNamespace(MoviePipelineGameOverrideSetting=game,
        MoviePipelineGameMode=object(),
        MoviePipelineDeferredPassBase=Deferred, MoviePipelineDeferredPass_PathTracer=PathTracer,
        MoviePipelineImageSequenceOutput_PNG=PNG,
        MoviePipelineAntiAliasingSetting=aa,
        MoviePipelineConsoleVariableSetting=console,
        MoviePipelineTextureStreamingMethod=SimpleNamespace(NONE='None'),
        AntiAliasingMethod=SimpleNamespace(AAM_TSR='TSR'))
    import sys
    native.SystemLibrary=SimpleNamespace(get_engine_version=lambda:'5.8.2')
    monkeypatch.setitem(sys.modules,'unreal',native)
    configure = native_namespace('ue_appearance.py', native, {'configure_mrq_defaults'})['configure_mrq_defaults']
    settings[game].set_editor_property = lambda key, value: setattr(settings[game], key, value)
    def add(kind):
        if kind not in settings: settings[kind] = kind()
        return settings[kind]
    config = SimpleNamespace(find_or_add_setting_by_class=add, get_all_settings=lambda: list(settings.values()),
                             remove_setting=lambda setting: settings.pop(type(setting)))
    configure(config, 'lumen')
    assert settings[game].soft_game_mode_override is native.MoviePipelineGameMode
    assert settings[Deferred].enabled and settings[PNG].enabled
    assert settings[game].cinematic_quality_settings
    assert settings[game].texture_streaming == 'None'
    assert not any(getattr(settings[game], key) for key in (
        'use_lod_zero', 'disable_hlods', 'use_high_quality_shadows', 'override_view_distance_scale'))
    assert (settings[aa].spatial_sample_count, settings[aa].temporal_sample_count,
            settings[aa].anti_aliasing_method) == (1, 1, 'TSR')
    assert values['r.TSR.History.ScreenPercentage'] == 100
    assert not settings[game].flush_grass_streaming
    configure(config, 'lumen', 'low')
    assert values['r.Lumen.Reflections.DownsampleFactor'] == 2
    configure(config, 'lumen', 'balanced')
    assert 'r.Lumen.Reflections.DownsampleFactor' not in values
    configure(config, 'lumen', 'quality')
    from mmd_bridge.ue_render_policy import foliage_distance_cvars
    assert values == {'r.CustomUserSetting': 17, 'r.RayTracing.Shadows': 1,
                      'ShowFlag.PathTracing': 0,
                      'r.Lumen.HardwareRayTracing': 1,
                      'r.Lumen.HardwareRayTracing.LightingMode': 1,
                      **foliage_distance_cvars()}
    assert settings[game].flush_grass_streaming
    configure(config, 'lumen', 'compatible')
    assert values['r.Lumen.HardwareRayTracing'] == values['r.RayTracing.Shadows'] == 0
    assert values['r.RayTracing.Geometry.StaticMeshes'] == 0
    configure(config, 'lumen', 'balanced')
    assert values['r.Lumen.HardwareRayTracing'] == values['r.RayTracing.Shadows'] == 1
    assert 'r.RayTracing.Geometry.StaticMeshes' not in values
    assert 'r.RayTracing.ForceAllRayTracingEffects' not in values
    configure(config, 'lumen', 'low')
    settings[aa].spatial_sample_count = 32
    configure(config, 'path_tracing')
    assert Deferred not in settings and settings[PathTracer].enabled and settings[PNG].enabled
    assert settings[aa].spatial_sample_count == 32
    assert values == {'r.CustomUserSetting': 17, 'ShowFlag.PathTracing': 1}
    configure(config, 'lumen')
    assert PathTracer not in settings and settings[Deferred].enabled
    values.update({'r.HeterogeneousVolumes.LightingCache':0,'r.HeterogeneousVolumes.Scalability':3})
    configure(config,'lumen','compatible',fog_occlusion_quality='native_balanced')
    assert values['r.VolumetricFog.InjectRaytracedLights']==1
    assert values['r.RayTracing.Shadows']==0
    assert 'r.HeterogeneousVolumes.LightingCache' not in values
    configure(config,'lumen',fog_occlusion_quality='legacy')
    assert 'r.VolumetricFog.InjectRaytracedLights' not in values
    assert values['r.CustomUserSetting']==17


class Asset:
    def __init__(self, path='', **properties):
        self.path, self.properties = path, properties

    def get_editor_property(self, name):
        return self.properties[name]

    def set_editor_property(self, name, value):
        self.properties[name] = value

    def get_path_name(self):
        return self.path


class Instance(Asset):
    pass


class Texture(Asset):
    pass


def test_foliage_preserves_area_and_alpha_idempotently_without_touching_foreign_or_voxel_meshes():
    root = '/Game/Task'
    parent = Asset(blend_mode='masked', two_sided=True, opacity_mask_clip_value=.33)
    material = Instance(root + '/Materials/MI_Leaf', parent=parent)
    texture = Texture(root + '/Textures/T_Leaf_D', do_scale_mips_for_alpha_coverage=False,
                      alpha_coverage_thresholds=SimpleNamespace(x=0, y=0, z=0, w=.75))
    meshes = []
    for path, enabled, shape in [(root + '/Mesh', True, 'none'),
            (root + '/Voxel', True, 'voxel'), (root + '/Ordinary', False, 'none'),
            ('/Game/User/Mesh', True, 'none')]:
        meshes.append(Asset(path, nanite_settings=Asset(enabled=enabled, shape_preservation=shape),
                            static_materials=[SimpleNamespace(material_interface=material)]))
    assets = {material.path: material, texture.path: texture}
    saves, rebuilds = [], []
    def vector(x, y, z, w):
        return SimpleNamespace(x=x, y=y, z=z, w=w)
    native = SimpleNamespace(MaterialInstanceConstant=Instance, Texture2D=Texture,
        BlendMode=SimpleNamespace(BLEND_MASKED='masked'), load_asset=assets.get, Vector4=vector,
        NaniteShapePreservation=SimpleNamespace(NONE='none', PRESERVE_AREA='area'),
        StaticMeshEditorSubsystem=object(),
        get_editor_subsystem=lambda cls: SimpleNamespace(
            set_nanite_settings=lambda mesh, settings, apply: rebuilds.append(mesh.path)),
        EditorAssetLibrary=SimpleNamespace(save_loaded_asset=lambda asset: saves.append(asset.path) or True))
    namespace = native_namespace('ue_foliage.py', native)
    alpha = {'op': 'texture', 'output': 'Alpha', 'image': {'id': 'leaf', 'asset_name': 'T_Leaf'}}
    limited = {'op': 'math', 'mode': 'MINIMUM', 'a': {'op': 'math', 'mode': 'MAXIMUM',
        'a': alpha, 'b': {'op': 'constant', 'value': 0.0}}, 'b': {'op': 'constant', 'value': 1.0}}
    assert namespace['coverage_images'](limited) == [(alpha['image'], 'alpha')]
    assert namespace['coverage_images']({'op': 'math', 'mode': 'MAXIMUM', 'a': alpha,
        'b': {'op': 'constant', 'value': .5}}) == []
    scene = {'materials': [{'id': 'leaf', 'channels': {'opacity': alpha}}]}
    report = {'assets': {'materials': {'leaf': material.path}}}
    configure = namespace['configure_distance_preservation']
    first = configure(scene, report, root, meshes + meshes)
    assert rebuilds == [root + '/Mesh']
    assert saves == [texture.path, root + '/Mesh']
    assert texture.properties['alpha_coverage_thresholds'].w == .33
    second = configure(scene, report, root, meshes)
    assert not any(item['changed'] for item in second['textures'] + second['meshes'])
    assert len(saves) == 2
    # Upstream images of a compound coverage graph must not be altered.
    scene['materials'][0]['channels']['opacity'] = {'op': 'mix', 'a': alpha, 'b': alpha}
    assert configure(scene, report, root, meshes)['complex_masks_need_review'] == ['leaf']
    assert namespace['coverage_images']({'op': 'texture', 'output': 'Color',
        'image': {'id': 'rgb', 'color_space': 'Non-Color'}}) == []
    # One texture serving two different cutoffs cannot use one mip threshold.
    another = Instance(root + '/Materials/MI_Other', parent=Asset(
        blend_mode='masked', two_sided=True, opacity_mask_clip_value=.5))
    assets[another.path] = another
    report['assets']['materials']['other'] = another.path
    scene['materials'] = [{'id': name, 'channels': {'opacity': alpha}}
                          for name in ('leaf', 'other')]
    result = configure(scene, report, root, meshes)
    assert result['shared_texture_cutoffs_need_review'] == [texture.path]
    assert len(saves) == 2
