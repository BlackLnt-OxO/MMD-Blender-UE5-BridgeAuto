"""Extract static PBR channels from the effective native shader, before UE IR."""
from contextlib import contextmanager

from .material_graph import constant_socket, selected_mix_input, external_socket


@contextmanager
def native_pbr_channels(material, surface, *, opaque_fallback=False, project_view_mix=False):
    tree = material.node_tree
    created, interfaces, limits, known = [], [], [], {}
    output = material.node_tree.get_output_node('CYCLES' if __import__('bpy').context.scene.render.engine=='CYCLES' else 'EEVEE')
    normal_required = bool(output and output.inputs['Displacement'].is_linked)

    def node(kind):
        item = tree.nodes.new(kind); created.append(item); return item

    def constant(value):
        is_color = hasattr(value, '__len__')
        item = node('ShaderNodeRGB' if is_color else 'ShaderNodeValue')
        item.outputs[0].default_value = value
        known[item.outputs[0].as_pointer()] = tuple(value) if is_color else value
        return item.outputs[0]

    def lift(socket, groups):
        if not socket.is_linked:
            return constant(socket.default_value)
        output = socket.links[0].from_socket
        for group in reversed(groups):
            source_tree = group.node_tree
            kind = 'NodeSocketColor' if output.type == 'RGBA' else 'NodeSocketVector' if output.type == 'VECTOR' else 'NodeSocketFloat'
            port = source_tree.interface.new_socket(name='MMDBridge_PBRBake', in_out='OUTPUT', socket_type=kind)
            interfaces.append((source_tree, port))
            terminal = next(n for n in source_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output)
            target = next(s for s in terminal.inputs if s.identifier == port.identifier)
            source_tree.links.new(output, target)
            output = next(s for s in group.outputs if s.identifier == port.identifier)
        return output

    def math(mode, a, b):
        va,vb=known.get(a.as_pointer()),known.get(b.as_pointer())
        if mode=='MULTIPLY':
            if va==0 or vb==0:return constant(0.)
            if va==1:return b
            if vb==1:return a
        if mode=='ADD':
            if va==0:return b
            if vb==0:return a
        if isinstance(va,(float,int)) and isinstance(vb,(float,int)):
            value={'SUBTRACT':lambda:va-vb,'MULTIPLY':lambda:va*vb,'ADD':lambda:va+vb,
                   'DIVIDE':lambda:va/vb if vb else 0.,'MAXIMUM':lambda:max(va,vb)}[mode]()
            return constant(value)
        item = node('ShaderNodeMath'); item.operation = mode
        tree.links.new(a, item.inputs[0]); tree.links.new(b, item.inputs[1])
        return item.outputs[0]

    def mix(a, b, factor):
        va,vb,vf=known.get(a.as_pointer()),known.get(b.as_pointer()),known.get(factor.as_pointer())
        if va is not None and va==vb:return a
        if vf==0:return a
        if vf==1:return b
        item = node('ShaderNodeMixRGB'); item.blend_type = 'MIX'; item.use_clamp = False
        for source, target in ((factor, item.inputs[0]), (a, item.inputs[1]), (b, item.inputs[2])):
            tree.links.new(source, target)
        return item.outputs[0]

    def walk(socket, groups=(), depth=0):
        nonlocal normal_required
        if depth > 128 or not socket.is_linked:
            raise ValueError('基础 PBR 烘焙缺少有效 Shader 或组递归过深')
        output = socket.links[0].from_socket; shader = output.node
        animation = getattr(shader.id_data, 'animation_data', None)
        if animation and (getattr(animation, 'action', None) or getattr(animation, 'drivers', ())):
            raise ValueError('基础 PBR 烘焙不能冻结材质动画或驱动')
        if shader.mute:
            bypass = next((l.from_socket for l in shader.internal_links if l.to_socket == output), None)
            if bypass is None: raise ValueError('基础 PBR 烘焙缺少静音旁路')
            return walk(bypass, groups, depth+1)
        if shader.type == 'REROUTE': return walk(shader.inputs[0], groups, depth+1)
        if shader.type == 'GROUP_INPUT':
            mapped = external_socket(output, groups)
            if mapped is None: raise ValueError('基础 PBR 烘焙无法绑定 Shader 组输入')
            return walk(mapped, groups[:-1], depth+1)
        if shader.type == 'GROUP':
            terminal = next((s for n in shader.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output
                             for s in n.inputs if s.identifier == output.identifier), None)
            if terminal is None: raise ValueError('基础 PBR 烘焙缺少组输出')
            return walk(terminal, (*groups, shader), depth+1)
        selected = selected_mix_input(shader, groups)
        if selected is not None: return walk(selected, groups, depth+1)
        if shader.type == 'ADD_SHADER':
            if project_view_mix:
                a,b=[walk(s,groups,depth+1) for s in shader.inputs if s.type=='SHADER']
                limits.append('Add Shader 能量叠加投影为等权单一 PBR')
                return {key:mix(a[key],b[key],constant(.5)) for key in a}
            raise ValueError('Add Shader 能量叠加不能可靠提取为单一基础 PBR')
        if shader.type == 'MIX_SHADER':
            a, b = [walk(s, groups, depth+1) for s in shader.inputs if s.type == 'SHADER']
            factor = lift(shader.inputs[0], groups)
            if project_view_mix:
                from .material_graph import static_bake_decision
                decision=static_bake_decision(factor)
                if not decision['allowed'] and set(decision['domains']) & {'view','ray'}:
                    factor=constant(0. if opaque_fallback else .5)
                    # Prefer a printable opaque branch to glass on a mixed surface.
                    if opaque_fallback:
                        inputs=[s for s in shader.inputs if s.type=='SHADER']
                        if inputs[0].is_linked and inputs[0].links[0].from_node.type=='BSDF_GLASS':factor=constant(1.)
                    limits.append('视角或光线相关 Shader 混合投影为固定权重，不能保留实时 Fresnel/双面响应')
            alpha_a,alpha_b=known.get(a['opacity'].as_pointer()),known.get(b['opacity'].as_pointer())
            if alpha_a is not None and alpha_a==alpha_b:
                result={key:mix(a[key],b[key],factor) for key in ('base_color','roughness','metallic','emission')}
                result['opacity']=a['opacity']
                return result
            inverse = math('SUBTRACT', constant(1.), factor)
            coverage_a = math('MULTIPLY', inverse, a['opacity'])
            coverage_b = math('MULTIPLY', factor, b['opacity'])
            coverage = math('ADD', coverage_a, coverage_b)
            conditional = math('DIVIDE', coverage_b, math('MAXIMUM', coverage, constant(1e-8)))
            result = {key: mix(a[key], b[key], conditional) for key in ('base_color', 'roughness', 'metallic', 'emission')}
            result['opacity'] = coverage
            return result
        if shader.type == 'BSDF_TRANSPARENT':
            if opaque_fallback:
                limits.append('Transparent 透明表面降级为不透明')
                return {'base_color':lift(shader.inputs['Color'],groups),'roughness':constant(.5),
                    'metallic':constant(0.),'opacity':constant(1.),'emission':constant((0,0,0,1))}
            color = constant_socket(shader.inputs['Color'], groups)
            if color is None or any(abs(v-1) > 1e-6 for v in color[:3]):
                raise ValueError('彩色透明不能可靠压缩为基础 PBR Alpha')
            return {'base_color': constant((0,0,0,1)), 'roughness': constant(.5),
                    'metallic': constant(0.), 'opacity': constant(0.), 'emission': constant((0,0,0,1))}
        if shader.type=='BSDF_GLASS' and opaque_fallback:
            normal_required |= bool(shader.inputs.get('Normal') and shader.inputs['Normal'].is_linked)
            limits.append('Glass 折射/透射降级为不透明 PBR')
            return {'base_color':lift(shader.inputs['Color'],groups),'roughness':lift(shader.inputs['Roughness'],groups),
                'metallic':constant(0.),'opacity':constant(1.),'emission':constant((0,0,0,1))}
        if shader.type not in ('BSDF_PRINCIPLED', 'BSDF_DIFFUSE', 'BSDF_GLOSSY', 'BSDF_METALLIC', 'EMISSION'):
            raise ValueError('基础 PBR 烘焙尚不能提取 '+shader.bl_idname)
        field = lambda name, default: lift(shader.inputs[name], groups) if shader.inputs.get(name) else constant(default)
        normal_required |= bool(shader.inputs.get('Normal') and shader.inputs['Normal'].is_linked)
        if shader.inputs.get('Normal') and shader.inputs['Normal'].is_linked:
            from .material_graph import static_bake_decision
            normal_decision = static_bake_decision(lift(shader.inputs['Normal'], groups), native_normal=True)
            if not normal_decision['allowed']:
                raise ValueError('法线含不能静态烘焙的数据依赖：' + str(normal_decision['domains']))
        result = {'base_color': field('Base Color' if shader.type in ('BSDF_PRINCIPLED','BSDF_METALLIC') else 'Color', (0,0,0,1)),
                  'roughness': field('Roughness', .5), 'metallic': field('Metallic', 1. if shader.type in ('BSDF_GLOSSY','BSDF_METALLIC') else 0.),
                  'opacity': field('Alpha', 1.), 'emission': constant((0,0,0,1))}
        if shader.type == 'EMISSION':
            result['base_color'] = constant((0,0,0,1))
            color, strength = field('Color', (0,0,0,1)), field('Strength', 1.)
        else:
            color, strength = field('Emission Color', (0,0,0,1)), field('Emission Strength', 1.)
        emission_name = 'Color' if shader.type=='EMISSION' else 'Emission Color'
        strength_name = 'Strength' if shader.type=='EMISSION' else 'Emission Strength'
        emission_value = constant_socket(shader.inputs[emission_name], groups) if shader.inputs.get(emission_name) else [0,0,0,1]
        strength_value = constant_socket(shader.inputs[strength_name], groups) if shader.inputs.get(strength_name) else 1.
        if strength_value==0 or isinstance(emission_value,list) and not any(emission_value[:3]):
            result['emission'] = constant((0,0,0,1))
        elif emission_value is not None and strength_value is not None:
            result['emission'] = constant(tuple(v*strength_value for v in emission_value[:3])+(1.,))
        else:
            item = node('ShaderNodeVectorMath'); item.operation = 'SCALE'
            tree.links.new(color, item.inputs[0]); tree.links.new(strength, item.inputs['Scale'])
            result['emission'] = item.outputs[0]
        for name in ('Transmission Weight', 'Subsurface Weight', 'Coat Weight', 'Sheen Weight', 'Anisotropic IOR Level'):
            s = shader.inputs.get(name)
            if s and (s.is_linked or abs(float(s.default_value)) > 1e-6): limits.append(name)
        return result

    try:
        channels = walk(surface)
        yield channels, limits, normal_required
    finally:
        for source_tree, port in reversed(interfaces): source_tree.interface.remove(port)
        for item in reversed(created): tree.nodes.remove(item)
