"""Blender material extraction. Evaluates unsupported surface inputs via Cycles."""
import math
import json
from pathlib import Path
import shutil
import bpy
from .core import asset_name, stable_id
from .material_policy import classify_surface, projected_uv


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


class Materials:
    def simple_surface(self, material):
        """Conservative check of the active surface, using the real translator."""
        if not material.use_nodes:
            return True
        output = next((n for n in material.node_tree.nodes
                       if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
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
                                      'math', 'mix', 'invert'}:
                return False
            return all(simple(v) for v in expr.values() if isinstance(v, dict) and 'op' in v)
        self.group_inputs = {}
        try:
            return all(socket.name in supported and simple(self.expr(socket))
                       for socket in shader.inputs if socket.is_linked)
        except ValueError:
            return False

    def __init__(self, out, source, resolution=0, strategy="auto"):
        self.out = Path(out)
        self.source = source
        self.resolution = resolution
        self.strategy = strategy
        self.records = {}
        self.images = {}
        self.warnings = []
        self.object = None
        self.meters_per_unit = 1.0
        self.world_bounds = None
        self.material_world_bounds = {}
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
        if ext not in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".exr", ".hdr", ".tif", ".tiff"):
            ext = ".png"
        target = self.out / "Textures" / (asset_name("T", image.name, identity) + ext)
        original = Path(bpy.path.abspath(image.filepath))
        if image.packed_file:
            target.write_bytes(image.packed_file.data)
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
                  "color_space": image.colorspace_settings.name}
        self.images[image.name] = record
        return record

    def expr(self, socket, depth=0):
        if depth > 40:
            raise ValueError("节点递归过深")
        if not socket.is_linked:
            return {"op": "constant", "value": value(socket)}
        link = socket.links[0]
        node = link.from_node
        out = link.from_socket.name
        def sub(s):
            return self.expr(s, depth+1)
        if node.type == "REROUTE":
            return sub(node.inputs[0])
        if node.type == "GROUP_INPUT":
            mapped = self.group_inputs.get(link.from_socket.as_pointer())
            if mapped is None:
                raise ValueError("Node Group 输入无法映射到外部插槽")
            return self.expr(mapped, depth + 1)
        if node.type in ("VALUE", "RGB"):
            return {"op": "constant", "value": value(link.from_socket)}
        if node.type == "TEX_IMAGE":
            if not node.image:
                raise ValueError("图像节点未选择贴图")
            if node.projection != "FLAT" or node.image.source in ("MOVIE", "SEQUENCE", "TILED"):
                raise ValueError("非平面或动态图像需要烘焙")
            return {"op": "texture", "image": self.image(node.image), "output": out,
                    "uv": sub(node.inputs["Vector"]) if node.inputs["Vector"].is_linked else {"op": "uv", "index": 0},
                    "extension": node.extension}
        if node.type == "TEX_COORD" and out == "UV":
            return {"op": "uv", "index": 0}
        if node.type == "UVMAP":
            index = self.object.data.uv_layers.find(node.uv_map) if self.object and node.uv_map else 0
            if index < 0:
                raise ValueError('指定 UV 不存在: '+node.uv_map)
            return {"op": "uv", "index": index, "name": node.uv_map}
        if node.type == "MAPPING":
            if node.vector_type not in ("POINT", "TEXTURE", "VECTOR"):
                raise ValueError(f"{node.vector_type} Mapping 需要烘焙")
            rotation = sub(node.inputs["Rotation"])
            rotation_value = rotation.get("value") if rotation.get("op") == "constant" else None
            if (not isinstance(rotation_value, list) or len(rotation_value) < 3 or
                    any(abs(float(v)) > 1e-6 for v in rotation_value[:2])):
                raise ValueError("包含 X/Y 旋转或动态旋转的 Mapping 需要烘焙")
            return {"op": "mapping", "uv": sub(node.inputs["Vector"]),
                    "scale": sub(node.inputs["Scale"]), "offset": sub(node.inputs["Location"]),
                    "rotation": rotation, "vector_type": node.vector_type}
        blend_modes = ("MIX", "MULTIPLY", "ADD", "SUBTRACT", "SCREEN", "OVERLAY",
                       "DIFFERENCE", "DARKEN", "LIGHTEN", "DIVIDE")
        if node.type == "MIX_RGB":
            if node.blend_type not in blend_modes:
                raise ValueError("混合模式需要烘焙")
            return {"op": "mix", "mode": node.blend_type, "factor": sub(node.inputs[0]),
                    "a": sub(node.inputs[1]), "b": sub(node.inputs[2]), "clamp": node.use_clamp}
        if node.type == "MIX":
            if node.data_type != "RGBA" or node.blend_type not in blend_modes:
                raise ValueError("混合类型需要烘焙")
            return {"op": "mix", "mode": node.blend_type, "factor": sub(node.inputs[0]),
                    "a": sub(node.inputs[6]), "b": sub(node.inputs[7]), "clamp": node.clamp_result}
        if node.type == "INVERT":
            return {"op": "invert", "factor": sub(node.inputs[0]), "color": sub(node.inputs[1])}
        if node.type == "MATH" and node.operation in ("ABSOLUTE", "FLOOR", "CEIL", "FRACT", "SINE", "COSINE"):
            return {"op": "unary_math", "mode": node.operation, "a": sub(node.inputs[0]), "clamp": node.use_clamp}
        if node.type == "MATH" and node.operation in ("MULTIPLY", "ADD", "SUBTRACT", "DIVIDE", "POWER", "MINIMUM", "MAXIMUM"):
            return {"op": "math", "mode": node.operation, "a": sub(node.inputs[0]), "b": sub(node.inputs[1]), "clamp": node.use_clamp}
        if node.type == "NORMAL_MAP" and node.space == "TANGENT":
            return {"op": "normal", "color": sub(node.inputs["Color"]), "strength": sub(node.inputs["Strength"])}
        if node.type == "BUMP":
            normal = sub(node.inputs["Normal"]) if node.inputs["Normal"].is_linked else {
                "op": "constant", "value": [0, 0, 1]}
            try:
                height = sub(node.inputs["Height"])
            except ValueError as exc:
                if not node.inputs["Normal"].is_linked:
                    raise
                height = {"op": "constant", "value": 0}
                self.warnings.append(f"Bump 高度无法稳定翻译（{exc}），已保留输入法线")
            return {"op": "bump", "normal": normal, "height": height,
                    "strength": sub(node.inputs["Strength"]),
                    "distance": sub(node.inputs["Distance"]), "invert": bool(node.invert)}
        if node.type == "VALTORGB" and node.color_ramp.interpolation in ("LINEAR", "CONSTANT"):
            return {"op": "ramp", "factor": sub(node.inputs[0]), "interpolation": node.color_ramp.interpolation,
                    "elements": [{"position": e.position, "color": list(e.color)} for e in node.color_ramp.elements]}
        raise ValueError(f"{node.bl_idname}/{out} 需要烘焙")

    def bake(self, obj, material, socket, channel, identity, from_output=None):
        if obj.type != "MESH":
            raise ValueError("只能为网格烘焙")
        scene = bpy.context.scene
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
            world_points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
            spans = [max(p[i] for p in world_points) - min(p[i] for p in world_points) for i in range(3)]
            flat_axis = min(range(3), key=lambda i: spans[i])
            if self.surface_policy.get("category") == "tiled" and spans[flat_axis] < 1e-5 and self.world_bounds:
                axes = [i for i in range(3) if i != flat_axis]
                for loop in obj.data.loops:
                    bake_uv.data[loop.index].uv = projected_uv(world_points[loop.vertex_index], *self.world_bounds, axes)
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
        output = next(n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output)
        original_links = [(l.from_socket, l.to_socket) for l in output.inputs["Surface"].links]
        image_name = asset_name("Bake", channel, identity)
        cached_path = self.out / "Textures" / (image_name + ".png")
        if cached_path.is_file() and cached_path.stat().st_size > 128:
            rec = {"id": stable_id(self.source, image_name), "path": str(cached_path),
                   "name": image_name,
                   "color_space": "sRGB" if channel in ("base_color", "emission") else "Non-Color"}
            self.images[image_name] = rec
            return {"op": "texture", "image": rec, "output": "Color",
                    "uv": {"op": "uv", "index": bake_index}, "normal_map": channel == "normal", "extension": "EXTEND"}
        visibility = {o:o.hide_render for o in scene.objects}
        for other in scene.objects:
            other.hide_render = other != obj
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 16
        resolution = self.bake_resolution(material)
        image = bpy.data.images.new(image_name, resolution, resolution, alpha=True)
        image.colorspace_settings.name = "sRGB" if channel in ("base_color", "emission") else "Non-Color"
        image.filepath_raw = str(self.out / "Textures" / (image.name + ".png"))
        image.file_format = "PNG"
        targets = []
        emission = None
        try:
            # Every material slot needs a bake target; only this slot is kept in the result.
            for mat in obj.data.materials:
                if mat and mat.use_nodes:
                    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
                    tex.image = image
                    mat.node_tree.nodes.active = tex
                    targets.append((mat, tex))
            if channel != "normal":
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
            bpy.ops.object.bake(type="NORMAL" if channel == "normal" else "EMIT", use_clear=True,
                                margin=16, use_selected_to_active=False)
            image.save()
            rec = self.image(image)
            return {"op": "texture", "image": rec, "output": "Color", "uv": {"op": "uv", "index": bake_index},
                    "normal_map": channel == "normal", "extension": "EXTEND"}
        finally:
            if emission:
                nodes.remove(emission)
                for a, b in original_links:
                    links.new(a, b)
            for mat, tex in targets:
                mat.node_tree.nodes.remove(tex)
            scene.render.engine, scene.cycles.samples = old_engine, old_samples
            if 0 <= old_uv < len(obj.data.uv_layers):
                obj.data.uv_layers.active_index = old_uv
            for other, hidden in visibility.items():
                other.hide_render = hidden

    def _surface_nodes(self, node, depth=0):
        if node is None or depth > 30:
            return []
        if node.type in ("BSDF_PRINCIPLED", "BSDF_GLASS", "BSDF_METALLIC"):
            return [node]
        if node.type in ("MIX_SHADER", "ADD_SHADER"):
            result = []
            for socket in node.inputs:
                if socket.type == "SHADER" and socket.is_linked:
                    result.extend(self._surface_nodes(socket.links[0].from_node, depth + 1))
            return result
        if node.type == "GROUP" and node.node_tree:
            group_input = next((item for item in node.node_tree.nodes if item.type == "GROUP_INPUT"), None)
            if group_input:
                for index, internal in enumerate(group_input.outputs):
                    external = node.inputs.get(internal.name)
                    if external is None and index < len(node.inputs):
                        external = node.inputs[index]
                    if external is not None:
                        self.group_inputs[internal.as_pointer()] = external
            output = next((item for item in node.node_tree.nodes
                           if item.type == "GROUP_OUTPUT" and item.is_active_output), None)
            if output is None:
                output = next((item for item in node.node_tree.nodes if item.type == "GROUP_OUTPUT"), None)
            if output:
                surface = output.inputs.get("Shader") or output.inputs.get("Surface")
                if surface and surface.is_linked:
                    return self._surface_nodes(surface.links[0].from_node, depth + 1)
        return []

    def _downstream_mask_output(self, image_node):
        """Find the last color/math result derived from a named mask image."""
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

    def analyze(self, material, obj=None, water=False, pmx_index=None,
                source_name=None, geometry_identity=None):
        self.object=obj
        self.bake_layout = None
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
            points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
            spans = [(max(p[i] for p in points) - min(p[i] for p in points)) * self.meters_per_unit for i in range(3)]
            uv = obj.data.uv_layers[0] if obj.data.uv_layers else None
            uv_span = [max((p.uv[i] for p in uv.data), default=1) - min((p.uv[i] for p in uv.data), default=0) for i in range(2)] if uv else [1, 1]
            mapping_scale = [1, 1]
            if material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "MAPPING" and not node.inputs["Scale"].is_linked:
                        mapping_scale = [max(mapping_scale[i], abs(node.inputs["Scale"].default_value[i])) for i in range(2)]
            policy = classify_surface(spans, uv_span, mapping_scale,
                                      str(obj.get("mmd_bridge_surface_class", "auto")), min(spans) < 0.02)
        self.surface_policy = policy or {}
        # Per-object identities allow geometry-dependent procedural baking.
        override = str(obj.get("mmd_bridge_material_strategy", material.get("mmd_bridge_material_strategy", self.strategy)) if obj else material.get("mmd_bridge_material_strategy", self.strategy)).lower()
        requested = override
        simple_surface = pmx_index is None and override == 'auto' and self.simple_surface(material)
        if override == "auto" and policy:
            override = policy["strategy"]
        if simple_surface:
            override = 'nodes'
        override = "bake" if override == "baked" else override
        if override not in ("auto", "nodes", "hybrid", "bake"):
            raise ValueError(f"材质 {original_name} 的 mmd_bridge_material_strategy 无效: {override}")
        identity = stable_id(base_id, geometry_identity or obj.name, override, self.resolution,
                             json.dumps(policy, sort_keys=True)) if obj else base_id
        if identity in self.records:
            return self.records[identity]
        record = {"id": identity, "source_material_id": base_id, "name": original_name,
                  "asset_name": asset_name("M", original_name + "_MMDBridge", identity), "channels": {},
                  "water": water, "warnings": [], "pmx_index": pmx_index}
        record.update(strategy="nodes", strategy_requested=requested, surface_policy=policy,
                      strategy_reasons=[policy["reason"]] if policy else [],
                      baked_channels=[], bake_resolution=None)
        record['simple_surface'] = simple_surface
        if simple_surface:
            record['strategy_reasons'] = ['有效表面仅含可还原的基础 PBR、贴图与简单计算，优先直接生成 UE 材质，无需烘焙']
        self.group_inputs = {}
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
            output = next((n for n in material.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
            if output and output.inputs["Surface"].is_linked:
                shader = output.inputs["Surface"].links[0].from_node
                if output.inputs["Displacement"].is_linked:
                    displacement=output.inputs['Displacement'].links[0].from_node
                    record["displacement_node"] = displacement.name
                    if displacement.type=='DISPLACEMENT' and shader.inputs.get('Normal'):
                        bump=material.node_tree.nodes.new('ShaderNodeBump')
                        for source_key,target_key in (('Height','Height'),('Scale','Distance')):
                            source=displacement.inputs[source_key]
                            if source.is_linked:
                                material.node_tree.links.new(source.links[0].from_socket,bump.inputs[target_key])
                            else:
                                bump.inputs[target_key].default_value=source.default_value
                        if shader.inputs['Normal'].is_linked:
                            material.node_tree.links.new(shader.inputs['Normal'].links[0].from_socket,bump.inputs['Normal'])
                        material.node_tree.links.new(bump.outputs['Normal'],shader.inputs['Normal'])
                        for link in list(output.inputs['Displacement'].links):
                            material.node_tree.links.remove(link)
                        record['warnings'].append('位移高度已合入法线烘焙；不改变网格轮廓')
                    else:
                        record['warnings'].append('位移节点无法近似为法线，需要专项适配: '+displacement.bl_idname)
        surface_nodes = self._surface_nodes(shader)
        if shader and shader.type in ("MIX_SHADER", "ADD_SHADER", "GROUP") and surface_nodes:
            record["requires_visual_review"] = True
            # Retain a relightable PBR backbone and bake only unsupported masks.
            shader = max(surface_nodes, key=lambda node: sum(socket.is_linked for socket in node.inputs))
            record["strategy_reasons"].append(
                "已展开 Node Group/Shader 闭包，保留主要 PBR 闭包并仅烘焙不兼容通道")
            # Mixed closures still receive displacement-to-normal conversion on
            # their relightable PBR backbone.  Physical vertex height is unchanged.
            if output and output.inputs["Displacement"].is_linked and shader.inputs.get("Normal"):
                displacement = output.inputs["Displacement"].links[0].from_node
                if displacement.type == "DISPLACEMENT":
                    bump = material.node_tree.nodes.new("ShaderNodeBump")
                    for source_key, target_key in (("Height", "Height"), ("Scale", "Distance")):
                        source = displacement.inputs[source_key]
                        if source.is_linked:
                            material.node_tree.links.new(source.links[0].from_socket, bump.inputs[target_key])
                        else:
                            bump.inputs[target_key].default_value = source.default_value
                    if shader.inputs["Normal"].is_linked:
                        material.node_tree.links.new(shader.inputs["Normal"].links[0].from_socket,
                                                     bump.inputs["Normal"])
                    material.node_tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
                    record["strategy_reasons"].append("位移仅转换为 Normal/Height 细节，不改变物理地面高度")
        if shader and shader.type in ("BSDF_PRINCIPLED", "BSDF_GLASS", "BSDF_METALLIC"):
            glass = shader.type == "BSDF_GLASS"
            metallic_surface = shader.type == "BSDF_METALLIC"
            record["surface"] = "glass" if glass else "pbr"
            mapping = {"base_color": "Color" if glass else "Base Color", "roughness": "Roughness",
                       "normal": "Normal", "ior": "IOR"}
            if not glass and not metallic_surface:
                mapping.update(metallic="Metallic", opacity="Alpha", emission="Emission Color",
                               emission_strength="Emission Strength", transmission="Transmission Weight",
                               subsurface="Subsurface Weight")
            elif metallic_surface:
                record["channels"]["metallic"] = {"op": "constant", "value": 1.0}
                record["strategy_reasons"].append("Blender Metallic BSDF 已映射为 UE 金属 PBR；保留颜色与粗糙度")
            for channel, name in mapping.items():
                socket = shader.inputs.get(name)
                if socket is None or (channel == "normal" and not socket.is_linked):
                    continue
                channel_socket = (self._without_special_color_overlay(socket)
                                  if channel == "base_color" and override != "bake" else socket)
                try:
                    if override == "bake" and socket.is_linked:
                        raise ValueError("用户要求完整通道烘焙")
                    if channel_socket != socket:
                        record["strategy_reasons"].append(
                            "Base Color 保留命名遮罩叠加前的可重打光 Albedo/AO 节点")
                    record["channels"][channel] = self.expr(channel_socket)
                except ValueError as exc:
                    if obj is None:
                        raise
                    if override == "nodes":
                        raise ValueError(f"材质 {original_name} 强制 nodes，但 {channel} 无法一一映射: {exc}") from exc
                    record["warnings"].append(f"{channel}: {exc}；已烘焙")
                    record["channels"][channel] = self.bake(obj, material, channel_socket, channel, stable_id(identity, channel))
                    record["baked_channels"].append(channel)
                    record["strategy_reasons"].append(f"{channel} 含 Blender 专属或程序节点")
            # Generic mixed-surface mask extraction also covers the MyGO wetness
            # and parking-line graph without hard-coding a material name.
            if len(surface_nodes) > 1 and obj is not None and override != "bake":
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
                raise ValueError(f"材质 {original_name} 的 {kind} 无法可靠还原；已明确失败，未生成诊断色占位")
            record["surface"] = "unsupported"
            record["warnings"].append("角色预览表面无法转换；角色母材质属于第二阶段第二部分")
        if record["baked_channels"]:
            record["bake_layout"] = self.bake_layout
            record["bake_margin_pixels"] = 16
            if policy and policy["category"] == "tiled":
                record["requires_visual_review"] = True
            translated = [name for name in record["channels"] if name not in record["baked_channels"]]
            record["strategy"] = "hybrid" if translated and override != "bake" else "baked"
            record["bake_resolution"] = self.bake_resolution(material)
        elif len(surface_nodes) > 1:
            record["strategy"] = "hybrid"
        else:
            record["strategy"] = "nodes"
        if override == "hybrid" and record["strategy"] == "nodes":
            record["strategy_reasons"].append("用户指定 hybrid；当前图可完全节点复刻，无需产生冗余贴图")
            record["strategy"] = "hybrid"
        elif override == "bake":
            record["strategy"] = "baked"
        if not record["strategy_reasons"]:
            record["strategy_reasons"].append("所有使用中的语义均可稳定映射到 UE")
        record["master_material_candidate"] = bool(
            pmx_index is None and record.get("surface") == "pbr" and
            record["strategy"] == "nodes" and not record.get("special_masks") and
            all(self.master_compatible_expr(expression) for expression in record["channels"].values()))
        if record["master_material_candidate"]:
            record["strategy_reasons"].append(
                "通道仅含贴图、常量或简单计算；UE 将读取用户场景母材质真实参数后决定是否创建实例")
        # Pure node graphs are independent of the evaluated object's topology.
        # Canonicalize them by graph content so linked copies, collection
        # instances and Geometry Nodes outputs compile one UE material. Baked
        # records retain their per-geometry identity because their BakeUV and
        # pixels can differ even when the source Blender material is shared.
        if not record["baked_channels"] and pmx_index is None:
            graph = self.normalized_graph({
                "surface": record.get("surface"), "channels": record["channels"],
                "water": record.get("water", False),
                "strategy": record["strategy"],
            })
            canonical = stable_id("material_graph", json.dumps(
                graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            cached = self.records.get(canonical)
            if cached is not None:
                aliases = cached.setdefault("source_aliases", [cached["source_name"]])
                if original_name not in aliases:
                    aliases.append(original_name)
                material.name = cached["asset_name"]
                return cached
            identity = canonical
            record["id"] = canonical
            record["asset_name"] = asset_name("M", original_name + "_MMDBridge", canonical)
            record["source_aliases"] = [original_name]
        material.name = record["asset_name"]
        record["slot_name"] = material.name
        self.records[identity] = record
        self.warnings.extend(f"{original_name}: {w}" for w in record["warnings"])
        return record
