"""Deterministic shader analysis and reusable function IR. No renderer or AI needed."""
import copy
import hashlib
import json
import math
from pathlib import Path

_IMAGE_DIGESTS={}

VERSION = 4

# Capability decisions concern semantics, not Blender node labels or presets.
DIRECT_NODES={'ShaderNodeValue','ShaderNodeRGB','ShaderNodeMath','ShaderNodeVectorMath',
    'ShaderNodeMix','ShaderNodeMixRGB','ShaderNodeHueSaturation','ShaderNodeBrightContrast',
    'ShaderNodeGamma','ShaderNodeInvert','ShaderNodeClamp','ShaderNodeMapRange','ShaderNodeMapping',
    'ShaderNodeSeparateColor','ShaderNodeCombineColor','ShaderNodeSeparateXYZ','ShaderNodeCombineXYZ',
    'ShaderNodeTexImage','ShaderNodeUVMap','ShaderNodeNormalMap','ShaderNodeFresnel',
    'ShaderNodeBsdfPrincipled','ShaderNodeBsdfDiffuse','ShaderNodeBsdfGlossy','ShaderNodeBsdfTranslucent',
    'ShaderNodeEmission','ShaderNodeMixShader','ShaderNodeAddShader','ShaderNodeBsdfTransparent',
    'ShaderNodeTexChecker','ShaderNodeBsdfGlass','ShaderNodeBsdfMetallic'}
MATH_MODES={'MULTIPLY','ADD','SUBTRACT','DIVIDE','POWER','MINIMUM','MAXIMUM','LESS_THAN','GREATER_THAN',
    'MULTIPLY_ADD','ABSOLUTE','FLOOR','CEIL','FRACT','SINE','COSINE','SQRT','ROUND','SIGN','RADIANS','DEGREES'}


def node_capability(node, output):
    name=node.bl_idname
    result={'route':'local_cache','reason':'unverified_node_semantics','basis':'estimate',
            'types':['VALUE','VECTOR','RGBA'],'pixel':True,'ray_tracing':'unverified'}
    if name in DIRECT_NODES:result.update(route='nodes',reason='supported_operator',ray_tracing='compile_required')
    if node.type == 'TEX_COORD' and output.name == 'UV':
        result.update(route='nodes', reason='source_uv', ray_tracing='compile_required')
    if node.type in ('BSDF_GLASS', 'BSDF_METALLIC'):
        result.update(route='nodes', reason='native_bsdf_adapter', ray_tracing='compile_required')
    if node.type=='MATH' and node.operation not in MATH_MODES:result.update(route='local_cache',reason='unsupported_math_mode')
    if node.type in ('VALTORGB','CURVE_RGB'):result.update(route='shared_lookup',reason='blender_evaluated_curve',ray_tracing='compile_required')
    if node.type=='VERTEX_COLOR':result.update(route='mesh_data',reason='source_color_attribute')
    if node.type=='OBJECT_INFO' and output.name=='Random':result.update(route='instance_data',reason='cycles_object_random')
    if node.type=='NEW_GEOMETRY' and output.name=='Random Per Island':result.update(route='mesh_data',reason='cycles_connected_component_random')
    if node.type=='NEW_GEOMETRY' and output.name=='Backfacing':result.update(route='nodes',reason='live_two_sided_sign')
    if node.type in ('FRESNEL','LAYER_WEIGHT','LIGHT_PATH','CAMERA','SCRIPT') and result['route']=='local_cache':
        result.update(route='unsupported',reason='static_cache_would_change_view_or_time_semantics')
    if node.type=='TEX_IMAGE' and (node.image and node.image.source in ('MOVIE','SEQUENCE','TILED') or node.projection!='FLAT'):
        result.update(route='unsupported',reason='image_domain_requires_adapter')
    if node.type=='BUMP':result.update(reason='derivatives_require_ray_tracing_validation')
    return result


