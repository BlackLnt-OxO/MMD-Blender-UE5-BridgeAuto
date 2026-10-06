import pytest
from mmd_bridge.ue_render_policy import memory_cvars


def test_budget_profiles_preserve_lighting_geometry_and_output_resolution():
    for profile in ('balanced', 'low', 'quality'):
        values = memory_cvars(profile)
        # These budgets must not turn off tracing, thin the forest or reduce output resolution.
        assert not set(values).intersection({'r.RayTracing', 'r.RayTracing.Shadows',
            'r.Lumen.HardwareRayTracing', 'r.Lumen.HardwareRayTracing.LightingMode',
            'foliage.DensityScale', 'grass.DensityScale', 'r.ScreenPercentage', 'r.MipMapLODBias'})
    balanced, low = memory_cvars(), memory_cvars('low')
    assert low['r.Streaming.PoolSize'] < balanced['r.Streaming.PoolSize']
    assert low['r.LumenScene.SurfaceCache.AtlasSize'] < balanced['r.LumenScene.SurfaceCache.AtlasSize']
    assert 'r.Lumen.Reflections.DownsampleFactor' not in balanced
    assert balanced['r.Streaming.MipBias'] == 0
    balanced['r.Streaming.PoolSize'] = 1
    assert memory_cvars()['r.Streaming.PoolSize'] == 2048
    with pytest.raises(ValueError):
        memory_cvars('invalid')


def test_leaf_detail_range_retains_bounded_raytracing_and_pool_budget():
    from mmd_bridge.ue_render_policy import foliage_distance_cvars
    near, far = foliage_distance_cvars(1), foliage_distance_cvars(2)
    assert far['r.StaticMeshLODDistanceScale'] < near['r.StaticMeshLODDistanceScale']
    assert far['r.Streaming.Boost'] > near['r.Streaming.Boost']
    assert far['foliage.MinimumScreenSize'] == near['foliage.MinimumScreenSize'] / 4
    assert far['r.RayTracing.Geometry.InstancedStaticMeshes.Culling'] == 1
    assert far['r.RayTracing.Geometry.InstancedStaticMeshes.CullClusterRadius'] == 20000
    assert 'r.Streaming.PoolSize' not in far
    assert not set(far).intersection({'foliage.DensityScale','r.RayTracing.Shadows','r.RayTracing'})
    for invalid in (0, 5, float('nan')):
        with pytest.raises(ValueError):
            foliage_distance_cvars(invalid)


def test_standalone_render_uses_one_game_world_and_retains_streaming(tmp_path):
    from mmd_bridge.ue_render_policy import standalone_command
    executable = tmp_path / 'Engine with spaces/UnrealEditor.exe'
    executable.parent.mkdir()
    executable.with_name('UnrealEditor-Cmd.exe').touch()
    project = tmp_path / 'My Project.uproject'
    project.touch()
    cfg = {'project': str(project), 'unreal': str(executable)}
    report = {'assets': {'level': '/Game/Task/Maps/Preview',
                         'sequence': '/Game/Task/Sequences/LS.LS'}}
    command = standalone_command(cfg, report, '/Game/Task/Materials/MRQ_Balanced')
    assert command[:3] == [str(executable.with_name('UnrealEditor-Cmd.exe')), str(project), '/Game/Task/Maps/Preview']
    assert '-game' in command and '-RenderOffscreen' in command
    assert '-notexturestreaming' not in command and '-NullRHI' not in command
    assert '-MoviePipelineConfig=/Game/Task/Materials/MRQ_Balanced' in command
    with pytest.raises(ValueError):
        standalone_command(cfg, report, '/Game/Other/Materials/UserPreset')
    compatible = standalone_command(cfg, report, '/Game/Task/Materials/MRQ_Compatible', 'compatible')
    assert '-ini:Engine:[/Script/Engine.RendererSettings]:r.RayTracing=False,r.GenerateMeshDistanceFields=True' in compatible
    assert not any('RendererSettings' in item or 'SystemSettings' in item for item in command)
    assert any('DevOptions.Shaders' in item for item in command)


def test_compatible_path_disables_hardware_builds_but_preserves_visible_content():
    from mmd_bridge.ue_render_policy import lighting_cvars
    values = memory_cvars('compatible')
    assert values['r.Lumen.HardwareRayTracing'] == 0
    assert values['r.RayTracing.Geometry.StaticMeshes'] == 0
    assert values['r.RayTracing.Geometry.InstancedStaticMeshes'] == 0
    assert values['r.Shadow.Virtual.Enable'] == 1
    assert not set(values).intersection({'foliage.DensityScale', 'grass.DensityScale', 'r.ScreenPercentage'})
    assert lighting_cvars('balanced')['r.RayTracing.Shadows'] == 1
    assert memory_cvars('low')['r.RayTracing.DynamicGeometry.MaxUpdatePrimitivesPerFrame'] > 0
