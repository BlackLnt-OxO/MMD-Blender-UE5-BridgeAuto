"""Bind explicit character shader profiles; adapt only tool-owned copies."""
from pathlib import Path

import unreal as u

from .character_profiles import load_profiles, resolve_profile, material_role, outline_values, binding_signature, fingerprint, ramp_lit_gain
from .core import resource_root, file_hash
from .face_sdf import ATLAS_HLSL, THRESHOLD_ANGLE_HLSL, THRESHOLD_SAMPLE_HLSL, THRESHOLD_EDGE_HLSL

ADAPTER_VERSION = 18


def enable_character_usage(instance):
    for usage in (u.MaterialUsage.MATUSAGE_SKELETAL_MESH,u.MaterialUsage.MATUSAGE_MORPH_TARGETS):
        if u.MaterialEditingLibrary.has_material_usage(instance,usage):
            continue
        setter=getattr(u.MaterialEditingLibrary,'set_material_usage_override',None)
        if setter is None:
            raise RuntimeError('所选母材质缺少角色/表情 usage，当前 UE 不能在实例上覆盖')
        setter(instance,usage,True,True)


def custom_inputs(names):
    result = []
    for name in names:
        value = u.CustomInput()
        value.set_editor_property('input_name',name)
        result.append(value)
    return result


def profile_for(manager, record, parent):
    if manager.cfg.get('character_material_profile') == 'ue_official_toon':
        return None
    if not hasattr(manager, '_character_profiles'):
        manager._character_profiles = load_profiles(resource_root(), manager.cfg.get('character_material_profiles_dir', ''))
    eye = manager._eye_material(record)
    return resolve_profile(manager._character_profiles, parent,
                           'auto' if eye else manager.cfg.get('character_material_profile', 'auto'))


def dependency_signature(manager, master):
    from .ue_shader_data import asset_dependencies
    packages = asset_dependencies([master.get_path_name()])
    content = Path(u.Paths.project_content_dir())
    return fingerprint({package: file_hash(content / (package.removeprefix('/Game/')+'.uasset'))
                        for package in packages
                        if (content/(package.removeprefix('/Game/')+'.uasset')).is_file()})


def _node(nodes, name):
    return next(n for n in nodes if n.get_name() == name)