def selected_mix_input(node, groups=()):
    """Prune only provably inactive branches, keeping output clamps intact."""
    if node.type == 'MIX_SHADER':
        fac = constant_socket(node.inputs[0], groups)
        return node.inputs[1 + int(fac)] if fac in (0, 1) else None
    if node.type not in ('MIX', 'MIX_RGB'):
        return None
    if getattr(node, 'blend_type', 'MIX') != 'MIX' or getattr(node, 'use_clamp', False) or getattr(node, 'clamp_result', False):
        return None
    if node.type == 'MIX' and getattr(node, 'factor_mode', 'UNIFORM') != 'UNIFORM':
        return None
    fac = constant_socket(node.inputs[0], groups)
    if not isinstance(fac, (float, int)):
        return None
    if getattr(node, 'clamp_factor', node.type == 'MIX_RGB'):
        fac = min(1, max(0, fac))
    index = 1 if node.type == 'MIX_RGB' else {'FLOAT': 2, 'VECTOR': 4, 'RGBA': 6}.get(node.data_type)
    return node.inputs[index + int(fac)] if index is not None and fac in (0, 1) else None


def effective_graph(socket, groups=(), seen=None, memo=None):
    """Content signature of effective links, values and semantic node options."""
    folded=constant_socket(socket,groups)
    if folded is not None:return {'constant':folded,'type':socket.type}
    if not socket.is_linked:return {'dynamic_default':socket_value(socket),'type':socket.type}
    output=socket.links[0].from_socket;node=output.node
    key=(output.as_pointer(),tuple(g.as_pointer() for g in groups))
    memo={} if memo is None else memo
    if key in memo:return memo[key]
    seen=set() if seen is None else seen
    if key in seen:return {'cycle':True}
    seen=seen|{key}
    if node.mute:
        bypass=next((l.from_socket for l in node.internal_links if l.to_socket==output),None)
        return effective_graph(bypass,groups,seen,memo) if bypass else {'invalid_bypass':True}
    if node.type=='REROUTE':return effective_graph(node.inputs[0],groups,seen,memo)
    if node.type=='GROUP_INPUT':
        mapped=external_socket(output,groups)
        return effective_graph(mapped,groups[:-1],seen,memo) if mapped else {'unbound':output.identifier}
    if node.type=='GROUP':
        target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                     for s in n.inputs if s.identifier==output.identifier),None)
        return effective_graph(target,(*groups,node),seen,memo) if target else {'missing_output':output.identifier}
    selected = selected_mix_input(node, groups)
    if selected is not None:
        return effective_graph(selected, groups, seen, memo)
    if node.type=='MIX_SHADER':
        fac=constant_socket(node.inputs[0],groups)
        if fac in (0,1):return effective_graph(node.inputs[1+int(fac)],groups,seen,memo)
    options={}
    for name in ('operation','blend_type','data_type','interpolation_type','vector_type','noise_dimensions',
                 'noise_type','normalize','use_clamp','clamp_factor','clamp_result','projection','extension',
                 'space','uv_map','layer_name','attribute_name','distribution','component'):
        if hasattr(node,name):options[name]=getattr(node,name)
    if node.type=='VALTORGB':
        ramp=node.color_ramp
        options['ramp']=[ramp.interpolation,ramp.color_mode,ramp.hue_interpolation,
                         [[e.position,list(e.color)] for e in ramp.elements]]
    if node.type=='CURVE_RGB':
        curve=node.mapping
        options['curves']=[[[list(p.location),p.handle_type] for p in c.points] for c in curve.curves]
        options.update(black=list(curve.black_level),white=list(curve.white_level),extend=curve.extend,clip=curve.use_clip)
    if node.type=='TEX_IMAGE' and node.image:
        image=node.image
        options['image']=[image.filepath,image.colorspace_settings.name,list(image.size)]
        if image.packed_file:options['image_digest']=hashlib.sha256(image.packed_file.data).hexdigest()
        else:
            path=Path(image.filepath_from_user() if hasattr(image,'filepath_from_user') else image.filepath)
            if path.is_file():
                stat=path.stat();key=(str(path.resolve()),stat.st_size,stat.st_mtime_ns)
                if key not in _IMAGE_DIGESTS:
                    from .core import file_hash
                    _IMAGE_DIGESTS[key]=file_hash(path)
                options['image_digest']=_IMAGE_DIGESTS[key]
    value={'type':node.bl_idname,'output':output.identifier,'options':options,
            'inputs':[[s.identifier,effective_graph(s,groups,seen,memo)] for s in node.inputs if not s.is_unavailable]}
    result={'ref':signature(value)};memo[key]=result
    return result


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode('utf-8')).hexdigest()[:24]


