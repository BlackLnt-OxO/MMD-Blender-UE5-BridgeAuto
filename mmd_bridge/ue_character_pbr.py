"""Editable native UE character PBR material family and isolated instances."""
from pathlib import Path
import math
import json

import unreal as u

from .character_pbr import VERSION, PARAMETERS, detail_map_names, SKIN_SCATTERING
from .character_profiles import fingerprint
from .core import file_hash, resource_root, stable_id
from .ue_character_materials import enable_character_usage


def configure_soft_shadows(actor_sub, tag, report, targets=(), *, has_scene=False):
    """Configure only lights in this newly generated import world."""
    checks = report.setdefault('checks', {})
    rows = []
    if has_scene:
        for actor in actor_sub.get_all_level_actors():
            if 'MMDBridge' not in [str(value) for value in actor.tags]:
                continue
            for component in actor.get_components_by_class(u.LightComponent):
                if isinstance(component, u.DirectionalLightComponent):
                    values = {'light_source_angle': 7.}
                elif isinstance(component, u.RectLightComponent):
                    values = {'source_width': 30., 'source_height': 30.}
                elif isinstance(component, u.PointLightComponent):
                    values = {'source_radius': 3.}
                else:
                    continue
                before = {key: float(component.get_editor_property(key)) for key in values}
                for key, minimum in values.items():
                    component.set_editor_property(key, max(before[key], minimum))
                rows.append({'actor': actor.get_path_name(), 'component': component.get_path_name(),
                             'before': before, 'after': {key: float(component.get_editor_property(key)) for key in values},
                             'intensity_preserved': float(component.get_editor_property('intensity'))})
        checks['character_soft_shadows'] = {'enabled': True, 'mode': 'soften_imported_lights', 'lights': rows,
                                            'source_intensities_and_transforms_preserved': True}
        return
    centers = []
    for component, character in targets:
        bone = next((row['exported'] for row in character.get('bones', []) if row['original'] in ('頭','头','head','Head')), None)
        centers.append(component.get_socket_location(bone) if bone else component.get_owner().get_actor_location()+u.Vector(0,0,140))
    target = (sum(centers, u.Vector()) / len(centers) if centers else u.Vector(0,0,140)) + u.Vector(0,0,-1)
    sun = tag(actor_sub.spawn_actor_from_class(u.DirectionalLight, u.Vector(0,0,300)), 'Preview Soft Key')
    sun.set_actor_rotation(u.Rotator(pitch=-25., yaw=145., roll=0.), False)
    sun.light_component.set_editor_property('light_source_angle', 7.)
    sun.light_component.set_intensity(6.)
    sky = tag(actor_sub.spawn_actor_from_class(u.SkyLight, u.Vector()), 'Preview Soft Ambient')
    sky.light_component.set_editor_property('source_type', u.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    sky.light_component.set_editor_property('cubemap', u.load_asset('/Engine/MapTemplates/Sky/DaylightAmbientCubemap'))
    sky.light_component.set_editor_property('real_time_capture', False)
    sky.light_component.set_intensity(.65)
    location = target + u.Vector(100., -66.67, 25.)
    fill = tag(actor_sub.spawn_actor_from_class(u.RectLight, location), 'Preview Soft Fill')
    fill.set_actor_rotation(u.MathLibrary.find_look_at_rotation(location, target), False)
    component = fill.light_component; component.set_mobility(u.ComponentMobility.MOVABLE)
    span = max((abs(center.y-target.y) for center in centers), default=0.)
    width = max(100., span*2.)
    for key, value in {'intensity_units': u.LightUnits.LUMENS, 'intensity': 12.*width/100.,
                       'source_width': width, 'source_height': 140., 'attenuation_radius': max(1000., span*3.)}.items():
        component.set_editor_property(key, value)
    checks['character_soft_shadows'] = {'enabled': True, 'mode': 'character_preview_rig',
        'sun_lux': 6., 'source_angle_degrees': 7., 'sky_intensity': .65,
        'fill_lumens': 12.*width/100., 'fill_size_cm': [width, 140.],
        'actors': [actor.get_path_name() for actor in (sun, sky, fill)]}


def signature(record):
    pbr = record.get('character_material', {}).get('pbr', {})
    hashes = {key: file_hash(spec['path']) for key, spec in record.get('textures', {}).items()
              if spec.get('path') and Path(spec['path']).is_file()}
    values = [VERSION, pbr, record.get('mmd', {}), hashes, 'explicit_color_compression_1']
    if pbr.get('type') == 'glass' and pbr.get('alpha_mode') == 'translucent':
        values.append('explicit_refraction_ior_1')
    return fingerprint(values)


def _lerp(m, a, b, alpha):
    node = m.node('LinearInterpolate')
    for source, pin in ((a, 'A'), (b, 'B'), (alpha, 'Alpha')):
        m.connect(source, node, pin)
    return node


def _vector(m, name, values):
    return m.node('VectorParameter', parameter_name=name,
                  default_value=u.LinearColor(*(list(values)+[1])[:4]))


def _sample(m, semantic, *, normal=False, data=False, coordinates=None, builtin=None):
    if builtin:
        path = resource_root()/'resources/character_materials/Character_PBR'/builtin
        texture = m.texture({'id': stable_id(path.name, file_hash(path)), 'path': str(path),
                             'asset_name': 'T_PBR_'+path.stem,
                             'color_space': 'Non-Color'}, normal=normal, data=data)
    elif data:
        if not hasattr(m, '_pbr_neutral_data'):
            m._pbr_neutral_data = m.texture({'id': 'PBR_NeutralData_v1',
                'path': str(resource_root()/'resources/character_materials/Character_PBR/neutral_data.png'),
                'color_space': 'Non-Color'}, data=True)
        texture = m._pbr_neutral_data
    elif normal:
        # Engine DefaultNormal is a textured relief map, not a neutral normal.
        if not hasattr(m, '_pbr_flat_normal'):
            m._pbr_flat_normal = m.texture({'id': 'PBR_FlatNormal_v2',
                'path': str(resource_root()/'resources/character_materials/Character_PBR/flat_normal.png'),
                'color_space': 'Non-Color'}, normal=True)
        texture = m._pbr_flat_normal
    else:
        texture = u.load_asset('/Engine/EngineResources/WhiteSquareTexture')
    node = m.node('TextureSampleParameter2D', parameter_name=semantic+'_Tex', texture=texture,
                  sampler_type=(u.MaterialSamplerType.SAMPLERTYPE_NORMAL if normal else
                                u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR if data else u.MaterialSamplerType.SAMPLERTYPE_COLOR))
    if coordinates:
        m.connect(coordinates, node, 'Coordinates')
    return node


def _channel(m, sample, semantic):
    rgb = m.op('DotProduct', (sample[0], 'RGB'), _vector(m, semantic+'_Channel', [1, 0, 0, 0]))
    value = _lerp(m, rgb, (sample[0], 'A'), m.scalar(semantic+'_UseAlpha', 0))
    return _lerp(m, value, m.op('OneMinus', value), m.scalar(semantic+'_Invert', 0))


def _property(m, source, pin):
    shader = getattr(m, '_pbr_shader', None)
    inputs = {'MP_BASE_COLOR': 'BaseColor', 'MP_METALLIC': 'Metallic',
              'MP_SPECULAR': 'Specular', 'MP_ROUGHNESS': 'Roughness',
              'MP_NORMAL': 'Normal', 'MP_TANGENT': 'Tangent', 'MP_ANISOTROPY': 'Anisotropy',
              'MP_EMISSIVE_COLOR': 'EmissiveColor', 'MP_SUBSURFACE_COLOR': 'SubSurfaceColor',
              'MP_CUSTOM_DATA0': 'ClearCoat', 'MP_CUSTOM_DATA1': 'ClearCoatRoughness',
              'MP_OPACITY': 'Opacity'}
    if shader and pin in inputs:
        target = inputs[pin]
        if shader[0].get_editor_property('shading_model_override') == u.MaterialShadingModel.MSM_CLOTH:
            target = {'MP_CUSTOM_DATA0': 'Fuzz Amount', 'MP_SUBSURFACE_COLOR': 'Fuzz Color'}.get(pin, target)
        m.connect(source, shader, target)
        return
    if not m.lib.connect_material_property(source[0], source[1], getattr(u.MaterialProperty, pin)):
        raise RuntimeError('角色PBR输出连接失败: ' + pin)


def _profile(m, kind, scattering='legacy_v8'):
    name = 'SSP_Character_PBR_'+kind+'_v'+str(VERSION)
    if scattering != 'legacy_v8':
        name += '_Balanced1'
    path = m.root+'/Materials/'+name
    profile = u.load_asset(path)
    if profile:
        m.verify_asset(profile)
        return profile
    profile = m.create(name, u.SubsurfaceProfile, u.SubsurfaceProfileFactory())
    settings = profile.get_editor_property('settings')
    preset = SKIN_SCATTERING[scattering]
    for key, value in {'surface_albedo': u.LinearColor(*preset['surface_albedo'], 1),
                       'mean_free_path_color': u.LinearColor(*preset['mean_free_path_color'], 1),
                       # Explicit flags prevent legacy profile upgrade from replacing
                       # these distances on first render or reload. Units are cm.
                       'enable_burley': True, 'enable_mean_free_path': True,
                       'mean_free_path_distance': .18 if kind == 'face' else .32,
                       'world_unit_scale': 1.,
                       'roughness0': .75, 'roughness1': 1.2, 'lobe_mix': .7}.items():
        settings.set_editor_property(key, value)
    profile.set_editor_property('settings', settings)
    u.EditorAssetLibrary.save_loaded_asset(profile)
    m.protect_asset(profile)
    return profile


def _rotate_xy(m, value, turns):
    cosine, sine = m.node('Cosine'), m.node('Sine')
    m.connect(turns, cosine, 'Input'); m.connect(turns, sine, 'Input')
    x = m.op('DotProduct', value, m.constant([1, 0]))
    y = m.op('DotProduct', value, m.constant([0, 1]))
    pair = m.node('AppendVector')
    m.connect(m.op('Subtract', m.op('Multiply', x, cosine), m.op('Multiply', y, sine)), pair, 'A')
    m.connect(m.op('Add', m.op('Multiply', x, sine), m.op('Multiply', y, cosine)), pair, 'B')
    return pair


def _face_scattering(m):
    """Use a smooth source-space mask; per-triangle derivatives cause faceting."""
    sample = _channel(m, _sample(m, 'sss_mask', data=True), 'sss_mask')
    strength = m.op('Multiply', m.scalar('Use_sss_mask', 0), m.scalar('SSSDetailPreservation', 1.))
    mask = _lerp(m, m.constant(1), sample, strength)
    return m.op('Multiply', m.scalar('SSSStrength', .3), mask)


def _hosiery_opacity(m, opacity, uv):
    # Keep the angle-dependent knit at the standard thickness, then scale its
    # coverage linearly. Putting thickness in the transmission exponent left
    # thick presets visibly sheer in front and capped by the source PMX alpha.
    knit_uv = m.op('Multiply', uv, m.scalar('DetailTiling', 135))
    sample = _sample(m, 'hosiery_mask', data=True, coordinates=knit_uv, builtin='hosiery_mask.png')
    density = m.scalar('HosieryCoverage', .5)
    amplitude = m.op('Multiply', density, m.op('Subtract', m.constant(1), density))
    variation = m.op('Multiply', m.op('Subtract', (sample[0], 'R'), m.constant(.5)),
                     m.op('Multiply', amplitude, m.constant(.32)))
    coverage = m.op('Saturate', m.op('Add', density, variation))
    cosine = m.op('Abs', m.op('DotProduct', m.node('VertexNormalWS'), m.node('CameraVectorWS')))
    path = m.op('Divide', m.constant(1), m.op('Max', cosine, m.constant(.15)))
    path = _lerp(m, m.constant(1), path, m.scalar('HosieryEdgeDensity', 1))
    transmission = m.node('Power')
    m.connect(m.op('Max', m.op('Subtract', m.constant(1), coverage), m.constant(1e-5)), transmission, 'Base')
    m.connect(path, transmission, 'Exp')
    thickness = m.scalar('HosieryThickness', 1)
    for key, value in {'group': u.Name('Hosiery'), 'slider_min': 0., 'slider_max': 3.,
                       'sort_priority': 0,
                       'desc': 'Continuous thickness 0-3. Linear opacity scale, clamped at full coverage; 0 hides the stocking layer.'}.items():
        thickness[0].set_editor_property(key, value)
    coverage = m.op('Multiply', opacity, m.op('Subtract', m.constant(1), transmission))
    # Source alpha remains multiplicative, including deliberately hidden slots.
    return m.op('Saturate', m.op('Multiply', coverage, thickness))


def master(m, record, white=False):
    pbr = record['character_material']['pbr']
    kind = 'white' if white else pbr['type']
    blend = pbr.get('alpha_mode', 'opaque')
    side = bool(record.get('mmd', {}).get('two_sided', True))
    key = f'{kind}_{blend}_{"Two" if side else "One"}_v{VERSION}'
    if kind in ('skin', 'face') and pbr.get('skin_scattering', 'legacy_v8') != 'legacy_v8':
        key += '_Balanced1'
    if kind == 'glass' and blend == 'translucent':
        key += '_IOR1'
    name = 'M_CharacterPBR_'+key
    path = m.root+'/Materials/'+name
    material = u.load_asset(path)
    if material:
        if u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.Owner') != m.owner:
            raise RuntimeError('PBR母材质归属冲突: ' + path)
        if path in m._created_this_pass:
            if u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.BuildState') != 'complete':
                raise RuntimeError('本轮PBR母材质构建未完成: ' + path)
            return material
        m.verify_asset(material)
        return material
    material = m.create(name, u.Material, u.MaterialFactoryNew())
    previous = m.material
    previous_shader = getattr(m, '_pbr_shader', None)
    m.material = material
    try:
        # Substrate exposes clear-coat/cloth inputs directly. The corresponding
        # legacy CustomData MaterialProperty values are hidden from UE Python.
        shading = u.MaterialShadingModel.MSM_DEFAULT_LIT
        if not white and blend != 'translucent':
            if kind in ('skin', 'face'):
                shading = u.MaterialShadingModel.MSM_SUBSURFACE_PROFILE
            elif kind in ('iris', 'sclera', 'eye_highlight', 'plastic', 'glass'):
                shading = u.MaterialShadingModel.MSM_CLEAR_COAT
            elif kind == 'cloth':
                shading = u.MaterialShadingModel.MSM_CLOTH
        shader = m.node('SubstrateShadingModels', shading_model_override=shading)
        m._pbr_shader = shader
        material.set_editor_property('blend_mode', {'opaque': u.BlendMode.BLEND_OPAQUE,
            'masked': u.BlendMode.BLEND_MASKED, 'translucent': u.BlendMode.BLEND_TRANSLUCENT}[blend])
        material.set_editor_property('two_sided', side)
        material.set_editor_property('shading_model', u.MaterialShadingModel.MSM_DEFAULT_LIT)
        if blend == 'translucent':
            material.set_editor_property('translucency_lighting_mode', u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
            if kind == 'glass':
                material.set_editor_property('refraction_method', u.RefractionMode.RM_INDEX_OF_REFRACTION)
        base = _sample(m, 'base_color')
        uv = m.node('TextureCoordinate', coordinate_index=0)
        # Iris parallax is gated by an explicitly supplied iris mask.
        if kind == 'iris':
            iris = _channel(m, _sample(m, 'iris_mask', data=True), 'iris_mask')
            camera = m.node('CameraVectorWS')
            tangent = m.node('Transform', transform_source_type=u.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_WORLD,
                             transform_type=u.MaterialVectorCoordTransform.TRANSFORM_TANGENT)
            m.connect(camera, tangent, 'Input')
            xy = m.node('ComponentMask', r=True, g=True)
            m.connect(tangent, xy, 'Input')
            offset = m.op('Multiply', xy, m.op('Multiply', iris, m.scalar('IrisDepth', 0)))
            m.connect(m.op('Subtract', uv, offset), base, 'Coordinates')
        color = m.op('Multiply', (base[0], 'RGB'), _vector(m, 'BaseColorTint', [1, 1, 1, 1]))
        opacity = m.op('Multiply', (base[0], 'A'), m.scalar('Opacity', 1))
        separate = _channel(m, _sample(m, 'opacity', data=True), 'opacity')
        opacity = m.op('Multiply', opacity, _lerp(m, m.constant(1), separate, m.scalar('Use_opacity', 0)))
        if kind == 'hosiery' and blend == 'translucent':
            opacity = _hosiery_opacity(m, opacity, uv)
        _property(m, opacity, 'MP_OPACITY_MASK' if blend == 'masked' else 'MP_OPACITY')
        if white:
            _property(m, m.constant([1, 1, 1]), 'MP_BASE_COLOR')
            _property(m, m.constant(.8), 'MP_ROUGHNESS')
        else:
            detail_names = detail_map_names(kind)
            wet = kind in ('skin', 'face')
            wet_alpha = wet_control = wet_normal = wet_rotation = None
            if wet:
                wet_control = m.op('Multiply', m.scalar('WetEnabled', 0), m.scalar('WetAmount', 1.))
                wet_rotation = m.op('Multiply', m.scalar('WetRotation', 0), m.constant(1/360))
                wet_uv = _rotate_xy(m, m.op('Multiply', uv, m.scalar('WetTiling', 8)), wet_rotation)
                water = _sample(m, 'wet_mask', data=True, coordinates=wet_uv, builtin='wet_mask.png')
                mask = (water[0], 'R')
                # A thin film is subtle; individual drops and trails stay smooth.
                wet_alpha = m.op('Multiply', wet_control,
                    m.op('Add', m.constant(.5), m.op('Multiply', mask, m.constant(.5))))
                wet_normal = _sample(m, 'wet_normal', normal=True, coordinates=wet_uv, builtin='wet_normal.png')
            # MatCap samples the view-space final normal; it remains optional.
            view = m.node('Transform', transform_source_type=u.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_WORLD,
                          transform_type=u.MaterialVectorCoordTransform.TRANSFORM_VIEW)
            m.connect(m.node('PixelNormalWS'), view, 'Input')
            xy = m.node('ComponentMask', r=True, g=True)
            m.connect(view, xy, 'Input')
            cap_uv = m.op('Add', m.op('Multiply', xy, m.constant([.5, -.5])), m.constant([.5, .5]))
            cap = _sample(m, 'sphere', coordinates=cap_uv)
            strength = m.op('Multiply', m.scalar('UseMatCap', 0), m.scalar('MatCapStrength', 1))
            multiplied = m.op('Multiply', color, _lerp(m, m.constant([1, 1, 1]), (cap[0], 'RGB'), strength))
            added = m.op('Add', color, m.op('Multiply', (cap[0], 'RGB'), strength))
            color = _lerp(m, multiplied, added, m.scalar('MatCapAdd', 0))
            if kind in ('skin', 'face'):
                # A gentle chromatic tint preserves painted facial details and
                # luminance variation. It adds no emission or extra scattering.
                rosy = m.op('Multiply', color, _vector(m, 'SkinRosyTint', [1.04, .82, .96, 1]))
                color = _lerp(m, color, rosy, m.scalar('SkinRosiness', .3))
            if wet:
                color = m.op('Multiply', color, _lerp(m, m.constant(1), m.constant(.98), wet_alpha))
            _property(m, color, 'MP_BASE_COLOR')
            for semantic, parameter, default, output in (
                ('roughness', 'Roughness', .55, 'MP_ROUGHNESS'),
                ('metallic', 'Metallic', 0, 'MP_METALLIC'),
                ('ao', 'AO', 1, 'MP_AMBIENT_OCCLUSION')):
                sample = _channel(m, _sample(m, semantic, data=True), semantic)
                value = _lerp(m, m.scalar(parameter, default), sample, m.scalar('Use_'+semantic, 0))
                if semantic == 'roughness':
                    if 'detail_roughness' in detail_names:
                        detail_uv = m.op('Multiply', uv, m.scalar('DetailTiling', 45))
                        grain = _sample(m, 'detail_roughness', data=True, coordinates=detail_uv,
                                        builtin=detail_names['detail_roughness'])
                        amount = m.scalar('FabricRoughnessVariation', .22) if kind in ('cloth', 'satin') else m.constant(.12)
                        variation = m.op('Multiply', m.op('Subtract', (grain[0], 'R'), m.constant(.5)), amount)
                        value = m.op('Saturate', m.op('Add', value, variation))
                    if wet:
                        value = _lerp(m, value, m.scalar('WetRoughness', .04), wet_alpha)
                _property(m, value, output)
            specular = m.scalar('Specular', .4)
            if wet:
                # UE's water dielectric F0 is approximately 0.02 (Specular .255).
                specular = _lerp(m, specular, m.constant(.255), wet_alpha)
            _property(m, specular, 'MP_SPECULAR')
            normal = _sample(m, 'normal', normal=True)
            detail_uv = m.op('Multiply', uv, m.scalar('DetailTiling', 45))
            detail = _sample(m, 'detail_normal', normal=True, coordinates=detail_uv,
                             builtin=detail_names['detail_normal'])
            # Add small XY perturbations, then normalize; never add two normal Zs.
            perturb = m.op('Multiply', m.op('Subtract', (detail[0], 'RGB'), m.constant([0, 0, 1])),
                           m.scalar('DetailStrength', .05))
            final_normal = m.op('Add', (normal[0], 'RGB'), perturb)
            if wet:
                xy = m.node('ComponentMask', r=True, g=True)
                m.connect((wet_normal[0], 'RGB'), xy, 'Input')
                rotated = _rotate_xy(m, xy, m.op('Multiply', wet_rotation, m.constant(-1)))
                xyz = m.node('AppendVector')
                m.connect(rotated, xyz, 'A'); m.connect((wet_normal[0], 'B'), xyz, 'B')
                water_perturb = m.op('Multiply', m.op('Subtract', xyz, m.constant([0, 0, 1])),
                    m.op('Multiply', wet_control, m.scalar('WetNormalStrength', 1.)))
                final_normal = m.op('Add', final_normal, water_perturb)
            _property(m, m.op('Normalize', final_normal), 'MP_NORMAL')
            emission = _sample(m, 'emission')
            mask = _channel(m, _sample(m, 'emission_mask', data=True), 'emission_mask')
            mask = _lerp(m, m.constant(1), mask, m.scalar('Use_emission_mask', 0))
            emissive = m.op('Multiply', (emission[0], 'RGB'), _vector(m, 'EmissiveColor', [1, 1, 1, 1]))
            emissive = m.op('Multiply', emissive, m.op('Multiply', mask, m.scalar('EmissiveStrength', 0)))
            _property(m, m.op('Multiply', emissive, opacity), 'MP_EMISSIVE_COLOR')
            if kind in ('face', 'skin') and blend != 'translucent':
                material.set_editor_property('shading_model', u.MaterialShadingModel.MSM_SUBSURFACE_PROFILE)
                material.set_editor_property('subsurface_profile', _profile(m, kind, pbr.get('skin_scattering', 'legacy_v8')))
                _property(m, _face_scattering(m) if kind == 'face' else m.scalar('SSSStrength', .4), 'MP_OPACITY')
                _property(m, _vector(m, 'SubsurfaceTint', [1, .42, .28, 1]), 'MP_SUBSURFACE_COLOR')
            if kind in ('iris', 'sclera', 'eye_highlight', 'plastic', 'glass') and blend != 'translucent':
                material.set_editor_property('shading_model', u.MaterialShadingModel.MSM_CLEAR_COAT)
                _property(m, m.scalar('ClearCoat', 1), 'MP_CUSTOM_DATA0')
                _property(m, m.scalar('CoatRoughness', .08), 'MP_CUSTOM_DATA1')
            if kind == 'cloth' and blend != 'translucent':
                material.set_editor_property('shading_model', u.MaterialShadingModel.MSM_CLOTH)
                # Black dyed fabric still has a dielectric fiber sheen. Its
                # tint is adjustable independently of the dark base texture.
                fiber = _lerp(m, color, m.constant([1, 1, 1]), m.scalar('FuzzBrightness', .08))
                _property(m, m.op('Multiply', fiber, m.constant(.35)), 'MP_SUBSURFACE_COLOR')
                _property(m, m.scalar('Fuzz', .7), 'MP_CUSTOM_DATA0')
            if kind in ('satin', 'hosiery'):
                _property(m, m.scalar('Anisotropy', .5), 'MP_ANISOTROPY')
                angle = m.op('Multiply', m.scalar('FabricDirection', 90), m.constant(1/360))
                cosine, sine = m.node('Cosine'), m.node('Sine')
                m.connect(angle, cosine, 'Input'); m.connect(angle, sine, 'Input')
                pair, tangent = m.node('AppendVector'), m.node('AppendVector')
                m.connect(cosine, pair, 'A'); m.connect(sine, pair, 'B')
                m.connect(pair, tangent, 'A'); m.connect(m.constant(0), tangent, 'B')
                _property(m, tangent, 'MP_TANGENT')
            if kind == 'hair':
                _property(m, m.scalar('Anisotropy', .7), 'MP_ANISOTROPY')
                angle = m.op('Multiply', m.scalar('HairDirection', 90), m.constant(1/360))
                cosine, sine = m.node('Cosine'), m.node('Sine')
                m.connect(angle, cosine, 'Input'); m.connect(angle, sine, 'Input')
                pair = m.node('AppendVector')
                m.connect(cosine, pair, 'A'); m.connect(sine, pair, 'B')
                tangent = m.node('AppendVector')
                m.connect(pair, tangent, 'A'); m.connect(m.constant(0), tangent, 'B')
                flow = _sample(m, 'direction', data=True)
                decoded = m.op('Subtract', m.op('Multiply', (flow[0], 'RGB'), m.constant(2)), m.constant([1, 1, 1]))
                # A flow map's neutral rotation is 90 degrees, matching the V-axis fallback.
                flow_angle = m.op('Subtract', angle, m.constant(.25))
                fc, fs = m.node('Cosine'), m.node('Sine')
                m.connect(flow_angle, fc, 'Input'); m.connect(flow_angle, fs, 'Input')
                x = m.op('DotProduct', decoded, m.constant([1, 0, 0]))
                y = m.op('DotProduct', decoded, m.constant([0, 1, 0]))
                rotated = m.node('AppendVector')
                m.connect(m.op('Subtract', m.op('Multiply', x, fc), m.op('Multiply', y, fs)), rotated, 'A')
                m.connect(m.op('Add', m.op('Multiply', x, fs), m.op('Multiply', y, fc)), rotated, 'B')
                rotated3 = m.node('AppendVector')
                m.connect(rotated, rotated3, 'A'); m.connect(m.constant(0), rotated3, 'B')
                tangent = m.op('Normalize', _lerp(m, tangent, rotated3, m.scalar('Use_direction', 0)))
                _property(m, tangent, 'MP_TANGENT')
        shader[0].set_editor_property('shading_model_override', material.get_editor_property('shading_model'))
        if kind in ('face', 'skin') and blend != 'translucent':
            shader[0].set_editor_property('subsurface_profile', material.get_editor_property('subsurface_profile'))
        if not m.lib.connect_material_property(shader[0], '', u.MaterialProperty.MP_FRONT_MATERIAL):
            raise RuntimeError('角色PBR Substrate输出连接失败')
        for usage in (u.MaterialUsage.MATUSAGE_SKELETAL_MESH, u.MaterialUsage.MATUSAGE_MORPH_TARGETS):
            m.lib.set_material_usage(material, usage)
        # Usage changes can compile the graph. Connect refraction afterwards so
        # UE 5.8 never reads a new root pin against the preceding expression cache.
        if kind == 'glass' and blend == 'translucent':
            _property(m, m.scalar('IOR', 1.45), 'MP_REFRACTION')
        errors = m.lib.recompile_material(material)
        if errors:
            raise RuntimeError('PBR母材质编译失败: ' + str(errors))
        m.lib.layout_material_expressions(material)
        for tag, value in {'PBRVersion': str(VERSION), 'ShadingAlgorithmVersion': '8',
                           'MaterialRoute': 'character_pbr', 'BuildState': 'complete'}.items():
            u.EditorAssetLibrary.set_metadata_tag(material, 'MMDBridge.'+tag, value)
        if not u.EditorAssetLibrary.save_loaded_asset(material):
            raise RuntimeError('PBR母材质保存失败')
        m.protect_asset(material)
        return material
    finally:
        m._pbr_shader = previous_shader
        m.material = previous


def build(m, record):
    pbr = record.get('character_material', {}).get('pbr')
    if not pbr:
        raise ValueError('角色缺少PBR准备数据，请重新生成角色')
    white = pbr.get('status') == 'white'
    try:
        parent = master(m, record, white)
        return instance(m, record, parent, white)
    except Exception as exc:
        # Ownership conflicts protect user assets and must never be swallowed.
        if any(word in str(exc) for word in ('归属', '覆盖', '用户', '未登记', '冲突')):
            raise
        pbr.setdefault('issues', []).append(str(exc))
        pbr['status'] = 'white'
        return instance(m, record, master(m, record, True), True)


def instance(m, record, parent, white=False):
    pbr = record['character_material']['pbr']
    binding = signature(record)
    result = m.create('MI_'+record.get('asset_name', record['id']), u.MaterialInstanceConstant,
                      u.MaterialInstanceConstantFactoryNew(), record=record)
    m.lib.set_material_instance_parent(result, parent)
    scalar_names = {str(n) for n in m.lib.get_scalar_parameter_names(parent)}
    texture_names = {str(n) for n in m.lib.get_texture_parameter_names(parent)}
    def scalar(name, value):
        if name in scalar_names:
            m.lib.set_material_instance_scalar_parameter_value(result, name, float(value))
    params = dict(pbr.get('params', {}))
    if white:
        params['EmissiveStrength'] = 0.
    for name, value in params.items():
        scalar(name, value)
    for semantic, spec in record.get('textures', {}).items():
        parameter = semantic+'_Tex'
        if parameter not in texture_names:
            continue
        if white and semantic not in ('base_color', 'opacity'):
            continue
        normal = semantic in ('normal', 'detail_normal', 'wet_normal')
        data = semantic not in ('base_color', 'sphere', 'emission') and not normal
        try:
            texture = m.texture(spec, normal=normal, data=data)
        except Exception:
            if white:
                continue
            raise
        m.lib.set_material_instance_texture_parameter_value(result, parameter, texture)
        scalar('Use_'+semantic, 1.)
        binding_info = pbr.get('textures', {}).get(semantic, {})
        channel = binding_info.get('channel', 'r')
        scalar(semantic+'_UseAlpha', channel == 'a')
        scalar(semantic+'_Invert', binding_info.get('invert', False))
        if channel in ('r', 'g', 'b'):
            m.lib.set_material_instance_vector_parameter_value(result, semantic+'_Channel',
                u.LinearColor(float(channel == 'r'), float(channel == 'g'), float(channel == 'b'), 0))
    tint = [1., 1., 1.] if 'base_color' in record.get('textures', {}) else record.get('mmd', {}).get('diffuse_color', [1., 1., 1.])
    m.lib.set_material_instance_vector_parameter_value(result, 'BaseColorTint', u.LinearColor(*tint[:3], 1))
    matcap = pbr.get('matcap', {})
    scalar('UseMatCap', not white and matcap.get('enabled', False))
    scalar('MatCapAdd', matcap.get('mode') == 2)
    # Explicitly reset unused maps on rebuilding an existing instance.
    for semantic in ('opacity', 'roughness', 'metallic', 'ao', 'direction', 'emission_mask', 'sss_mask'):
        scalar('Use_'+semantic, semantic in pbr.get('textures', {}))
    enable_character_usage(result)
    m.lib.update_material_instance(result)
    for tag, value in {'PBRBinding': binding, 'MaterialRoute': 'character_pbr',
                       'SourceMaterial': record['id'], 'BuildState': 'complete',
                       'PBRRecord': json.dumps(record, ensure_ascii=False),
                       'PBRRoot': m.root}.items():
        u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.'+tag, value)
    u.EditorAssetLibrary.save_loaded_asset(result)
    m.protect_asset(result)
    m.assets[record['id']] = result.get_path_name()
    m.report.setdefault('material_routes', {})[record['id']] = {
        'route': 'character_pbr', 'type': pbr['type'], 'parent': parent.get_path_name(),
        'alpha_mode': pbr.get('alpha_mode'), 'matcap': matcap, 'binding_hash': binding, 'white': white}
    if pbr.get('missing_effects'):
        m.report.setdefault('needs_review_materials', []).append({'material_id': record['id'],
            'name': record['name'], 'missing_effects': pbr['missing_effects'],
            'reasons': pbr.get('issues', []), 'base_color_preserved': not white})
    if white:
        record['material_error'] = '; '.join(pbr.get('issues', ['PBR转换失败']))
        m.report.setdefault('failures', []).append({'material_id': record['id'], 'error': record['material_error']})
        m.report.setdefault('white_materials', []).append({'material_id': record['id'], 'name': record['name'], 'reasons': pbr.get('issues', [])})
    return result


def reuse(m, record, instance):
    if not instance or u.EditorAssetLibrary.get_metadata_tag(instance, 'MMDBridge.PBRBinding') != signature(record):
        return False
    m.verify_asset(instance)
    m.verify_asset(instance.get_editor_property('parent'))
    m.assets[record['id']] = instance.get_path_name()
    pbr = record['character_material']['pbr']
    if pbr.get('missing_effects'):
        items = m.report.setdefault('needs_review_materials', [])
        if not any(item['material_id'] == record['id'] for item in items):
            items.append({'material_id': record['id'], 'name': record['name'],
                          'missing_effects': pbr['missing_effects'], 'reasons': pbr.get('issues', []),
                          'base_color_preserved': pbr.get('status') != 'white'})
    return True
