"""Editable Cel reference graphs, adapted only inside an owned task namespace."""
from pathlib import Path
import json
import unreal as u

from .character_cel import VERSION, PREFIX, PRESET, settings_signature
from .character_profiles import fingerprint, binding_signature
from .ue_character_materials import dependency_signature, enable_character_usage


def _signature(m, record):
    return fingerprint([VERSION, settings_signature(m.cfg),
                        binding_signature(record, {'id': PRESET}, PREFIX),
                        dependency_signature(m, u.load_asset(PREFIX+'Character/Materials/Master/M_NPR'))])


def _adapter(m, layer, alpha_mode, switch_defaults=None):
    source = u.load_asset(PREFIX+'Character/Materials/Master/'+{'base': 'M_PBR', 'filter': 'M_NPR', 'outline': 'M_Outline'}[layer])
    if not isinstance(source, u.Material):
        raise RuntimeError('缺少内置卡通母版，请重新连接工程安装资源: '+layer)
    switch_defaults = switch_defaults or {}
    identity = fingerprint([VERSION, layer, alpha_mode, switch_defaults, dependency_signature(m, source)])
    path = m.root+'/Materials/M_Cel_'+layer+'_'+identity[:16]
    result = u.load_asset(path)
    if result:
        m.verify_asset(result)
        if u.EditorAssetLibrary.get_metadata_tag(result, 'MMDBridge.CelAdapter') != identity:
            raise RuntimeError('内置卡通母材质缓存不完整，请使用新的输出目录')
        return result
    result = u.EditorAssetLibrary.duplicate_asset(source.get_path_name(), path)
    if not result:
        raise RuntimeError('内置卡通母材质复制失败')
    m.managed(result)
    m._created_this_pass.add(path)
    m._pending_created.add(path)
    previous = m.material
    m.material = result
    try:
        nodes = {n.get_name(): n for n in m.lib.get_material_expressions(result)}
        for node in nodes.values():
            if isinstance(node, u.MaterialExpressionStaticSwitchParameter):
                name = str(node.get_editor_property('parameter_name'))
                if name in switch_defaults:
                    node.set_editor_property('default_value', bool(switch_defaults[name]))
        base = m.node('TextureSampleParameter2D', parameter_name='PMXBaseTex',
                      texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
        opacity = m.op('Multiply', (base[0], 'A'), m.scalar('PMXOpacity', 1))
        tint = m.node('VectorParameter', parameter_name='PMXDiffuse', default_value=u.LinearColor(1, 1, 1, 0))
        color = m.op('Multiply', (base[0], 'RGB'), (tint[0], 'RGB'))
        if layer == 'base':
            m.lib.connect_material_property(color[0], color[1], u.MaterialProperty.MP_BASE_COLOR)
            # Preserve the reference MSRE driven emission while including PMX tint.
            emission = m.op('Multiply', color, (nodes['MaterialExpressionVectorParameter_0'], 'A'))
            m.lib.connect_material_property(emission[0], emission[1], u.MaterialProperty.MP_EMISSIVE_COLOR)
            result.set_editor_property('blend_mode', {'opaque': u.BlendMode.BLEND_OPAQUE,
                'masked': u.BlendMode.BLEND_MASKED, 'translucent': u.BlendMode.BLEND_TRANSLUCENT}[alpha_mode])
        elif layer == 'filter':
            # Composite the NPR color with its lit base before both receive DOF.
            result.set_editor_property('translucency_pass', u.MaterialTranslucencyPass.MTP_BEFORE_DOF)
            for name in ('MaterialExpressionMaterialFunctionCall_0', 'MaterialExpressionMaterialFunctionCall_1'):
                m.connect(color, (nodes[name], ''), 'In')
            # Preserve the reference view/normal UVs and shadow gating. Only the
            # final MatCap composition is extended for PMX multiply/add semantics.
            mode = m.scalar('PMXSphereMode', 0)
            mc_color = (nodes['MaterialExpressionVectorParameter_5'], '')
            sampled = m.op('Multiply', (nodes['MaterialExpressionTextureSampleParameter2D_0'], 'RGB'), (mc_color[0], 'RGB'))
            mask = (nodes['MaterialExpressionCurveAtlasRowParameter_0'], 'A')
            inputs = []
            for name in ('Base', 'Sphere', 'AddContribution', 'Mask', 'Mode'):
                item = u.CustomInput(); item.set_editor_property('input_name', name); inputs.append(item)
            blend = m.node('Custom', output_type=u.CustomMaterialOutputType.CMOT_FLOAT3, inputs=inputs,
                code='if(Mode<0.5) return Base; if(Mode<1.5) return Base*lerp(float3(1,1,1),Sphere,saturate(Mask)); return Base+AddContribution;')
            for name, value in [('Base', (nodes['MaterialExpressionNamedRerouteUsage_13'], '')),
                                ('Sphere', sampled), ('AddContribution', (nodes['MaterialExpressionMultiply_11'], '')),
                                ('Mask', mask), ('Mode', mode)]:
                m.connect(value, blend, name)
            m.connect(blend, (nodes['MaterialExpressionNamedRerouteDeclaration_5'], ''), 'Input')
        else:
            original = m.lib.get_material_property_input_node(result, u.MaterialProperty.MP_OPACITY_MASK)
            opacity = m.op('Min', (original, ''), opacity)
        for prop in (u.MaterialProperty.MP_OPACITY, u.MaterialProperty.MP_OPACITY_MASK):
            m.lib.connect_material_property(opacity[0], opacity[1], prop)
        for usage in (u.MaterialUsage.MATUSAGE_SKELETAL_MESH, u.MaterialUsage.MATUSAGE_MORPH_TARGETS):
            m.lib.set_material_usage(result, usage)
        m.lib.recompile_material(result)
        u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.CelAdapter', identity)
        assert u.EditorAssetLibrary.save_loaded_asset(result, only_if_is_dirty=False)
        m.protect_asset(result)
        return result
    finally:
        m.material = previous


def _instance(m, record, layer):
    note = record.get('character_material', {})
    cel = note.get('cel')
    if not cel or cel.get('version') != VERSION or cel.get('settings_signature') != settings_signature(m.cfg):
        raise ValueError('内置卡通准备记录与当前选择不匹配，请新建任务')
    parameters = cel['parameters'] if layer == 'filter' else {}
    parent = _adapter(m, layer, note['alpha_mode'], parameters.get('switches', {}))
    name = 'MI_'+record.get('asset_name', record['id'])
    if layer != 'base':
        name += '_Cel_'+layer
    result = m.create(name, u.MaterialInstanceConstant, u.MaterialInstanceConstantFactoryNew(), record=record)
    m.lib.set_material_instance_parent(result, parent)
    scalars = {str(n) for n in m.lib.get_scalar_parameter_names(parent)}
    vectors = {str(n) for n in m.lib.get_vector_parameter_names(parent)}
    textures = {str(n) for n in m.lib.get_texture_parameter_names(parent)}
    for name, value in parameters.get('scalars', {}).items():
        if name in scalars:
            m.lib.set_material_instance_scalar_parameter_value(result, name, float(value))
    for name, value in parameters.get('vectors', {}).items():
        if name in vectors:
            m.lib.set_material_instance_vector_parameter_value(result, name, u.LinearColor(*value))
    mmd = record.get('mmd', {})
    m.lib.set_material_instance_scalar_parameter_value(result, 'PMXOpacity', float(mmd.get('diffuse_alpha', 1)))
    m.lib.set_material_instance_vector_parameter_value(result, 'PMXDiffuse', u.LinearColor(*mmd.get('diffuse_color', [1, 1, 1])[:3], 0))
    spec = record.get('textures', {}).get('base_color')
    base = m.texture(spec) if spec else u.load_asset('/Engine/EngineResources/WhiteSquareTexture')
    m.lib.set_material_instance_texture_parameter_value(result, 'PMXBaseTex', base)
    if 'BaseTex' in textures:
        m.lib.set_material_instance_texture_parameter_value(result, 'BaseTex', base)
    if layer == 'filter':
        cap = cel['matcap']
        texture = (m.texture(record['textures']['sphere']) if cap['source'] == 'pmx' else
                   u.load_asset(PREFIX+'Character/MatCap/HairSpa1_psd') if cap['source'] == 'cel_hair' else
                   u.load_asset('/Engine/EngineResources/Black'))
        m.lib.set_material_instance_texture_parameter_value(result, 'MatCapMap', texture)
        m.lib.set_material_instance_scalar_parameter_value(result, 'PMXSphereMode', float(cap['mode']))
    enable_character_usage(result)
    m.lib.update_material_instance(result)
    u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.CelBinding', _signature(m, record))
    u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.MaterialRoute', PRESET)
    u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.CelRecord', json.dumps(record, ensure_ascii=False))
    assert u.EditorAssetLibrary.save_loaded_asset(result, only_if_is_dirty=False)
    m.protect_asset(result)
    return result


def _report(m, record, instance, reused=False):
    cel = record['character_material']['cel']
    m.assets[record['id']] = instance.get_path_name()
    m.report.setdefault('material_routes', {})[record['id']] = {'route': PRESET, 'role': cel['role'],
        'color_mode': cel['color_mode'], 'matcap': cel['matcap'], 'reused': reused,
        'alpha_mode': record['character_material']['alpha_mode'], 'binding_hash': _signature(m, record)}
    if cel['status'] == 'needs_review':
        m.report.setdefault('needs_review_materials', []).append({'material_id': record['id'],
            'name': record['name'], 'reasons': cel['issues'], 'classification': cel['classification']})


def build(m, record):
    instance = _instance(m, record, 'base')
    _report(m, record, instance)
    return instance


def reuse(m, record, instance):
    if not instance or u.EditorAssetLibrary.get_metadata_tag(instance, 'MMDBridge.CelBinding') != _signature(m, record):
        return False
    m.verify_asset(instance)
    m.verify_asset(instance.get_editor_property('parent'))
    _report(m, record, instance, True)
    return True


def blueprint(m, mesh, records, animation=None, namespace=''):
    actor_class = getattr(u, 'MMDBridgeCelCharacterActor', None)
    if actor_class is None:
        raise RuntimeError('三层卡通角色需要重新编译并部署 MMDBridgeCharacter 插件；旧 DLL 不能使用')
    by_slot = {r['slot_name']: r for r in records}
    ordered = [by_slot[str(s.get_editor_property('imported_material_slot_name'))] for s in mesh.materials]
    filters = [_instance(m, r, 'filter') for r in ordered]
    outlines = [_instance(m, r, 'outline') for r in ordered]
    result = m.create('BP_CelCharacter'+('_'+namespace if namespace else ''), u.Blueprint, _factory(actor_class))
    u.BlueprintEditorLibrary.compile_blueprint(result)
    defaults = u.get_default_object(result.generated_class())
    for name, value in [('character_mesh', mesh), ('main_materials', [s.material_interface for s in mesh.materials]),
                        ('filter_materials', filters), ('outline_materials', outlines),
                        ('main_animation', animation), ('filter_animation', animation), ('outline_animation', animation)]:
        defaults.set_editor_property(name, value)
    for component, materials in [(defaults.skeletal_mesh_component, defaults.main_materials),
                                 (defaults.filter_mesh, filters), (defaults.outline_mesh, outlines)]:
        component.set_skeletal_mesh_asset(mesh)
        component.set_editor_property('override_materials', materials)
        component.override_animation_data(animation, True, True)
        component.set_simulate_physics(False)
        component.set_editor_property('component_use_fixed_skel_bounds', False)
        component.set_editor_property('consider_all_bodies_for_bounds', True)
        component.set_editor_property('bounds_scale', max(1.1, component.get_editor_property('bounds_scale')))
    # The reference outline expands in local position space and uses inverted
    # component scale. Its actual scale is captured with the bundled defaults.
    defaults.outline_mesh.set_relative_scale3d(u.Vector(-1, -1, -1))
    u.BlueprintEditorLibrary.compile_blueprint(result)
    assert u.EditorAssetLibrary.save_loaded_asset(result, only_if_is_dirty=False)
    m.protect_asset(result)
    m.report.setdefault('checks', {})['character_blueprint'] = {'path': result.get_path_name(),
        'meshes': 3, 'animation_playback': 'independent', 'color_mode': m.cfg.get('character_cel_color_mode', 'role'),
        'animation': animation.get_path_name() if animation else None}
    return result


def _factory(actor_class):
    factory = u.BlueprintFactory()
    factory.set_editor_property('parent_class', actor_class)
    return factory