def semantic_ir(value):
    if isinstance(value,dict):
        if 'id' in value and 'path' in value and 'color_space' in value:
            return {k:semantic_ir(v) for k,v in value.items() if k not in ('path','name','asset_name')}
        return {k:semantic_ir(v) for k,v in value.items()}
    if isinstance(value,list):return [semantic_ir(v) for v in value]
    return value


def material_template(record, state):
    """Parameterize call-site constants, preserving closure and sampler topology.

    Function internals are deliberately untouched: an internal constant is part
    of that function's identity, whereas a bound preset value belongs to its MI.
    """
    bindings={}
    def visit(value):
        if isinstance(value,dict):
            if 'id' in value and 'path' in value and 'color_space' in value:
                name='Source_'+str(len(bindings));bindings[name]={'type':'texture','value':copy.deepcopy(value)}
                # Sampling semantics, not image names or import paths, determine
                # whether one material parent can serve both resources.
                return {'_parameter':name, **{k:value[k] for k in ('hdr','color_space','lookup','width') if k in value}}
            if value.get('op')=='constant':
                default=value['value'];name='Source_'+str(len(bindings))
                kind='vector' if isinstance(default,list) else 'scalar'
                bindings[name]={'type':kind,'value':copy.deepcopy(default)}
                return {'op':'parameter','name':name,'type':kind,'size':len(default) if kind=='vector' else 1}
            return {k:visit(v) for k,v in sorted(value.items())}
        if isinstance(value,list):return [visit(v) for v in value]
        return value
    graph=visit(record['surface_graph'])
    opacity=visit(record.get('channels',{}).get('opacity')) if state['masked'] else None
    refraction=visit(record.get('refraction_ior'))
    key=signature({'version':3,'graph':graph,'opacity':opacity,'refraction':refraction,'state':state})
    template=copy.deepcopy(record)
    template['surface_graph']=graph
    if opacity is not None:template['channels']['opacity']=opacity
    if refraction is not None:template['refraction_ior']=refraction
    template['_parent_key']=key;template['_bindings']=bindings;template['_render_state']=state
    return template


def socket_value(socket):
    value = getattr(socket, 'default_value', None)
    return list(value) if hasattr(value, '__len__') and not isinstance(value, str) else value


def external_socket(socket, groups):
    if not groups:
        return None
    return next((s for s in groups[-1].inputs if s.identifier == socket.identifier), None)


def constant_socket(socket, groups=(), seen=None, memo=None):
    if socket is None:return None
    memo={} if memo is None else memo
    key=(socket.as_pointer(),tuple(g.as_pointer() for g in groups))
    if key in memo:return memo[key]
    value = _constant_socket(socket, groups, seen, memo)
    if value is not None and socket and socket.is_linked:
        source = socket.links[0].from_socket.type
        if socket.type in ('VALUE','INT') and source in ('RGBA','VECTOR') and isinstance(value,list):
            weights = (.2126,.7152,.0722) if source=='RGBA' else (1/3,)*3
            value=sum(a*b for a,b in zip(value,weights))
        if socket.type in ('RGBA','VECTOR') and isinstance(value,(float,int)):
            value=[value]*3 + ([1.] if socket.type=='RGBA' else [])
    memo[key]=value
    return value


