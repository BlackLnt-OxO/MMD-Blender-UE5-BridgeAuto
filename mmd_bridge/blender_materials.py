"""Preserved path-tracing material baseline; unsupported inputs use native Cycles.

Lumen may reuse source data; its acceptance remains separate.
"""
from array import array
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import bpy
from .core import asset_name, stable_id, atomic_json, read_json, Cancelled
from .material_policy import classify_surface, projected_uv
from .blender_mesh_metrics import world_points, uv_spans, sheet_surface_evidence
from .material_graph import FunctionLibrary, constant_socket, analyze_material, signature
from .material_contract import PRINCIPLED_CHANNELS, closure_features, has_translucent_closure, add_issue, finalize_restoration


class SurfaceConversionError(ValueError):
    """The complete shader closure failed; a local field failure is separate."""


class ChannelConversionError(ValueError):
    """A local channel remains unsupported; do not replace the whole shader."""


class MaterialConversionTimeout(Exception):
    """Cooperative conversion deadline; never swallowed by local bake retries."""


def write_bake_image(image, target, hdr):
    """Write one atomic output; preserve signed float32 data with lossless ZIP."""
    target = Path(target)
    temporary = target.with_name('.' + target.stem + '.partial' + target.suffix)
    try:
        if hdr:
            # Native ImBuf writing avoids two full RGBA copies and preserves
            # scene-linear signed float32 values; EXR bypasses the view transform.
            writer = bpy.data.scenes.new('MMDBridge_ExrWriter')
            try:
                settings = writer.render.image_settings
                settings.file_format = 'OPEN_EXR'
                settings.color_mode = 'RGBA'
                settings.color_depth = '32'
                settings.exr_codec = 'ZIP'
                image.save_render(str(temporary), scene=writer)
            finally:
                bpy.data.scenes.remove(writer)
        else:
            image.filepath_raw = str(temporary)
            image.file_format = 'PNG'
            image.save()
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def native_shader_description(material, check_budget=None):
    """Include real node parameters and external image versions in bake reuse."""
    def tree_signature(tree, stack):
        if check_budget:
            check_budget()
        if tree.as_pointer() in stack:
            raise ValueError('材质节点组循环引用')
        stack=stack | {tree.as_pointer()}
        nodes=list(tree.nodes); index={node.as_pointer():i for i,node in enumerate(nodes)}
        result=[]
        for node in nodes:
            parameters={}
            for prop in node.bl_rna.properties:
                if prop.is_readonly or prop.type not in ('BOOLEAN','INT','FLOAT','STRING','ENUM') or prop.identifier in ('name','label','location','width','height','select','hide','show_options','show_preview','show_texture','use_custom_color','color','warning_propagation'):
                    continue
                try:
                    item=getattr(node,prop.identifier)
                    parameters[prop.identifier]=list(item) if hasattr(item,'__len__') and not isinstance(item,str) else item
                except (TypeError,AttributeError):
                    continue
            entry={'type':node.bl_idname,'params':parameters,
                   'inputs':[(socket.identifier,value(socket)) for socket in node.inputs if hasattr(socket,'default_value') and socket.name!='MMDBridge_Field']}
            if node.type=='GROUP' and node.node_tree:
                entry['group']=tree_signature(node.node_tree,stack)
            if node.type=='TEX_IMAGE' and node.image:
                image=node.image;path=Path(bpy.path.abspath(image.filepath))
                try:
                    info=path.stat(); stamp=[info.st_size,info.st_mtime_ns]
                except OSError:stamp=None
                entry['image']=[str(path),stamp,image.colorspace_settings.name,list(image.size),len(image.packed_file.data) if image.packed_file else None]
            if node.type=='VALTORGB':
                ramp=node.color_ramp
                entry['ramp']=[ramp.interpolation,ramp.color_mode,ramp.hue_interpolation,
                               [(element.position,list(element.color)) for element in ramp.elements]]
            if node.type=='CURVE_RGB':
                curve=node.mapping
                entry['curves']=[[(list(point.location),point.handle_type) for point in channel.points] for channel in curve.curves]
                entry['curve_settings']=[list(curve.black_level),list(curve.white_level),curve.extend,curve.use_clip]
            target=getattr(node,'object',None)
            if target is not None:
                entry['object']=[target.name,[list(row) for row in target.matrix_world]]
            result.append(entry)
        links=sorted((index[link.from_node.as_pointer()],link.from_socket.identifier,
                      index[link.to_node.as_pointer()],link.to_socket.identifier) for link in tree.links
                     if link.from_socket.name!='MMDBridge_Field' and link.to_socket.name!='MMDBridge_Field')
        return [result,links]
    return tree_signature(material.node_tree,set())


def native_shader_signature(material):
    return stable_id(json.dumps(native_shader_description(material),sort_keys=True))


def value(socket):
    v = getattr(socket, "default_value", 0.0)
    return list(v) if hasattr(v, "__len__") and not isinstance(v, str) else v


def pin_source_uv(tree, original_uv, uv_names, depth=0):
    """Pin implicit UV inputs, including copied groups, before changing render UV."""
    if depth > 32:
        raise ValueError("材质组嵌套过深，无法固定源 UV")
    uv_node = tree.nodes.new('ShaderNodeUVMap')
    uv_node.uv_map = original_uv
    for node in list(tree.nodes):
        if node.type == 'GROUP' and node.node_tree:
            node.node_tree = node.node_tree.copy()
            pin_source_uv(node.node_tree, original_uv, uv_names, depth + 1)
        elif node.type == 'TEX_IMAGE' and not node.inputs['Vector'].is_linked:
            tree.links.new(uv_node.outputs['UV'], node.inputs['Vector'])
        elif node.type == 'TEX_COORD':
            for link in list(node.outputs['UV'].links):
                tree.links.new(uv_node.outputs['UV'], link.to_socket)
        elif node.type == 'NORMAL_MAP' and not node.uv_map:
            node.uv_map = original_uv
        elif node.type == 'UVMAP' and node.uv_map not in uv_names:
            node.uv_map = original_uv


def restore_field_socket_types(record):
    """Upgrade cached scalar field expressions without rebaking their pixels."""
    scalar_outputs = {('ShaderNodeTexNoise','Factor'),('ShaderNodeTexNoise','Fac'),
        ('ShaderNodeMath','Value'),('ShaderNodeNewGeometry','Random Per Island'),
        ('ShaderNodeObjectInfo','Random'),('ShaderNodeValToRGB','Alpha')}
    fields = [f for f in record.get('baked_fields',[]) if f.get('socket_type') in ('VALUE','INT')
              or (f.get('node_type'),f.get('output')) in scalar_outputs]
    def visit(value):
        if isinstance(value,dict):
            if value.get('op')=='texture':
                image=value['image']
                if any(f.get('image_id')==image.get('id') or f['channel'] in image.get('name','') for f in fields):
                    value['output']='Value'
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(record)


