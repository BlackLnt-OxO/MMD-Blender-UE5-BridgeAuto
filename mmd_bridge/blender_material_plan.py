"""Read effective source semantics before choosing a conversion route."""
from .material_graph import (analyze_material, constant_socket, external_socket,
                             node_capability, output_domains, selected_mix_input)
from .material_plan import DIRECT_ROUTES, plan_material, field_route


def material_facts(material, renderer='CYCLES', *, analysis=None, data_errors=(), check_budget=None):
    analysis = analysis or analyze_material(material, renderer, check_budget)
    facts = {'domains': list(analysis.get('domains', [])), 'features': [],
             'cache_fields': [], 'blocked_fields': [], 'data_errors': list(data_errors),
             'blocked_operators': [],
             'coverage': False, 'fully_adapted': False, 'closure_count': 0}
    if not material or not material.use_nodes:
        facts['fully_adapted'] = True
        return facts
    from .blender_appearance import nested_material_outputs
    terminals = nested_material_outputs(material.node_tree, renderer)
    if len(terminals) != 1:
        facts['data_errors'].append('缺少唯一有效 Surface 输出')
        return facts
    path, terminal = terminals[0]
    features, cache, blocked, seen = set(), set(), set(), set()

    def source(socket, groups, depth=0):
        if depth > 128:
            facts['data_errors'].append('字段组递归过深')
            return None, groups
        if not socket.is_linked and not getattr(socket, 'is_output', False):
            return None, groups
        out = socket if getattr(socket, 'is_output', False) else socket.links[0].from_socket
        n = out.node
        if n.mute:
            bypass = next((l.from_socket for l in n.internal_links if l.to_socket == out), None)
            return source(bypass, groups, depth+1) if bypass else (None, groups)
        if n.type == 'REROUTE': return source(n.inputs[0], groups, depth+1)
        if n.type == 'GROUP_INPUT':
            mapped = external_socket(out, groups)
            return source(mapped, groups[:-1], depth+1) if mapped else (None, groups)
        if n.type == 'GROUP':
            target = next((s for x in n.node_tree.nodes if x.type == 'GROUP_OUTPUT' and x.is_active_output
                           for s in x.inputs if s.identifier == out.identifier), None)
            return source(target, (*groups, n), depth+1) if target else (None, groups)
        selected = selected_mix_input(n, groups)
        if selected is not None: return source(selected, groups, depth+1)
        return out, groups

    def field(socket, groups, label, normal=False):
        if constant_socket(socket, groups) is not None: return
        out, context = source(socket, groups)
        if out is None: return
        domains = output_domains(out, context)
        facts['domains'] = sorted(set(facts['domains']) | domains)
        visited = set()
        def inspect(output, ctx):
            if check_budget: check_budget()
            key = (output.as_pointer(), tuple(g.as_pointer() for g in ctx))
            if key in visited: return
            visited.add(key)
            n = output.node
            # A static Noise inside a live Fresnel factor remains bakeable;
            # only the unsupported operator's own dependencies govern its cache.
            native_vector = n.type=='BUMP' and output.name=='Normal' and output.type=='VECTOR'
            route = field_route(node_capability(n, output), output_domains(output,ctx),
                                native_normal=normal or native_vector)
            if route not in DIRECT_ROUTES:
                if route == 'blocked':
                    blocked.add(label)
                    facts['blocked_operators'].append({'field':label,'node':n.bl_idname,
                        'output':output.name,'domains':sorted(output_domains(output,ctx)),
                        'name':n.name,'groups':[g.name for g in ctx]})
                else: cache.add(label)
            for s in n.inputs:
                if not s.is_unavailable and constant_socket(s, ctx) is None:
                    child, child_ctx = source(s, ctx)
                    if child is not None: inspect(child, child_ctx)
        # Native NORMAL baking is a dedicated adapter for static derivatives.
        if normal and 'derivative' in domains and not domains & {'view', 'time', 'light', 'unknown'}:
            cache.add(label)
        else: inspect(out, context)

    def walk(socket, groups, depth=0):
        if check_budget: check_budget()
        if depth > 128:
            facts['data_errors'].append('Shader 递归过深')
            return []
        out, groups = source(socket, groups)
        if out is None: return []
        key = (out.as_pointer(), tuple(g.as_pointer() for g in groups))
        if key in seen: return []
        seen.add(key)
        n = out.node
        selected = selected_mix_input(n, groups)
        if selected is not None: return walk(selected, groups, depth+1)
        if n.type in ('MIX_SHADER', 'ADD_SHADER'):
            if n.type == 'MIX_SHADER': field(n.inputs[0], groups, 'coverage_or_mix')
            children = [walk(s, groups, depth+1) for s in n.inputs if s.type == 'SHADER']
            leaves = [kind for branch in children for kind in branch]
            # Transparent is coverage, not another reflective layer.
            surfaces = [kind for kind in leaves if kind != 'BSDF_TRANSPARENT']
            if n.type == 'ADD_SHADER' or len(surfaces) > 1: features.add('layers')
            return leaves
        if n.type == 'BSDF_TRANSPARENT':
            facts['coverage'] = True
            return [n.type]
        facts['closure_count'] += 1
        if n.type == 'BSDF_GLASS': features.update(('refraction', 'transmission'))
        if n.type == 'BSDF_TRANSLUCENT': features.update(('thin_transmission', 'transmission'))
        for socket_name, feature in (
                ('Transmission Weight', 'transmission'), ('Coat Weight', 'coat'),
                ('Subsurface Weight', 'subsurface'), ('Sheen Weight', 'sheen'),
                ('Anisotropic IOR Level', 'anisotropy')):
            s = n.inputs.get(socket_name)
            if s is not None and constant_socket(s, groups) != 0: features.add(feature)
        alpha = n.inputs.get('Alpha')
        if alpha is not None and constant_socket(alpha, groups) != 1: facts['coverage'] = True
        for s in n.inputs:
            if not s.is_unavailable and s.type != 'SHADER':
                field(s, groups, s.name, normal=s.name in ('Normal', 'Coat Normal'))
        return [n.type]

    walk(terminal.inputs['Surface'], tuple(path))
    for name, feature in (('Volume', 'volume'), ('Displacement', 'displacement')):
        if terminal.inputs[name].is_linked: features.add(feature)
    entries = analysis.get('nodes', [])
    facts.update(features=sorted(features), cache_fields=sorted(cache), blocked_fields=sorted(blocked),
                 fully_adapted=bool(entries and not cache and not blocked and
                     not features & {'volume', 'displacement'} and all(
                         e.get('capability', {}).get('route') in DIRECT_ROUTES for e in entries)))
    return facts


def source_material_plan(material, renderer='CYCLES', *, analysis=None, policy=None,
                         repeat_count=1, requested='auto', data_errors=(), check_budget=None):
    facts = material_facts(material, renderer, analysis=analysis, data_errors=data_errors,
                           check_budget=check_budget)
    return plan_material(facts, requested=requested, repeat_count=repeat_count,
                         category=(policy or {}).get('category', 'small'))