def _constant_socket(socket, groups=(), seen=None, memo=None):
    """Evaluate only provably static inputs; unknown is None, never zero."""
    if socket is None:
        return None
    animation = getattr(getattr(socket,'id_data',None),'animation_data',None)
    if animation and (getattr(animation,'action',None) or getattr(animation,'drivers',())):
        return None
    if not socket.is_linked:
        return socket_value(socket)
    output = socket.links[0].from_socket
    key = (output.as_pointer(), tuple(g.as_pointer() for g in groups))
    seen = set() if seen is None else seen
    if key in seen:
        return None
    seen = seen | {key}
    node = output.node
    # Drivers/curves can change defaults without a socket link. Do not freeze them.
    animation = getattr(node.id_data, 'animation_data', None)
    if animation and (getattr(animation, 'action', None) or getattr(animation, 'drivers', ())):
        return None
    sub = lambda s: constant_socket(s, groups, seen, memo)
    if node.mute:
        bypass = next((l.from_socket for l in node.internal_links if l.to_socket == output), None)
        return sub(bypass) if bypass else None
    if node.type == 'REROUTE':
        return sub(node.inputs[0])
    if node.type in ('VALUE', 'RGB'):
        return socket_value(output)
    if node.type == 'GROUP_INPUT':
        mapped = external_socket(output, groups)
        return constant_socket(mapped, groups[:-1], seen, memo) if mapped else None
    if node.type == 'GROUP' and node.node_tree:
        target = next((s for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output
                       for s in n.inputs if s.identifier == output.identifier), None)
        return constant_socket(target, (*groups, node), seen, memo) if target else None
    try:
        if node.type == 'MATH':
            a = sub(node.inputs[0]); b = sub(node.inputs[1])
            mode = node.operation
            # Absorbing constants do not require evaluating a costly/unknown operand.
            if mode == 'MULTIPLY' and (a == 0 or b == 0): result = 0.
            elif mode == 'MULTIPLY_ADD' and (a == 0 or b == 0): result = sub(node.inputs[2])
            elif a is None: return None
            elif mode in ('ABSOLUTE','FLOOR','CEIL','FRACT','SINE','COSINE','SQRT','ROUND','SIGN','RADIANS','DEGREES'):
                result = {'ABSOLUTE': abs, 'FLOOR': math.floor, 'CEIL': math.ceil,
                          'FRACT': lambda x:x-math.floor(x), 'SINE':math.sin, 'COSINE':math.cos,
                          'SQRT':lambda x:math.sqrt(max(0,x)), 'ROUND':lambda x:math.floor(x+.5),
                          'SIGN':lambda x:float(x>0)-float(x<0), 'RADIANS':math.radians,
                          'DEGREES':math.degrees}[mode](a)
            elif b is None: return None
            else:
                result = {'MULTIPLY':lambda:a*b, 'ADD':lambda:a+b, 'SUBTRACT':lambda:a-b,
                          'DIVIDE':lambda:a/b if b else 0., 'MINIMUM':lambda:min(a,b),
                          'MAXIMUM':lambda:max(a,b), 'LESS_THAN':lambda:float(a<b),
                          'GREATER_THAN':lambda:float(a>b), 'POWER':lambda:a**b,
                          'MULTIPLY_ADD':lambda:a*b+sub(node.inputs[2])}.get(mode, lambda:None)()
            if result is not None and node.use_clamp: result = min(1,max(0,result))
            return result if result is None or math.isfinite(result) else None
        if node.type == 'VALTORGB':
            fac = sub(node.inputs[0])
            if fac is not None:
                color = list(node.color_ramp.evaluate(fac))
                return color[3] if output.name == 'Alpha' else color
        if node.type in ('MIX','MIX_RGB'):
            selected = selected_mix_input(node, groups)
            if selected is not None:
                return sub(selected)
            if getattr(node,'blend_type','MIX')=='MIX' and not getattr(node,'use_alpha',False) and getattr(node,'factor_mode','UNIFORM')=='UNIFORM':
                factor=sub(node.inputs[0])
                index=1 if node.type=='MIX_RGB' else {'FLOAT':2,'VECTOR':4,'RGBA':6}.get(node.data_type)
                if isinstance(factor,(float,int)) and index is not None:
                    if getattr(node,'clamp_factor',node.type=='MIX_RGB'):factor=min(1,max(0,factor))
                    a,b=sub(node.inputs[index]),sub(node.inputs[index+1])
                    if a is not None and b is not None:
                        if isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
                            if node.type=='MIX_RGB' and len(a)>3 and a[3]!=b[3]:return None
                            result=[x*(1-factor)+y*factor for x,y in zip(a,b)]
                        elif isinstance(a,(int,float)) and isinstance(b,(int,float)):
                            result=a*(1-factor)+b*factor
                        else:return None
                        if getattr(node,'use_clamp',False) or getattr(node,'clamp_result',False):
                            result=[min(1,max(0,v)) for v in result] if isinstance(result,list) else min(1,max(0,result))
                        return result
        if node.type == 'MAP_RANGE' and node.data_type == 'FLOAT':
            vals=[sub(node.inputs[n]) for n in ('Value','From Min','From Max','To Min','To Max')]
            if any(v is None for v in vals):return None
            x,a,b,c,d=vals;t=(x-a)/(b-a) if b!=a else 0
            mode=node.interpolation_type
            if mode in ('SMOOTHSTEP','SMOOTHERSTEP'):
                t=min(1,max(0,t));t=t*t*(3-2*t) if mode=='SMOOTHSTEP' else t*t*t*(t*(6*t-15)+10)
            elif mode=='STEPPED':
                steps=sub(node.inputs['Steps'])
                if steps is None:return None
                t=math.floor(t*(steps+1))/steps if steps else 0
            result=c+t*(d-c)
            return min(max(c,d),max(min(c,d),result)) if node.clamp else result
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return None