class Materials:
    def adapt_displacement(self, material, output, shader, record):
        """Map bump-only height onto every reachable closure; report shape loss."""
        port = output.inputs['Displacement']
        if not port.is_linked:
            return
        displacement = port.links[0].from_node
        record['displacement_node'] = displacement.name
        mode = getattr(material, 'displacement_method', getattr(getattr(material, 'cycles', None), 'displacement_method', 'UNKNOWN'))
        record['source_displacement_method'] = mode
        if mode != 'BUMP':
            add_issue(record, 'physical_displacement_unpreserved',
                '源位移模式 ' + str(mode) + '；当前冻结网格未验证着色器位移，法线近似不能还原轮廓', severity='approximation')
        leaves = [node for node in self._surface_nodes(shader) if node.inputs.get('Normal')]
        if displacement.type != 'DISPLACEMENT' or not leaves or any(node.id_data != material.node_tree for node in leaves):
            add_issue(record, 'displacement_adapter_missing',
                '位移节点或跨节点组闭包尚无可靠法线适配: ' + displacement.bl_idname, severity='approximation')
            record['warnings'].append('位移未完整还原，需专项复核')
            return
        for leaf in leaves:
            bump = material.node_tree.nodes.new('ShaderNodeBump')
            for source_key,target_key in (('Height','Height'),('Scale','Distance')):
                source = displacement.inputs[source_key]
                if source.is_linked:
                    material.node_tree.links.new(source.links[0].from_socket,bump.inputs[target_key])
                else:
                    bump.inputs[target_key].default_value=source.default_value
            if leaf.inputs['Normal'].is_linked:
                material.node_tree.links.new(leaf.inputs['Normal'].links[0].from_socket,bump.inputs['Normal'])
            material.node_tree.links.new(bump.outputs['Normal'],leaf.inputs['Normal'])
        for link in list(port.links):material.node_tree.links.remove(link)
        add_issue(record, 'displacement_normal_adapter',
            '高度已作用于全部有效闭包法线；源模式 ' + str(mode) + '，需要法线视觉复核')
        record['warnings'].append('位移高度已合入全部闭包的法线；网格轮廓保持冻结结果')

    def fallback(self, material, error, source_name=None):
        """Keep geometry exportable when one Blender shader cannot be translated."""
        name = source_name or material.name
        context = self._active_record or {}
        identity = stable_id(self.source, name, context.get('id', ''), "fallback-v2")
        record = self.records.get(identity)
        if record is None:
            record = {"id": identity, "source_material_id": stable_id(self.source, name),
                      "name": name, "source_name": name,
                      "asset_name": asset_name("M", name + "_Fallback", identity),
                      "surface": "pbr", "strategy": "nodes", "channels": {
                          "base_color": {"op": "constant", "value": [0.5, 0.5, 0.5, 1.0]}},
                      "warnings": [str(error)], "requires_visual_review": True,
                      "material_error": str(error), "pmx_index": None, "water": False,
                      "material_workflow": self.workflow}
            record['source_preset']={'version':1,'definition':native_shader_description(material),
                                    'editable_scope':'source_only','inactive_features':'reexport_required'}
            record['material_decision']={'basis':'unsupported','visual_status':'needs_review',
                                         'reason':str(error),'full_bake_allowed':False}
            active = self._active_record
            if active is not None and active.get('name') == name:
                for key in ('fallback_attempts', 'basic_pbr_bake', 'material_complex_strategy',
                            'restoration_plan', 'automatic_route', 'material_usage_count', 'transmission_candidate'):
                    if key in active: record[key] = active[key]
            self.records[identity] = record
            self.warnings.append(f"材质 {name} 无法可靠还原：{error}；已使用灰色占位材质")
        material.name = record["asset_name"]
        record["slot_name"] = material.name
        return finalize_restoration(record, self.object.get('_mmd_source_name',self.object.name) if self.object is not None else None)

    @staticmethod
    def _complex_input(socket, seen=None):
        """Only nodes feeding the final socket matter; unused graph nodes do not."""
        if not socket or not socket.is_linked:
            return False
        seen = seen if seen is not None else set()
        node = socket.links[0].from_node
        if node.as_pointer() in seen:
            return False
        seen.add(node.as_pointer())
        if node.type in {"TEX_NOISE", "TEX_VORONOI", "TEX_WAVE", "TEX_MUSGRAVE",
                         "VALTORGB", "MIX_SHADER", "ADD_SHADER", "GROUP", "NEW_GEOMETRY",
                         "OBJECT_INFO", "SCRIPT"}:
            return True
        return any(Materials._complex_input(input_socket, seen) for input_socket in node.inputs)

    def _tiled_base(self, material, channel):
        """Use an existing PBR map where a large procedural overlay cannot tile."""
        aliases = {"base_color": ("albedo", "basecolor", "diffuse", "color"),
                   "roughness": ("roughness", "rough"), "metallic": ("metallic", "metalness"),
                   "normal": ("normal",), "opacity": ("opacity", "alpha")}.get(channel, ())
        images = [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image]
        ranked = [node for node in images if any(alias in (node.name + node.image.name).lower().replace(" ", "")
                                                for alias in aliases)]
        node = ranked[0] if ranked else images[0] if len(images) == 1 else None
        if node:
            try:
                uv = self.expr(node.inputs["Vector"]) if node.inputs["Vector"].is_linked else {"op": "uv", "index": 0}
            except ValueError:
                uv = {"op": "uv", "index": 0}
            return {"op": "texture", "image": self.image(node.image), "output": "Color", "uv": uv,
                    "extension": getattr(node, "extension", "REPEAT")}
        defaults = {"base_color": list(material.diffuse_color), "roughness": material.roughness,
                    "metallic": material.metallic, "opacity": 1.0, "emission": [0, 0, 0, 1]}
        return {"op": "constant", "value": defaults.get(channel, 0.0)}

    def simple_surface(self, material):
        """Conservative check of the active surface, using the real translator."""
        if not material.use_nodes:
            return True
        output = material.node_tree.get_output_node('CYCLES' if bpy.context.scene.render.engine == 'CYCLES' else 'EEVEE')
        if not output or not output.inputs['Surface'].is_linked:
            return False
        if any(output.inputs[name].is_linked for name in ('Volume', 'Displacement')):
            return False
        shader = output.inputs['Surface'].links[0].from_node
        if shader.type != 'BSDF_PRINCIPLED':
            return False
        supported = {'Base Color', 'Metallic', 'Roughness', 'IOR', 'Alpha', 'Normal',
                     'Emission Color', 'Emission Strength'}
        # These lobes need specialist handling even when their weights are constants.
        for name in ('Coat Weight', 'Sheen Weight', 'Transmission Weight',
                     'Subsurface Weight', 'Anisotropic IOR Level'):
            socket = shader.inputs.get(name)
            if socket and (socket.is_linked or abs(float(socket.default_value)) > 1e-6):
                return False
        def simple(expr):
            if expr.get('op') not in {'constant', 'uv', 'texture', 'normal', 'mapping',
                                      'math', 'unary_math', 'mix', 'invert', 'hsv',
                                      'brightness_contrast', 'gamma', 'separate', 'combine',
                                      'to_scalar', 'ramp', 'clamp', 'map_range', 'mapping_3d','blender_uv','checker'}:
                return False
            return all(simple(v) for v in expr.values() if isinstance(v, dict) and 'op' in v)
        self.group_inputs = {}
        self.group_stack = []
        self.field_bake = None
        try:
            return all(socket.name in supported and simple(self.expr(socket))
                       for socket in shader.inputs if socket.is_linked)
        except ValueError:
            return False

    def __init__(self, out, source, resolution=0, strategy="auto", bake_fallback=True, complex_strategy='pbr_bake', force_bake=None, conversion_timeout=120.0, workflow='semantic_plan_v1'):
        self.out = Path(out).resolve()
        self.source = source
        self.resolution = resolution
        self.strategy = strategy
        self.workflow = workflow
        self.bake_fallback = bool(bake_fallback)
        self.complex_strategy = complex_strategy
        self.force_bake = force_bake or {}
        self.conversion_timeout = float(conversion_timeout)
        self._conversion_deadline = None
        self._active_record = None
        self._main_transmission_repair = False
        self._force_static_fields = False
        self._channel_context = None
        self.records = {}
        self.images = {}
        self.warnings = []
        self.object = None
        self.restore_scene_appearance = False
        self.group_inputs, self.group_stack, self.field_cache = {}, [], {}
        self.functions = FunctionLibrary()
        self.field_semantics={}
        self._function_frames = []
        self._surface_contexts = {}
        self.field_bake = None
        self.isolated_baking = False
        self._bake_session = None
        self.field_reasons = {}
        self.field_descriptions = {}
        self.field_uv_dependencies = {}
        self.uniform_sampling = False
        self.bake_stats = {'written': 0, 'reused': 0, 'constant_samples': 0,
                           'constant_reused': 0, 'bytes_written': 0}
        self.meters_per_unit = 1.0
        self.world_bounds = None
        self.material_world_bounds = {}
        self.progress = None
        self.progress_context = ""
        (self.out / "Textures").mkdir(parents=True, exist_ok=True)

    def bake_resolution(self, material):
        if self.resolution:
            return self.resolution
        largest = max((max(node.image.size) for node in material.node_tree.nodes
                       if node.type == "TEX_IMAGE" and node.image and node.image.has_data), default=0)
        return 4096 if largest >= 4096 else 2048 if largest >= 1600 else 1024

    def master_compatible_expr(self, expression):
        """Whether an MI can represent this channel after runtime parameter discovery."""
        if not isinstance(expression, dict):
            return False
        kind = expression.get("op")
        if kind in ("constant", "uv"):
            return True
        if kind == "texture":
            return self.master_compatible_expr(expression.get("uv", {"op": "uv"}))
        if kind == "normal":
            return (self.master_compatible_expr(expression["color"]) and
                    self.master_compatible_expr(expression["strength"]))
        if kind == "math" and expression.get("mode") in ("MULTIPLY", "ADD"):
            # Runtime MI routing accepts a simple texture x/+ scalar or color;
            # texture-to-texture graph composition remains a generated material.
            children = (expression["a"], expression["b"])
            textures = sum(1 for child in children if child.get("op") == "texture")
            return textures <= 1 and all(self.master_compatible_expr(child) for child in children)
        if kind in ("mix", "invert"):
            return all(self.master_compatible_expr(expression[key]) for key in
                       (("factor", "a", "b") if kind == "mix" else ("factor", "color")))
        return False

    def normalized_graph(self, value):
        """Remove batch-local paths while retaining every shading-relevant value."""
        if isinstance(value, dict):
            if "id" in value and "path" in value and "color_space" in value:
                return {"image_id": value["id"], "color_space": value["color_space"]}
            return {key: self.normalized_graph(item) for key, item in sorted(value.items())
                    if key != "path"}
        if isinstance(value, list):
            return [self.normalized_graph(item) for item in value]
        return value

    def image(self, image):
        if image.name in self.images:
            return self.images[image.name]
        identity = stable_id(self.source, image.name)
        ext = Path(image.filepath).suffix.lower() or ".png"
        original = Path(bpy.path.abspath(image.filepath))
        packed = image.packed_file.data if image.packed_file else None
        if packed is not None:
            head = bytes(packed[:12])
        elif original.is_file():
            with original.open("rb") as stream:
                head = stream.read(12)
        else:
            head = b""
        transcode = (ext not in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".exr", ".hdr", ".tif", ".tiff")
                     or ext == ".png" and head and not head.startswith(b"\x89PNG\r\n\x1a\n")
                     or ext in (".jpg", ".jpeg") and head and not head.startswith(b"\xff\xd8"))
        if transcode:
            ext = ".png"
        name = asset_name("T", image.name, identity)
        target = self.out / "Textures" / (name + ext)
        if transcode:
            if image.size[0] == 0 or not image.has_data:
                raise ValueError(f"贴图无法解码: {image.name}")
            _ = image.pixels[0]
            previous_path, previous_format = image.filepath_raw, image.file_format
            try:
                image.filepath_raw = str(target)
                image.file_format = "PNG"
                image.save()
            finally:
                image.filepath_raw, image.file_format = previous_path, previous_format
        elif packed is not None:
            target.write_bytes(packed)
        elif original.is_file():
            if original.resolve() != target.resolve():
                shutil.copy2(original, target)
        elif image.has_data:
            previous = image.filepath_raw
            image.filepath_raw = str(target)
            image.save()
            image.filepath_raw = previous
        else:
            raise ValueError(f"贴图缺失: {image.name}: {original}")
        record = {"id": identity, "path": str(target), "name": image.name,
                  "asset_name": name,
                  "hdr": ext in ('.exr', '.hdr'),
                  "color_space": image.colorspace_settings.name}
        self.images[image.name] = record
        return record

    def expr(self, socket, depth=0):
        self.check_conversion_budget()
        context=self._surface_contexts.get(socket.id_data.as_pointer(),()) if not self.group_stack else ()
        if context:
            self.group_stack[:]=context
            try:return self._expr(socket,depth)
            finally:self.group_stack.clear()
        return self._expr(socket,depth)

    def _expr(self, socket, depth=0):
        try:
            output = socket.links[0].from_socket if socket.is_linked else None
            from .material_graph import static_bake_decision
            cache_static = bool(self._force_static_fields and self.field_bake and output and
                constant_socket(socket, tuple(self.group_stack)) is None and
                output.node.type not in ('TEX_IMAGE', 'UVMAP', 'TEX_COORD', 'GROUP_INPUT', 'NORMAL_MAP') and
                static_bake_decision(output, tuple(self.group_stack))['allowed'])
            if cache_static:
                result = self.field_bake(output)
                if self._function_frames:
                    frame = self._function_frames[-1]
                    port = 'field_' + str(len(frame['bindings']))
                    frame['bindings'][port] = (result, output.type, output.name)
                    result = {'op':'function_input', 'id':port}
            else:
                result = self.expression(socket,depth)
        except ValueError as exc:
            if not socket.is_linked or not getattr(self, 'field_bake', None):
                raise
            # Catch at the unsupported node boundary, not at the final PBR
            # channel: downstream mapped operations remain editable in UE.
            output = socket.links[0].from_socket
            self.field_reasons[(output.as_pointer(), tuple(n.as_pointer() for n in self.group_stack))] = str(exc)
            try:
                result = self.field_bake(output)
            except (ValueError, RuntimeError) as bake_error:
                if self._active_record is not None:
                    self._active_record.setdefault('fallback_attempts', []).append({
                        'scope': 'local_field', 'node_type': output.node.bl_idname,
                        'node_name': output.node.name, 'output': output.name,
                        'group_path': [n.name for n in self.group_stack],
                        'reason': str(exc), 'status': 'failed', 'error': str(bake_error)})
                raise ValueError(str(bake_error)) from bake_error
            if self._function_frames:
                frame = self._function_frames[-1]
                port = 'field_' + str(len(frame['bindings']))
                frame['bindings'][port] = (result, output.type, output.name)
                result = {'op':'function_input', 'id':port}
        if socket.is_linked and socket.type in ('VALUE','INT'):
            source_type = socket.links[0].from_socket.type
            if source_type in ('RGBA','VECTOR'):
                result = {'op':'to_scalar','value':result,
                          'weights':[.2126,.7152,.0722] if source_type == 'RGBA' else [1/3]*3}
        elif socket.is_linked and socket.type == 'VECTOR' and socket.links[0].from_socket.type in ('VALUE','INT'):
            result = {'op':'combine','mode':'RGB','components':[result,result,result]}
        return result

    def expression(self, socket, depth=0):
        if depth > 128:
            raise ValueError("节点递归过深")
        folded = constant_socket(socket, tuple(self.group_stack))
        fold_node=socket.links[0].from_node if socket.is_linked else None
        # Keep ordinary preset parameters editable. Only absorbing zeroes are
        # specialized while building a function; other constant arithmetic stays.
        absorbing=bool(fold_node and fold_node.type=='MATH' and fold_node.operation in ('MULTIPLY','MULTIPLY_ADD')
                       and any(constant_socket(s,tuple(self.group_stack))==0 for s in list(fold_node.inputs)[:2]))
        if folded is not None and fold_node and fold_node.type not in ('GROUP_INPUT','GROUP') and (not self._function_frames or absorbing):
            return {'op':'constant', 'value':folded}
        if not socket.is_linked:
            return {"op": "constant", "value": value(socket)}
        link = socket.links[0]
        node = link.from_node
        out = link.from_socket.name
        def sub(s):
            return self.expr(s, depth+1)
        if node.mute:
            bypass = next((l.from_socket for l in node.internal_links if l.to_socket == link.from_socket), None)
            if bypass is None:
                raise ValueError('静音节点没有有效旁路：'+node.bl_idname)
            return sub(bypass)
        if node.type == "REROUTE":
            return sub(node.inputs[0])
        if node.type == "GROUP_INPUT":
            mapped = self.group_inputs.get(link.from_socket.as_pointer())
            if mapped is None:
                raise ValueError("Node Group 输入无法映射到外部插槽")
            saved = self.group_stack[:]
            frame = self._function_frames[-1] if self._function_frames else None
            capture = bool(frame and self.group_stack and frame['node'] == self.group_stack[-1])
            while self.group_stack and mapped.id_data != self.group_stack[-1].node_tree:
                self.group_stack.pop()
            try:
                if capture:
                    self._function_frames.pop()
                    try:
                        binding = self.expr(mapped, depth + 1)
                    finally:
                        self._function_frames.append(frame)
                    port = link.from_socket.identifier
                    frame['bindings'][port] = (binding, link.from_socket.type, mapped.name)
                    return {'op':'function_input', 'id':port}
                return self.expr(mapped, depth + 1)
            finally:
                self.group_stack[:] = saved
        if node.type == 'GROUP' and node.node_tree:
            return self.group_expression(node, link.from_socket, depth)
        from .material_graph import selected_mix_input
        selected = selected_mix_input(node, tuple(self.group_stack))
        if selected is not None:
            return sub(selected)
        if node.type == 'VERTEX_COLOR':
            if self.object is None:raise ValueError('顶点色缺少几何上下文')
            colors=self.object.data.color_attributes
            attribute=colors.get(node.layer_name) if node.layer_name else colors.active_color
            if attribute is None:return {'op':'constant','value':1. if out=='Alpha' else [1,1,1,1]}
            active=colors.active_color
            if active is not None and attribute.name!=active.name:
                raise ValueError('非活动顶点色需要独立属性通道: '+attribute.name)
            return {'op':'vertex_color','attribute':attribute.name,'output':out}
        if node.type=='NEW_GEOMETRY' and out=='Random Per Island':
            if bpy.context.scene.render.engine!='CYCLES':raise ValueError('网格岛随机值兼容路径目前验证 Cycles 语义')
            if self.object is None:raise ValueError('网格岛随机值缺少几何上下文')
            from .scene_shader_data import ensure_island_layer
            return {'op':'mesh_scalar','semantic':'island_random',**ensure_island_layer(self.object.data)}
        if node.type == 'NEW_GEOMETRY' and out == 'Backfacing':
            return {'op':'backfacing'}
        if node.type=='OBJECT_INFO' and out=='Random':
            if bpy.context.scene.render.engine!='CYCLES':raise ValueError('对象随机值兼容路径目前验证 Cycles 语义')
            return {'op':'object_random','primitive_index':0,'instance_index':0}
        if node.type == 'FRESNEL':
            result = {'op':'fresnel', 'ior':sub(node.inputs['IOR'])}
            if node.inputs['Normal'].is_linked:
                result['normal'] = sub(node.inputs['Normal'])
            return result
        if node.type in ("VALUE", "RGB"):
            return {"op": "constant", "value": value(link.from_socket)}
        if node.type == "TEX_IMAGE":
            if not node.image:
                raise ValueError("图像节点未选择贴图")
            if node.projection != "FLAT" or node.image.source in ("MOVIE", "SEQUENCE", "TILED"):
                raise ValueError("非平面或动态图像需要烘焙")
            resource={'image':self.image(node.image)}
            if self._function_frames:
                frame=self._function_frames[-1];port='texture_'+str(node.as_pointer())
                frame['bindings'][port]=({'op':'texture_object',**resource},'TEXTURE2D',node.image.name)
                resource={'texture':{'op':'function_input','id':port},'color_space':node.image.colorspace_settings.name,
                          'hdr':resource['image']['hdr']}
            return {"op": "texture", **resource, "output": out,
                    "uv": sub(node.inputs["Vector"]) if node.inputs["Vector"].is_linked else {"op": "uv", "index": 0},
                    'coordinate_space':'blender' if node.inputs['Vector'].is_linked else 'ue',
                    "extension": node.extension}
        if node.type == 'TEX_CHECKER':
            if not node.inputs['Vector'].is_linked:
                raise ValueError('Checker 隐式 Generated 尚未绑定原始坐标载体')
            return {'op': 'checker', 'vector': sub(node.inputs['Vector']),
                    'scale': sub(node.inputs['Scale']), 'a': sub(node.inputs['Color1']),
                    'b': sub(node.inputs['Color2']), 'output': out,
                    'antialias': self.workflow == 'legacy_main_v1'}
        if node.type == "TEX_COORD" and out == "UV":
            return {'op':'blender_uv','uv':{"op": "uv", "index": 0}}
        if node.type == "UVMAP":
            index = self.object.data.uv_layers.find(node.uv_map) if self.object and node.uv_map else 0
            if index < 0:
                raise ValueError('指定 UV 不存在: '+node.uv_map)
            uv={"op": "uv", "index": index, "name": node.uv_map}
            return {'op':'blender_uv','uv':uv}
        if node.type == "MAPPING":
            offset_socket=node.inputs.get('Location')
            offset=sub(offset_socket) if offset_socket else {'op':'constant','value':[0,0,0]}
            rotation = sub(node.inputs["Rotation"])
            rotation_value = rotation.get("value") if rotation.get("op") == "constant" else None
            full_vector = True  # Blender Vector remains float3 through all Mapping operations.
            if full_vector:
                def canonical(expr):
                    if expr['op']=='blender_uv':return expr
                    if expr['op']=='uv':return {'op':'blender_uv', 'uv':expr}
                    if expr['op']=='texture':return expr  # sampled fields already contain Blender values
                    if expr['op']=='mapping':
                        return {**expr, 'op':'mapping_3d', 'uv':canonical(expr['uv'])}
                    return {key:canonical(val) if isinstance(val,dict) and 'op' in val else
                            [canonical(item) if isinstance(item,dict) and 'op' in item else item for item in val] if isinstance(val,list) else val
                            for key,val in expr.items()}
                return {'op':'mapping_3d', 'uv':canonical(sub(node.inputs['Vector'])),
                        'scale':sub(node.inputs['Scale']), 'offset':offset,
                        'rotation':rotation, 'vector_type':node.vector_type}
            return {"op": "mapping", "uv": sub(node.inputs["Vector"]),
                    "scale": sub(node.inputs["Scale"]), "offset": offset,
                    "rotation": rotation, "vector_type": node.vector_type}
        blend_modes = ("MIX", "MULTIPLY", "ADD", "SUBTRACT", "SCREEN", "OVERLAY",
                       "DIFFERENCE", "DARKEN", "LIGHTEN", "DIVIDE", 'HUE','SATURATION','VALUE','COLOR')
        if node.type == "MIX_RGB":
            if node.blend_type not in blend_modes:
                raise ValueError("混合模式需要烘焙")
            return {"op": "mix", "mode": node.blend_type, "factor": sub(node.inputs[0]),
                    "a": sub(node.inputs[1]), "b": sub(node.inputs[2]), "clamp": node.use_clamp,
                    "clamp_factor": True}
        if node.type == "MIX":
            if node.data_type not in ('RGBA','FLOAT','VECTOR') or node.data_type=='RGBA' and node.blend_type not in blend_modes:
                raise ValueError("混合类型需要烘焙")
            index = {'FLOAT':2,'VECTOR':4,'RGBA':6}[node.data_type]
            fac_index = 1 if node.data_type=='VECTOR' and node.factor_mode=='NON_UNIFORM' else 0
            return {"op": "mix", "mode": node.blend_type if node.data_type=='RGBA' else 'MIX',
                    "factor": sub(node.inputs[fac_index]), "a": sub(node.inputs[index]),
                    "b": sub(node.inputs[index+1]), "clamp": node.clamp_result if node.data_type=='RGBA' else False,
                    "clamp_factor": node.clamp_factor}
        if node.type == "INVERT":
            return {"op": "invert", "factor": sub(node.inputs[0]), "color": sub(node.inputs[1])}
        if node.type == 'HUE_SAT':
            return {'op':'hsv', **{key:sub(node.inputs[name]) for key,name in
                (('color','Color'),('hue','Hue'),('saturation','Saturation'),
                 ('value','Value'),('factor','Fac' if node.inputs.get('Fac') else 'Factor'))}}
        if node.type == 'BRIGHTCONTRAST':
            return {'op':'brightness_contrast', 'color':sub(node.inputs['Color']),
                    'brightness':sub(node.inputs['Bright']), 'contrast':sub(node.inputs['Contrast'])}
        if node.type == 'GAMMA':
            return {'op':'gamma', 'color':sub(node.inputs['Color']), 'gamma':sub(node.inputs['Gamma'])}
        if node.type == 'RGBTOBW':
            return {'op':'to_scalar', 'value':sub(node.inputs[0]), 'weights':[.2126,.7152,.0722]}
        if node.type in ('SEPRGB', 'SEPXYZ', 'SEPARATE_COLOR'):
            mode = getattr(node, 'mode', 'RGB')
            if mode not in ('RGB','HSV'):
                raise ValueError('Separate Color '+mode+' 需要烘焙')
            return {'op':'separate', 'mode':mode, 'index':list(node.outputs).index(link.from_socket),
                    'color':sub(node.inputs[0])}
        if node.type in ('COMBRGB', 'COMBXYZ', 'COMBINE_COLOR'):
            mode = getattr(node, 'mode', 'RGB')
            if mode not in ('RGB','HSV'):
                raise ValueError('Combine Color '+mode+' 需要烘焙')
            return {'op':'combine', 'mode':mode, 'components':[sub(s) for s in list(node.inputs)[:3]]}
        if node.type == 'CLAMP':
            return {'op':'clamp', 'value':sub(node.inputs[0]), 'min':sub(node.inputs[1]),
                    'max':sub(node.inputs[2]), 'sort':node.clamp_type=='RANGE'}
        if node.type == 'MAP_RANGE' and node.data_type=='FLOAT':
            return {'op':'map_range', 'mode':node.interpolation_type,
                    **{key:sub(node.inputs[name]) for key,name in (('value','Value'),
                       ('from_min','From Min'),('from_max','From Max'),('to_min','To Min'),
                       ('to_max','To Max'))},
                    'steps':sub(node.inputs['Steps']) if node.inputs.get('Steps') else {'op':'constant','value':1},
                    'clamp':node.clamp}
        if node.type == "MATH" and node.operation in ("ABSOLUTE", "FLOOR", "CEIL", "FRACT", "SINE", "COSINE", 'SQRT','ROUND','SIGN','RADIANS','DEGREES'):
            return {"op": "unary_math", "mode": node.operation, "a": sub(node.inputs[0]), "clamp": node.use_clamp}
        if node.type == "MATH" and node.operation in ("MULTIPLY", "ADD", "SUBTRACT", "DIVIDE", "POWER", "MINIMUM", "MAXIMUM", 'LESS_THAN','GREATER_THAN'):
            return {"op": "math", "mode": node.operation, "a": sub(node.inputs[0]), "b": sub(node.inputs[1]), "clamp": node.use_clamp}
        if node.type == 'MATH' and node.operation == 'MULTIPLY_ADD':
            return {'op':'math', 'mode':'ADD', 'clamp':node.use_clamp,
                    'a':{'op':'math','mode':'MULTIPLY','a':sub(node.inputs[0]),
                         'b':sub(node.inputs[1]),'clamp':False}, 'b':sub(node.inputs[2])}
        if node.type == "NORMAL_MAP" and node.space == "TANGENT":
            return {"op": "normal", "color": sub(node.inputs["Color"]), "strength": sub(node.inputs["Strength"])}
        if node.type == "BUMP":
            normal = sub(node.inputs["Normal"]) if node.inputs["Normal"].is_linked else {
                "op": "constant", "value": [0, 0, 1]}
            try:
                height = sub(node.inputs["Height"])
            except ValueError as exc:
                if self.bake_fallback:
                    raise
                if not node.inputs["Normal"].is_linked:
                    raise
                height = {"op": "constant", "value": 0}
                self.warnings.append(f"Bump 高度无法稳定翻译（{exc}），已保留输入法线")
            return {"op": "bump", "normal": normal, "height": height,
                    "strength": sub(node.inputs["Strength"]),
                    "distance": sub(node.inputs["Distance"]), "invert": bool(node.invert)}
        if (node.type == "VALTORGB" and node.color_ramp.interpolation in ("LINEAR", "CONSTANT")
                and node.color_ramp.color_mode == 'RGB'):
            return {"op": "ramp", "factor": sub(node.inputs[0]), "interpolation": node.color_ramp.interpolation,
                    "elements": [{"position": e.position, "color": e.color[3] if out=='Alpha' else list(e.color)} for e in node.color_ramp.elements]}
        if node.type == 'VALTORGB':
            ramp=node.color_ramp
            key=signature([[[e.position,list(e.color)] for e in ramp.elements],
                           ramp.interpolation,ramp.color_mode,ramp.hue_interpolation])
            return {'op':'lookup','image':self.lookup_image(key,
                [list(ramp.evaluate(i/4095)) for i in range(4096)]),
                'factor':sub(node.inputs[0]),'alpha':out=='Alpha'}
        if node.type == 'CURVE_RGB':
            mapping=node.mapping;mapping.initialize()
            descriptor=[[[list(p.location),p.handle_type] for p in c.points] for c in mapping.curves]
            if any(p.location.x<0 or p.location.x>1 for c in mapping.curves for p in c.points):
                raise ValueError('RGB Curves 超出已验证的 0..1 控制点域，需要兼容字段缓存')
            key=signature(['cycles_composite_v2',descriptor,mapping.extend,mapping.use_clip])
            def evaluate(x):
                combined=mapping.evaluate(mapping.curves[3],x)
                return [mapping.evaluate(c,combined) for c in mapping.curves[:3]]+[1.]
            samples=[evaluate(i/4095) for i in range(4096)]
            return {'op':'rgb_curve','image':self.lookup_image(key,samples),'color':sub(node.inputs['Color']),
                    'factor':sub(node.inputs[0]),'black':list(mapping.black_level),'white':list(mapping.white_level),
                    'levels_applied':False,'precomposed':True,
                    'endpoints':[samples[0],samples[-1]],
                    'slopes':[[a-b for a,b in zip(evaluate(-.001),evaluate(0))],
                              [a-b for a,b in zip(evaluate(1.001),evaluate(1))]]}
        raise ValueError(f"{node.bl_idname}/{out} 需要烘焙")

    def lookup_image(self, key, samples):
        image_id='lookup_'+key
        if image_id in self.images:return self.images[image_id]
        image=bpy.data.images.new(image_id,width=len(samples),height=1,float_buffer=True)
        try:
            image.colorspace_settings.name='Non-Color'
            image.pixels.foreach_set([v for row in samples for v in row])
            path=self.out/'Textures'/(image_id+'.exr')
            write_bake_image(image,path,True)
            record={'id':image_id,'path':str(path),'name':image_id,'asset_name':'T_'+image_id,
                    'hdr':True,'color_space':'Non-Color','lookup':True,'width':len(samples)}
            self.images[image_id]=record
            return record
        finally:
            bpy.data.images.remove(image)

    def group_expression(self, node, socket, depth, closure=False, masks=False):
        self.check_conversion_budget()
        output = next((n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
        if output is None:
            raise ValueError('节点组缺少有效输出')
        for item in node.node_tree.nodes:
            if item.type == 'GROUP_INPUT':
                for internal in item.outputs:
                    external = next((s for s in node.inputs if s.identifier == internal.identifier), None)
                    if external is not None:
                        self.group_inputs[internal.as_pointer()] = external
        target = next((s for s in output.inputs if s.identifier == socket.identifier), None)
        if target is None:
            raise ValueError('节点组输出无法映射')
        self.group_stack.append(node)
        frame={'node':node,'bindings':{}}
        capture=not masks
        if capture:self._function_frames.append(frame)
        try:
            if masks:
                return self.surface_masks(target, depth+1)
            body=self.closure(target,depth+1) if closure else self.expr(target,depth+1)
            call=self.functions.intern(body,frame['bindings'],node.node_tree.name,socket.type)
            if closure:call['kind']=call.pop('op')
            return call
        finally:
            if capture:self._function_frames.pop()
            self.group_stack.pop()

    def surface_masks(self, socket, depth=0):
        """Traverse the active shader output, retaining holes outside PBR lobes."""
        self.check_conversion_budget()
        from .surface_coverage import constant, saturate, mix_masks
        if depth > 30 or not socket.is_linked:
            raise ValueError('覆盖率 Shader 未连接或递归过深')
        link = socket.links[0]
        node = link.from_node
        if node.mute:
            bypass = next((l.from_socket for l in node.internal_links if l.to_socket == link.from_socket), None)
            if bypass is None:
                raise ValueError('静音覆盖率节点没有旁路')
            return self.surface_masks(bypass, depth+1)
        if node.type == 'REROUTE':
            return self.surface_masks(node.inputs[0], depth+1)
        if node.type == 'GROUP_INPUT':
            mapped = self.group_inputs.get(link.from_socket.as_pointer())
            if mapped is None:
                raise ValueError('覆盖率节点组输入无法映射')
            saved = self.group_stack[:]
            while self.group_stack and mapped.id_data != self.group_stack[-1].node_tree:
                self.group_stack.pop()
            try:
                return self.surface_masks(mapped, depth+1)
            finally:
                self.group_stack[:] = saved
        if node.type == 'GROUP':
            return self.group_expression(node, link.from_socket, depth, masks=True)
        def field(input_socket):
            try:
                return self.expr(input_socket)
            except ValueError:
                if self.field_bake and input_socket.is_linked:
                    source = input_socket.links[0].from_socket
                    result = self.field_bake(source)
                    if input_socket.type in ('VALUE', 'INT') and source.type in ('RGBA', 'VECTOR'):
                        result = {'op':'to_scalar', 'value':result,
                                  'weights':[.2126,.7152,.0722] if source.type == 'RGBA' else [1/3]*3}
                    return result
                raise
        if node.type in ('MIX_SHADER', 'ADD_SHADER'):
            inputs = [s for s in node.inputs if s.type == 'SHADER']
            fac=constant_socket(node.inputs[0],tuple(self.group_stack)) if node.type=='MIX_SHADER' else None
            if fac in (0,1):return self.surface_masks(inputs[int(fac)],depth+1)
            a, b = (self.surface_masks(s, depth+1) for s in inputs)
            if node.type == 'ADD_SHADER':
                if a['has_transparent'] or b['has_transparent']:
                    raise ValueError('Add Shader 的透明能量叠加不能等价为遮罩覆盖率')
                return dict(a, has_translucent=a['has_translucent'] or b['has_translucent'])
            # Equal opaque masks need no evaluation of view/light-dependent factors.
            if a == b:
                return a
            return mix_masks(a, b, field(node.inputs[0]))
        transparent = node.type == 'BSDF_TRANSPARENT'
        if transparent:
            color = node.inputs['Color']
            if color.is_linked or any(abs(float(c)-1) > 1e-6 for c in value(color)[:3]):
                raise ValueError('有色 Transparent BSDF 需要彩色透射，不能丢弃为透明遮罩')
        elif not (node.type.startswith('BSDF_') or node.type == 'EMISSION'):
            raise ValueError('未知覆盖率闭包：'+node.bl_idname)
        alpha = node.inputs.get('Alpha')
        transmission = node.inputs.get('Transmission Weight')
        return {'opacity': constant(0) if transparent else saturate(field(alpha)) if alpha else constant(1),
                'transmission': constant(1) if node.type == 'BSDF_TRANSLUCENT' else
                                saturate(field(transmission)) if transmission else constant(0),
                'has_transparent': transparent, 'has_translucent': node.type == 'BSDF_TRANSLUCENT'}

    def closure(self, socket, depth=0):
        self.check_conversion_budget()
        if depth > 30 or not socket.is_linked:
            raise ValueError('闭包过深或 Shader 未连接')
        link = socket.links[0]
        node = link.from_node
        if node.mute:
            bypass = next((l.from_socket for l in node.internal_links if l.to_socket == link.from_socket), None)
            if bypass is None:
                raise ValueError('静音闭包没有有效旁路：'+node.bl_idname)
            return self.closure(bypass,depth+1)
        if node.type == 'REROUTE':
            return self.closure(node.inputs[0],depth+1)
        if node.type == 'GROUP_INPUT':
            mapped=self.group_inputs[link.from_socket.as_pointer()]
            saved=self.group_stack[:]
            while self.group_stack and mapped.id_data != self.group_stack[-1].node_tree:
                self.group_stack.pop()
            try:
                return self.closure(mapped,depth+1)
            finally:
                self.group_stack[:]=saved
        if node.type == 'GROUP':
            return self.group_expression(node,link.from_socket,depth,closure=True)
        if node.type in ('MIX_SHADER','ADD_SHADER'):
            inputs = [s for s in node.inputs if s.type == 'SHADER']
            fac=constant_socket(node.inputs[0],tuple(self.group_stack)) if node.type=='MIX_SHADER' else None
            if fac in (0,1):return self.closure(inputs[int(fac)],depth+1)
            if node.type=='MIX_SHADER':
                # White Transparent is handled by the independent coverage graph.
                for index,item in enumerate(inputs):
                    source=item.links[0].from_node if item.is_linked else None
                    if source and source.type=='BSDF_TRANSPARENT':
                        color=constant_socket(source.inputs['Color'],tuple(self.group_stack))
                        if color is not None and all(abs(c-1)<1e-6 for c in color[:3]):
                            return self.closure(inputs[1-index],depth+1)
            result = {'kind':'mix' if node.type == 'MIX_SHADER' else 'add',
                      'a':self.closure(inputs[0],depth+1), 'b':self.closure(inputs[1],depth+1)}
            if node.type == 'MIX_SHADER':
                result['factor'] = self.expr(node.inputs[0])
            return result
        mappings = {
            'BSDF_PRINCIPLED':PRINCIPLED_CHANNELS,
            'BSDF_GLASS':{'base_color':'Color','roughness':'Roughness','ior':'IOR','normal':'Normal'},
            'BSDF_GLOSSY':{'base_color':'Color','roughness':'Roughness','normal':'Normal'},
            'BSDF_DIFFUSE':{'base_color':'Color','roughness':'Roughness','normal':'Normal'},
            'BSDF_TRANSLUCENT':{'base_color':'Color','normal':'Normal'},
            'BSDF_METALLIC':{'base_color':'Base Color','roughness':'Roughness','normal':'Normal'},
            'EMISSION':{'emission':'Color','emission_strength':'Strength'}}
        if node.type not in mappings:
            raise ValueError(f'无法保留闭包 {node.bl_idname}')
        if self.workflow == 'legacy_main_v1':
            from .material_workflow import legacy_channels
            if node.type == 'BSDF_GLASS' and not getattr(self,'_main_transmission_repair',False):
                raise ValueError('旧版主方案不升级透光闭包；失败项可通过实验性补修处理')
            mappings['BSDF_PRINCIPLED'] = legacy_channels(PRINCIPLED_CHANNELS)
            if getattr(self,'_main_transmission_repair',False):
                mappings['BSDF_PRINCIPLED']['transmission']='Transmission Weight'
        if node.type == 'BSDF_PRINCIPLED':
            for name in (('Coat Weight', 'Subsurface Weight', 'Transmission Weight', 'Sheen Weight', 'Anisotropic IOR Level')
                         if self.workflow == 'legacy_main_v1' else ('Sheen Weight','Anisotropic IOR Level')):
                if name=='Transmission Weight' and getattr(self,'_main_transmission_repair',False):continue
                item = node.inputs.get(name)
                if item and (item.is_linked or abs(float(value(item))) > 1e-6):
                    raise ValueError(f'Principled {name} 尚未保留为独立闭包')
            alpha = node.inputs['Alpha']
            if (alpha.is_linked or abs(float(value(alpha))-1) > 1e-6) and not getattr(self,'_main_transmission_repair',False):
                raise ValueError('混合闭包的局部 Alpha 需要透射或覆盖率适配')
        result = {'kind':node.type.lower(), 'channels':{
            key:self.translate_channel(node.inputs[name], key, depth+1) for key,name in mappings[node.type].items()
            if node.inputs.get(name) and (key not in ('normal', 'coat_normal') or node.inputs[name].is_linked)}}
        result['features'] = sorted(closure_features(result))
        return result

    def translate_channel(self, socket, channel, depth=0):
        """Escalate a failed local field without dropping other material channels."""
        previous_channel = self._channel_context
        self._channel_context = channel
        try:
            if (self.workflow != 'legacy_main_v1' and channel in ('normal','coat_normal') and socket.is_linked and self.bake_fallback and
                    self._requested_strategy != 'nodes' and self.object is not None):
                from .material_graph import static_bake_decision
                decision = static_bake_decision(socket.links[0].from_socket, tuple(self.group_stack), native_normal=True)
                if 'derivative' in decision['domains']:
                    raise ValueError('Bump 的世界空间导数使用原生闭包法线烘焙，避免屏幕导数近似')
            return self.expr(socket, depth)
        except ValueError as exc:
            if not self.bake_fallback or self._requested_strategy == 'nodes' or self.object is None or not socket.is_linked:
                raise
            record = self._active_record
            attempt = {'scope': 'material_channel', 'channel': channel,
                       'node_type': socket.links[0].from_node.bl_idname,
                       'node_name': socket.links[0].from_node.name,
                       'output': socket.links[0].from_socket.name,
                       'group_path': [n.name for n in self.group_stack], 'reason': str(exc)}
            record.setdefault('fallback_attempts', []).append(attempt)
            start = time.perf_counter()
            try:
                result = self.procedural_field(socket.links[0].from_socket, self._active_material,
                    self.object, self._active_identity, record, channel_override='normal' if channel == 'coat_normal' else channel)
            except (ValueError, RuntimeError) as bake_error:
                attempt.update(status='failed', error=str(bake_error))
                raise ChannelConversionError(f'{channel} 烘焙兜底失败：{bake_error}') from bake_error
            else:
                attempt.update(status='baked', image_id=result.get('image', {}).get('id'))
                record['requires_visual_review'] = True
                record['strategy_reasons'].append(channel + ' 局部转换失败，已烘焙该通道；其它通道保留')
                return result
            finally:
                attempt['seconds'] = time.perf_counter() - start
        finally:
            self._channel_context = previous_channel

    def field_resolution(self, socket, obj, record):
        """Bounded sampling of static albedo noise; legacy normals/tiled controls stay fixed."""
        if (self.workflow != 'legacy_main_v1' or self._channel_context != 'base_color' or
                socket.node.type != 'TEX_NOISE' or self.surface_policy.get('category') == 'tiled'):
            return self.resolution
        def span(output, seen):
            if output.as_pointer() in seen: return None
            seen = seen | {output.as_pointer()}; node = output.node
            if node.type in ('UVMAP', 'TEX_COORD'):
                layer=obj.data.uv_layers.get(getattr(node,'uv_map','')) or (obj.data.uv_layers[0] if obj.data.uv_layers else None)
                if layer is None:return None
                import numpy as np
                values=np.empty(len(layer.data)*2,dtype=np.float32);layer.data.foreach_get('uv',values)
                return float(np.ptp(values.reshape((-1,2)),axis=0).max())
            if node.type=='MAPPING' and node.inputs['Vector'].is_linked:
                scale=constant_socket(node.inputs['Scale'],tuple(self.group_stack))
                inner=span(node.inputs['Vector'].links[0].from_socket,seen)
                if inner is None or scale is None:return None
                factors=[abs(v) for v in scale[:3]]
                gain=max(1/max(v,1e-6) for v in factors) if node.vector_type=='TEXTURE' else max(factors)
                return inner*gain
            return None
        vector=socket.node.inputs['Vector']
        width=span(vector.links[0].from_socket,set()) if vector.is_linked else 1.
        scale=constant_socket(socket.node.inputs['Scale'],tuple(self.group_stack))
        detail=constant_socket(socket.node.inputs['Detail'],tuple(self.group_stack))
        if width is None or scale is None or detail is None:return self.resolution
        import math
        requested=max(1,math.ceil(width*abs(scale)*2**max(0,min(float(detail),15))*8))
        base=self.bake_resolution(self._active_material)
        cap=min(4096,base*4)
        chosen=min(cap,max(base,2**math.ceil(math.log2(requested))))
        record.setdefault('field_sampling',[]).append({'node':socket.node.name,'channel':'base_color',
            'requested_resolution':requested,'chosen_resolution':chosen,'limit':cap,'samples_per_period':8,
            'basis':'static_uv_mapping_noise_octaves'})
        if requested>cap:add_issue(record,'field_sampling_budget','程序颜色字段频率超过采样预算，需近景复核',channel='base_color')
        return chosen

    def procedural_field(self, socket, material, obj, identity, record, channel_override=None):
        if 'time' in record.get('material_decision',{}).get('domains',[]):
            raise ValueError('有效材质图包含动画或驱动，不允许静态字段烘焙')
        # Bake spatial fields only: view/light-dependent expressions must stay live.
        def spatial(output, seen):
            node=output.node
            if output.as_pointer() in seen:
                return True
            seen.add(output.as_pointer())
            if node.type == 'GROUP_INPUT':
                mapped = self.group_inputs.get(output.as_pointer())
                return mapped is not None and (not mapped.is_linked or spatial(mapped.links[0].from_socket,seen))
            if node.type == 'GROUP' and node.node_tree:
                for item in node.node_tree.nodes:
                    if item.type == 'GROUP_INPUT':
                        for internal in item.outputs:
                            external=next((s for s in node.inputs if s.identifier==internal.identifier),None)
                            if external is not None:
                                self.group_inputs[internal.as_pointer()]=external
                target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                             for s in n.inputs if s.identifier==output.identifier),None)
                return target is not None and (not target.is_linked or spatial(target.links[0].from_socket,seen))
            if node.type in ('FRESNEL','LAYER_WEIGHT','LIGHT_PATH','CAMERA','SCRIPT'):
                return False
            if node.type == 'TEX_COORD' and output.name in ('Camera','Reflection'):
                return False
            if node.type == 'NEW_GEOMETRY' and output.name in ('Incoming','Backfacing'):
                return False
            return all(spatial(s.links[0].from_socket,seen) for s in node.inputs if s.is_linked)
        from .material_graph import static_bake_decision
        decision = static_bake_decision(socket, tuple(self.group_stack), native_normal=channel_override == 'normal')
        if not decision['allowed']:
            raise ValueError('程序字段含视角、光线或动态/未知数据依赖，不能静态烘焙：' + str(decision['domains']))
        def uniform(output, seen):
            node=output.node
            if output.as_pointer() in seen:return True
            seen.add(output.as_pointer())
            if node.type=='GROUP_INPUT':
                mapped=self.group_inputs.get(output.as_pointer())
                return mapped is not None and (not mapped.is_linked or uniform(mapped.links[0].from_socket,seen))
            if node.type=='GROUP':
                target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                             for s in n.inputs if s.identifier==output.identifier),None)
                return target is not None and (not target.is_linked or uniform(target.links[0].from_socket,seen))
            if node.type=='OBJECT_INFO':return output.name in ('Random','Color','Object Index','Material Index')
            if node.type not in ('VALUE','RGB','REROUTE','MATH','MIX','MIX_RGB','VALTORGB',
                                 'HUE_SAT','BRIGHTCONTRAST','GAMMA','INVERT','CLAMP','MAP_RANGE'):
                return False
            return all(uniform(s.links[0].from_socket,seen) for s in node.inputs if s.is_linked)
        is_uniform=uniform(socket,set())
        def reads_uv(output, seen):
            if output.as_pointer() in seen:return False
            seen.add(output.as_pointer());node=output.node
            if node.type=='GROUP_INPUT':
                mapped=self.group_inputs.get(output.as_pointer())
                return bool(mapped and mapped.is_linked and reads_uv(mapped.links[0].from_socket,seen))
            if node.type=='GROUP':
                target=next((s for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output
                             for s in n.inputs if s.identifier==output.identifier),None)
                return bool(target and target.is_linked and reads_uv(target.links[0].from_socket,seen))
            if node.type in ('UVMAP','TEX_IMAGE') or node.type=='TEX_COORD' and output.name=='UV':return True
            return any(reads_uv(s.links[0].from_socket,seen) for s in node.inputs if s.is_linked)
        uses_source_uv=reads_uv(socket,set())
        key = (socket.as_pointer(), tuple(n.as_pointer() for n in self.group_stack))
        if key in self.field_cache:
            return self.field_cache[key]
        temporary = []
        output = socket
        try:
            for group in reversed(self.group_stack):
                tree = group.node_tree
                interface = tree.interface.new_socket(name='MMDBridge_Field',in_out='OUTPUT',
                    socket_type='NodeSocketColor' if socket.type in ('RGBA','VECTOR') else 'NodeSocketFloat')
                temporary.append((tree,interface))
                group_output = next(n for n in tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output)
                tree.links.new(output,next(s for s in group_output.inputs if s.identifier == interface.identifier))
                output = next(s for s in group.outputs if s.identifier == interface.identifier)
            from .material_graph import effective_graph,output_domains
            semantic=signature(effective_graph(socket,tuple(self.group_stack)))
            domains=output_domains(socket,tuple(self.group_stack))
            if domains & {'time','view','ray','light','unknown'}:raise ValueError('字段含动态 / 视角 / 未知数据依赖，禁止静态缓存')
            if 'generated' in domains and obj.get('_mmd_generated_error'):
                raise ValueError('源 Generated 坐标无法等价保留，禁止用冻结网格边界缓存: '+obj['_mmd_generated_error'])
            if 'instance' in domains and obj.get('_mmd_object_random') is None:
                raise ValueError('字段依赖源实例数据，但旧冻结断点未保留该数据，请重新冻结几何')
            channel = channel_override or 'field_' + semantic
            self.field_semantics[channel]={'signature':semantic,'domains':sorted(domains)}
            footprint=signature([p.index for p in obj.data.polygons
                if p.material_index<len(obj.data.materials) and obj.data.materials[p.material_index]==material])
            field_identity=stable_id(obj.get('_mmd_geometry_identity',identity),semantic,footprint)
            self.field_uv_dependencies[channel]=uses_source_uv
            reason = self.field_reasons.get(key, '字段无法等价转换为 UE 节点')
            node_detail = socket.node.bl_idname + '/' + socket.name
            if socket.node.type == 'MATH':
                node_detail += '/' + socket.node.operation
            if socket.node.type == 'VALTORGB':
                node_detail += '/' + socket.node.color_ramp.interpolation
            group_path = '/'.join(n.name for n in self.group_stack)
            self.field_descriptions[channel] = (f'{group_path}/{socket.node.name} [{node_detail}]'
                + (' · 均匀值采样后转常量' if is_uniform else ' · 局部空间字段') + f' · 原因：{reason}')
            record.setdefault('baked_fields', []).append({'channel':channel,
                'node_type':socket.node.bl_idname, 'node_name':socket.node.name,
                'output':socket.name, 'group_path':[n.name for n in self.group_stack],
                'socket_type':socket.type,
                'uses_source_uv':uses_source_uv,
                'reason':reason,
                'signature':semantic,'domains':sorted(domains),
                'decision_basis':'semantics','performance_basis':'unmeasured',
                'fallback':'material_channel' if channel_override else 'conservative_local_cache',
                'representation':'constant_sample' if is_uniform else 'texture'})
            if is_uniform:
                # Object-uniform fields need no tree-sized atlas. Sample the
                # same native shader and object identity on a tiny temporary
                # mesh, then restore the frozen mesh before USD serialization.
                original_mesh,original_resolution=obj.data,self.resolution
                previous_sampling=self.uniform_sampling
                probe=bpy.data.meshes.new('MMDBridge_UniformFieldProbe')
                probe.from_pydata([(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],[],[(0,1,2,3)])
                probe.materials.append(material)
                # Keep the source UV names and a ready bake target. Creating a
                # differently named layer caused bake setup to repin the actual
                # material's albedo/alpha sockets to the temporary probe's UV.
                names=[uv.name for uv in original_mesh.uv_layers]
                if 'MMD_BakeUV' not in names:names.append('MMD_BakeUV')
                for name in names:
                    uv=probe.uv_layers.new(name=name)
                    for loop,point in zip(uv.data,((0,0),(1,0),(1,1),(0,1))):loop.uv=point
                probe.uv_layers.active=probe.uv_layers['MMD_BakeUV']
                obj.data=probe;self.resolution=16;self.uniform_sampling=True
                try:
                    sampled=self.bake(obj,material,None,channel,stable_id(field_identity,'uniform-v1'),from_output=output)
                    pixel=sampled['value']
                    result={'op':'constant','value':pixel if socket.type in ('RGBA','VECTOR') else pixel[0]}
                finally:
                    obj.data=original_mesh;self.resolution=original_resolution
                    self.uniform_sampling=previous_sampling
                    bpy.data.meshes.remove(probe)
                record['requires_visual_review']=True
                record['warnings'].append('对象均匀字段使用原生采样常量；共享原型的实例随机差异需复核')
            else:
                previous_resolution=self.resolution
                self.resolution=self.field_resolution(socket,obj,record)
                try: result = self.bake(obj,material,None,channel,field_identity,from_output=output)
                finally: self.resolution=previous_resolution
                if socket.type in ('VALUE','INT'):
                    result['output'] = 'Value'
                record['baked_fields'][-1]['image_id'] = result['image']['id']
                record['baked_channels'].append(channel)
            self.field_cache[key] = result
            return result
        finally:
            for tree,interface in reversed(temporary):
                tree.interface.remove(interface)

    @contextmanager
    def bake_session(self, obj):
        """Keep one tiny scene for a material's fields, then release its BVH."""
        previous = self._bake_session
        session = {'object': obj, 'source': bpy.context.scene, 'scene': None}
        self._bake_session = session
        try:
            yield
        finally:
            self._bake_session = previous
            if session['scene'] is not None:
                bpy.data.scenes.remove(session['scene'])

    def bake(self, obj, material, socket, channel, identity, from_output=None, final_surface=False):
        with self.source_random_for_bake(obj,material):
            return self._bake(obj,material,socket,channel,identity,from_output,final_surface)

    @contextmanager
    def source_random_for_bake(self,obj,material):
        """Frozen helper names must not change Cycles Object Info.Random."""
        random=obj.get('_mmd_object_random');changes=[];created=[];seen=set()
        def visit(tree):
            if tree.as_pointer() in seen:return
            seen.add(tree.as_pointer())
            for node in list(tree.nodes):
                if node.type=='GROUP':visit(node.node_tree)
                if node.type=='OBJECT_INFO' and node.outputs['Random'].is_linked:
                    value_node=tree.nodes.new('ShaderNodeValue');value_node.outputs[0].default_value=random
                    created.append((tree,value_node))
                    for link in list(node.outputs['Random'].links):
                        changes.append((tree,node.outputs['Random'],link.to_socket))
                        tree.links.new(value_node.outputs[0],link.to_socket)
        try:
            if random is not None:visit(material.node_tree)
            yield
        finally:
            for tree,source,target in changes:tree.links.new(source,target)
            for tree,node in reversed(created):tree.nodes.remove(node)

    def _bake(self, obj, material, socket, channel, identity, from_output=None, final_surface=False):
        if not self.isolated_baking:
            return self.bake_in_scene(obj,material,socket,channel,identity,from_output,final_surface)
        session = self._bake_session
        reuse = session is not None and session['object'] == obj
        source = session['source'] if reuse else bpy.context.scene
        isolated = session['scene'] if reuse else None
        if isolated is None:
            isolated=bpy.data.scenes.new('MMDBridge_TemporaryBakeScene')
            isolated.collection.objects.link(obj)
            isolated.render.engine=source.render.engine
            isolated.unit_settings.scale_length=source.unit_settings.scale_length
            isolated.frame_set(source.frame_current)
            isolated.cycles.device=source.cycles.device
            isolated.render.use_persistent_data=True
            if reuse:
                session['scene'] = isolated
        referenced=set()
        def references(tree,seen):
            if tree.as_pointer() in seen:return
            seen.add(tree.as_pointer())
            for node in tree.nodes:
                if node.type=='GROUP' and node.node_tree:references(node.node_tree,seen)
                target=getattr(node,'object',None)
                if target and target!=obj:referenced.add(target)
        references(material.node_tree,set())
        hidden={target:target.hide_render for target in referenced}
        for target in referenced:
            if isolated.collection.objects.get(target.name) is None:
                isolated.collection.objects.link(target)
            target.hide_render=True
        window=bpy.context.window
        previous_scene=window.scene if window else None
        previous_layer=window.view_layer if window else None
        try:
            layer=isolated.view_layers[0]
            # Edit-mode operators resolve their edit object through the window
            # scene even when scene/view_layer are overridden. Synchronize the
            # actual window too, then restore it before removing the bake scene.
            if window:
                window.scene=isolated
                window.view_layer=layer
            layer.update()
            layer.objects.active=obj
            obj.select_set(True,view_layer=layer)
            with bpy.context.temp_override(scene=isolated,view_layer=layer,object=obj,
                                           active_object=obj,selected_objects=[obj],
                                           selected_editable_objects=[obj]):
                return self.bake_in_scene(obj,material,socket,channel,identity,from_output,final_surface)
        finally:
            if window:
                window.scene=previous_scene
                window.view_layer=previous_layer
            for target,state in hidden.items():target.hide_render=state
            if not reuse:
                bpy.data.scenes.remove(isolated)

    def bake_in_scene(self, obj, material, socket, channel, identity, from_output=None, final_surface=False):
        if obj.type != "MESH":
            raise ValueError("只能为网格烘焙")
        scene = bpy.context.scene
        if not obj.users_collection:
            scene.collection.objects.link(obj)
        # Fresh export helpers have not been evaluated in this view layer yet.
        # Without synchronization Blender can expose OBJECT as the only valid
        # mode even after setting the active mesh, breaking smart UV projection.
        bpy.context.view_layer.update()
        old_engine, old_samples = scene.render.engine, scene.cycles.samples
        old_uv = obj.data.uv_layers.active_index
        bake_uv = obj.data.uv_layers.get('MMD_BakeUV')
        bake_uv_name = 'MMD_BakeUV'
        if not bake_uv:
            original_uv = next((uv.name for uv in obj.data.uv_layers if uv.active_render),None)
            # Explicitly retain the source UV for shader evaluation while the dedicated
            # non-overlapping UV becomes the bake target.
            if original_uv and not obj.get("_mmd_uv_pinned", False):
                for mat in obj.data.materials:
                    if not mat or not mat.use_nodes:
                        continue
                    pin_source_uv(mat.node_tree, original_uv, {uv.name for uv in obj.data.uv_layers})
            bake_uv=obj.data.uv_layers.new(name=bake_uv_name)
            obj.data.uv_layers.active=bake_uv
            bake_uv.active_render=True
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.context.view_layer.update()
            points = world_points(obj.data, obj.matrix_world)
            spans = (points.max(axis=0) - points.min(axis=0)).tolist()
            flat_axis = min(range(3), key=lambda i: spans[i])
            if self.surface_policy.get("category") == "tiled" and spans[flat_axis] < 1e-5 and self.world_bounds:
                axes = [i for i in range(3) if i != flat_axis]
                for loop in obj.data.loops:
                    bake_uv.data[loop.index].uv = projected_uv(points[loop.vertex_index], *self.world_bounds, axes)
                self.bake_layout = "shared_world_projection"
            else:
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.smart_project(island_margin=0.02)
                bpy.ops.object.mode_set(mode="OBJECT")
                self.bake_layout = "nonoverlapping_object_atlas"
            for mat in obj.data.materials:
                if mat and mat.use_nodes:
                    for node in mat.node_tree.nodes:
                        if node.type == 'UVMAP' and obj.data.uv_layers.get(node.uv_map) is None:
                            node.uv_map = original_uv or bake_uv_name
        # Edit-mode UV operators may rebuild the RNA collection; never retain a
        # UVLoopLayer wrapper across that boundary.
        bake_index=obj.data.uv_layers.find(bake_uv_name)
        if bake_index < 0:
            raise ValueError("无法创建稳定的 MMD_BakeUV")
        obj.data.uv_layers.active_index=bake_index
        nodes, links = material.node_tree.nodes, material.node_tree.links
        output = material.node_tree.get_output_node('CYCLES' if old_engine == 'CYCLES' else 'EEVEE')
        if output is None:
            raise ValueError('源渲染器没有有效材质输出')
        original_links = [(l.from_socket, l.to_socket) for l in output.inputs["Surface"].links]
        # A bake is mapped by the UVs that existed when it was rendered.  The
        # source mesh identity alone is insufficient: smart_project can change
        # the atlas when evaluated pieces are merged or reordered.
        bake_uv = obj.data.uv_layers[bake_index]
        uv_values = array("f", [0.0]) * (len(bake_uv.data) * 2)
        bake_uv.data.foreach_get("uv", uv_values)
        uv_hash = hashlib.sha256(uv_values.tobytes()).hexdigest()
        hdr = channel == 'emission' or channel.startswith('field_')
        extension = '.exr' if hdr else '.png'
        try:
            source_stat = Path(self.source).stat()
            source_stamp = (source_stat.st_size, source_stat.st_mtime_ns)
        except OSError:
            source_stamp = None
        cache_version = ('bake-v8-linear-exr-preserved-uv' if self.field_uv_dependencies.get(channel)
                         else 'bake-v7-linear-exr') if hdr else (
                         'bake-v4-native-closure-normal' if channel == 'normal' and from_output is not None else 'bake-v3-coverage')
        field_semantic=self.field_semantics.get(channel)
        object_context=(obj.get('_mmd_object_random'),[list(row) for row in obj.matrix_world]) if field_semantic and (
                        set(field_semantic['domains'])&{'world','instance'}) else None
        cache_key = stable_id(cache_version, identity, self.workflow,
                              final_surface, uv_hash, source_stamp, bpy.app.version_string,
                              self.bake_resolution(material),scene.frame_current_final,
                              field_semantic or native_shader_signature(material),
                              object_context if field_semantic else (obj.name,list(obj.color),obj.pass_index,
                                  material.pass_index,[list(row) for row in obj.matrix_world]))
        # Resource paths must isolate incompatible contexts too; a changed key
        # cannot overwrite a texture still referenced by another material.
        image_name = asset_name('Bake',channel,stable_id('context-resource-v1',identity,uv_hash,cache_key))
        cached_path = self.out/'Textures'/(image_name+extension)
        image_id = stable_id(self.source,image_name)
        extracted_path = self.out/'Textures'/(asset_name('T',image_name,image_id)+extension)
        cache_metadata = self.out.parent / "BakeCache" / (image_name + ".json")
        try:
            cache_data = read_json(cache_metadata)
            cache_valid = cache_data.get("key") == cache_key
        except (OSError, ValueError, TypeError):
            cache_data = {}
            cache_valid = False
        if self.uniform_sampling and cache_valid and isinstance(cache_data.get('constant'), list):
            self.bake_stats['constant_reused'] += 1
            if self.progress:
                self.progress('scene_baking', f'复用字段常量 {self.progress_context} · {material.name} · {channel}')
            return {'op':'constant', 'value':cache_data['constant']}
        reusable_path = None
        for candidate in (extracted_path, cached_path):
            if not cache_valid or not candidate.is_file() or candidate.stat().st_size <= 128:
                continue
            try:
                if hdr:
                    with candidate.open('rb') as stream:
                        if stream.read(4) != b'\x76\x2f\x31\x01':
                            continue
                loaded = bpy.data.images.load(str(candidate), check_existing=False)
                try:
                    if tuple(loaded.size) != (self.bake_resolution(material),) * 2:
                        continue
                    _ = loaded.pixels[0]  # Force PNG decoding; an interrupted write is not a cache hit.
                    reusable_path = candidate
                    break
                finally:
                    bpy.data.images.remove(loaded)
            except (OSError, RuntimeError, IndexError, ValueError):
                continue
        if reusable_path is not None:
            if self.uniform_sampling:
                loaded = bpy.data.images.load(str(reusable_path), check_existing=False)
                try:
                    size = loaded.size[0]
                    offset = ((size // 2) * size + size // 2) * 4
                    pixel = list(loaded.pixels[offset:offset+3])
                finally:
                    bpy.data.images.remove(loaded)
                atomic_json(cache_metadata, {'key':cache_key, 'constant':pixel})
                self.bake_stats['constant_reused'] += 1
                return {'op':'constant', 'value':pixel}
            rec = {"id": image_id, "path": str(reusable_path),
                   "asset_name": asset_name("T", image_name, image_id),
                   "name": image_name,
                   "hdr":hdr, "generated_bake":True,
                   "color_space": "Non-Color" if hdr else "sRGB" if channel == 'base_color' else "Non-Color"}
            self.images[image_name] = rec
            self.bake_stats['reused'] += 1
            if self.progress:
                self.progress("scene_baking", f"复用已烘焙贴图 {self.progress_context} · {material.name} · {channel}"
                              + (" · " + self.field_descriptions[channel] if channel in self.field_descriptions else ""))
            return {"op": "texture", "image": rec, "output": "Color",
                    "uv": {"op": "uv", "index": bake_index}, "normal_map": channel == "normal", "extension": "EXTEND"}
        visibility = {o:o.hide_render for o in scene.objects}
        for other in scene.objects:
            other.hide_render = other != obj
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 1
        resolution = self.bake_resolution(material)
        description = f"{self.progress_context} · {material.name} · {channel} · {resolution}×{resolution}"
        if channel in self.field_descriptions:
            description += " · " + self.field_descriptions[channel]
        if self.progress:
            self.progress("scene_baking", "开始烘焙贴图 " + description)
        image = bpy.data.images.new(image_name, resolution, resolution, alpha=True, float_buffer=hdr)
        # Data targets avoid Cycles' bake color encoding; values are scene-linear.
        image.colorspace_settings.name = "sRGB" if channel == 'base_color' and not hdr else "Non-Color"
        image.filepath_raw = str(extracted_path)
        image.file_format = 'OPEN_EXR' if hdr else 'PNG'
        targets = []
        emission = None
        output_states=[(n,n.is_active_output) for n in nodes if n.type == 'OUTPUT_MATERIAL']
        temporary_output=nodes.new('ShaderNodeOutputMaterial')
        temporary_output.target='CYCLES'
        temporary_output.is_active_output=True
        for source,_ in original_links:
            links.new(source,temporary_output.inputs['Surface'])
        output=temporary_output
        try:
            # Every material slot needs a bake target; only this slot is kept in the result.
            for mat in obj.data.materials:
                if mat and mat.use_nodes:
                    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
                    tex.image = image
                    mat.node_tree.nodes.active = tex
                    targets.append((mat, tex))
            if channel == 'normal' and from_output is not None:
                # Bake this closure's normal, not the unrelated complete surface.
                # Group outputs are lifted by procedural_field for the duration.
                emission = nodes.new('ShaderNodeBsdfPrincipled')
                links.new(from_output, emission.inputs['Normal'])
                links.new(emission.outputs[0], output.inputs['Surface'])
            elif channel != "normal" and not final_surface:
                emission = nodes.new("ShaderNodeEmission")
                if from_output is not None:
                    links.new(from_output, emission.inputs["Color"])
                elif socket.is_linked:
                    links.new(socket.links[0].from_socket, emission.inputs["Color"])
                else:
                    v = value(socket)
                    emission.inputs["Color"].default_value = v if isinstance(v, list) and len(v) == 4 else (v, v, v, 1)
                links.new(emission.outputs[0], output.inputs["Surface"])
            bpy.ops.object.select_all(action="DESELECT")
            obj.hide_set(False)
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bake_options = ({"type": "DIFFUSE", "pass_filter": {"COLOR"}} if final_surface else
                            {"type": "NORMAL" if channel == "normal" else "EMIT"})
            bpy.ops.object.bake(**bake_options, use_clear=True, margin=16,
                                use_selected_to_active=False)
            # Native bake resets this to the scene's bake format (usually PNG).
            # Set it after baking so EXR really stays linear and retains HDR.
            if self.uniform_sampling:
                offset=((resolution//2)*resolution+resolution//2)*4
                pixel=list(image.pixels[offset:offset+3])
                atomic_json(cache_metadata, {'key':cache_key, 'constant':pixel})
                self.bake_stats['constant_samples'] += 1
                if self.progress:
                    self.progress('scene_baking', '完成字段常量采样（不保存贴图） ' + description)
                return {'op':'constant', 'value':pixel}
            write_bake_image(image, extracted_path, hdr)
            rec = {'id':image_id, 'path':str(extracted_path), 'name':image_name,
                   'asset_name':asset_name('T', image_name, image_id), 'hdr':hdr, 'generated_bake':True,
                   'color_space':image.colorspace_settings.name}
            self.images[image_name] = rec
            atomic_json(cache_metadata, {'key':cache_key, 'path':str(extracted_path),
                                       'compression':'ZIP' if hdr else 'PNG', 'depth':32 if hdr else 8})
            self.bake_stats['written'] += 1
            self.bake_stats['bytes_written'] += extracted_path.stat().st_size
            if self.progress:
                self.progress("scene_baking", "完成烘焙贴图 " + description)
            return {"op": "texture", "image": rec, "output": "Color", "uv": {"op": "uv", "index": bake_index},
                    "normal_map": channel == "normal", "extension": "EXTEND"}
        finally:
            if emission:
                nodes.remove(emission)
            nodes.remove(temporary_output)
            for original,active in output_states:
                original.is_active_output=active
            for mat, tex in targets:
                mat.node_tree.nodes.remove(tex)
            bpy.data.images.remove(image)
            scene.render.engine, scene.cycles.samples = old_engine, old_samples
            if 0 <= old_uv < len(obj.data.uv_layers):
                obj.data.uv_layers.active_index = old_uv
            for other, hidden in visibility.items():
                other.hide_render = hidden

    def _surface_nodes(self, node, depth=0, from_socket=None):
        self.check_conversion_budget()
        if node is None or depth > 30:
            return []
        if node.type in ("BSDF_PRINCIPLED", "BSDF_GLASS", "BSDF_METALLIC", 'BSDF_DIFFUSE', 'BSDF_TRANSLUCENT'):
            return [node]
        if node.type == 'REROUTE' and node.inputs[0].is_linked:
            link = node.inputs[0].links[0]
            return self._surface_nodes(link.from_node, depth+1, link.from_socket)
        if node.type == 'GROUP_INPUT' and from_socket is not None:
            mapped = self.group_inputs.get(from_socket.as_pointer())
            if mapped and mapped.is_linked:
                link = mapped.links[0]
                return self._surface_nodes(link.from_node, depth+1, link.from_socket)
            return []
        if node.type in ("MIX_SHADER", "ADD_SHADER"):
            result = []
            inputs=[s for s in node.inputs if s.type=='SHADER']
            fac=constant_socket(node.inputs[0],tuple(self.group_stack)) if node.type=='MIX_SHADER' else None
            if fac in (0,1):inputs=[inputs[int(fac)]]
            for socket in inputs:
                if socket.type == "SHADER" and socket.is_linked:
                    link = socket.links[0]
                    result.extend(self._surface_nodes(link.from_node, depth + 1, link.from_socket))
            return result
        if node.type == "GROUP" and node.node_tree:
            group_input = next((item for item in node.node_tree.nodes if item.type == "GROUP_INPUT"), None)
            if group_input:
                for index, internal in enumerate(group_input.outputs):
                    external = next((s for s in node.inputs if s.identifier==internal.identifier),None)
                    if external is None and index < len(node.inputs):
                        external = node.inputs[index]
                    if external is not None:
                        self.group_inputs[internal.as_pointer()] = external
            output = next((item for item in node.node_tree.nodes
                           if item.type == "GROUP_OUTPUT" and item.is_active_output), None)
            if output is None:
                output = next((item for item in node.node_tree.nodes if item.type == "GROUP_OUTPUT"), None)
            if output:
                surface = (next((s for s in output.inputs if from_socket and s.identifier == from_socket.identifier), None) or
                           output.inputs.get('MMDBridge_Surface') or
                           output.inputs.get("Shader") or output.inputs.get("Surface"))
                if surface and surface.is_linked:
                    link = surface.links[0]
                    self.group_stack.append(node)
                    self._surface_contexts[node.node_tree.as_pointer()]=tuple(self.group_stack)
                    try:return self._surface_nodes(link.from_node, depth + 1, link.from_socket)
                    finally:self.group_stack.pop()
        return []

    def _downstream_mask_output(self, image_node):
        """Choose the spatial mask, excluding color overlays and displacement."""
        semantic = self._special_image_semantic(image_node)
        if semantic == "parking_line":
            return image_node.outputs.get("Color")
        queue = [(image_node, 0)]
        seen = {image_node.as_pointer()}
        candidates = []
        while queue:
            node, distance = queue.pop(0)
            if node.type in ("VALTORGB", "MIX", "MIX_RGB", "MATH", "INVERT"):
                output = next((item for item in node.outputs if item.name in ("Color", "Result", "Value")),
                              node.outputs[0] if node.outputs else None)
                if output:
                    candidates.append((distance, output))
            for output in node.outputs:
                for link in output.links:
                    child = link.to_node
                    # A color Mix feeding a surface color is the consumer of a
                    # mask, not the mask itself.  Stop at this semantic boundary
                    # so parking-line/wet masks do not bake the final albedo.
                    if child.type in ("MIX", "MIX_RGB") and any(
                            downstream.to_node.type in ("BSDF_PRINCIPLED", "BSDF_GLASS") and
                            downstream.to_socket.name in ("Base Color", "Color", "Emission Color")
                            for child_output in child.outputs for downstream in child_output.links):
                        continue
                    pointer = child.as_pointer()
                    if pointer not in seen and child.type not in ("BSDF_PRINCIPLED", "BSDF_GLASS", "OUTPUT_MATERIAL"):
                        seen.add(pointer)
                        queue.append((child, distance + 1))
        if semantic == "water":
            ramps = [(distance, output) for distance, output in candidates
                     if output.node.type == "VALTORGB"]
            if ramps:
                return min(ramps, key=lambda item: item[0])[1]
        return max(candidates, default=(0, image_node.outputs.get("Color")), key=lambda item: item[0])[1]

    @staticmethod
    def _special_image_semantic(node):
        if node.type != "TEX_IMAGE" or not node.image:
            return None
        lowered = (node.image.name + " " + node.name).lower()
        if "unwater" in lowered or "wet" in lowered:
            return "water"
        if "parkline" in lowered or "parking" in lowered:
            return "parking_line"
        return None

    def _socket_has_special_image(self, socket, seen=None):
        if socket is None or not socket.is_linked:
            return False
        seen = set() if seen is None else seen
        node = socket.links[0].from_node
        pointer = node.as_pointer()
        if pointer in seen:
            return False
        seen.add(pointer)
        if self._special_image_semantic(node):
            return True
        return any(self._socket_has_special_image(item, seen) for item in node.inputs if item.is_linked)

    def _without_special_color_overlay(self, socket):
        """Return the relightable base branch before a named mask color overlay."""
        current = socket
        for _ in range(12):
            if not current.is_linked:
                break
            node = current.links[0].from_node
            if node.type == "MIX_RGB":
                a, b = node.inputs[1], node.inputs[2]
            elif node.type == "MIX" and node.data_type == "RGBA":
                a, b = node.inputs[6], node.inputs[7]
            else:
                break
            a_special = self._socket_has_special_image(a)
            b_special = self._socket_has_special_image(b)
            if a_special == b_special:
                break
            current = b if a_special else a
        return current

    def glass_mix(self, shader):
        if not shader or shader.type != 'MIX_SHADER' or not all(s.is_linked for s in shader.inputs[1:3]):
            return None
        a,b=(s.links[0].from_node for s in shader.inputs[1:3])
        if a.type != 'BSDF_GLASS' or b.type != 'BSDF_GLASS':
            return None
        # Preserve a roughness mask when both glass closures have the same optical properties.
        for name in ('Color','IOR','Normal'):
            if a.inputs[name].is_linked or b.inputs[name].is_linked or value(a.inputs[name]) != value(b.inputs[name]):
                return None
        return {'base_color':self.expr(a.inputs['Color']), 'ior':self.expr(a.inputs['IOR']),
                'roughness':{'op':'mix','mode':'MIX','factor':self.expr(shader.inputs[0]),
                             'a':self.expr(a.inputs['Roughness']),'b':self.expr(b.inputs['Roughness'])}}

    def check_conversion_budget(self):
        if self._conversion_deadline is not None and time.perf_counter() >= self._conversion_deadline:
            raise MaterialConversionTimeout(f'单材质转换超过 {self.conversion_timeout:g} 秒时间预算')

    def analyze(self, material, obj=None, water=False, pmx_index=None,
                source_name=None, geometry_identity=None):
        self._active_record = None
        self._main_transmission_repair = False
        self._requested_strategy = self.strategy
        self._conversion_deadline = (time.perf_counter() + self.conversion_timeout
            if self.conversion_timeout > 0 and pmx_index is None else None)
        with self.bake_session(obj):
            try:
                result = self._analyze(material, obj, water, pmx_index, source_name, geometry_identity)
                self.check_conversion_budget()
                return finalize_restoration(result, obj.get('_mmd_source_name',obj.name) if obj is not None else None)
            except Cancelled:
                raise
            except (ValueError, RuntimeError, MaterialConversionTimeout) as exc:
                self._conversion_deadline = None
                record = self._active_record
                if (self.complex_strategy != 'pbr_bake' or not self.bake_fallback or obj is None or pmx_index is not None or
                        self._requested_strategy not in ('auto', 'hybrid', 'bake', 'baked') or record is None or record.get('basic_pbr_bake')):
                    raise
                return self.recover_material(material, obj, record, exc, source_name, geometry_identity)
            finally:
                self._conversion_deadline = None
                self._main_transmission_repair = False
                self._force_static_fields = False

    def plan(self, material, policy=None, obj=None, analysis=None, requested='auto', repeat_count=1):
        from .blender_material_plan import source_material_plan
        plan = source_material_plan(material, bpy.context.scene.render.engine, analysis=analysis,
            policy=policy, repeat_count=repeat_count, requested=requested,
            check_budget=self.check_conversion_budget)
        if self.workflow == 'legacy_main_v1' and requested == 'auto':
            plan.update(route='nodes' if self.simple_surface(material) else 'hybrid',
                        reason='旧版主方案：节点优先，程序字段局部高度烘焙')
        if obj is not None and obj.get('_mmd_generated_error') and 'generated' in plan['facts']['domains']:
            plan['facts']['data_errors'].append(obj['_mmd_generated_error'])
            plan.update(route='blocked', reason='原始 Generated 数据未可靠保留，禁止节点和烘焙猜测坐标')
        return plan

    def recover_material(self, material, obj, record, error, source_name, geometry_identity):
        if self.workflow == 'legacy_main_v1':
            record.setdefault('fallback_attempts', []).append({'scope': 'complete_surface',
                'route': 'legacy_pbr_bake', 'error': str(error)})
            return finalize_restoration(self.bake_basic_pbr(material, obj, record,
                str(error), opaque_fallback=record.get('transmission_candidate', False)),
                obj.get('_mmd_source_name', obj.name))
        from .material_plan import fallback_plan
        plan = record.get('restoration_plan') or self.plan(material, self.surface_policy, obj,
            requested=self._requested_strategy)
        record['restoration_plan'] = plan
        source_metadata = {key: record[key] for key in ('source_preset', 'material_decision',
            'source_displacement_method', 'displacement_node') if key in record}
        source_issues = list(record.get('issues', []))
        scope = 'conversion_timeout' if isinstance(error, MaterialConversionTimeout) else 'complete_surface'
        step = fallback_plan(plan, scope=scope)
        attempts = record.setdefault('fallback_attempts', [])
        attempts.append({**step, 'error':str(error), 'status':'started'})
        if step['route'] == 'local_fields':
            self._force_static_fields = True
            try:
                result = self._analyze(material, obj, False, None, source_name, geometry_identity)
                result.update(source_metadata, restoration_plan=plan, automatic_route=plan)
                for issue in source_issues:
                    if issue not in result.setdefault('issues', []): result['issues'].append(issue)
                attempts[-1]['status'] = 'converted'
                target_attempts=result.setdefault('fallback_attempts', [])
                if target_attempts is not attempts: target_attempts.extend(attempts)
                return finalize_restoration(result, obj.get('_mmd_source_name',obj.name))
            except Cancelled: raise
            except (ValueError, RuntimeError, MaterialConversionTimeout) as local_error:
                attempts[-1].update(status='failed', error=str(local_error))
                # Do not repeatedly retry an already failed native PBR bake.
                if self._active_record.get('basic_pbr_bake'): raise
                record = self._active_record
                record.update(source_metadata, restoration_plan=plan, automatic_route=plan)
                for issue in source_issues:
                    if issue not in record.setdefault('issues', []): record['issues'].append(issue)
                target_attempts=record.setdefault('fallback_attempts', [])
                if target_attempts is not attempts: target_attempts.extend(attempts)
                step = fallback_plan(plan, scope=scope, local_attempted=True)
                record['fallback_attempts'].append({**step, 'status':'started'})
            finally:
                self._force_static_fields = False
        if step['route'] == 'blocked':
            add_issue(record, 'fallback_semantics_blocked', step['reason'], severity='error')
            raise ValueError(step['reason'] + '；原始问题：' + str(error)) from error
        result = self.bake_basic_pbr(material, obj, record, step['reason'],
            opaque_fallback=step['opaque_fallback'], project_view_mix=step['project_view_mix'])
        record['fallback_attempts'][-1]['status'] = 'converted'
        return finalize_restoration(result, obj.get('_mmd_source_name',obj.name))

    def bake_basic_pbr(self, material, obj, record, reason, opaque_fallback=False, project_view_mix=False):
        """Bake source channels directly, without creating the complex UE graph."""
        from .blender_pbr_bake import native_pbr_channels
        from .material_graph import static_bake_decision
        self._conversion_deadline = None
        renderer = bpy.context.scene.render.engine
        output = material.node_tree.get_output_node('CYCLES' if renderer == 'CYCLES' else 'EEVEE')
        if output is None: raise ValueError('基础 PBR 烘焙没有有效材质输出')
        start = time.perf_counter()
        attempt = {'scope': 'basic_pbr_material', 'reason': reason, 'status': 'preflight', 'resolution': self.bake_resolution(material)}
        record['basic_pbr_bake'] = attempt
        previous_uniform_sampling = self.uniform_sampling
        self.uniform_sampling = False
        try:
            with native_pbr_channels(material, output.inputs['Surface'], opaque_fallback=opaque_fallback,
                    project_view_mix=project_view_mix or opaque_fallback) as (channels, limits, normal_required):
                for channel, socket in channels.items():
                    decision = static_bake_decision(socket)
                    if not decision['allowed']:
                        raise ValueError(channel + ' 含不能静态烘焙的数据依赖：' + str(decision['domains']))
                    if 'generated' in decision['domains'] and obj.get('_mmd_generated_error'):
                        raise ValueError('原始 Generated 坐标尚未可靠保留：' + obj['_mmd_generated_error'])
                textures = {}
                footprint = signature([p.index for p in obj.data.polygons
                    if p.material_index<len(obj.data.materials) and obj.data.materials[p.material_index]==material])
                for channel, socket in channels.items():
                    from .material_graph import constant_socket
                    constant_value=constant_socket(socket)
                    if constant_value is not None:
                        if channel in ('roughness','metallic','opacity') and isinstance(constant_value,list):
                            constant_value=sum(v*w for v,w in zip(constant_value,(.2126,.7152,.0722)))
                        textures[channel]={'op':'constant','value':constant_value}
                        continue
                    probe = material.node_tree.nodes.new('ShaderNodeMath' if channel in ('roughness','metallic','opacity') else 'ShaderNodeEmission')
                    target = probe.inputs[0]
                    material.node_tree.links.new(socket,target)
                    try:
                        from .material_graph import effective_graph, output_domains
                        semantic = signature(effective_graph(target))
                        domains = sorted(output_domains(socket))
                    finally:
                        material.node_tree.nodes.remove(probe)
                    self.field_semantics[channel] = {'signature':semantic,'domains':domains}
                    self.field_uv_dependencies[channel] = 'uv' in domains
                    identity = stable_id(obj.get('_mmd_geometry_identity',record['id']),semantic,footprint,'basic-pbr-textures-v4')
                    expression = self.bake(obj, material, None, channel, identity, from_output=socket)
                    if channel in ('roughness', 'metallic', 'opacity'): expression['output'] = 'Value'
                    textures[channel] = expression
                semantic = native_shader_signature(material)
                self.field_semantics['normal'] = {'signature':semantic,'domains':record['material_decision']['domains']}
                identity = stable_id(obj.get('_mmd_geometry_identity',record['id']),semantic,footprint,'basic-pbr-normal-textures-v3')
                textures['normal'] = self.bake(obj, material, None, 'normal', identity) if normal_required else {'op':'constant','value':[0,0,1]}
            record.pop('surface_graph', None); record.pop('function_definitions', None)
            for key in ('special_masks', 'thin_surface_transmission', 'coverage_source', 'master_material'):
                record.pop(key, None)
            record.pop('closure_features',None)
            if opaque_fallback:
                add_issue(record,'opaque_transmission_fallback','透光还原不可靠，已按不透明表面降级；透射和折射不保留',severity='approximation')
                record['opaque_transmission_fallback']=True
                if record.get('main_transmission_attempt',{}).get('eligible'):
                    alpha=textures.get('opacity',{})
                    if alpha.get('op')=='constant' and 0<float(alpha.get('value',1))<1:
                        textures['opacity']={'op':'constant','value':1.}
                        record['warnings'].append('透光失败回退为不透明覆盖，避免连续透明度被裁剪为空')
            record.update(surface='pbr', strategy='baked', channels=textures,
                          water=False, simple_surface=False,
                          baked_channels=[k for k,v in textures.items() if v['op']=='texture'], bake_resolution=self.bake_resolution(material),
                          bake_layout=self.bake_layout, bake_margin_pixels=16, requires_visual_review=True,
                          asset_name=asset_name('M', record['name'] + '_BakedPBR', record['id']))
            if obj.get('_mmd_auto_chunked',False):record['bake_layout']='chunked_object_atlas'
            record['strategy_reasons'].append('从源有效 Shader 直接烘焙基础 PBR；不生成复杂 UE 对应节点')
            facts=record.get('restoration_plan',{}).get('facts',{})
            projected=not facts or 'layers' in facts.get('features',[])
            if projected:
                record['warnings'].append('多 BSDF 压缩为单一 PBR：高光分布和分层法线可能近似；基础色不含灯光或阴影')
            attempt.update(status='baked', channels=list(textures),
                           closure_projection=projected,
                           constant_channels=[k for k,v in textures.items() if v['op']=='constant'],
                           omitted_effects=sorted(set(limits)), visual_status='needs_review')
            material.name = record['asset_name']; record['slot_name'] = material.name
            self.records[record['id']] = record
            return record
        except (ValueError, RuntimeError) as exc:
            attempt.update(status='failed', error=str(exc))
            raise
        finally:
            self.uniform_sampling = previous_uniform_sampling
            attempt['seconds'] = time.perf_counter() - start

    def _analyze(self, material, obj=None, water=False, pmx_index=None,
                 source_name=None, geometry_identity=None):
        self.object=obj
        self.bake_layout = None
        self.surface_policy = {}
        original_name = source_name or material.name
        self._requested_strategy = str(obj.get('mmd_bridge_material_strategy', material.get('mmd_bridge_material_strategy', self.strategy)) if obj else material.get('mmd_bridge_material_strategy', self.strategy)).lower()
        # Keep a usable failure record even when the initial graph analysis fails.
        self._active_record = {'id': stable_id(self.source, original_name, geometry_identity or (obj.name if obj else ''), 'conversion-fallback-v3'),
            'name': original_name, 'source_material_id': stable_id(self.source, original_name),
            'pmx_index': pmx_index, 'channels': {}, 'warnings': [], 'strategy_reasons': [],
            'material_decision': {'domains': []}, 'strategy_requested': self._requested_strategy}
        if obj is not None and not obj.get("_mmd_uv_pinned", False):
            original_uv = next((uv.name for uv in obj.data.uv_layers if uv.active_render), None)
            if original_uv:
                for mat in obj.data.materials:
                    if mat and mat.use_nodes:
                        pin_source_uv(mat.node_tree, original_uv, {uv.name for uv in obj.data.uv_layers})
                obj["_mmd_uv_pinned"] = True
        original_name = source_name or material.name
        self.world_bounds = self.material_world_bounds.get(original_name, self.world_bounds)
        base_id = stable_id(self.source, original_name)
        policy = None
        if obj is not None and pmx_index is None:
            size = obj.get('_mmd_world_size')
            if size is None:
                points = world_points(obj.data, obj.matrix_world)
                size = (points.max(axis=0) - points.min(axis=0)).tolist()
            spans = [span * self.meters_per_unit for span in size]
            uv = obj.data.uv_layers[0] if obj.data.uv_layers else None
            uv_span = uv_spans(uv)
            mapping_scale = [1, 1]
            if material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "MAPPING" and not node.inputs["Scale"].is_linked:
                        mapping_scale = [max(mapping_scale[i], abs(node.inputs["Scale"].default_value[i])) for i in range(2)]
            policy = classify_surface(spans, uv_span, mapping_scale,
                                      str(obj.get("mmd_bridge_surface_class", "auto")), min(spans) < 0.02)
            if obj.get("_mmd_auto_chunked", False):
                policy["requested"] = "auto"
                policy["reason"] = "大型立体物件分块烘焙"
        self.surface_policy = policy or {}
        # Per-object identities allow geometry-dependent procedural baking.
        override = str(obj.get("mmd_bridge_material_strategy", material.get("mmd_bridge_material_strategy", self.strategy)) if obj else material.get("mmd_bridge_material_strategy", self.strategy)).lower()
        requested = override
        simple_surface = pmx_index is None and override == 'auto' and self.simple_surface(material)
        if override == "auto" and policy:
            override = "hybrid"
        if simple_surface:
            override = 'nodes'
        override = "bake" if override == "baked" else override
        if override not in ("auto", "nodes", "hybrid", "bake"):
            raise ValueError(f"材质 {original_name} 的 mmd_bridge_material_strategy 无效: {override}")
        identity = stable_id(base_id, geometry_identity or obj.name, override, self.resolution, self.bake_fallback, self.complex_strategy, self.force_bake.get(base_id), self.workflow,
                             json.dumps(policy, sort_keys=True)) if obj else base_id
        if identity in self.records:
            self._active_record = self.records[identity]
            return self.records[identity]
        record = {"id": identity, "source_material_id": base_id, "name": original_name, "source_name":original_name,
                  "asset_name": None, "channels": {},
                  "water": water, "warnings": [], "pmx_index": pmx_index}
        self._active_record = record
        record['material_workflow'] = self.workflow
        record.update(strategy_reasons=[policy['reason']] if policy else [], material_decision={'domains': []})
        if pmx_index is None:
            record['material_decision']=analyze_material(material,bpy.context.scene.render.engine, check_budget=self.check_conversion_budget)
            self.check_conversion_budget()
            record['source_preset']={'version':1,'definition':native_shader_description(material, self.check_conversion_budget),
                'editable_scope':'active_parameters','inactive_features':'reexport_required'}
            if 'time' in record['material_decision'].get('domains',[]):
                raise ValueError('有效材质图包含动画或驱动，尚无 UE 动态参数适配，拒绝冻结当前帧')
        record.update(strategy="nodes", strategy_requested=requested, surface_policy=policy,
                      strategy_reasons=[policy["reason"]] if policy else [],
                      baked_channels=[], bake_resolution=None)
        self._active_record, self._active_material, self._active_identity = record, material, identity
        self._requested_strategy = requested
        record['material_bake_fallback'] = self.bake_fallback
        record['material_complex_strategy'] = self.complex_strategy
        record['simple_surface'] = simple_surface
        surface_types={entry.get('type') for entry in record['material_decision'].get('nodes',[])}
        source_output=material.node_tree.get_output_node('CYCLES' if bpy.context.scene.render.engine=='CYCLES' else 'EEVEE') if material.use_nodes else None
        source_shader=source_output.inputs['Surface'].links[0].from_node if source_output and source_output.inputs['Surface'].is_linked else None
        entries=record['material_decision'].get('nodes',[])
        adapted_surface=bool(entries and source_output and
            not any(source_output.inputs[name].is_linked for name in ('Volume','Displacement')) and
            all(entry.get('capability',{}).get('route') in ('nodes','shared_lookup','mesh_data','instance_data') for entry in entries))
        if requested=='auto' and adapted_surface:
            simple_surface=True
            record['simple_surface']=True
            override='nodes'
        transmitting=bool('ShaderNodeBsdfGlass' in surface_types or
            any(n.type=='BSDF_PRINCIPLED' and n.inputs.get('Transmission Weight') and
                (n.inputs['Transmission Weight'].is_linked or value(n.inputs['Transmission Weight'])>1e-6)
                for n in self._surface_nodes(source_shader)))
        record['transmission_candidate']=transmitting
        if self.workflow=='legacy_main_v1' and obj is not None and pmx_index is None:
            from .material_policy import main_transmission_attempt
            candidate_nodes=self._surface_nodes(source_shader)
            partial_alpha=any(n.type=='BSDF_PRINCIPLED' and not n.inputs['Alpha'].is_linked and
                              0<float(value(n.inputs['Alpha']))<1 for n in candidate_nodes)
            candidate=transmitting or partial_alpha or 'ShaderNodeBsdfTranslucent' in surface_types
            thin_cutout=False
            if candidate and 'ShaderNodeBsdfTransparent' in surface_types:
                slots=[i for i,m in enumerate(obj.data.materials) if m==material]
                evidence=sheet_surface_evidence(obj.data,slots) if slots else {}
                thin_cutout=evidence.get('thin_open_surface',False)
            attempt=main_transmission_attempt(candidate=candidate,
                names=(original_name,obj.get('_mmd_source_name',obj.name)),
                size_m=(policy or {}).get('size_m',()),
                usage_count=int(obj.get('_mmd_transmission_usage',obj.get('_mmd_material_usage',1))),thin_cutout=thin_cutout)
            if candidate:
                record['main_transmission_attempt']=attempt
            self._main_transmission_repair=attempt['eligible']
        if obj is not None and pmx_index is None and self.workflow != 'legacy_main_v1':
            route=self.plan(material, policy, obj, record['material_decision'], requested=requested,
                repeat_count=int(obj.get('_mmd_material_usage',1)))
            if self.complex_strategy=='nodes' and route['route']=='pbr_bake':
                route.update(route='hybrid',reason='用户指定优先节点，覆盖自动完整 PBR 烘焙')
            record['restoration_plan']=route
            record['automatic_route']=route
            record['material_usage_count']=int(obj.get('_mmd_material_usage',1))
            record['strategy_reasons'].append(route['reason'])
            transmitting=bool(set(route['facts']['features']) & {'transmission','refraction','thin_transmission'})
            record['transmission_candidate']=transmitting
            if route['route']=='blocked':raise ValueError(route['reason']+'：'+str(route['facts']))
            override='hybrid' if route['route']=='hybrid' else override
        if simple_surface:
            record['strategy_reasons'] = ['有效表面仅含可还原的基础 PBR、贴图与简单计算，优先直接生成 UE 材质，无需烘焙']
        self.group_inputs = {}
        self.group_stack = []
        self.field_cache = {}
        self._surface_contexts = {}
        if hasattr(material, "mmd_material") and pmx_index is not None:
            mm = material.mmd_material
            record["mmd"] = {k: str(getattr(mm, k, "")) for k in
                              ("name_j", "name_e", "sphere_texture_type", "toon_texture", "shared_toon_texture",'is_shared_toon_texture','texture_rel_path','sphere_texture_rel_path','toon_texture_rel_path')}
            for key in ('diffuse_color','ambient_color','specular_color','edge_color'):
                record['mmd'][key] = list(getattr(mm,key))
            record['textures'] = {}
            for semantic,node_name in (('base_color','mmd_base_tex'),('sphere','mmd_sphere_tex'),('toon','mmd_toon_tex')):
                node = material.node_tree.nodes.get(node_name) if material.use_nodes else None
                if node and node.image:
                    record['textures'][semantic] = self.image(node.image)
        shader = None
        output = None
        if material.use_nodes:
            output = material.node_tree.get_output_node('CYCLES' if bpy.context.scene.render.engine == 'CYCLES' else 'EEVEE')
            if output and output.inputs["Surface"].is_linked:
                shader = output.inputs["Surface"].links[0].from_node
                for _ in range(30):
                    if shader.type != 'REROUTE' or not shader.inputs[0].is_linked:
                        break
                    shader = shader.inputs[0].links[0].from_node
        if (base_id in self.force_bake and self.complex_strategy=='pbr_bake' and self.bake_fallback and
                requested!='nodes' and obj is not None and pmx_index is None):
            record['compiler_fallback'] = self.force_bake[base_id]
            retry=self.force_bake[base_id]
            from .material_plan import fallback_plan
            step=fallback_plan(record['restoration_plan'],scope=retry.get('scope','ue_material_compile'),
                local_attempted=retry.get('opaque_fallback',False) or retry.get('route')=='pbr_bake')
            record.setdefault('fallback_attempts',[]).append({**step,'status':'started'})
            if step['route']=='blocked':raise ValueError(step['reason'])
            if step['route']=='local_fields':
                self._force_static_fields=True
            else:
                result=self.bake_basic_pbr(material,obj,record,step['reason'],
                    opaque_fallback=step['opaque_fallback'],project_view_mix=step['project_view_mix'])
                record['fallback_attempts'][-1]['status']='converted'
                return result
        if obj is not None and pmx_index is None and (requested=='bake' or
                record.get('automatic_route',{}).get('route')=='pbr_bake'):
            return self.bake_basic_pbr(material,obj,record,
                record.get('automatic_route',{}).get('reason','用户指定完整 PBR 烘焙'),
                opaque_fallback=transmitting,project_view_mix=requested in ('bake','baked'))
        if output and pmx_index is None and self.workflow != 'legacy_main_v1':
            self.adapt_displacement(material,output,shader,record)
        elif output and output.inputs['Displacement'].is_linked and pmx_index is None:
            add_issue(record, 'legacy_displacement_omitted', '主方案保持旧版法线，不额外叠加着色器位移', severity='approximation')
        unadapted_features=set(record.get('restoration_plan',{}).get('facts',{}).get('features',[])) & {'sheen','anisotropy'}
        if unadapted_features:
            raise ValueError('尚无可靠实时闭包适配：'+','.join(sorted(unadapted_features)))
        surface_nodes = self._surface_nodes(shader)
        if pmx_index is not None:
            # MMD's legacy Diffuse/Toon groups are handled by the character
            # texture route; scene Diffuse support must not opt them into PBR.
            surface_nodes = [node for node in surface_nodes if node.type in
                             ('BSDF_PRINCIPLED', 'BSDF_GLASS', 'BSDF_METALLIC')]
        glass_channels = self.glass_mix(shader) if self.restore_scene_appearance else None
        mixed_surface = shader is not None and shader.type in ("MIX_SHADER", "ADD_SHADER", "GROUP")
        principled_closure = bool((self.workflow != 'legacy_main_v1' or self._main_transmission_repair) and shader and shader.type == 'BSDF_PRINCIPLED' and ((self._main_transmission_repair and partial_alpha) or any(
            shader.inputs.get(name) and (shader.inputs[name].is_linked or abs(float(value(shader.inputs[name]))) > 1e-6)
            for name in ('Transmission Weight','Coat Weight','Subsurface Weight'))))
        legacy_translucent = bool(self.workflow == 'legacy_main_v1' and shader and
                                  shader.type == 'BSDF_TRANSLUCENT')
        surface_graph = None
        masks = None
        if (mixed_surface or legacy_translucent or shader and shader.type == 'BSDF_TRANSPARENT') and output and pmx_index is None:
            self.field_bake = (lambda socket: self.procedural_field(socket, material, obj, identity, record)) if obj else None
            try:
                masks = self.surface_masks(output.inputs['Surface'])
            except ValueError as exc:
                record['requires_visual_review'] = True
                record['coverage_error'] = str(exc)
                record['warnings'].append('表面覆盖率未完整还原：'+str(exc))
            finally:
                self.field_bake = None
        if (self.restore_scene_appearance and (mixed_surface or principled_closure or legacy_translucent or shader and shader.type in ('BSDF_GLOSSY','BSDF_DIFFUSE','EMISSION'))
                and not glass_channels and obj is not None and pmx_index is None):
            self.field_bake = lambda socket:self.procedural_field(socket,material,obj,identity,record)
            try:
                surface_graph = self.closure(output.inputs['Surface'])
                record['surface_graph'] = surface_graph
                expanded_surface = self.functions.expand(surface_graph)
                if self.workflow == 'legacy_main_v1' and has_translucent_closure(expanded_surface):
                    record['legacy_thin_sheet'] = True
                record['closure_features'] = sorted(closure_features(expanded_surface))
                if 'subsurface' in record['closure_features']:
                    add_issue(record, 'subsurface_model_difference',
                        '保留散射半径、尺度与权重混合；源随机游走与 UE 扩散模型需要视觉复核', severity='approximation')
                if 'transmission' in record['closure_features']:
                    add_issue(record, 'transmission_model_difference',
                        '保留透射闭包与源 IOR；屏幕折射、光线追踪和介质厚度响应需要视觉复核')
                if 'coat' in record['closure_features']:
                    add_issue(record, 'coat_model_difference',
                        '保留独立清漆层、权重、IOR、粗糙度、颜色和法线；UE 分层模型需要视觉复核')
                record['requires_visual_review'] = bool(record.get('requires_visual_review') or mixed_surface or record['closure_features'])
                record['strategy_reasons'].append('保留有效 Shader 混合树；程序字段局部烘焙，反射随视角实时求值')
                if mixed_surface:
                    add_issue(record, 'microfacet_model_difference', 'Blender 与 Substrate 的微表面分布和 Fresnel 存在差异')
                    record['warnings'].append('Blender 与 Substrate 的微表面分布和 Fresnel 存在差异，需视觉复核')
            except ValueError as exc:
                record['requires_visual_review'] = True
                record['unsupported_closures'] = str(exc)
                record['warnings'].append('闭包图未完整还原：'+str(exc))
                plan=record.get('restoration_plan',{})
                if plan.get('preserve') or set(plan.get('facts',{}).get('domains',[])) & {'view','ray'}:
                    raise SurfaceConversionError('受保护闭包未完整还原：'+str(exc)) from exc
                if override!='bake':
                    if isinstance(exc,ChannelConversionError): raise
                    raise SurfaceConversionError('无法保留完整 Shader 闭包，禁止自动选择主要 Principled：'+str(exc)) from exc
            finally:
                self.field_bake = None
        if shader and shader.type in ("MIX_SHADER", "ADD_SHADER", "GROUP") and surface_nodes and not surface_graph:
            record["requires_visual_review"] = True
            # Retain a relightable PBR backbone and bake only unsupported masks.
            shader = max(surface_nodes, key=lambda node: sum(socket.is_linked for socket in node.inputs))
            record["strategy_reasons"].append(
                "已展开 Node Group/Shader 闭包，保留主要 PBR 闭包并仅烘焙不兼容通道")
        if shader and shader.type == 'BSDF_PRINCIPLED' and not surface_graph:
            missing = []
            for name in ('Coat Weight','Sheen Weight','Subsurface Weight','Anisotropic IOR Level','Transmission Weight'):
                socket = shader.inputs.get(name)
                if socket and (socket.is_linked or abs(float(value(socket))) > 1e-6):
                    missing.append(name)
            if missing:
                record['requires_visual_review'] = True
                record['unsupported_inputs'] = missing
                record['warnings'].append('未完整还原的 Principled 分量：'+', '.join(missing))
                if self.workflow == 'legacy_main_v1':
                    add_issue(record,'legacy_principled_lobes','主方案保留旧版简化外观：'+', '.join(missing),severity='approximation')
            alpha=shader.inputs.get('Alpha')
            if alpha and not alpha.is_linked and 0 < float(value(alpha)) < 1:
                record['requires_visual_review']=True
                record['warnings'].append('部分覆盖率 Alpha 当前使用遮罩预览，需要透射视觉复核')
        if surface_graph:
            record['surface'] = 'pbr'
            leaf = self.functions.expand(surface_graph)
            while leaf['kind'] in ('mix','add'):
                leaf = leaf['a']
            record['channels'] = leaf['channels']
        elif shader and shader.type in ("BSDF_PRINCIPLED", "BSDF_GLASS", "BSDF_METALLIC", 'BSDF_DIFFUSE', 'BSDF_TRANSLUCENT'):
            glass = shader.type == "BSDF_GLASS"
            metallic_surface = shader.type == "BSDF_METALLIC"
            diffuse_surface = shader.type in ('BSDF_DIFFUSE', 'BSDF_TRANSLUCENT')
            record["surface"] = "glass" if glass else "pbr"
            mapping = {"base_color": "Color" if glass or diffuse_surface else "Base Color", "roughness": "Roughness",
                       "normal": "Normal", "ior": "IOR"}
            if diffuse_surface:
                record['channels']['specular'] = {'op':'constant', 'value':0.0}
                record['channels']['metallic'] = {'op':'constant', 'value':0.0}
            elif not glass and not metallic_surface:
                mapping.update(metallic="Metallic", opacity="Alpha", emission="Emission Color",
                               emission_strength="Emission Strength", transmission="Transmission Weight",
                               subsurface="Subsurface Weight")
            elif metallic_surface:
                record["channels"]["metallic"] = {"op": "constant", "value": 1.0}
                record["strategy_reasons"].append("Blender Metallic BSDF 已映射为 UE 金属 PBR；保留颜色与粗糙度")
            for channel, name in mapping.items():
                if glass_channels and channel in glass_channels:
                    record['channels'][channel] = glass_channels[channel]
                    continue
                socket = shader.inputs.get(name)
                if socket is None or (channel == "normal" and not socket.is_linked):
                    continue
                # Source semantics, not image names such as wet/parking, own
                # albedo and overlays. Legacy helpers remain for old records.
                channel_socket = socket
                self.field_bake = (lambda output: self.procedural_field(output, material, obj, identity, record)) if obj and override not in ('nodes','bake') else None
                try:
                    if override == "bake" and socket.is_linked:
                        raise ValueError("用户要求完整通道烘焙")
                    # Try the actual connected graph first, regardless of mesh
                    # size or the presence of groups/mixed shader closures.
                    if channel_socket != socket:
                        record["strategy_reasons"].append(
                            "Base Color 保留命名遮罩叠加前的可重打光 Albedo/AO 节点")
                    record["channels"][channel] = self.translate_channel(channel_socket, channel)
                except ValueError as exc:
                    if obj is None:
                        raise
                    if override == "nodes":
                        raise ValueError(f"材质 {original_name} 强制 nodes，但 {channel} 无法一一映射: {exc}") from exc
                    if pmx_index is None and override!='bake':
                        raise ValueError(f"材质 {original_name} 的 {channel} 没有通过语义检查的局部转换方案: {exc}；完整烘焙需显式选择 bake") from exc
                    if requested == "auto" and policy and policy["category"] == "tiled":
                        record["channels"][channel] = self._tiled_base(material, channel)
                        record["requires_visual_review"] = True
                        record["warnings"].append(f"{channel}: {exc}；大面积表面保留基础 PBR，程序效果需复核")
                        continue
                    record["warnings"].append(f"{channel}: {exc}；已烘焙")
                    record["channels"][channel] = self.bake(
                        obj, material, channel_socket, channel, stable_id(identity, channel),
                        final_surface=False)
                    record["baked_channels"].append(channel)
                    record["strategy_reasons"].append(f"{channel} 含 Blender 专属或程序节点")
                finally:
                    self.field_bake = None
            if glass_channels:
                record['warnings'].append('玻璃混合保留粗糙度遮罩；多闭包折射分布以单一 BSDF 近似，需视觉复核')
            # Generic mixed-surface mask extraction also covers the MyGO wetness
            # and parking-line graph without hard-coding a material name.
            if (len(surface_nodes) > 1 and obj is not None and override != "bake" and
                    not record.get('restoration_plan')):
                special = {}
                for node in material.node_tree.nodes:
                    semantic = self._special_image_semantic(node)
                    if semantic and semantic not in special:
                        output_socket = self._downstream_mask_output(node)
                        if output_socket is not None:
                            if override == "nodes":
                                raise ValueError(f"材质 {original_name} 的 {semantic} 遮罩需要局部烘焙，不能强制 nodes")
                            channel = semantic + "_mask"
                            special[semantic] = self.bake(obj, material, None, channel,
                                                          stable_id(identity, channel), output_socket)
                            record["baked_channels"].append(channel)
                            record["strategy_reasons"].append(f"{semantic} 程序遮罩已局部烘焙")
                if special:
                    record["special_masks"] = special
        elif not material.use_nodes and pmx_index is None:
            record['surface'] = 'pbr'
            for channel, val in (('base_color', list(material.diffuse_color)),
                                 ('metallic', material.metallic), ('roughness', material.roughness),
                                 ('opacity', material.diffuse_color[3])):
                record['channels'][channel] = {'op': 'constant', 'value': val}
        elif pmx_index is not None:
            record["surface"] = "pbr"
            record["channels"]["base_color"] = {"op": "constant", "value": list(material.diffuse_color)}
            if material.use_nodes:
                textures = [n for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]
                texture = next((n for n in textures if "base" in n.name.lower()), textures[0] if textures else None)
                if texture:
                    record["channels"]["base_color"] = {"op": "texture", "image": self.image(texture.image),
                        "output": "Color", "uv": {"op": "uv", "index": 0}}
                    record["channels"]["opacity"] = {**record["channels"]["base_color"], "output": "Alpha"}
            record["warnings"].append("角色使用基础贴图预览；Toon/Sphere 留待阶段二母材质")
        else:
            if obj is not None:
                kind = shader.bl_idname if shader else "无有效 Surface"
                if self.bake_fallback and self.complex_strategy == 'pbr_bake' and requested != 'nodes':
                    raise SurfaceConversionError(f'{kind} 无法可靠还原')
                fallback = self.fallback(material, f"{kind} 无法可靠还原", original_name)
                # Unknown color/BSDF behavior must not turn known empty pixels solid.
                if masks and masks['has_transparent']:
                    self.apply_surface_masks(fallback, masks)
                return fallback
            record["surface"] = "unsupported"
            record["warnings"].append("角色预览表面无法转换；角色母材质属于第二阶段第二部分")
        if masks and (masks['has_transparent'] or record.get('legacy_thin_sheet')):
            self.apply_surface_masks(record, masks)
        opacity=record['channels'].get('opacity')
        if (self._main_transmission_repair and opacity and opacity.get('op')=='constant' and
                0<float(opacity['value'])<1):
            record['continuous_opacity']=True
        if record.get('legacy_thin_sheet'):
            add_issue(record, 'legacy_thin_transmission',
                      'Translucent 颜色与法线保留；薄片透光使用原有 legacy_diffusion 双面散射近似，透明孔洞独立遮罩',
                      severity='approximation')
        if record["baked_channels"]:
            record["bake_layout"] = ("chunked_object_atlas" if policy and policy["category"] == "chunked"
                                     else self.bake_layout)
            record["bake_margin_pixels"] = 16
            if policy and policy["category"] == "tiled":
                record["requires_visual_review"] = True
            translated = [name for name in record["channels"] if name not in record["baked_channels"]]
            record["strategy"] = "hybrid" if translated and override != "bake" else "baked"
            record["bake_resolution"] = self.bake_resolution(material)
        elif len(surface_nodes) > 1:
            record["strategy"] = "nodes" if record.get('automatic_route',{}).get('route')=='nodes' else "hybrid"
        else:
            record["strategy"] = "nodes"
        if override == "hybrid" and record["strategy"] == "nodes":
            record["strategy_reasons"].append("用户指定 hybrid；当前图可完全节点复刻，无需产生冗余贴图")
            record["strategy"] = "hybrid"
        elif override == "bake":
            record["strategy"] = "baked"
        if not record["strategy_reasons"]:
            record["strategy_reasons"].append("所有使用中的语义均可稳定映射到 UE")
        if (obj is not None and pmx_index is None and record.get('surface') == 'pbr' and
                not water and 'transmission' in record['channels'] and
                record['channels'].get('opacity', {}).get('op') not in (None, 'constant')):
            # Inspect the complete mesh, including other material slots. The
            # clump's bounds do not describe the thickness of each open blade.
            slots = [i for i, candidate in enumerate(obj.data.materials)
                     if candidate and (candidate == material or candidate.name == original_name)]
            if slots:
                evidence = sheet_surface_evidence(obj.data, slots)
                record['surface_policy']['mesh_surface'] = evidence
                if evidence['thin_open_surface']:
                    record['requires_visual_review'] = True
                    record['strategy_reasons'].append('材质所在连通表面均为开放薄片；独立镂空与透光分别保留')
        record["master_material_candidate"] = bool(
            pmx_index is None and record.get("surface") == "pbr" and
            record["strategy"] == "nodes" and not record.get("special_masks") and not surface_graph and
            not record.get('coverage_source') and
            all(self.master_compatible_expr(expression) for expression in record["channels"].values()))
        if record["master_material_candidate"]:
            record["strategy_reasons"].append(
                "通道仅含贴图、常量或简单计算；UE 将读取用户场景母材质真实参数后决定是否创建实例")
        if pmx_index is None:
            definitions=self.functions.reachable(record)
            if definitions:record['function_definitions']=definitions
            decision=record.setdefault('material_decision',{})
            decision.update(function_count=len(definitions),lookup_images=sum(bool(i.get('lookup')) for i in self.images.values()),
                            field_records=len(record.get('baked_fields',[])),basis='estimate',
                            policy='functions_first',visual_status='needs_review')
        # Pure node graphs are independent of the evaluated object's topology.
        # Canonicalize them by graph content so linked copies, collection
        # instances and Geometry Nodes outputs compile one UE material. Baked
        # records retain their per-geometry identity because their BakeUV and
        # pixels can differ even when the source Blender material is shared.
        if not record["baked_channels"] and pmx_index is None:
            graph = self.normalized_graph({
                "surface": record.get("surface"), "channels": record["channels"],
                "surface_graph": record.get('surface_graph'),
                "water": record.get("water", False),
                "strategy": record["strategy"],
                "coverage_source": record.get('coverage_source'),
                "thin_surface_transmission": record.get('thin_surface_transmission', False),
            })
            if record.get('continuous_opacity'):graph['continuous_opacity']=True
            if self._main_transmission_repair and (record.get('surface')=='glass' or
                    'transmission' in record.get('closure_features',[]) or record.get('continuous_opacity')):
                graph['main_transmission_repair']=1
            if self.restore_scene_appearance:
                from .material_policy import cutout_transmission, glass_refraction_mode
                if record.get('surface') == 'glass':
                    record['refraction_mode'] = glass_refraction_mode(record)
                    graph['refraction_mode'] = record['refraction_mode']
                    if record['refraction_mode'] == 'pixel_normal_offset':
                        record['requires_visual_review'] = True
                        record['warnings'].append('薄平板使用像素法线偏移避免屏幕折射伪影；IOR 仍保留在 BSDF，需复核跨渲染器折射')
                elif 'transmission' in record['channels']:
                    graph['cutout_transmission'] = cutout_transmission(record)
            canonical = stable_id("material_graph", json.dumps(
                graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            cached = self.records.get(canonical)
            if cached is not None:
                aliases = cached.setdefault("source_aliases", [cached.get("source_name",cached['name'])])
                if original_name not in aliases:
                    aliases.append(original_name)
                cached.setdefault('source_restoration_plans',{})[original_name]=record.get('restoration_plan')
                material.name = cached["asset_name"]
                self._active_record = cached
                return cached
            identity = canonical
            record["id"] = canonical
            record["asset_name"] = asset_name("M", original_name + "_MMDBridge", canonical)
            record["source_aliases"] = [original_name]
        if record["asset_name"] is None:
            record["asset_name"] = asset_name("M", original_name + "_MMDBridge", identity)
        material.name = record["asset_name"]
        record["slot_name"] = material.name
        self.records[identity] = record
        self.warnings.extend(f"{original_name}: {w}" for w in record["warnings"])
        return record

    @staticmethod
    def apply_surface_masks(record, masks):
        record['channels']['opacity'] = masks['opacity']
        record['coverage_source'] = 'shader_mix'
        record['coverage_version'] = 1
        record.setdefault('strategy_reasons', []).append('有效 Shader 混合树的透明覆盖率独立连接 UE Opacity Mask')
        if masks['has_translucent']:
            record['channels']['transmission'] = masks['transmission']
            record['thin_surface_transmission'] = True
            record['requires_visual_review'] = True
            record['warnings'].append('Translucent BSDF 与透明孔洞分开转换；叶片透光使用双面散射近似，需视觉复核')
