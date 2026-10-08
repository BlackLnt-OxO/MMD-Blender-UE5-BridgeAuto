"""Preserved path-tracing baseline: native static world, light and volume extraction.

Lumen may reuse source data; its acceptance does not change this baseline.
"""
import math
import shutil
import time
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from .core import Cancelled, atomic_json, file_hash, read_json, stable_id
from .scene_appearance import grid_layout

_GPU_CHECK_AT = 0.
_EXPRESSION_CACHE = {}


def check_resources(job, force=False):
    """Cooperative boundaries keep completed chunks/cache and respond to stop."""
    global _GPU_CHECK_AT
    from .core import check_stop
    from . import process
    check_stop(job)
    if not force and time.monotonic() < _GPU_CHECK_AT:
        return
    cfg=job.data['settings']
    paused=False
    while True:
        info=process.gpu_memory_info()
        usage=process.gpu_budget_percent(info) or 0
        threshold=cfg.get('ue_vram_resume_percent',80) if paused else cfg.get('ue_vram_pause_percent',90)
        if usage < threshold:
            if paused:
                job.status('scene_resumed','GPU 内存已降至恢复阈值，继续场景转换',gpu_memory=info)
            _GPU_CHECK_AT=time.monotonic()+10
            return
        paused=True
        job.status('scene_wait_vram','专用显存或共享 GPU 内存达到阈值，保留缓存并等待释放',gpu_memory=info)
        for _ in range(5):
            check_stop(job)
            time.sleep(1)


def capabilities():
    import openvdb
    import PyOpenColorIO
    import numpy
    import OpenImageIO
    if bpy.app.version < (5, 2, 0):
        raise RuntimeError('完整雾与光影转换需要部署 Blender 5.2 或更新版本')
    return {'blender': bpy.app.version_string, 'openvdb': str(getattr(openvdb, '__version__', 'bundled')),
            'ocio': PyOpenColorIO.GetVersion(), 'numpy': numpy.__version__,
            'openimageio':OpenImageIO.VERSION_STRING}


def output(tree, kind, renderer):
    if not tree:
        return None
    candidates = [n for n in tree.nodes if n.bl_idname == kind and n.is_active_output]
    target = 'CYCLES' if renderer == 'CYCLES' else 'EEVEE'
    return next((n for n in candidates if getattr(n, 'target', 'ALL') == target),
                next((n for n in candidates if getattr(n, 'target', 'ALL') == 'ALL'), None))


def nested_material_outputs(tree, renderer, ancestors=()):
    if not tree or tree.as_pointer() in ancestors:
        return []
    ancestors = ancestors + (tree.as_pointer(),)
    active = output(tree, 'ShaderNodeOutputMaterial', renderer)
    if active:
        return [([], active)]
    found = []
    for node in tree.nodes:
        if node.type == 'GROUP' and node.node_tree and not node.mute:
            found.extend(([node, *path], terminal) for path, terminal in
                         nested_material_outputs(node.node_tree, renderer, ancestors))
    return found