def analyze_material(material, renderer='CYCLES', check_budget=None):
    """Inspect effective dependencies, including embedded vegetation outputs."""
    from .blender_appearance import nested_material_outputs
    if not material or not material.use_nodes:
        return {'version':VERSION,'domains':[],'nodes':[],'pruned_branches':0,'status':'constant'}
    terminals=nested_material_outputs(material.node_tree, renderer)
    if len(terminals)!=1:
        return {'version':VERSION,'domains':['unknown'],'nodes':[],'status':'ambiguous_output',
                'pruned_branches':0,'outputs':len(terminals)}
    nodes=[]; domains=set(); visited=set(); pruned=0
    def visit(socket, groups):
        nonlocal pruned
        if check_budget:
            check_budget()
        if not socket.is_linked:return
        output=socket.links[0].from_socket;node=output.node
        key=(output.as_pointer(),tuple(g.as_pointer() for g in groups))
        if key in visited:return
        visited.add(key)
        animation=getattr(node.id_data,'animation_data',None)
        if animation and (getattr(animation,'action',None) or getattr(animation,'drivers',())):domains.add('time')
        if node.mute:
            bypass=next((l.from_socket for l in node.internal_links if l.to_socket==output),None)
            if bypass:visit(bypass,groups)
            else:domains.add('unknown')
            return
        if node.type=='REROUTE':visit(node.inputs[0],groups);return
        if node.type=='GROUP_INPUT':
            mapped=external_socket(output,groups)
            if mapped is not None:visit(mapped,groups[:-1])
            else:domains.add('unknown')
            return
        if node.type=='GROUP':
            target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                         for s in n.inputs if s.identifier==output.identifier),None)
            if target:visit(target,(*groups,node))
            else:domains.add('unknown')
            return
        selected = selected_mix_input(node, groups)
        if selected is not None:
            visit(selected, groups); pruned += 1; return
        mode=getattr(node,'operation',None)
        entry={'type':node.bl_idname,'output':output.name}
        entry['capability']=node_capability(node,output)
        entry['data_domains']=sorted(output_domains(output,groups))
        if mode:entry['mode']=mode
        if node.type=='TEX_NOISE':
            entry.update(detail=socket_value(node.inputs['Detail']), compatibility='unverified',
                         default='local_cache', basis='estimate')
        nodes.append(entry)
        if constant_socket(socket,groups) is not None:
            pruned+=sum(s.is_linked for s in node.inputs);return
        if node.type=='MIX_SHADER':
            fac=constant_socket(node.inputs[0],groups)
            if fac in (0,1):visit(node.inputs[1+int(fac)],groups);pruned+=1;return
        if node.type in ('OBJECT_INFO',):domains.add('instance')
        elif node.type in ('VERTEX_COLOR','ATTRIBUTE'):domains.add('vertex')
        elif node.type=='NEW_GEOMETRY':domains.add('island' if output.name=='Random Per Island' else 'world' if output.name in ('Normal','True Normal','Position') else 'vertex')
        elif node.type=='TEX_COORD':domains.add({'Generated':'object','Object':'object','UV':'uv','Normal':'object',
                                               'Camera':'view','Reflection':'view','Window':'view'}.get(output.name,'unknown'))
        elif node.type in ('UVMAP','TEX_IMAGE'):domains.add('uv')
        elif node.type in ('TEX_NOISE','TEX_VORONOI','TEX_MUSGRAVE'):
            if not node.inputs['Vector'].is_linked:domains.add('object')
        elif node.type in ('FRESNEL','LAYER_WEIGHT','LIGHT_PATH','CAMERA'):domains.add('view')
        elif node.type in ('SCRIPT',):domains.add('unknown')
        for item in node.inputs:visit(item,groups)
    path,terminal=terminals[0]
    for name in ('Surface','Volume','Displacement'):
        visit(terminal.inputs[name],tuple(path))
    graph={name:effective_graph(terminal.inputs[name],tuple(path)) for name in ('Surface','Volume','Displacement')}
    return {'version':VERSION,'domains':sorted(domains),'nodes':nodes,'pruned_branches':pruned,
            'status':'analyzed','signature':signature(graph),'effective_graph':graph}