def adapt_zzz(manager, source, profile):
    source_digest = dependency_signature(manager, source)
    identity = fingerprint([ADAPTER_VERSION, profile, source_digest])
    path = manager.root + '/Materials/M_Render_PMX_' + identity[:16]
    copied = u.load_asset(path)
    if copied:
        manager.verify_asset(copied)
        if u.EditorAssetLibrary.get_metadata_tag(copied,'MMDBridge.BuildState') == 'character_adapter_failed':
            if not u.EditorAssetLibrary.delete_asset(path):
                raise RuntimeError('不能恢复已验证归属的失败母材质副本')
            copied = None
    if copied:
        if u.EditorAssetLibrary.get_metadata_tag(copied, 'MMDBridge.CharacterAdapter') != identity:
            raise RuntimeError('母材质适配缓存签名不一致')
        return copied
    copied = u.EditorAssetLibrary.duplicate_asset(source.get_path_name(), path)
    if not isinstance(copied, u.Material):
        raise RuntimeError('无法复制 M_RenderMaster')
    u.EditorAssetLibrary.set_metadata_tag(copied, 'MMDBridge.Owner', manager.owner)
    manager._created_this_pass.add(path); manager._pending_created.add(path)
    previous = manager.material; manager.material = copied
    try:
        nodes = manager.lib.get_material_expressions(copied)
        original = _node(nodes, 'MaterialExpressionStaticSwitchParameter_1')
        original.set_editor_property('default_value', False)
        fallback = _node(nodes, 'MaterialExpressionMultiply_28')
        mn = _node(nodes, 'MaterialExpressionTextureSampleParameter2D_7')
        uv = manager.node('TextureCoordinate')
        forward = manager.node('VectorParameter', parameter_name='SDF_HeadForward',
                               default_value=u.LinearColor(1, 0, 0, 0),
                               use_custom_primitive_data=True, primitive_data_index=20)
        right = manager.node('VectorParameter', parameter_name='SDF_HeadRight',
                             default_value=u.LinearColor(0, -1, 0, 0),
                             use_custom_primitive_data=True, primitive_data_index=24)
        up = manager.node('VectorParameter', parameter_name='SDF_HeadUp',
                          default_value=u.LinearColor(0, 0, 1, 0),
                          use_custom_primitive_data=True, primitive_data_index=28)
        transform = manager.node('VectorParameter', parameter_name='SDF_UVTransform',
                                 default_value=u.LinearColor(1, 1, 0, 0))
        transform4 = manager.op('AppendVector',(transform[0],'RGB'),(transform[0],'A'))
        light = _node(nodes, 'MaterialExpressionSkyAtmosphereLightDirection_0')
        if profile.get('sdf',{}).get('format') == 'threshold_lr':
            _node(nodes,'MaterialExpressionIf_0').set_editor_property('equals_threshold',0.)
            offset = _node(nodes,'MaterialExpressionScalarParameter_9')
            inputs = ('LightDirection','HeadForward','HeadRight','HeadUp','Bias','Contrast')
            angle = manager.node('Custom',code=THRESHOLD_ANGLE_HLSL,
                                 output_type=u.CustomMaterialOutputType.CMOT_FLOAT1,inputs=custom_inputs(inputs))
            contrast = manager.scalar('SDF_Contrast',profile['sdf'].get('contrast',0))
            for name,value in zip(inputs,((light,''),(forward[0],'RGB'),(right[0],'RGB'),(up[0],'RGB'),(offset,''),contrast)):
                manager.connect(value,angle,name)
            manager.connect(angle,(_node(nodes,'MaterialExpressionIf_0'),''),'A')
            manager.connect(angle,(_node(nodes,'MaterialExpressionStep_0'),''),'X')
            manager.connect(angle,(_node(nodes,'MaterialExpressionAbs_0'),''),'Input')
            transformed = manager.node('Custom',code='float2 p=float2(UV.x,1.0-UV.y)*Transform.xy+Transform.zw; return float2(p.x,1.0-p.y);',
                                       output_type=u.CustomMaterialOutputType.CMOT_FLOAT2,
                                       inputs=custom_inputs(('UV','Transform')))
            manager.connect(uv,transformed,'UV');manager.connect(transform4,transformed,'Transform')
            sample = _node(nodes,'MaterialExpressionCustom_2')
            sample.set_editor_property('code',THRESHOLD_SAMPLE_HLSL)
            sample.set_editor_property('inputs',custom_inputs(('TexObject','TexCoord','LightSide')))
            manager.connect((_node(nodes,'MaterialExpressionTextureObjectParameter_0'),''),(sample,''),'TexObject')
            manager.connect(transformed,(sample,''),'TexCoord');manager.connect(angle,(sample,''),'LightSide')
            edge = manager.scalar('SDF_EdgePixels',profile['sdf'].get('edge_pixels',1.5))
            soft = manager.node('Custom',code=THRESHOLD_EDGE_HLSL,
                                output_type=u.CustomMaterialOutputType.CMOT_FLOAT1,
                                inputs=custom_inputs(('SDFValue','LightSide','EdgePixels','HardShadow')))
            for name,value in zip(('SDFValue','LightSide','EdgePixels','HardShadow'),
                                  ((sample,''),angle,edge,(_node(nodes,'MaterialExpressionIf_0'),''))):
                manager.connect(value,soft,name)
            manager.connect(soft,(_node(nodes,'MaterialExpressionOneMinus_3'),''),'Input')
            control = _node(nodes,'MaterialExpressionTextureSample_4')
            control.set_editor_property('sampler_type',u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
            control.set_editor_property('texture',u.load_asset('/InterchangeAssets/gltf/Textures/T_White_Linear'))
            manager.connect(transformed,(control,''),'UVs')
            covered = manager.node('LinearInterpolate')
            manager.connect((fallback,''),covered,'A')
            manager.connect(manager.op('Multiply',(_node(nodes,'MaterialExpressionMultiply_29'),''),(mn,'B')),covered,'B')
            manager.connect((control,'B'),covered,'Alpha')
            shadow = covered
        elif profile.get('sdf',{}).get('format') == 'direction_atlas':
            original.set_editor_property('parameter_name', 'UseSDFAtlas')
            atlas = manager.node('TextureObjectParameter', parameter_name='SDF_Atlas',
                                 texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
            coverage = manager.node('TextureObjectParameter', parameter_name='SDF_Coverage',
                                    texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
            tile = manager.scalar('SDF_TileSize', profile['sdf'].get('tile_size', 512))
            softness = manager.scalar('SDF_Softness', 1.0)
            inputs = ['Atlas','Coverage','UV','UVTransform','HeadForward','HeadRight','HeadUp',
                      'LightDirection','TileSize','Softness','Fallback']
            custom = manager.node('Custom', code=ATLAS_HLSL,
                                  output_type=u.CustomMaterialOutputType.CMOT_FLOAT1,inputs=custom_inputs(inputs))
            sources = (atlas, coverage, uv, transform4, (forward[0],'RGB'), (right[0],'RGB'), (up[0],'RGB'),
                       (light,''), tile, softness, (fallback,''))
            for name, value in zip(inputs, sources):
                manager.connect(value, custom, name)
            shadow = manager.op('Multiply', custom, (mn,'B'))
        else:
            shadow = (fallback,'')
        manager.connect(shadow, (original,''), 'True')
        base = _node(nodes, 'MaterialExpressionTextureSampleParameter2D_0')
        # PMX tint multiplies the texture in every Ramp path; its alpha is not desaturation.
        tint=_node(nodes,'MaterialExpressionVectorParameter_0')
        lit_color=manager.node('VectorParameter',parameter_name='LitColor',default_value=u.LinearColor(1,1,1,0))
        manager.connect((lit_color[0],'RGB'),(_node(nodes,'MaterialExpressionReroute_2'),''),'Input')
        manager.connect((lit_color[0],'A'),(_node(nodes,'MaterialExpressionDesaturation_0'),''),'Fraction')
        tinted=manager.op('Multiply',(base,'RGB'),(tint,'RGB'))
        manager.connect(tinted,(_node(nodes,'MaterialExpressionReroute_1'),''),'Input')
        for index in range(1,4):
            curve=_node(nodes,'MaterialExpressionCurveAtlasRowParameter_'+str(index+1))
            gain=manager.node('VectorParameter',parameter_name=f'Ramp_Cloth_{index}_LitGain',default_value=u.LinearColor(1,1,1,0))
            corrected=manager.op('Multiply',(curve,''),(gain[0],'RGB'))
            manager.connect(corrected,(_node(nodes,'MaterialExpressionMultiply_'+str(index+11)),''),'A')
        opacity = manager.op('Multiply', (base,'A'), manager.scalar('PMX_Opacity',1))
        manager.lib.connect_material_property(opacity[0], opacity[1], u.MaterialProperty.MP_OPACITY_MASK)
        manager.lib.connect_material_property(opacity[0], opacity[1], u.MaterialProperty.MP_OPACITY)
        sphere = manager.node('TextureSampleParameter2D', parameter_name='PMX_SphereTex',
                              texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
        manager.connect((_node(nodes,'MaterialExpressionNamedRerouteUsage_15'),''), sphere, 'UVs')
        existing = manager.lib.get_material_property_input_node(copied, u.MaterialProperty.MP_BASE_COLOR)
        lightmap = _node(nodes,'MaterialExpressionTextureSampleParameter2D_2')
        weighted_sphere = manager.node('LinearInterpolate')
        manager.connect(manager.node('Constant',r=1), weighted_sphere,'A')
        manager.connect((sphere[0],'RGB'),weighted_sphere,'B')
        manager.connect((lightmap,'G'),weighted_sphere,'Alpha')
        mult = manager.op('Multiply', (existing,''), weighted_sphere)
        add = manager.op('Add', (existing,''), manager.op('Multiply', (sphere[0],'RGB'),
                         manager.op('Multiply', (lightmap,'B'), (lightmap,'A'))))
        mode = manager.scalar('PMX_SphereMode',0)
        blend = manager.node('Custom', code='if(Mode<0.5 || Mode>2.5) return Base; if(Mode<1.5) return MultiplyColor; return AddColor;',
                             output_type=u.CustomMaterialOutputType.CMOT_FLOAT3,
                             inputs=custom_inputs(('Mode','Base','MultiplyColor','AddColor')))
        for name, value in zip(('Mode','Base','MultiplyColor','AddColor'), (mode,(existing,''),mult,add)):
            manager.connect(value,blend,name)
        manager.lib.connect_material_property(blend[0],blend[1],u.MaterialProperty.MP_BASE_COLOR)
        msre = _node(nodes,'MaterialExpressionVectorParameter_3')
        emissive_pin = list(manager.lib.get_material_expression_output_names(msre))[4]
        emissive = manager.op('Multiply', blend, (msre,emissive_pin))
        emissive = manager.op('Multiply',emissive,opacity)
        manager.lib.connect_material_property(emissive[0],emissive[1],u.MaterialProperty.MP_EMISSIVE_COLOR)
        # No generated instance may inherit another character's control maps.
        for node in nodes:
            if isinstance(node, (u.MaterialExpressionTextureSampleParameter2D, u.MaterialExpressionTextureObjectParameter)):
                parameter = str(node.get_editor_property('parameter_name'))
                if parameter in ('BaseTex','MN','Lightmap','SDF_Tex'):
                    neutral = u.load_asset('/InterchangeAssets/gltf/Textures/T_White_Linear') if parameter in ('MN','Lightmap','SDF_Tex') else u.load_asset('/Engine/EngineResources/WhiteSquareTexture')
                    if not neutral:
                        raise RuntimeError('缺少中性线性贴图')
                    node.set_editor_property('texture',neutral)
                    if parameter in ('MN','Lightmap') and isinstance(node,u.MaterialExpressionTextureSampleParameter2D):
                        node.set_editor_property('sampler_type',u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
        manager.lib.recompile_material(copied)
        u.EditorAssetLibrary.set_metadata_tag(copied, 'MMDBridge.CharacterAdapter', identity)
        manager.managed(copied); u.EditorAssetLibrary.save_loaded_asset(copied); manager.protect_asset(copied)
        manager.report.setdefault('checks',{})['character_adapter'] = {'source':source.get_path_name(),
            'parent':path,'dependency_hash':source_digest,'profile':profile['id']}
        return copied
    except Exception:
        u.EditorAssetLibrary.set_metadata_tag(copied,'MMDBridge.BuildState','character_adapter_failed')
        if u.EditorAssetLibrary.save_loaded_asset(copied):
            manager.protect_asset(copied)
        raise
    finally:
        manager.material = previous


def build_registered(manager, record, master, requested, profile):
    if record.get('character_material',{}).get('profile_hash') not in (None,fingerprint(profile)):
        raise ValueError('母材质配置在 Blender 生成数据后发生变化，请重新生成角色材质')
    original = master
    if profile.get('adapter') == 'zzz_render':
        master = adapt_zzz(manager, master, profile)
    names = {kind: manager._parameter_names(method, master) for kind,method in (
        ('textures','get_texture_parameter_names'), ('scalars','get_scalar_parameter_names'),
        ('vectors','get_vector_parameter_names'), ('switches','get_static_switch_parameter_names'))}
    available = set(sum(names.values(), []))
    missing = set(profile.get('required', []))-available
    if missing:
        raise ValueError('母材质配置必需参数缺失: ' + ', '.join(sorted(missing)))
    instance = manager.create('MI_'+record.get('asset_name',record['id']),u.MaterialInstanceConstant,u.MaterialInstanceConstantFactoryNew())
    manager.lib.set_material_instance_parent(instance, master)
    assigned, skipped = {}, []
    textures = record.get('textures', {})
    for semantic, binding in profile.get('textures', {}).items():
        if profile.get('sdf',{}).get('format','none') == 'none' and semantic in ('face_sdf','sdf_atlas','sdf_coverage'):
            continue
        parameter = binding['parameter']
        image = textures.get(semantic)
        if semantic == 'base_color' and not image:
            image = record.get('channels',{}).get('base_color',{}).get('image')
        if not image and semantic != 'base_color':
            continue
        if parameter not in names['textures']:
            skipped.append({'semantic':semantic,'parameter':parameter,'reason':'parameter_missing'})
            continue
        color = binding.get('color_space','data')
        if image:
            image=dict(image,color_space='sRGB' if color=='srgb' else 'Non-Color')
        if image and color=='half_float':
            image=dict(image,half_float=True)
        tex = (manager.texture(image, normal=color=='normal', data=color not in ('srgb','normal'))
               if image else u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
        manager.lib.set_material_instance_texture_parameter_value(instance,parameter,tex)
        assigned[semantic] = parameter
    mmd = record.get('mmd', {})
    note = record.get('character_material', {})
    sdf = note.get('sdf', {})
    if sdf.get('status')=='needs_review':
        manager.report['warnings'].append('面部 SDF 回退普通光照: '+record['name']+'；'+sdf.get('reason',''))
    values = {'diffuse_color': mmd.get('diffuse_color',[1,1,1]),
              'opacity': float(mmd.get('diffuse_alpha',1)),
              'sphere_mode': int(mmd.get('sphere_texture_type') or 0),
              'sdf_uv_transform': sdf.get('uv_transform',[1,1,0,0])}
    for kind in ('scalars','vectors'):
        for semantic, binding in profile.get(kind,{}).items():
            parameter = binding['parameter']
            value = binding.get('value',values.get(semantic))
            if parameter not in names[kind] or value is None:
                skipped.append({'semantic':semantic,'parameter':parameter,'reason':'parameter_or_value_missing'})
                continue
            if kind == 'vectors':
                value = list(value); value += [1]*(4-len(value))
                manager.lib.set_material_instance_vector_parameter_value(instance,parameter,u.LinearColor(*value[:4]))
            else:
                manager.lib.set_material_instance_scalar_parameter_value(instance,parameter,float(value))
            assigned[semantic] = parameter
    enabled = sdf.get('format') == profile.get('sdf',{}).get('format') and any(key in assigned for key in ('sdf_atlas','face_sdf'))
    for semantic,binding in profile.get('switches',{}).items():
        if binding['parameter'] in names['switches']:
            manager.lib.set_material_instance_static_switch_parameter_value(instance,binding['parameter'],
                bool(enabled if semantic in ('sdf_atlas','face_sdf') else binding.get('value',semantic in assigned)))
    if profile.get('adapter') == 'zzz_render':
        role = material_role(record,profile)
        shadow_color = (.366352,.361920,.572917,0) if role=='hair' else ((.40625,)*3+(0,) if role in ('face','skin') else (.479167,)*3+(0,))
        manager.lib.set_material_instance_vector_parameter_value(instance,'ShadowColor',u.LinearColor(*shadow_color))
        manager.lib.set_material_instance_static_switch_parameter_value(instance,'UseRamp',role.startswith('cloth_'))
        if role.startswith('cloth_') and note.get('sources',{}).get('lightmap',{}).get('kind')=='generated':
            for node in manager.lib.get_material_expressions(master):
                if not isinstance(node,u.MaterialExpressionCurveAtlasRowParameter):
                    continue
                parameter=str(node.get_editor_property('parameter_name'))
                if parameter not in ('Ramp_Cloth_1','Ramp_Cloth_2','Ramp_Cloth_3'):
                    continue
                color=node.get_editor_property('curve').get_linear_color_value(1)
                try:
                    gain=ramp_lit_gain([color.r,color.g,color.b])
                except ValueError as exc:
                    manager.report['warnings'].append(record['name']+'：'+str(exc))
                    continue
                manager.lib.set_material_instance_vector_parameter_value(instance,parameter+'_LitGain',u.LinearColor(*gain))
        manager.lib.set_material_instance_scalar_parameter_value(instance,'Normal_Strengh',0)
        manager.lib.set_material_instance_scalar_parameter_value(instance,'VertexNormal_Strengh',0)
        overrides = u.MaterialInstanceBasePropertyOverrides()
        overrides.set_editor_property('override_two_sided',True)
        overrides.set_editor_property('two_sided',bool(mmd.get('two_sided',True)))
        alpha_mode = note.get('alpha_mode','opaque')
        overrides.set_editor_property('override_blend_mode',True)
        overrides.set_editor_property('blend_mode',{'opaque':u.BlendMode.BLEND_OPAQUE,
            'masked':u.BlendMode.BLEND_MASKED,'translucent':u.BlendMode.BLEND_TRANSLUCENT}[alpha_mode])
        instance.set_editor_property('base_property_overrides',overrides)
        if values['sphere_mode'] == 3:
            skipped.append({'semantic':'sphere','reason':'subtexture_additional_uv_not_supported'})
    presets=profile.get('role_presets',{})
    for kind in ('scalars','vectors','switches'):
        parameters=dict(presets.get('default',{}).get(kind,{}))
        parameters.update(presets.get(material_role(record,profile),{}).get(kind,{}))
        for parameter,value in parameters.items():
            if parameter not in names[kind]:
                skipped.append({'parameter':parameter,'reason':'preset_parameter_missing'})
                continue
            if kind=='vectors':
                color=list(value)+[1]*(4-len(value))
                manager.lib.set_material_instance_vector_parameter_value(instance,parameter,u.LinearColor(*color))
            elif kind=='scalars':
                manager.lib.set_material_instance_scalar_parameter_value(instance,parameter,float(value))
            else:
                manager.lib.set_material_instance_static_switch_parameter_value(instance,parameter,value)
    if skipped:
        record['requires_visual_review'] = True
        manager.report['warnings'].append('角色母材质存在未映射数据: '+record['name'])
    signature = fingerprint([ADAPTER_VERSION,binding_signature(record,profile,requested),dependency_signature(manager,original)])
    enable_character_usage(instance)
    manager.lib.update_material_instance(instance); manager.managed(instance)
    for key,value in {'MaterialRoute':'character_profile','ParentMaterial':requested,'CharacterBinding':signature}.items():
        u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.'+key,value)
    u.EditorAssetLibrary.save_loaded_asset(instance); manager.protect_asset(instance)
    manager.assets[record['id']] = instance.get_path_name()
    manager.report.setdefault('material_routes',{})[record['id']] = {'route':'character_profile','profile':profile['id'],
        'requested_parent':requested,'parent':master.get_path_name(),'assigned_parameters':assigned,'skipped':skipped,
        'sdf':sdf.get('format','none'),'binding_hash':signature}
    return instance


def attach_runtime(actor, mesh, records, manager):
    slots = [index for index,record in enumerate(records)
             if record.get('character_material',{}).get('sdf',{}).get('format') in ('direction_atlas','threshold_lr')]
    if not slots:
        return
    for index in slots:
        record=records[index]
        profile=profile_for(manager,record,manager.cfg.get('character_master_material',''))
        if profile and profile.get('adapter')!='zzz_render' and profile.get('sdf',{}).get('runtime',{}).get('data_indices')!=[20,24,28]:
            raise RuntimeError('自选图集母材质必须声明并使用 Runtime 自定义数据索引 20、24、28')
        runtime=profile.get('sdf',{}).get('runtime',{}) if profile else {}
        for axis,expected in (('forward',20),('right',24),('up',28)):
            parameter=runtime.get(axis)
            if not parameter or actor.skeletal_mesh_component.get_custom_primitive_data_index_for_vector_parameter(parameter)!=expected:
                raise RuntimeError('面部方向参数未正确接入自定义数据: '+str(parameter))
    helper = getattr(u,'MMDBridgeCharacterLibrary',None)
    if helper is None:
        raise RuntimeError('面部 SDF 缺少 MMDBridgeCharacter Runtime 插件，不能使用冻结方向')
    heads = {records[index]['character_material']['sdf']['head_bone'] for index in slots}
    if len(heads)!=1:
        raise RuntimeError('面部 SDF 头骨映射不唯一')
    component = helper.attach_face_direction(actor,next(iter(heads)),u.Vector(1,0,0),u.Vector(0,-1,0),u.Vector(0,0,1))
    if not component:
        raise RuntimeError('面部方向组件创建失败')
    material_ids={records[index]['id'] for index in slots}
    actual_slots=[row['ue_slot'] for row in manager.report.get('checks',{}).get('material_slots',[]) if row['material_id'] in material_ids]
    manager.report.setdefault('checks',{})['face_runtime'] = {'component':component.get_path_name(),
        'head_bone':next(iter(heads)),'slots':actual_slots,'data_indices':[20,24,28]}


def character_blueprint(manager, mesh, records, animation=None):
    """Create the configured main/outline actor; unregistered shaders keep their route."""
    profiles=[profile_for(manager,r,manager.cfg.get('character_master_material','')) for r in records]
    profile=next((p for p in profiles if p and p.get('outline')),None)
    if not profile:
        return None
    actor_class=getattr(u,'MMDBridgeCharacterActor',None)
    if actor_class is None:
        raise RuntimeError('双网格角色缺少已编译的 MMDBridgeCharacter Runtime 插件')
    outline=profile['outline']
    source=u.load_asset(outline['parent'])
    if not isinstance(source,u.Material):
        raise ValueError('描边母材质不存在: '+outline['parent'])
    available={str(n) for method in ('get_texture_parameter_names','get_scalar_parameter_names','get_vector_parameter_names')
               for n in getattr(manager.lib,method)(source)}
    if not {'Color','OutLineScale','OutLineMask'}<=available:
        raise ValueError('描边母材质缺少 Color、OutLineScale 或 OutLineMask 参数')
    identity=fingerprint([1,outline,dependency_signature(manager,source)])
    path=manager.root+'/Materials/M_Outline_PMX_'+identity[:16]
    parent=u.load_asset(path)
    if parent:
        manager.verify_asset(parent)
        if u.EditorAssetLibrary.get_metadata_tag(parent,'MMDBridge.BuildState')=='outline_adapter_failed':
            if not u.EditorAssetLibrary.delete_asset(path):
                raise RuntimeError('无法恢复工具生成的失败描边母材质')
            parent=None
    if parent is None:
        parent=u.EditorAssetLibrary.duplicate_asset(source.get_path_name(),path)
        if not parent:
            raise RuntimeError('无法复制描边母材质')
        manager.managed(parent)
        manager._created_this_pass.add(path);manager._pending_created.add(path)
        previous=manager.material;manager.material=parent
        try:
            original=manager.lib.get_material_property_input_node(parent,u.MaterialProperty.MP_OPACITY_MASK)
            base=manager.node('TextureSampleParameter2D',parameter_name='OutlineBaseTex',
                              texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
            alpha=manager.op('Multiply',(base[0],'A'),manager.scalar('OutlineOpacity',1))
            mask=manager.op('Min',(original,''),alpha)
            for property_name in (u.MaterialProperty.MP_OPACITY_MASK,u.MaterialProperty.MP_OPACITY):
                manager.lib.connect_material_property(mask[0],mask[1],property_name)
            manager.lib.recompile_material(parent)
            assert u.EditorAssetLibrary.save_loaded_asset(parent)
            manager.protect_asset(parent)
        except Exception:
            u.EditorAssetLibrary.set_metadata_tag(parent,'MMDBridge.BuildState','outline_adapter_failed')
            if u.EditorAssetLibrary.save_loaded_asset(parent): manager.protect_asset(parent)
            raise
        finally:
            manager.material=previous
    records_by_slot={r['slot_name']:r for r in records}
    default_edges=not any(r.get('mmd',{}).get('enabled_toon_edge',True) for r in records)
    if default_edges:
        manager.report['warnings'].append('PMX 全部关闭描边，按母材质默认部位补齐描边')
    outlines=[]
    for slot in mesh.materials:
        slot_name=str(slot.get_editor_property('imported_material_slot_name'))
        record=records_by_slot.get(slot_name)
        if record is None:
            raise ValueError('描边材质槽未匹配: '+slot_name)
        mi=manager.create('MI_Outline_'+record['asset_name'],u.MaterialInstanceConstant,u.MaterialInstanceConstantFactoryNew())
        manager.lib.set_material_instance_parent(mi,parent)
        mmd=record.get('mmd',{})
        color,scale=outline_values(record,profile,default_edges)
        manager.lib.set_material_instance_vector_parameter_value(mi,'Color',u.LinearColor(*color))
        manager.lib.set_material_instance_scalar_parameter_value(mi,'OutLineScale',scale)
        manager.lib.set_material_instance_scalar_parameter_value(mi,'OutlineOpacity',float(mmd.get('diffuse_alpha',1)))
        spec=record.get('textures',{}).get('base_color')
        base=manager.texture(dict(spec,color_space='sRGB')) if spec else u.load_asset('/Engine/EngineResources/WhiteSquareTexture')
        manager.lib.set_material_instance_texture_parameter_value(mi,'OutlineBaseTex',base)
        manager.lib.set_material_instance_texture_parameter_value(mi,'OutLineMask',u.load_asset('/InterchangeAssets/gltf/Textures/T_White_Linear'))
        overrides=mi.get_editor_property('base_property_overrides')
        overrides.set_editor_property('override_blend_mode',True)
        overrides.set_editor_property('blend_mode',u.BlendMode.BLEND_TRANSLUCENT if record.get('character_material',{}).get('alpha_mode')=='translucent' else u.BlendMode.BLEND_MASKED)
        mi.set_editor_property('base_property_overrides',overrides)
        enable_character_usage(mi);manager.lib.update_material_instance(mi)
        assert u.EditorAssetLibrary.save_loaded_asset(mi)
        manager.protect_asset(mi);outlines.append(mi)
    folder='/Game/Character/MMDBridge/'+manager.root.rsplit('/',1)[-1]
    path=folder+'/BP_Character'
    blueprint=u.load_asset(path)
    if blueprint:
        manager.verify_asset(blueprint)
    else:
        factory=u.BlueprintFactory();factory.set_editor_property('parent_class',actor_class)
        blueprint=u.AssetToolsHelpers.get_asset_tools().create_asset('BP_Character',folder,u.Blueprint,factory)
        if not blueprint:
            raise RuntimeError('角色蓝图创建失败')
        manager.managed(blueprint)
        u.BlueprintEditorLibrary.compile_blueprint(blueprint)
    defaults=u.get_default_object(blueprint.generated_class())
    defaults.set_editor_property('character_mesh',mesh)
    defaults.set_editor_property('main_materials',[slot.material_interface for slot in mesh.materials])
    defaults.set_editor_property('outline_materials',outlines)
    defaults.set_editor_property('main_animation',animation)
    defaults.set_editor_property('outline_animation',animation)
    for component,materials in ((defaults.skeletal_mesh_component,defaults.main_materials),(defaults.outline_mesh,defaults.outline_materials)):
        component.set_skeletal_mesh_asset(mesh)
        component.set_editor_property('override_materials',materials)
        component.override_animation_data(animation,True,True)
    # UE's new Blueprint class needs recompilation after editing its defaults.
    u.BlueprintEditorLibrary.compile_blueprint(blueprint)
    if u.get_default_object(blueprint.generated_class()).get_editor_property('character_mesh')!=mesh:
        raise RuntimeError('角色蓝图编译后默认网格未保留')
    assert u.EditorAssetLibrary.save_loaded_asset(blueprint,only_if_is_dirty=False)
    manager.protect_asset(blueprint)
    manager.report.setdefault('checks',{})['character_blueprint']={'path':blueprint.get_path_name(),
        'meshes':2,'animation_playback':'independent','animation':animation.get_path_name() if animation else None,
        'outline_materials':[mi.get_path_name() for mi in outlines]}
    return blueprint