def expose_material_output(material, renderer):
    """Promote a unique embedded output through copied group interfaces.

    Some vegetation assets place Material Output inside a node group. Blender
    renders it, but top-level translators cannot see it. Preserve the existing
    group inputs, and do not edit shared node groups in place.
    """
    if not material or not material.use_nodes:
        return False
    tree = material.node_tree
    if output(tree, 'ShaderNodeOutputMaterial', renderer):
        return False
    found = nested_material_outputs(tree, renderer)
    if len(found) != 1:
        return False
    path, terminal = found[0]
    if not path:
        return False
    names = [node.name for node in path]
    copied_path = []
    for name in names:
        node = tree.nodes[name]
        node.node_tree = node.node_tree.copy()
        copied_path.append((tree, node))
        tree = node.node_tree
    terminal = tree.nodes[terminal.name]
    ports = {}
    for channel in ('Surface', 'Volume', 'Displacement'):
        socket = terminal.inputs[channel]
        if socket.is_linked:
            ports[channel] = socket.links[0].from_socket
    for parent, group in reversed(copied_path):
        internal = group.node_tree
        group_output = next((n for n in internal.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
        if group_output is None:
            group_output = internal.nodes.new('NodeGroupOutput')
            group_output.is_active_output = True
        promoted = {}
        for channel, source in ports.items():
            interface = internal.interface.new_socket(name='MMDBridge_' + channel, in_out='OUTPUT',
                socket_type='NodeSocketVector' if channel == 'Displacement' else 'NodeSocketShader')
            target = next(s for s in group_output.inputs if s.identifier == interface.identifier)
            internal.links.new(source, target)
            promoted[channel] = next(s for s in group.outputs if s.identifier == interface.identifier)
        ports = promoted
    root = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    root.target = 'CYCLES' if renderer == 'CYCLES' else 'EEVEE'
    root.is_active_output = True
    for channel, source in ports.items():
        material.node_tree.links.new(source, root.inputs[channel])
    return True


def material_channels(material, renderer):
    if not material or not material.use_nodes:
        return True, None
    active = output(material.node_tree, 'ShaderNodeOutputMaterial', renderer)
    if not active:
        # Unknown/embedded outputs do not prove that a mesh has no surface.
        # Only a positively identified output may remove surface geometry.
        return True, None
    return bool(active.inputs['Surface'].is_linked), (active.inputs['Volume'] if active.inputs['Volume'].is_linked else None)


def camera_record(obj, active=False):
    cam = obj.data
    dof = cam.dof
    distance = dof.focus_distance
    if dof.focus_object:
        target = dof.focus_object.matrix_world.translation
        if dof.focus_subtarget and dof.focus_object.pose:
            bone = dof.focus_object.pose.bones.get(dof.focus_subtarget)
            if bone:
                target = (dof.focus_object.matrix_world @ bone.matrix).translation
        axis = obj.matrix_world.to_quaternion() @ Vector((0,0,1))
        distance = abs((obj.matrix_world.translation-target).dot(axis))
    return dict(lens=cam.lens,sensor_width=cam.sensor_width,sensor_height=cam.sensor_height,
                projection=cam.type,sensor_fit=cam.sensor_fit,shift_x=cam.shift_x,shift_y=cam.shift_y,
                clip_start=cam.clip_start,clip_end=cam.clip_end,ortho_scale=cam.ortho_scale,active=active,
                dof={'enabled':dof.use_dof,'focus_distance':distance,'fstop':dof.aperture_fstop,
                     'blades':dof.aperture_blades,'ratio':dof.aperture_ratio,'rotation':dof.aperture_rotation})


def upstream(socket, groups=()):
    """Resolve reroutes, muted nodes and nested shader group interfaces."""
    if not socket.is_linked:
        return socket, groups
    source = socket.links[0].from_socket
    node = source.node
    if node.type == 'REROUTE':
        return upstream(node.inputs[0], groups)
    if node.mute:
        internal = next((l for l in node.internal_links if l.to_socket == source), None)
        if internal:
            return upstream(internal.from_socket, groups)
    if node.type == 'GROUP':
        active = next((n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
        if not active:
            raise ValueError('节点组缺少有效输出: ' + node.name)
        index = list(node.outputs).index(source)
        return upstream(active.inputs[index], groups + (node,))
    if node.type == 'GROUP_INPUT':
        if not groups:
            raise ValueError('节点组输入缺少上下文')
        return upstream(groups[-1].inputs[list(node.outputs).index(source)], groups[:-1])
    return source, groups


def constant(socket, groups=()):
    source, context = upstream(socket, groups)
    if source.is_output:
        if source.node.type in ('VALUE', 'RGB'):
            value = source.default_value
        else:
            return None
    else:
        value = source.default_value
    return value if isinstance(value,str) else list(value) if hasattr(value, '__len__') else float(value)


def expression_value(socket, groups=()):
    """Evaluate static appearance fields with Blender, never invent shader math."""
    value = constant(socket, groups)
    if value is not None:
        return value
    nodes = active_nodes_with_context(socket, groups=groups)
    allowed = {'ShaderNodeBlackbody', 'ShaderNodeMath', 'ShaderNodeVectorMath',
               'ShaderNodeMix', 'ShaderNodeMixRGB', 'ShaderNodeClamp', 'ShaderNodeMapRange',
               'ShaderNodeCombineXYZ', 'ShaderNodeSeparateXYZ', 'ShaderNodeRGB', 'ShaderNodeValue',
               'ShaderNodeValToRGB', 'ShaderNodeHueSaturation', 'ShaderNodeInvert'}
    if any(node.bl_idname not in allowed for node, context in nodes):
        return None
    def defaults(node):
        return [(s.name,list(s.default_value) if hasattr(s.default_value,'__len__') else s.default_value)
                for s in node.inputs if hasattr(s,'default_value')]
    def settings(node):
        scalar=[(p.identifier,getattr(node,p.identifier)) for p in node.bl_rna.properties
                if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and p.identifier not in ('name','label')]
        outputs=[(s.identifier,list(s.default_value) if hasattr(s.default_value,'__len__') else s.default_value)
                 for s in node.outputs if hasattr(s,'default_value')]
        links=[(s.identifier,l.from_socket.as_pointer()) for s in node.inputs for l in s.links]
        ramp=getattr(node,'color_ramp',None)
        ramp_settings=(ramp.color_mode,ramp.interpolation,ramp.hue_interpolation,
                       [(e.position,list(e.color)) for e in ramp.elements]) if ramp else None
        return scalar,outputs,links,ramp_settings
    source,context=upstream(socket,groups)
    key=(source.as_pointer(),tuple(n.as_pointer() for n in context),
         stable_id([(n.bl_idname,n.name,defaults(n),settings(n),
                    [defaults(g) for g in c]) for n,c in nodes]),bpy.context.scene.frame_current)
    if key in _EXPRESSION_CACHE:
        result=_EXPRESSION_CACHE[key]
        return list(result) if isinstance(result,list) else result
    vector = socket.type in ('RGBA', 'VECTOR')
    class StaticFields(Fields):
        def copy(self, socket, groups=()):
            value=constant(socket,groups)
            if value is not None:return None,value
            return super().copy(socket,groups)
    fields = StaticFields([[-1,-1,-1],[1,1,1]], Matrix.Identity(4))
    fields.allowed = fields.allowed | {'ShaderNodeBlackbody'}
    mesh = obj = None
    try:
        fields.attributes([('result', socket, groups, vector)])
        mesh = bpy.data.meshes.new('MMDBridge_StaticAppearance')
        mesh.from_pydata([(0,0,0)], [], [])
        obj = bpy.data.objects.new(mesh.name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        modifier = obj.modifiers.new('Native appearance expression', 'NODES')
        modifier.node_group = fields.tree
        bpy.context.view_layer.update()
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        data = evaluated.attributes['result'].data[0]
        result = list(data.vector) if vector else float(data.value)
        if not all(math.isfinite(v) for v in (result if vector else [result])):
            raise ValueError('外观表达式产生非有限值')
        _EXPRESSION_CACHE[key]=result
        return result
    finally:
        if obj: bpy.data.objects.remove(obj, do_unlink=True)
        if mesh: bpy.data.meshes.remove(mesh)
        bpy.data.node_groups.remove(fields.tree)


def prepare_light_expressions(scene):
    """Finish native helper evaluation before depsgraph instance enumeration."""
    _EXPRESSION_CACHE.clear()
    seen=set()
    for obj in scene.objects:
        if obj.type!='LIGHT' or not obj.data.use_nodes or obj.data.as_pointer() in seen:continue
        seen.add(obj.data.as_pointer())
        active=output(obj.data.node_tree,'ShaderNodeOutputLight',scene.render.engine)
        if not active or not active.inputs['Surface'].is_linked:continue
        socket,context=upstream(active.inputs['Surface'])
        if socket.node.type=='EMISSION':
            expression_value(socket.node.inputs['Color'],context)
            expression_value(socket.node.inputs['Strength'],context)


def angular_light_field(socket, groups=(), depth=0):
    """Small achromatic angular contract; unsupported fields remain explicit."""
    if depth>32:raise ValueError('方向性灯光表达式过深')
    source,context=upstream(socket,groups)
    value=expression_value(socket,groups)
    if value is not None:
        if isinstance(value,list):
            if max(value[:3])-min(value[:3])>1e-6:raise ValueError('方向性彩色发光不能转成标量光函数')
            value=value[0]
        return {'op':'constant','value':float(value)}
    node=source.node
    if node.type=='TEX_IES' and getattr(node,'mode','EXTERNAL')=='EXTERNAL' and not node.inputs['Vector'].is_linked:
        path=Path(bpy.path.abspath(node.filepath)).resolve()
        if not path.is_file():raise ValueError('灯光 IES 文件缺失: '+str(path))
        gain=expression_value(node.inputs['Strength'],context)
        if gain is None or not math.isfinite(gain) or gain<0:raise ValueError('IES Strength 无法可靠求值')
        return {'op':'ies','path':str(path),'sha256':file_hash(path),'gain':gain}
    if node.type=='NEW_GEOMETRY' and source.name=='Incoming':return {'op':'incoming'}
    if node.type=='SEPXYZ':
        return {'op':'component','axis':list(node.outputs).index(source),
                'value':angular_light_field(node.inputs[0],context,depth+1)}
    if node.type=='MATH' and node.operation in ('ADD','SUBTRACT','MULTIPLY','DIVIDE','MINIMUM','MAXIMUM','ABSOLUTE','SINE','COSINE','POWER'):
        count=1 if node.operation in ('ABSOLUTE','SINE','COSINE') else 2
        args=[angular_light_field(s,context,depth+1) for s in list(node.inputs)[:count]]
        if any(a['op']=='ies' for a in args):raise ValueError('组合 IES 表达式尚不能可靠归一化')
        if node.operation=='DIVIDE' and (args[1]['op']!='constant' or abs(args[1]['value'])<1e-8):
            raise ValueError('方向性除法仅支持非零常量分母')
        if node.operation=='POWER':
            raise ValueError('方向性幂运算的负数及零点语义尚不能可靠转换')
        return {'op':'math','mode':node.operation,'args':args,'clamp':node.use_clamp}
    raise ValueError('不支持的方向性灯光字段: '+node.bl_idname+'/'+source.name)


def closures(socket, groups=(), weights=()):
    source, context = upstream(socket, groups)
    if not source.is_output:
        return []  # An empty shader branch contributes zero volume.
    node = source.node
    if node.type == 'ADD_SHADER':
        return closures(node.inputs[0], context, weights) + closures(node.inputs[1], context, weights)
    if node.type == 'MIX_SHADER':
        return (closures(node.inputs[1],context,weights+((node.inputs[0],context,True),)) +
                closures(node.inputs[2],context,weights+((node.inputs[0],context,False),)))
    if node.type not in ('PRINCIPLED_VOLUME', 'VOLUME_SCATTER', 'VOLUME_ABSORPTION', 'EMISSION'):
        raise ValueError('不支持的有效体积闭包: ' + node.bl_idname)
    return [(node, context, weights)]


def light_record(obj, renderer, meters_per_unit):
    light = obj.data
    color, energy = list(light.color), light.energy
    review = []
    directional = None
    if renderer == 'CYCLES' and light.use_nodes:
        active = output(obj.original.data.node_tree, 'ShaderNodeOutputLight', renderer)
        if active and active.inputs['Surface'].is_linked:
            socket, context = upstream(active.inputs['Surface'])
            if socket.node.type == 'EMISSION':
                c, e = expression_value(socket.node.inputs['Color'], context), expression_value(socket.node.inputs['Strength'], context)
                if c is not None and e is not None:
                    # In Blender 5.2 the light's power/color multiply the emission shader.
                    color, energy = [light.color[i]*c[i] for i in range(3)], light.energy*e
                else:
                    try:
                        if c is None:
                            if e is None:raise ValueError('颜色和强度同时含方向依赖，尚不能归一化')
                            directional=angular_light_field(socket.node.inputs['Color'],context)
                            energy=light.energy*e
                        else:
                            directional=angular_light_field(socket.node.inputs['Strength'],context)
                            color=[light.color[i]*c[i] for i in range(3)]
                        if directional.get('op')=='ies':energy*=directional.get('gain',1.)
                        review.append('方向性灯光使用原生 IES/标量 Light Function，需渲染复核')
                    except ValueError as exc:
                        review.append('程序灯光着色器尚不能表示为单个 UE 光源: '+str(exc))
            else:
                review.append('第三方或复杂灯光着色器: ' + socket.node.bl_idname)
    energy *= 2**getattr(light,'exposure',0)
    shape = getattr(light,'shape','SQUARE')
    sx,sy,sz = (abs(v) for v in obj.matrix_world.to_scale())
    if light.type == 'AREA' and shape in ('DISK','ELLIPSE'):
        review.append('椭圆面积灯使用 UE 矩形面积灯近似，散景及软阴影需复核')
    if light.type == 'AREA' and renderer == 'CYCLES' and abs(light.spread-math.pi)>1e-5:
        review.append('面积灯 Spread 的角度分布需要复核')
    if light.type == 'SPOT' and (abs(sx-sy)>1e-5 or abs(sx-sz)>1e-5):
        review.append('非均匀缩放的聚光灯锥形需要复核')
    linking = getattr(obj,'light_linking',None)
    for name in ('receiver_collection','blocker_collection'):
        collection=getattr(linking,name,None)
        if collection:
            review.append('灯光关联集合需要复核: '+name+' / '+collection.name)
    record = {'version': 1, 'renderer': renderer, 'type': light.type, 'color': color, 'energy': energy,
            'meters_per_unit':meters_per_unit,
            'normalize': getattr(light, 'normalize', True), 'shape': shape,
            'size': getattr(light, 'size', 1) * meters_per_unit*(sx if light.type=='AREA' else 1),
            'size_y': (getattr(light,'size_y',light.size) if light.type=='AREA' and light.shape in ('RECTANGLE','ELLIPSE')
                       else getattr(light,'size',1)) * meters_per_unit*(sy if light.type=='AREA' else 1),
            'radius': getattr(light, 'shadow_soft_size', 0) * meters_per_unit,
            'sun_angle': getattr(light, 'angle', 0), 'spot_size': getattr(light, 'spot_size', 1),
            'spot_blend': getattr(light, 'spot_blend', 0), 'cast_shadow': light.use_shadow,
            'volume_factor': getattr(light, 'volume_factor', 1)*getattr(obj,'visible_volume_scatter',True),
            'diffuse_factor': getattr(light, 'diffuse_factor', 1)*getattr(obj,'visible_diffuse',True),
            'specular_factor': getattr(light, 'specular_factor', 1)*getattr(obj,'visible_glossy',True),
            'use_temperature': getattr(light, 'use_temperature', False),
            'temperature': getattr(light, 'temperature', 6500), 'needs_review': review}
    if directional:record['directional']=directional
    return record


class Fields:
    """Copy supported shader fields into Geometry Nodes, keeping native noise/ramp math."""
    allowed = {'ShaderNodeTexNoise', 'ShaderNodeTexVoronoi', 'ShaderNodeTexGradient',
               'ShaderNodeValToRGB', 'ShaderNodeMath', 'ShaderNodeVectorMath', 'ShaderNodeMix',
               'ShaderNodeMixRGB', 'ShaderNodeCombineXYZ', 'ShaderNodeSeparateXYZ',
               'ShaderNodeVectorRotate', 'ShaderNodeRGB', 'ShaderNodeValue', 'ShaderNodeClamp',
               'ShaderNodeMapRange', 'ShaderNodeRGBCurve', 'ShaderNodeHueSaturation', 'ShaderNodeInvert'}

    def __init__(self, bounds, matrix):
        self.bounds, self.matrix = bounds, matrix
        self.tree = bpy.data.node_groups.new('MMDBridge_VolumeFields', 'GeometryNodeTree')
        for io in ('INPUT', 'OUTPUT'):
            self.tree.interface.new_socket(name='Geometry', in_out=io, socket_type='NodeSocketGeometry')
        self.input = self.tree.nodes.new('NodeGroupInput')
        self.output = self.tree.nodes.new('NodeGroupOutput')
        self.position = self.tree.nodes.new('GeometryNodeInputPosition').outputs[0]
        self.cache = {}

    def vector(self, operation, a, value):
        n = self.tree.nodes.new('ShaderNodeVectorMath')
        n.operation = operation
        self.tree.links.new(a, n.inputs[0])
        n.inputs[1].default_value = value
        return n.outputs[0]

    def transformed_position(self,matrix):
        combined=self.tree.nodes.new('ShaderNodeCombineXYZ')
        for axis in range(3):
            dot=self.tree.nodes.new('ShaderNodeVectorMath')
            dot.operation='DOT_PRODUCT'
            dot.inputs[1].default_value=matrix[axis][:3]
            self.tree.links.new(self.position,dot.inputs[0])
            add=self.tree.nodes.new('ShaderNodeMath')
            add.operation='ADD'
            add.inputs[1].default_value=matrix[axis][3]
            self.tree.links.new(dot.outputs['Value'],add.inputs[0])
            self.tree.links.new(add.outputs[0],combined.inputs[axis])
        return combined.outputs[0]

    def copy(self, socket, groups=()):
        source, context = upstream(socket, groups)
        if not source.is_output:
            return None, constant(socket, groups)
        node = source.node
        key = (source.as_pointer(), tuple(n.as_pointer() for n in context))
        if key in self.cache:
            return self.cache[key], None
        if node.type == 'TEX_COORD':
            if source.name == 'Generated':
                lo, hi = self.bounds
                out = self.vector('SUBTRACT', self.position, lo)
                out = self.vector('DIVIDE', out, [hi[i] - lo[i] for i in range(3)])
            elif source.name in ('Object', 'Geometry') and not getattr(node, 'object', None):
                out = self.position
            elif source.name=='Object' and node.object:
                out=self.transformed_position(node.object.matrix_world.inverted() @ self.matrix)
            else:
                raise ValueError('体积不支持的坐标输出: ' + source.name)
            self.cache[key] = out
            return out, None
        if node.type=='NEW_GEOMETRY' and source.name=='Position':
            out=self.transformed_position(self.matrix)
            self.cache[key]=out
            return out,None
        if node.type == 'ATTRIBUTE':
            clone = self.tree.nodes.new('GeometryNodeInputNamedAttribute')
            clone.data_type = 'FLOAT' if source.name in ('Fac','Alpha') else 'FLOAT_VECTOR'
            clone.inputs['Name'].default_value = node.attribute_name
            out = clone.outputs['Attribute']
            self.cache[key] = out
            return out,None
        if node.type == 'MAPPING':
            if any(s.is_linked for s in list(node.inputs)[1:]):
                raise ValueError('动态 Mapping 变换需要复核')
            out, val = self.copy(node.inputs['Vector'], context)
            if out is None:
                n = self.tree.nodes.new('ShaderNodeCombineXYZ')
                for i, x in enumerate(val):
                    n.inputs[i].default_value = x
                out = n.outputs[0]
            if node.vector_type not in ('POINT', 'VECTOR'):
                raise ValueError('体积 Mapping 类型不支持: ' + node.vector_type)
            out = self.vector('MULTIPLY', out, node.inputs['Scale'].default_value)
            rot = self.tree.nodes.new('ShaderNodeVectorRotate')
            rot.rotation_type = 'EULER_XYZ'
            rot.inputs['Rotation'].default_value = node.inputs['Rotation'].default_value
            self.tree.links.new(out, rot.inputs['Vector'])
            out = rot.outputs[0]
            if node.vector_type == 'POINT':
                out = self.vector('ADD', out, node.inputs['Location'].default_value)
            self.cache[key] = out
            return out, None
        if node.bl_idname not in self.allowed:
            raise ValueError('不支持的有效体积字段节点: ' + node.bl_idname + ' / ' + node.name)
        clone = self.tree.nodes.new(node.bl_idname)
        for prop in node.bl_rna.properties:
            name = prop.identifier
            if prop.is_readonly or name in ('name', 'label', 'location', 'parent', 'select'):
                continue
            if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
                try:
                    setattr(clone, name, getattr(node, name))
                except (TypeError, ValueError, AttributeError):
                    pass
        if node.type == 'VALTORGB':
            ramp = clone.color_ramp
            for attr in ('interpolation', 'color_mode', 'hue_interpolation'):
                setattr(ramp, attr, getattr(node.color_ramp, attr))
            while len(ramp.elements) > 1:
                ramp.elements.remove(ramp.elements[-1])
            for i, e in enumerate(node.color_ramp.elements):
                t = ramp.elements[0] if i == 0 else ramp.elements.new(e.position)
                t.position, t.color = e.position, e.color
        elif node.type == 'CURVE_RGB':
            raise ValueError('体积 RGB 曲线暂未可靠复制')
        for i, inp in enumerate(node.inputs):
            if i >= len(clone.inputs) or not hasattr(inp, 'default_value'):
                continue
            target = clone.inputs[i]
            if inp.is_linked:
                expression, val = self.copy(inp, context)
                if expression:
                    self.tree.links.new(expression, target)
                else:
                    target.default_value = val
            else:
                target.default_value = inp.default_value
        # Unconnected texture Vector uses Generated in shader graphs.
        if node.type in ('TEX_NOISE', 'TEX_VORONOI', 'TEX_GRADIENT') and not node.inputs['Vector'].is_linked:
            lo, hi = self.bounds
            coord = self.vector('DIVIDE', self.vector('SUBTRACT', self.position, lo), [hi[i]-lo[i] for i in range(3)])
            self.tree.links.new(coord, clone.inputs['Vector'])
        out = clone.outputs[list(node.outputs).index(source)]
        self.cache[key] = out
        return out, None

    def attributes(self, specs, grid_names=()):
        geometry = self.input.outputs['Geometry']
        for name, socket, context, vector in specs:
            n = self.tree.nodes.new('GeometryNodeStoreNamedAttribute')
            n.domain, n.data_type = 'POINT', 'FLOAT_VECTOR' if vector else 'FLOAT'
            n.inputs['Name'].default_value = name
            self.tree.links.new(geometry, n.inputs['Geometry'])
            if socket is None:
                expression,value = None,(1.,1.,1.) if vector else 1.
                if context[0]=='blackbody':
                    temperature=self.tree.nodes.new('GeometryNodeInputNamedAttribute')
                    temperature.data_type='FLOAT'
                    temperature.inputs['Name'].default_value=context[1]
                    attribute=self.tree.nodes.new('GeometryNodeInputNamedAttribute')
                    attribute.data_type='FLOAT'
                    attribute.inputs['Name'].default_value=context[2]
                    multiply=self.tree.nodes.new('ShaderNodeMath')
                    multiply.operation='MULTIPLY'
                    self.tree.links.new(temperature.outputs['Attribute'],multiply.inputs[0])
                    self.tree.links.new(attribute.outputs['Attribute'],multiply.inputs[1])
                    blackbody=self.tree.nodes.new('ShaderNodeBlackbody')
                    self.tree.links.new(multiply.outputs[0],blackbody.inputs['Temperature'])
                    expression=blackbody.outputs[0]
                elif context[0] in grid_names:
                    attribute = self.tree.nodes.new('GeometryNodeInputNamedAttribute')
                    attribute.data_type = 'FLOAT_VECTOR' if vector else 'FLOAT'
                    attribute.inputs['Name'].default_value = context[0]
                    expression = attribute.outputs['Attribute']
            else:
                expression, value = self.copy(socket, context)
            if expression:
                self.tree.links.new(expression, n.inputs['Value'])
            else:
                n.inputs['Value'].default_value = value[:3] if vector else value
            geometry = n.outputs['Geometry']
        self.tree.links.new(geometry, self.output.inputs['Geometry'])


class WorldFields(Fields):
    """Explicit source-camera approximation for previously unsupported world fields."""
    def copy(self, socket, groups=()):
        value=constant(socket,groups)
        if value is not None:return None,value
        source, context = upstream(socket, groups)
        if not source.is_output:return super().copy(socket, groups)
        node = source.node
        key = (source.as_pointer(),tuple(n.as_pointer() for n in context))
        if key in self.cache:return self.cache[key],None
        if node.type=='LIGHT_PATH':
            if source.name not in ('Is Camera Ray','Is Shadow Ray','Is Diffuse Ray','Is Glossy Ray',
                                   'Is Singular Ray','Is Reflection Ray','Is Transmission Ray'):
                raise ValueError('世界雾无法冻结的射线信息: '+source.name)
            return None,float(source.name=='Is Camera Ray')
        if node.type=='TEX_COORD' and source.name=='Camera':
            camera=bpy.context.scene.camera
            if not camera:raise ValueError('世界雾相机坐标需要源活动相机')
            out=self.transformed_position(camera.matrix_world.inverted() @ self.matrix)
        elif node.type=='NORMAL':
            if source.name=='Normal':return None,list(node.outputs['Normal'].default_value)
            value,val=self.copy(node.inputs['Normal'],context)
            dot=self.tree.nodes.new('ShaderNodeVectorMath');dot.operation='DOT_PRODUCT'
            if value:self.tree.links.new(value,dot.inputs[0])
            else:dot.inputs[0].default_value=val
            dot.inputs[1].default_value=node.outputs['Normal'].default_value
            # Cycles' Normal node exposes the negated dot product.
            invert=self.tree.nodes.new('ShaderNodeMath');invert.operation='MULTIPLY';invert.inputs[1].default_value=-1
            self.tree.links.new(dot.outputs['Value'],invert.inputs[0]);out=invert.outputs[0]
        elif node.type=='MAPPING' and any(s.is_linked for s in list(node.inputs)[1:]):
            if node.vector_type not in ('POINT','VECTOR'):
                raise ValueError('世界雾动态 Mapping 类型尚不支持: '+node.vector_type)
            value,val=self.copy(node.inputs['Vector'],context)
            if value is None:
                constant_node=self.tree.nodes.new('ShaderNodeCombineXYZ')
                for i,v in enumerate(val):constant_node.inputs[i].default_value=v
                value=constant_node.outputs[0]
            scale=self.tree.nodes.new('ShaderNodeVectorMath');scale.operation='MULTIPLY'
            self.tree.links.new(value,scale.inputs[0]);p,v=self.copy(node.inputs['Scale'],context)
            if p:self.tree.links.new(p,scale.inputs[1])
            else:scale.inputs[1].default_value=v
            rotate=self.tree.nodes.new('ShaderNodeVectorRotate');rotate.rotation_type='EULER_XYZ'
            self.tree.links.new(scale.outputs[0],rotate.inputs['Vector']);p,v=self.copy(node.inputs['Rotation'],context)
            if p:self.tree.links.new(p,rotate.inputs['Rotation'])
            else:rotate.inputs['Rotation'].default_value=v
            out=rotate.outputs[0]
            if node.vector_type=='POINT':
                add=self.tree.nodes.new('ShaderNodeVectorMath');add.operation='ADD'
                self.tree.links.new(out,add.inputs[0]);p,v=self.copy(node.inputs['Location'],context)
                if p:self.tree.links.new(p,add.inputs[1])
                else:add.inputs[1].default_value=v
                out=add.outputs[0]
        else:return super().copy(socket,groups)
        self.cache[key]=out
        return out,None


def volume_specs(socket, allow_mixed=False):
    specs, items = [], []
    phases=[]
    for index, (node, context, weights) in enumerate(closures(socket)):
        fields = {}
        fields['Weights']=[]
        for i,(weight,weight_context,inverse) in enumerate(weights):
            key=f'v{index}_weight_{i}'
            specs.append((key,weight,weight_context,False))
            fields['Weights'].append((key,inverse))
        for name in ('Density', 'Color', 'Anisotropy', 'Absorption Color', 'Emission Color', 'Emission Strength',
                     'Blackbody Intensity','Blackbody Tint','Temperature','Strength'):
            if name in node.inputs:
                key = f'v{index}_{name.replace(" ", "_")}'
                specs.append((key, node.inputs[name], context, 'Color' in name or name=='Blackbody Tint'))
                fields[name] = key
        density_attribute=(constant(node.inputs['Density Attribute'],context) if 'Density Attribute' in node.inputs
                           else getattr(node,'density_attribute',''))
        if node.type == 'PRINCIPLED_VOLUME' and density_attribute:
            key=f'v{index}_density_attribute'
            specs.append((key,None,(density_attribute,),False))
            fields['Density Attribute']=key
        color_attribute=constant(node.inputs['Color Attribute'],context) if 'Color Attribute' in node.inputs else ''
        if color_attribute:
            key=f'v{index}_color_attribute'
            specs.append((key,None,(color_attribute,),True))
            fields['Color Attribute']=key
        if node.type=='VOLUME_SCATTER' and getattr(node,'phase','HENYEY_GREENSTEIN') not in ('HENYEY_GREENSTEIN','HG'):
            raise ValueError('UE 单相函数不能准确表示源散射相函数: '+str(node.phase))
        if 'Anisotropy' in node.inputs:
            phases.append(constant(node.inputs['Anisotropy'],context))
        if 'Blackbody Intensity' in node.inputs:
            blackbody=constant(node.inputs['Blackbody Intensity'],context)
            if blackbody is None or blackbody>0:
                attr=constant(node.inputs['Temperature Attribute'],context) if 'Temperature Attribute' in node.inputs else 'temperature'
                key=f'v{index}_temperature_attribute'
                specs.append((key,None,(attr,),False))
                fields['Temperature Attribute']=key
                key=f'v{index}_blackbody_color'
                specs.append((key,None,('blackbody',fields['Temperature'],fields['Temperature Attribute']),True))
                fields['Blackbody Color']=key
        items.append((node.type, fields))
    if not allow_mixed and len(phases)>1 and (any(p is None for p in phases) or any(abs(p-phases[0])>1e-6 for p in phases)):
        raise ValueError('多个不同散射相函数不能可靠合并为 UE 单一 HG 相函数')
    return specs, items


def coefficients(values, items, shape):
    import numpy as np
    sigma_s, sigma_a, emission = (np.zeros(shape + (3,), dtype='float32') for _ in range(3))
    phase = np.zeros(shape, dtype='float32')
    weight = np.zeros(shape, dtype='float32')
    for kind, fields in items:
        mixing=np.ones(shape,dtype='float32')
        for key,inverse in fields['Weights']:
            value=np.clip(values[key],0,1)
            mixing *= 1-value if inverse else value
        density = np.maximum(0, values[fields['Density']]) if 'Density' in fields else np.ones(shape)
        density = density * mixing
        if 'Density Attribute' in fields:
            density = density * values[fields['Density Attribute']]
        color = np.clip(values[fields['Color']], 0, 1)
        if 'Color Attribute' in fields:
            color=np.clip(color*values[fields['Color Attribute']],0,1)
        if kind == 'VOLUME_ABSORPTION':
            sigma_a += density[..., None] * (1-color)
        elif kind in ('PRINCIPLED_VOLUME', 'VOLUME_SCATTER'):
            scatter = density[..., None] * color
            sigma_s += scatter
            if kind == 'PRINCIPLED_VOLUME':
                sigma_a += density[..., None] * (1-color) * (1-np.sqrt(np.maximum(values[fields['Absorption Color']],0)))
            w = scatter.mean(axis=-1)
            phase += w * values[fields['Anisotropy']]
            weight += w
        if 'Emission Strength' in fields:
            emission += values[fields['Emission Color']] * (values[fields['Emission Strength']]*mixing)[..., None]
        if kind=='EMISSION':
            emission += values[fields['Color']] * (values[fields['Strength']]*mixing)[...,None]
        if 'Blackbody Color' in fields:
            temperature=np.maximum(0,values[fields['Temperature']]*values[fields['Temperature Attribute']])
            blackbody=np.maximum(0,values[fields['Blackbody Intensity']])
            intensity=(5.670373e-14/math.pi)*(1+blackbody*(temperature**4-1))
            emission += values[fields['Blackbody Tint']]*values[fields['Blackbody Color']]*(intensity*mixing)[...,None]
    extinction = sigma_s + sigma_a
    albedo = np.divide(sigma_s, extinction, out=np.zeros_like(sigma_s), where=extinction > 0)
    phase = np.divide(phase, weight, out=np.zeros_like(phase), where=weight > 0)
    return extinction, albedo, emission, np.clip(phase, -.99, .99)


def uniform_volume(socket):
    import numpy as np
    specs, items = volume_specs(socket)
    values = {}
    for name, s, context, vector in specs:
        if s is None:
            if context[0]=='blackbody':
                return None
            values[name] = np.ones(3 if vector else (),dtype='float32')
            continue
        v = constant(s, context)
        if v is None:
            return None
        values[name] = np.array(v[:3] if vector else v, dtype='float32')
    ext, alb, em, phase = coefficients(values, items, ())
    # UE height fog exposes a scalar extinction and one phase value.
    if not np.allclose(ext, ext[0], atol=1e-6):
        return None
    if abs(float(phase))>.9:
        return None  # Height fog clamps its phase parameter; SVT retains the source value.
    if ext[0]<=0 and np.any(em>0):
        return None
    return {'extinction': float(ext[0]), 'albedo': alb.tolist(), 'emission': em.tolist(), 'anisotropy': float(phase)}


def bake_volume(record, socket, mesh, folder, job, resolution, meters_per_unit):
    import numpy as np
    import openvdb as vdb
    from .blender_usd import check_stop
    check_stop(job)
    source_bounds=record.get('source_bounds',record['bounds'])
    source_matrix=Matrix(record.get('source_matrix',record['matrix']))
    # UE integrates in SVT index space. Bake scale/shear into an isotropic lattice
    # while retaining the object's editable rotation/translation and source transform.
    frame=source_matrix.to_quaternion().to_matrix().to_4x4()
    frame.translation=source_matrix.translation
    to_frame=frame.inverted() @ source_matrix
    to_source=to_frame.inverted()
    corners=[to_frame @ Vector((x,y,z)) for x in (source_bounds[0][0],source_bounds[1][0])
             for y in (source_bounds[0][1],source_bounds[1][1]) for z in (source_bounds[0][2],source_bounds[1][2])]
    bounds=[[min(p[i] for p in corners) for i in range(3)],[max(p[i] for p in corners) for i in range(3)]]
    record.update(source_bounds=source_bounds,source_matrix=[list(r) for r in source_matrix],
                  bounds=bounds,matrix=[list(r) for r in frame],coefficient_units='per_voxel')
    shape, step, origin = grid_layout(bounds, resolution)
    try:
        specs, items = volume_specs(socket)
    except ValueError as exc:
        if '多个不同散射相函数' not in str(exc):raise
        specs, items = volume_specs(socket, allow_mixed=True)
        record.update(phase_model='scattering_weighted_hg', layout_version=2,
            visual_status='needs_review', phase_approximation=True,
            phase_reason='保留总消光与散射；单 HG 仅匹配散射加权一阶矩，非完整角分布')
        record['source_components'] = [dict(type=n.bl_idname,name=n.name,
            groups=[g.name for g in context],
            anisotropy=constant(n.inputs['Anisotropy'],context) if 'Anisotropy' in n.inputs else None,
            fields={i.name:dict(constant=constant(i,context),
                operators=[dict(type=p.bl_idname,name=p.name,groups=[g.name for g in c])
                           for p,c in active_nodes_with_context(i,groups=context)])
                for i in n.inputs if i.name in ('Density','Color','Anisotropy','Absorption Color','Emission Color','Emission Strength')},
            weights=[dict(inverse=inverse,constant=constant(w,c),
                operators=[p.bl_idname for p,g in active_nodes_with_context(w,groups=c)]) for w,c,inverse in weights],
            dynamic_fields=[i.name for i in n.inputs if i.is_linked],mixing_terms=len(weights))
            for n,context,weights in closures(socket)]
    cache = folder / (record['id'] + '.json')
    source = Path(job.data['settings']['scene']).stat()
    key = stable_id('volume-v6-mixed-phase-constants' if record.get('phase_approximation') else 'volume-v4-isotropic-voxel-units', source.st_size, source.st_mtime_ns,
                    bpy.context.scene.frame_current, record, resolution, bpy.app.version_string)
    if cache.is_file():
        saved = read_json(cache)
        if saved.get('key') == key and all(Path(p['path']).is_file() and file_hash(p['path']) == p['sha256'] for p in saved['files']):
            record.update(saved['volume'], cache_hit=True)
            return
    world_approximation = record.get('phase_approximation') and record.get('kind')=='world_grid'
    field = (WorldFields if world_approximation else Fields)(source_bounds, source_matrix)
    if world_approximation:
        record.update(field_context='source_camera_static',field_approximation=True,
            field_reason='Generated 使用有限采样域，相机坐标与 Camera Ray 分支冻结在参考帧；其他射线和视角不保证等价')
    obj = points = None
    try:
        source_grids=[]
        if record.get('source_vdb'):
            raw = vdb.readAll(record['source_vdb'])
            source_grids = raw[0] if isinstance(raw,tuple) else raw
        field.attributes(specs,[g.name for g in source_grids])
        points = bpy.data.meshes.new('MMDBridge_VolumeSamplePoints')
        obj = bpy.data.objects.new(points.name, points)
        bpy.context.scene.collection.objects.link(obj)
        modifier = obj.modifiers.new('Native shader fields', 'NODES')
        modifier.node_group = field.tree
        grids = {name: vdb.FloatGrid() for name in ('extinction_r', 'extinction_g', 'extinction_b', 'albedo_r', 'albedo_g', 'albedo_b', 'phase', 'emission_r', 'emission_g', 'emission_b')}
        transform = vdb.createLinearTransform(voxelSize=step)
        transform.postTranslate(tuple(origin))
        for name, grid in grids.items():
            grid.name, grid.transform = name, transform
        sdf = None
        if mesh is not None:
            # Refuse ambiguous open containers rather than filling their bounding boxes.
            counts = {}
            for p in mesh.polygons:
                for edge in p.edge_keys:
                    counts[edge] = counts.get(edge, 0) + 1
            if not counts or any(n != 2 for n in counts.values()):
                raise ValueError('体积容器不是闭合流形，无法可靠进行内部裁剪')
            mesh.calc_loop_triangles()
            verts = np.array([(to_frame @ v.co)[:] for v in mesh.vertices], dtype='float32')
            triangles = np.array([t.vertices[:] for t in mesh.loop_triangles], dtype='int32')
            if to_frame.determinant()<0:
                triangles=triangles[:,::-1].copy()
            sdf = vdb.FloatGrid.createLevelSetFromPolygons(verts, triangles=triangles, transform=transform)
        depth = max(1, min(shape[2], 131072 // (shape[0]*shape[1])))
        emitted = False
        for z in range(0, shape[2], depth):
            check_resources(job)
            chunk = (shape[0], shape[1], min(depth, shape[2]-z))
            indices = np.indices(chunk, dtype='float32').reshape(3, -1).T
            indices[:, 2] += z
            positions = indices * step + np.array(origin)
            local_positions=positions @ np.array([list(r)[:3] for r in to_source])[:3].T+np.array(to_source.translation)
            points.clear_geometry()
            points.vertices.add(len(positions))
            points.vertices.foreach_set('co', local_positions.astype('float32').ravel())
            for source_grid in source_grids:
                sampled = sample_grid(source_grid,local_positions)
                vector = sampled.ndim == 2
                attr = points.attributes.get(source_grid.name) or points.attributes.new(source_grid.name,'FLOAT_VECTOR' if vector else 'FLOAT','POINT')
                attr.data.foreach_set('vector' if vector else 'value',sampled.astype('float32').ravel())
            points.update()
            bpy.context.view_layer.update()
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
            values = {}
            for name, s, context, vector in specs:
                values[name] = np.empty(len(positions) * (3 if vector else 1), dtype='float32')
                evaluated.attributes[name].data.foreach_get('vector' if vector else 'value', values[name])
                values[name] = values[name].reshape(chunk + ((3,) if vector else ()))
            extinction, albedo, emission, phase = coefficients(values, items, chunk)
            mask = np.ones(chunk, dtype=bool)
            if sdf:
                distance = np.empty(chunk, dtype='float32')
                sdf.copyToArray(distance, ijk=(0, 0, z))
                mask = distance <= 0
            for i, suffix in enumerate('rgb'):
                grids['extinction_'+suffix].copyFromArray(np.where(mask, extinction[..., i]*step, 0).astype('float32'), ijk=(0,0,z))
                grids['albedo_'+suffix].copyFromArray(np.where(mask, albedo[..., i], 0).astype('float32'), ijk=(0,0,z))
                grids['emission_'+suffix].copyFromArray(np.where(mask, emission[..., i]*step, 0).astype('float32'), ijk=(0,0,z))
            grids['phase'].copyFromArray(np.where(mask, phase, 0).astype('float32'), ijk=(0,0,z))
            emitted |= bool(np.any(emission[mask] > 0))
            job.status('scene_volume', f"原生体积采样 {record['name']} · {z+chunk[2]}/{shape[2]}")
        main = folder / (record['id']+'.vdb')
        # Keep the authored lattice bounds even for emission-only or sparse edge voxels.
        accessor=grids['extinction_r'].getAccessor()
        for corner in ((0,0,0),tuple(n-1 for n in shape)):
            if not accessor.isValueOn(corner):
                accessor.setValueOn(corner,0.)
        vdb.write(str(main), grids=[g for n,g in grids.items() if not n.startswith('emission')])
        files = [{'path':str(main), 'sha256':file_hash(main)}]
        emission_path = None
        if emitted:
            emission_path = folder / (record['id']+'_emission.vdb')
            emission_accessor=grids['emission_r'].getAccessor()
            for corner in ((0,0,0),tuple(n-1 for n in shape)):
                if not emission_accessor.isValueOn(corner):
                    emission_accessor.setValueOn(corner,0.)
            vdb.write(str(emission_path), grids=[g for n,g in grids.items() if n.startswith('emission')])
            files.append({'path':str(emission_path), 'sha256':file_hash(emission_path)})
        record.update(path=str(main), emission_path=str(emission_path) if emission_path else None,
                      resolution=shape, voxel_size=step, origin=origin, cache_hit=False, status='converted')
        atomic_json(cache, {'key':key, 'files':files, 'volume':record})
    finally:
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)
        if points:
            bpy.data.meshes.remove(points)
        bpy.data.node_groups.remove(field.tree)


def sample_grid(grid, positions):
    """Trilinear sampling of a bounded grid window; never densify an entire source VDB."""
    import numpy as np
    origin=np.array(grid.transform.indexToWorld((0,0,0)))
    basis=np.array([np.array(grid.transform.indexToWorld(tuple(int(i==j) for i in range(3))))-origin for j in range(3)]).T
    index=(positions-origin) @ np.linalg.inv(basis).T
    base=np.floor(index).astype('int32')
    fraction=index-base
    lo=base.min(axis=0)
    hi=base.max(axis=0)+2
    vector=grid.valueTypeName in ('vec3s','vec3d','vec3i')
    shape=tuple(hi-lo)+((3,) if vector else ())
    if np.prod(shape)>64_000_000:
        raise ValueError('VDB 变换造成采样窗口过大，需要降低分辨率或调整范围')
    array=np.empty(shape,dtype='float32')
    grid.copyToArray(array,ijk=tuple(int(v) for v in lo))
    result=np.zeros((len(positions),3) if vector else len(positions),dtype='float32')
    relative=base-lo
    for x in (0,1):
        for y in (0,1):
            for z in (0,1):
                offset=np.array([x,y,z])
                weight=np.prod(np.where(offset,fraction,1-fraction),axis=1)
                value=array[relative[:,0]+x,relative[:,1]+y,relative[:,2]+z]
                result += value*(weight[:,None] if vector else weight)
    return result


def freeze_vdb(obj, matrix, cfg, index):
    volume=obj.data
    volume.grids.load()
    path=bpy.path.abspath(volume.grids.frame_filepath or volume.filepath)
    if not Path(path).is_file():
        raise ValueError('VDB 缓存缺失或体积没有外部缓存: '+path)
    import openvdb as vdb
    raw=vdb.readAll(path)
    grids=raw[0] if isinstance(raw,tuple) else raw
    corners=[]
    for grid in grids:
        low,high=grid.evalActiveVoxelBoundingBox()
        for x in (low[0]-.5,high[0]+.5):
            for y in (low[1]-.5,high[1]+.5):
                for z in (low[2]-.5,high[2]+.5):
                    corners.append(grid.transform.indexToWorld((x,y,z)))
    bounds=[[min(p[i] for p in corners) for i in range(3)],[max(p[i] for p in corners) for i in range(3)]]
    channels=[material_channels(m,cfg.get('source_renderer','CYCLES')) for m in volume.materials]
    sockets=[s for surface,s in channels if s is not None]
    if len(sockets)!=1:
        raise ValueError('VDB 必须有一个有效的体积材质输出')
    record={'id':stable_id(cfg['scene'],obj.name,index,'vdb'), 'name':obj.name,'kind':'vdb',
            'bounds':bounds,'matrix':[list(r) for r in matrix],'source_vdb':path,'source_vdb_hash':file_hash(path),
            'grid_names':[g.name for g in grids]}
    return record,sockets[0],None


def write_volumes(layer, volumes, root, content_root, asset_id):
    from pxr import Usd, UsdGeom, UsdVol, UsdShade, Sdf, Gf
    stage = Usd.Stage.Open(str(layer))
    UsdGeom.Scope.Define(stage,'/World/Volumes')
    for record in volumes:
        if record.get('status') != 'converted' or record['kind'] == 'height_fog':
            continue
        path = '/World/Volumes/V_' + record['id']
        record['prim_path'] = path
        volume = UsdVol.Volume.Define(stage, path)
        prim = volume.GetPrim()
        prim.SetMetadata('displayName',record['name'])
        volume.CreateExtentAttr([Gf.Vec3f(*record['bounds'][0]),Gf.Vec3f(*record['bounds'][1])])
        prim.SetMetadata('apiSchemas', Sdf.TokenListOp.CreateExplicit(['SparseVolumeTextureAPI', 'MaterialBindingAPI']))
        matrix = Matrix(record['matrix'])
        # SVT treats index zero as a voxel corner; OpenVDB stores voxel centers.
        offset=Vector((-record['voxel_size']/2,)*3)
        matrix.translation += matrix.to_3x3() @ offset
        record['ue_voxel_origin_offset']=list(offset)
        xform=UsdGeom.Xformable(prim)
        op=next((o for o in xform.GetOrderedXformOps() if o.GetOpName()=='xformOp:transform'),None)
        (op or xform.AddTransformOp()).Set(Gf.Matrix4d(*[matrix[r][c] for c in range(4) for r in range(4)]))
        fields = [('extinction_'+s, 'AttributesA.'+c) for s,c in zip('rgb','RGB')]
        fields += [('phase','AttributesA.A')] + [('albedo_'+s,'AttributesB.'+c) for s,c in zip('rgb','RGB')]
        mappings = []
        for name, channel in fields + ([('emission_'+s,'AttributesA.'+c) for s,c in zip('rgb','RGB')] if record.get('emission_path') else []):
            emission = name.startswith('emission')
            field = UsdVol.OpenVDBAsset.Define(stage, path+'/'+name)
            fp = field.GetPrim()
            fp.SetMetadata('apiSchemas', Sdf.TokenListOp.CreateExplicit(['SparseVolumeTextureAPI']))
            file = record['emission_path'] if emission else record['path']
            field.CreateFilePathAttr(Sdf.AssetPath('./'+Path(file).relative_to(root).as_posix()))
            field.CreateFieldNameAttr(name)
            for attr, value in [('unreal:SVT:attributesADataType','float16'),('unreal:SVT:attributesBDataType','float16')]:
                fp.CreateAttribute(attr, Sdf.ValueTypeNames.Token).Set(value)
            fp.CreateAttribute('unreal:SVT:mappedGridComponents', Sdf.ValueTypeNames.TokenArray).Set(['X'])
            fp.CreateAttribute('unreal:SVT:mappedAttributeChannels', Sdf.ValueTypeNames.TokenArray).Set([channel])
            volume.CreateFieldRelationship(name, field.GetPath())
            mappings.append('Emission' if emission else 'Volume')
        prim.CreateAttribute('unreal:SVT:mappedFields', Sdf.ValueTypeNames.TokenArray).Set([n for n,c in fields] + (['emission_'+s for s in 'rgb'] if record.get('emission_path') else []))
        prim.CreateAttribute('unreal:SVT:mappedMaterialParameters', Sdf.ValueTypeNames.TokenArray).Set(mappings)
        material = UsdShade.Material.Define(stage,path+'/Material')
        shader = UsdShade.Shader.Define(stage,path+'/Material/Shader')
        shader.CreateImplementationSourceAttr('sourceAsset')
        name = 'M_Volume_'+record['id']
        asset = f"{content_root}/A_{asset_id}/Materials/{name}.{name}"
        shader.SetSourceAsset(Sdf.AssetPath(asset), 'unreal')
        material.CreateSurfaceOutput('unreal').ConnectToSource(shader.CreateOutput('out', Sdf.ValueTypeNames.Token))
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)
    stage.GetRootLayer().Save()


def active_nodes(socket, seen=None, groups=()):
    seen = seen if seen is not None else set()
    source, groups = upstream(socket,groups)
    if not source.is_output or source.node.as_pointer() in seen:
        return []
    node = source.node
    seen.add(node.as_pointer())
    result = [node]
    for inp in node.inputs:
        if inp.is_linked:
            result.extend(active_nodes(inp, seen,groups))
    return result


def active_nodes_with_context(socket, seen=None, groups=()):
    """Group instances are distinct even when they share an internal node tree."""
    seen = set() if seen is None else seen
    source, context = upstream(socket, groups)
    if not source.is_output:
        return []
    key = (source.node.as_pointer(), tuple(n.as_pointer() for n in context))
    if key in seen:
        return []
    seen.add(key)
    result = [(source.node, context)]
    for inp in source.node.inputs:
        if inp.is_linked:
            result.extend(active_nodes_with_context(inp, seen, context))
    return result


def world_diagnostics(scene, active):
    entries = active_nodes_with_context(active.inputs['Surface']) if active else []
    return {'name':scene.world.name, 'renderer':scene.render.engine,
        'backgrounds':[{'name':n.name, 'groups':[g.name for g in context],
            'color':constant(n.inputs['Color'],context),
            'strength':constant(n.inputs['Strength'],context)} for n,context in entries if n.type=='BACKGROUND'],
        'skies':[{'name':n.name, **{key:getattr(n,key) for key in (
            'sky_type','sun_disc','sun_elevation','sun_rotation','sun_intensity','sun_size',
            'altitude','air_density','dust_density','ozone_density') if hasattr(n,key)}}
            for n,context in entries if n.type=='TEX_SKY'],
        'color_temperature_nodes':[{'name':n.name,'kelvin':constant(n.inputs[0],context)}
            for n,context in entries if n.type=='BLACKBODY'],
        'ray_dependent':any(n.type=='LIGHT_PATH' for n,context in entries)}


def freeze_world_ray_context(tree, copied_groups):
    """Only isolated world copies may replace camera switches for diffuse lighting."""
    values={'Is Camera Ray':0.,'Is Shadow Ray':0.,'Is Diffuse Ray':1.,'Is Glossy Ray':0.,
            'Is Singular Ray':0.,'Is Reflection Ray':1.,'Is Transmission Ray':0.,
            'Ray Depth':1.,'Diffuse Depth':1.,'Glossy Depth':0.,'Transparent Depth':0.,
            'Transmission Depth':0.,'Portal Depth':0.}
    for node in list(tree.nodes):
        if node.type=='GROUP' and node.node_tree:
            node.node_tree=node.node_tree.copy();copied_groups.append(node.node_tree)
            freeze_world_ray_context(node.node_tree,copied_groups)
        elif node.type=='LIGHT_PATH':
            for port in node.outputs:
                if not port.is_linked:continue
                if port.name not in values:raise ValueError('环境照明无法冻结射线字段: '+port.name)
                replacement=tree.nodes.new('ShaderNodeValue');replacement.outputs[0].default_value=values[port.name]
                for link in list(port.links):tree.links.new(replacement.outputs[0],link.to_socket)


def write_environment_hdr(path, pixels, width, height):
    import numpy as np
    import OpenImageIO as oiio
    if pixels.shape!=(height,width,3) or not np.isfinite(pixels).all():
        raise ValueError('世界 HDR 尺寸或像素无效')
    writer=oiio.ImageOutput.create(str(path))
    try:
        if not writer.open(str(path),oiio.ImageSpec(width,height,3,oiio.FLOAT)) or not writer.write_image(np.roll(pixels,-width//4,axis=1)):
            raise ValueError('线性 HDR 环境写入失败: '+writer.geterror())
    finally:writer.close()


def bake_environment(scene, folder, job):
    import numpy as np
    from .core import check_stop
    check_stop(job)
    active = output(scene.world.node_tree, 'ShaderNodeOutputWorld', scene.render.engine) if scene.world and scene.world.use_nodes else None
    if not scene.world or (scene.world.use_nodes and (not active or not active.inputs['Surface'].is_linked)):
        return {'status':'converted', 'surface':False}
    path = folder / 'Environment.hdr'
    stat = Path(job.data['settings']['scene']).stat()
    key = stable_id('environment-v8-context-diagnostics', stat.st_size, stat.st_mtime_ns, scene.frame_current, bpy.app.version_string)
    metadata = folder / 'Environment.json'
    if metadata.is_file() and path.is_file():
        saved = read_json(metadata)
        if saved.get('key') == key and saved.get('sha256') == file_hash(path):
            return dict(saved['record'], cache_hit=True)
    # Isolated scene: original objects/compositor/volume never contaminate the HDR.
    temp = bpy.data.scenes.new('MMDBridge_Environment')
    world = scene.world.copy()
    if not world.use_nodes:
        world.use_nodes=True
        world.node_tree.nodes.get('Background').inputs['Color'].default_value=(*world.color,1)
    camera_data = bpy.data.cameras.new('MMDBridge_EnvironmentCamera')
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    copied_groups=[]
    try:
        temp.world = world
        out = world.node_tree.nodes.get(active.name) if active else output(world.node_tree, 'ShaderNodeOutputWorld', scene.render.engine)
        # The isolated Cycles bake must use the source renderer's effective output.
        for node in list(world.node_tree.nodes):
            if node.bl_idname == 'ShaderNodeOutputWorld' and node != out:
                world.node_tree.nodes.remove(node)
        out.target = 'ALL'
        out.is_active_output = True
        for link in list(out.inputs['Volume'].links):
            world.node_tree.links.remove(link)
        temp.collection.objects.link(camera)
        temp.camera = camera
        camera.rotation_euler = (math.pi/2, 0, 0)
        camera_data.type, camera_data.panorama_type = 'PANO', 'EQUIRECTANGULAR'
        temp.render.engine = 'CYCLES'
        temp.cycles.device, temp.cycles.samples = 'CPU', 1
        sky_nodes = [n for n in active_nodes(out.inputs['Surface']) if n.type == 'TEX_SKY']
        ray_dependent=any(n.type=='LIGHT_PATH' for n,context in active_nodes_with_context(out.inputs['Surface']))
        width,height=(4096,2048) if sky_nodes else (2048,1024)
        temp.render.resolution_x, temp.render.resolution_y, temp.render.resolution_percentage = width,height,100
        temp.render.image_settings.file_format = 'OPEN_EXR'
        temp.render.image_settings.color_mode, temp.render.image_settings.color_depth = 'RGB', '16'
        temp.view_settings.view_transform = 'Raw'
        full_path=folder/'EnvironmentFull.exr'
        temp.render.filepath = str(full_path)
        job.status('scene_environment', 'Blender 原生烘焙线性 HDR 世界环境')
        check_resources(job,True)
        bpy.ops.render.render(scene=temp.name, write_still=True)
        import OpenImageIO as oiio
        def pixels(file):
            inp=oiio.ImageInput.open(str(file))
            array=np.asarray(inp.read_image(format=oiio.FLOAT),dtype='float32')[:,:,:3]
            inp.close()
            return array
        full=pixels(full_path)
        if full.shape != (height,width,3) or not np.isfinite(full).all():
            raise ValueError('世界 HDR 尺寸或像素无效')
        ambient=full
        sun=None
        review=[]
        if not ray_dependent and len(sky_nodes)==1 and sky_nodes[0].sun_disc:
            sky=sky_nodes[0]
            sky.sun_disc=False
            off_path=folder/'Environment.exr'
            temp.render.filepath=str(off_path)
            bpy.ops.render.render(scene=temp.name,write_still=True)
            ambient=pixels(off_path)
            difference=np.maximum(full-ambient,0)
            lat=(.5-(np.arange(height)+.5)/height)*math.pi
            solid_angle=(2*math.pi/width)*(math.pi/height)*np.cos(lat)[:,None]
            # Derive direction from the native panorama, including the sky model's azimuth convention.
            longitude=((np.arange(width)+.5)/width-.5)*2*math.pi
            brightness=difference.sum(axis=2)
            peak_y,peak_x=np.unravel_index(int(brightness.argmax()),brightness.shape)
            peak=Vector((math.cos(lat[peak_y])*math.sin(longitude[peak_x]),
                         math.cos(lat[peak_y])*math.cos(longitude[peak_x]),math.sin(lat[peak_y])))
            dot=np.cos(lat)[:,None]*(peak.x*np.sin(longitude)+peak.y*np.cos(longitude))[None,:]+peak.z*np.sin(lat)[:,None]
            disc=(dot>=math.cos(max(sky.sun_size,4*math.pi/width))) & (brightness>brightness.max()*.001)
            # Changing a sky's disc can also change low radiance background pixels.
            # Keep that sky energy in the HDR instead of turning it into a directional light.
            difference*=disc[:,:,None]
            ambient=full-difference
            energy=np.sum(difference*solid_angle[:,:,None],axis=(0,1))
            weights=difference.sum(axis=2)*solid_angle
            horizontal=(weights*np.cos(lat)[:,None]).sum(axis=0)
            direction=Vector((float(horizontal@np.sin(longitude)),float(horizontal@np.cos(longitude)),
                              float(weights.sum(axis=1)@np.sin(lat))))
            if direction.length:
                direction.normalize()
            mapping=sky.inputs['Vector'].links[0].from_node if sky.inputs['Vector'].is_linked else None
            if mapping and not (mapping.type=='MAPPING' and not any(s.is_linked for s in list(mapping.inputs)[1:])):
                review.append('天空太阳使用复杂坐标变换，当前保留完整 HDR，太阳方向需要复核')
                ambient=full
            if np.max(energy)>0 and not review:
                matrix=(-direction).to_track_quat('-Z','Y').to_matrix().to_4x4()
                sun={'matrix':[list(r) for r in matrix], 'light':{'version':1,'type':'SUN','energy':1.,'color':energy.tolist(),
                     'sun_angle':sky.sun_size,'cast_shadow':True,'volume_factor':1.,'specular_factor':1.,
                     'use_temperature':False,'temperature':6500,'diffuse_factor':1},'direction':list(direction)}
        background_path=None
        if ray_dependent:
            background_path=folder/'EnvironmentBackground.hdr'
            write_environment_hdr(background_path,full,width,height)
            try:
                freeze_world_ray_context(world.node_tree,copied_groups)
                lighting_path=folder/'EnvironmentLighting.exr';temp.render.filepath=str(lighting_path)
                bpy.ops.render.render(scene=temp.name,write_still=True)
                ambient=pixels(lighting_path)
                review.append('源世界依赖射线类型；天空光使用冻结的 diffuse/reflection 上下文，独立反射分支需复核')
            except (ValueError,RuntimeError,TypeError) as exc:
                ambient=full
                review.append('射线相关环境照明近似使用相机 HDR: '+str(exc))
        write_environment_hdr(path,ambient,width,height)
        record = {'status':'converted', 'surface':True, 'path':str(path), 'sha256':file_hash(path),
                  'color_space':'Linear Rec.709', 'resolution':[width,height], 'cache_hit':False,
                  'sun_separated':sun is not None, 'sun':sun, 'needs_review':review}
        if background_path:
            record.update(background_path=str(background_path),background_sha256=file_hash(background_path),
                          lighting_context='diffuse_reference',background_context='camera',visual_status='needs_review')
        try:
            record['source_world'] = world_diagnostics(scene, active)
        except (ValueError,RuntimeError,TypeError,AttributeError) as exc:
            record['source_world'] = {'name':scene.world.name,'diagnostic_error':str(exc)}
            record['needs_review'].append('世界诊断未完成；有效 HDR 已保留: '+str(exc))
        if len(sky_nodes)>1:
            record['needs_review'].append('多天空混合需要逐个分离太阳，当前保留完整 HDR')
        atomic_json(metadata, {'key':key, 'sha256':record['sha256'], 'record':record})
        return record
    finally:
        bpy.data.scenes.remove(temp)
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
        bpy.data.worlds.remove(world)
        for group in copied_groups:
            bpy.data.node_groups.remove(group)


def bake_look(scene, folder, cfg):
    import numpy as np
    import PyOpenColorIO as ocio
    source = Path(bpy.utils.resource_path('LOCAL')) / 'datafiles/colormanagement/config.ocio'
    config = ocio.Config.CreateFromFile(str(source))
    display = scene.display_settings.display_device
    settings = scene.view_settings
    transform = ocio.DisplayViewTransform(src='Linear Rec.709', display=display, view=settings.view_transform)
    chain = ocio.GroupTransform()
    if settings.look and settings.look != 'None':
        chain.appendTransform(ocio.LookTransform(src='Linear Rec.709', dst='Linear Rec.709', looks=settings.look))
    chain.appendTransform(transform)
    processor = config.getProcessor(chain).getDefaultCPUProcessor()
    # Log shaper retains HDR highlights; LUT describes the display transform only.
    # Exposure remains one editable UE parameter before this transform.
    size = 64
    values = np.maximum(0, np.exp2(np.linspace(-16,16,size,dtype='float32'))-2**-16)
    rgb = np.array([[r,g,b] for b in values for g in values for r in values],dtype='float32')
    tree = getattr(scene, 'compositing_node_group', None) or getattr(scene, 'node_tree', None)
    nodes=[]
    if tree:
        composites=[n for n in tree.nodes if n.type in ('COMPOSITE','GROUP_OUTPUT') and getattr(n,'is_active_output',True)]
        nodes=active_nodes(composites[0].inputs[0]) if composites else []
    grades=[n for n in reversed(nodes) if n.type in ('COLORBALANCE','COLORCORRECTION','CURVE_RGB','HUE_SAT') and not n.mute
            and not any(s.is_linked for s in n.inputs if s.name != 'Image')]
    if grades:
        rgb=native_grade(rgb/(2**settings.exposure),grades,folder)*(2**settings.exposure)
    processor.applyRGB(rgb)
    if settings.gamma != 1:
        rgb = np.maximum(rgb,0)**(1/settings.gamma)
    lut = folder / 'Display.cube'
    with lut.open('w',encoding='ascii') as stream:
        stream.write('TITLE "Blender display"\nLUT_3D_SIZE 64\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        for row in rgb:
            stream.write(' '.join(format(float(v), '.9g') for v in row)+'\n')
    portable = ocio.Config()
    portable.setVersion(2,2)
    portable.setSearchPath('.')
    linear = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE, name='Scene Linear')
    output_space = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE, name='Blender Display')
    shaper = ocio.LogAffineTransform(logSideSlope=[1/32]*3, logSideOffset=[.5]*3,
                                    linSideOffset=[2**-16]*3)
    shaper.setBase(2)
    group = ocio.GroupTransform()
    group.appendTransform(shaper)
    group.appendTransform(ocio.FileTransform(src='Display.cube', interpolation=ocio.INTERP_TETRAHEDRAL))
    output_space.setTransform(group, ocio.COLORSPACE_DIR_FROM_REFERENCE)
    portable.addColorSpace(linear)
    portable.addColorSpace(output_space)
    portable.setRole(ocio.ROLE_DEFAULT, 'Scene Linear')
    portable.setRole(ocio.ROLE_SCENE_LINEAR, 'Scene Linear')
    portable.setRole(ocio.ROLE_COLOR_TIMING, 'Scene Linear')
    log_space = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE,name='Scene Log')
    log_space.setTransform(shaper,ocio.COLORSPACE_DIR_FROM_REFERENCE)
    portable.addColorSpace(log_space)
    portable.setRole(ocio.ROLE_COMPOSITING_LOG,'Scene Log')
    aces = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE, name='ACES2065-1')
    aces.setTransform(config.getProcessor('Linear Rec.709','ACES2065-1').createGroupTransform(),ocio.COLORSPACE_DIR_FROM_REFERENCE)
    portable.addColorSpace(aces)
    portable.setRole('aces_interchange','ACES2065-1')
    portable.addDisplayView('sRGB','Blender','Blender Display','')
    portable.setActiveDisplays('sRGB')
    portable.setActiveViews('Blender')
    portable.validate()
    target = folder / 'config.ocio'
    target.write_text(portable.serialize(), encoding='utf-8')
    review, bloom = [], None
    if tree:
        composites = [n for n in tree.nodes if n.type in ('COMPOSITE','GROUP_OUTPUT') and getattr(n,'is_active_output',True)]
        nodes = active_nodes(composites[0].inputs[0]) if composites else []
        for n in nodes:
            if n.mute or n in grades or n.type in ('R_LAYERS','COMPOSITE','GROUP_OUTPUT','REROUTE'):
                continue
            glare_type=(constant(n.inputs['Type']) if n.type=='GLARE' and 'Type' in n.inputs else getattr(n,'glare_type',''))
            if n.type == 'GLARE' and str(glare_type).upper() == 'BLOOM':
                bloom = {'threshold':getattr(n,'threshold',1), 'strength':1.0}
                for name in ('Threshold','Strength','Size'):
                    if name in n.inputs and not n.inputs[name].is_linked:
                        bloom[name.lower()] = constant(n.inputs[name])
            else:
                review.append('有效合成节点需复核: '+n.bl_idname+' / '+n.name)
    if getattr(settings,'use_white_balance',False):
        review.append('源白平衡尚未包含在 LUT 中')
    return {'status':'converted', 'config':str(target), 'lut':str(lut), 'display':display,
            'view':settings.view_transform, 'look':settings.look, 'gamma':settings.gamma,
            'exposure':settings.exposure, 'bloom':bloom, 'needs_review':review,
            'color_grades':[n.name for n in grades],
            'input':'Scene Linear', 'output':'Blender Display', 'hdr_range_ev':[-16,16]}


def native_grade(rgb, grades, folder):
    """Evaluate pointwise compositor grades on LUT pixels in Blender, not Python approximations."""
    import numpy as np
    import OpenImageIO as oiio
    pixels=np.ones((len(rgb),4),dtype='float32')
    pixels[:,:3]=rgb
    image=bpy.data.images.new('MMDBridge_GradeLUT',512,512,float_buffer=True)
    image.colorspace_settings.name='Linear Rec.709'
    image.pixels.foreach_set(pixels.ravel())
    scene=bpy.data.scenes.new('MMDBridge_Grade')
    tree=bpy.data.node_groups.new('MMDBridge_Grade','CompositorNodeTree')
    camera_data=bpy.data.cameras.new('MMDBridge_GradeCamera')
    camera=bpy.data.objects.new(camera_data.name,camera_data)
    try:
        scene.compositing_node_group=tree
        scene.collection.objects.link(camera)
        scene.camera=camera
        scene.render.engine='CYCLES'
        scene.cycles.device,scene.cycles.samples='CPU',1
        scene.render.resolution_x=scene.render.resolution_y=512
        scene.render.resolution_percentage=100
        scene.render.compositor_device='CPU'
        scene.view_settings.view_transform='Raw'
        source=tree.nodes.new('CompositorNodeImage')
        source.image=image
        previous=source.outputs['Image']
        for node in grades:
            clone=tree.nodes.new(node.bl_idname)
            for p in node.bl_rna.properties:
                if p.is_readonly or p.identifier in ('name','parent','location','select'):
                    continue
                if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
                    try:
                        setattr(clone,p.identifier,getattr(node,p.identifier))
                    except (TypeError,ValueError,AttributeError):
                        pass
            if node.type == 'CURVE_RGB':
                for sc,tc in zip(node.mapping.curves,clone.mapping.curves):
                    while len(tc.points)>2:
                        tc.points.remove(tc.points[-2])
                    for i,p in enumerate(sc.points):
                        t=tc.points[i] if i<2 else tc.points.new(*p.location)
                        t.location,t.handle_type=p.location,p.handle_type
                clone.mapping.update()
            for i,s in enumerate(node.inputs):
                if s.name!='Image' and hasattr(s,'default_value'):
                    clone.inputs[i].default_value=s.default_value
            tree.links.new(previous,clone.inputs['Image'])
            previous=clone.outputs['Image']
        file=tree.nodes.new('CompositorNodeOutputFile')
        file.directory=str(folder)
        file.file_name='Grade'
        file.format.file_format='OPEN_EXR_MULTILAYER'
        file.format.color_mode,file.format.color_depth='RGBA','32'
        file.save_as_render=False
        file.file_output_items.new('RGBA','Grade')
        tree.links.new(previous,file.inputs[0])
        bpy.ops.render.render(scene=scene.name)
        outputs=list(folder.glob('Grade*.exr'))
        if len(outputs)!=1:
            raise ValueError('Blender 全局调色烘焙没有产生唯一输出')
        inp=oiio.ImageInput.open(str(outputs[0]))
        channels=list(inp.spec().channelnames)
        array=np.asarray(inp.read_image(format=oiio.FLOAT),dtype='float32')
        inp.close()
        # OpenImageIO reads top-first; Blender pixels are bottom-first.
        indices=[next(i for i,n in enumerate(channels) if n.split('.')[-1]==c) for c in 'RGB']
        return np.flipud(array).reshape(-1,array.shape[-1])[:,indices].copy()
    finally:
        bpy.data.scenes.remove(scene)
        bpy.data.node_groups.remove(tree)
        bpy.data.objects.remove(camera,do_unlink=True)
        bpy.data.cameras.remove(camera_data)
        bpy.data.images.remove(image)


def export_appearance(scene, frozen_volumes, package, job, meters_per_unit, visible_bounds):
    cfg = job.data['settings']
    folder = package/'Appearance'
    folder.mkdir(exist_ok=True)
    volume_folder = package/'Volumes'
    volume_folder.mkdir(exist_ok=True)
    review, volumes = [], []
    world_output = output(scene.world.node_tree, 'ShaderNodeOutputWorld', scene.render.engine) if scene.world and scene.world.use_nodes else None
    if world_output and world_output.inputs['Volume'].is_linked:
        socket = world_output.inputs['Volume']
        distance = getattr(scene.eevee,'volumetric_end',100/meters_per_unit) if hasattr(scene,'eevee') else 100/meters_per_unit
        if not getattr(getattr(scene,'eevee',None),'use_volume_custom_range',False):
            distance = cfg.get('world_volume_distance',100)/meters_per_unit
        camera = scene.camera.matrix_world.translation if scene.camera else Vector((0,0,0))
        lo, hi = visible_bounds or (list(camera),list(camera))
        lo = [min(lo[i],camera[i]-distance) for i in range(3)]
        hi = [max(hi[i],camera[i]+distance) for i in range(3)]
        padding = [(hi[i]-lo[i])*.05 for i in range(3)]
        record = {'id':stable_id(cfg['scene'],'world_volume'), 'name':'World Volume', 'kind':'world_grid',
                  'bounds':[[lo[i]-padding[i] for i in range(3)],[hi[i]+padding[i] for i in range(3)]],
                  'matrix':[list(r) for r in Matrix.Identity(4)], 'distance':distance*meters_per_unit,
                  'distance_defaulted':not getattr(getattr(scene,'eevee',None),'use_volume_custom_range',False)}
        try:
            try:
                homogeneous = uniform_volume(socket)
            except ValueError as exc:
                if '多个不同散射相函数' not in str(exc):raise
                homogeneous = None
                # Newly supported world volumes must not inherit the sky sphere's
                # huge bounds, which would erase near-camera detail at 256 voxels.
                record['bounds']=[[camera[i]-distance for i in range(3)],
                                  [camera[i]+distance for i in range(3)]]
                record['sampling_policy']='source_camera_distance'
            if homogeneous:
                record.update(homogeneous, kind='height_fog', status='converted')
            else:
                if cfg.get('scene_import_profile') == 'render_optimized' and cfg.get('material_workflow') != 'legacy_main_v1':
                    record.update(status='omitted', reason='渲染减负策略省去稀疏网格体积；保留普通高度雾')
                    review.append({'stage':'volume','name':record['name'],'reason':record['reason']})
                else:
                    bake_volume(record,socket,None,volume_folder,job,cfg.get('volume_resolution',256),meters_per_unit)
            if record.get('phase_approximation'):
                review.append({'stage':'volume','name':record['name'],'reason':record['phase_reason']})
            if record.get('field_approximation'):
                review.append({'stage':'volume','name':record['name'],'reason':record['field_reason']})
        except Cancelled:
            raise
        except (ValueError,RuntimeError,TypeError) as exc:
            record.update(status='needs_review',reason=str(exc))
            review.append({'stage':'volume','name':record['name'],'reason':str(exc)})
        record.update(reference_frame=scene.frame_current,time_policy='static_frame')
        volumes.append(record)
    for record,socket,mesh in frozen_volumes:
        if cfg.get('scene_import_profile') == 'render_optimized' and cfg.get('material_workflow') != 'legacy_main_v1':
            if mesh:
                bpy.data.meshes.remove(mesh)
            review.append({'stage':'volume','name':record['name'],'reason':'渲染减负策略省去局部网格体积'})
            continue
        try:
            bake_volume(record,socket,mesh,volume_folder,job,cfg.get('volume_resolution',256),meters_per_unit)
            if record.get('phase_approximation'):
                review.append({'stage':'volume','name':record['name'],'reason':record['phase_reason']})
        except Cancelled:
            raise
        except (ValueError,RuntimeError,TypeError) as exc:
            record.update(status='needs_review',reason=str(exc))
            review.append({'stage':'volume','name':record['name'],'reason':str(exc)})
        finally:
            if mesh:
                bpy.data.meshes.remove(mesh)
        record.update(reference_frame=scene.frame_current,time_policy='static_frame')
        volumes.append(record)
    environment, look = {}, {}
    for kind, bake in [('environment',lambda:bake_environment(scene,folder,job)),
                       ('render_look',lambda:bake_look(scene,folder,cfg))]:
        try:
            record = bake()
        except Cancelled:
            raise
        except (ValueError,RuntimeError,TypeError) as exc:
            record = {'status':'needs_review','needs_review':[str(exc)]}
        if kind == 'environment':
            environment = record
        else:
            look = record
    for kind, record in [('environment',environment),('render_look',look)]:
        for reason in record.get('needs_review',[]):
            review.append({'stage':kind,'name':kind,'reason':reason})
    return {'environment':environment,'volumes':volumes,'render_look':look,'needs_review':review}