def output_domains(output,groups=(),seen=None):
    """Scopes of this output only, so unrelated instance data cannot isolate Noise."""
    if output is None:return {'unknown'}
    node=output.node;key=(output.as_pointer(),tuple(g.as_pointer() for g in groups))
    seen=set() if seen is None else seen
    if key in seen:return set()
    seen=seen|{key}
    def input_domains(socket,context=groups):
        if constant_socket(socket,context) is not None:return set()
        if not socket.is_linked:return {'time'}
        return output_domains(socket.links[0].from_socket,context,seen)
    if node.mute:
        bypass=next((l.from_socket for l in node.internal_links if l.to_socket==output),None)
        return input_domains(bypass) if bypass else {'unknown'}
    if node.type=='GROUP_INPUT':
        mapped=external_socket(output,groups)
        return input_domains(mapped,groups[:-1]) if mapped else {'unknown'}
    if node.type=='GROUP':
        target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                     for s in n.inputs if s.identifier==output.identifier),None)
        return input_domains(target,(*groups,node)) if target else {'unknown'}
    selected = selected_mix_input(node, groups)
    if selected is not None:
        return input_domains(selected)
    domains=set()
    if node.type=='OBJECT_INFO':domains.add('instance')
    if node.type=='VERTEX_COLOR':domains.add('vertex')
    if node.type=='NEW_GEOMETRY':domains.add('view' if output.name in ('Incoming', 'Backfacing') else 'island' if output.name=='Random Per Island' else 'world')
    if node.type=='TEX_COORD' and output.name=='Generated':domains.add('generated')
    if node.type=='TEX_COORD':domains.add({'Generated':'object','Object':'object','UV':'uv','Normal':'object',
                                           'Camera':'view','Reflection':'view','Window':'view'}.get(output.name,'unknown'))
    if node.type in ('UVMAP','TEX_IMAGE'):domains.add('uv')
    if node.type.startswith('TEX_') and node.type!='TEX_IMAGE' and node.inputs.get('Vector') and not node.inputs['Vector'].is_linked:
        domains.update(('object','generated'))
    if node.type in ('FRESNEL','LAYER_WEIGHT','LIGHT_PATH','CAMERA'):domains.add('view')
    if node.type == 'SHADERTORGB': domains.add('light')
    if node.type == 'SCRIPT': domains.add('unknown')
    if node.type == 'BUMP': domains.add('derivative')
    if node.type == 'TEX_IMAGE' and node.image and node.image.source in ('MOVIE', 'SEQUENCE'):
        domains.add('time')
    animation=getattr(node.id_data,'animation_data',None)
    if animation and (getattr(animation,'action',None) or getattr(animation,'drivers',())):domains.add('time')
    for socket in node.inputs:
        if not socket.is_unavailable:domains.update(input_domains(socket))
    return domains


def static_bake_decision(output, groups=(), native_normal=False):
    domains = output_domains(output, groups)
    blocked = domains & {'view', 'light', 'time', 'unknown'}
    if not native_normal:
        blocked |= domains & {'derivative'}
    allowed = output is not None and output.type in ('VALUE', 'INT', 'BOOLEAN', 'VECTOR', 'RGBA') and not blocked
    return {'allowed': allowed, 'domains': sorted(domains),
            'reason': 'static_field' if allowed else 'static_bake_would_change_semantics',
            'blocked_domains': sorted(blocked)}


class FunctionLibrary:
    """Content addressed group functions; resource bindings remain at call sites."""
    def __init__(self):self.definitions={}

    def intern(self, body, bindings, name='', output_type='VALUE'):
        used={};inputs=[];args=[]
        def visit(value):
            if isinstance(value,dict):
                if value.get('op')=='texture_object':
                    port='__resource_'+str(len(bindings))
                    bindings[port]=(value,'TEXTURE2D',value['image'].get('name','Texture'))
                    return visit({'op':'function_input','id':port})
                if value.get('op')=='function_input':
                    port=value['id']
                    if port not in used:
                        used[port]=str(len(used));expr,kind,label=bindings[port]
                        args.append(copy.deepcopy(expr));inputs.append({'id':used[port],'type':kind,'name':label})
                    return {'op':'function_input','id':used[port]}
                return {k:visit(v) for k,v in value.items()}
            if isinstance(value,list):return [visit(v) for v in value]
            return value
        normalized=visit(body)
        identity=signature({'version':VERSION,'body':semantic_ir(normalized),
                            'inputs':[i['type'] for i in inputs],'output_type':output_type})
        definition={'id':identity,'version':VERSION,'name':name,'inputs':inputs,
                    'output_type':output_type,'body':normalized}
        self.definitions.setdefault(identity,definition)
        return {'op':'function_call','function':identity,'args':args}

    def reachable(self, value):
        found={}
        def visit(v):
            if isinstance(v,dict):
                identity=v.get('function') if v.get('op')=='function_call' or v.get('kind')=='function_call' else None
                if identity and identity not in found:
                    if identity not in self.definitions:raise ValueError('Missing material function: '+identity)
                    found[identity]=self.definitions[identity];visit(found[identity]['body'])
                for child in v.values():visit(child)
            elif isinstance(v,list):
                for child in v:visit(child)
        visit(value);return list(found.values())

    def expand(self, value):
        def visit(v,bindings=None):
            if isinstance(v,dict):
                if v.get('op')=='function_input':
                    if bindings is None:raise ValueError('Unbound function input')
                    return copy.deepcopy(bindings[int(v['id'])])
                if v.get('op')=='function_call' or v.get('kind')=='function_call':
                    args=[visit(a,bindings) for a in v['args']]
                    return visit(self.definitions[v['function']]['body'],args)
                return {k:visit(x,bindings) for k,x in v.items()}
            if isinstance(v,list):return [visit(x,bindings) for x in v]
            return v
        return visit(value)


def select_candidate(candidates, *, budget_mb, reserve_percent=20, tolerance=.05):
    """Reject invalid candidates before ranking. Missing metrics never mean zero."""
    accepted=[];rejected=[]
    for c in candidates:
        reason=next((k for k in ('compiled','semantic_valid','ray_tracing_valid') if c.get(k) is not True),None)
        peak=c.get('scene_peak_mb');cost=c.get('gpu_ms')
        if reason is None and (peak is None or cost is None):reason='unmeasured'
        if reason is None and (not math.isfinite(peak) or not math.isfinite(cost) or peak<0 or cost<0):reason='invalid_metrics'
        if reason is None and (not budget_mb or peak>budget_mb*(1-reserve_percent/100)):reason='memory_budget'
        if reason:rejected.append({'id':c['id'],'reason':reason})
        else:accepted.append(c)
    if not accepted:return {'selected':None,'basis':'no_valid_candidate','rejected':rejected}
    fastest=min(c['gpu_ms'] for c in accepted)
    close=[c for c in accepted if c['gpu_ms']<=fastest*(1+tolerance)]
    winner=min(close,key=lambda c:(c.get('baked_bytes',0),c.get('import_ms',0),c['scene_peak_mb'],c['id']))
    return {'selected':winner['id'],'basis':'measured','rejected':rejected}
