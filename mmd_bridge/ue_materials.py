"""Unreal material backend; JSON expressions are data, never executable source."""
import math
import struct
import zlib
from pathlib import Path
import unreal as u
from .core import file_hash
from .material_slots import validate_character_slots


def prop(obj, name, value):
    obj.set_editor_property(name, value)
    return obj


class Materials:
    def __init__(self, root, owner, report, cfg=None):
        self.root, self.owner, self.report, self.cfg = root, owner, report, (cfg or {})
        self.tools = u.AssetToolsHelpers.get_asset_tools()
        self.lib = u.MaterialEditingLibrary
        self.assets = {}
        self.textures = {}
        self.texture_hashes = {}
        self.water = set()
        self.material = None

    def managed(self, asset):
        if u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner") not in ("", self.owner):
            raise RuntimeError(f"资源归属冲突: {asset.get_path_name()}")
        u.EditorAssetLibrary.set_metadata_tag(asset, "MMDBridge.Owner", self.owner)

    def create(self, name, kind, factory):
        path = self.root + "/Materials/" + name
        asset = u.load_asset(path)
        if asset:
            if u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner") != self.owner:
                raise RuntimeError(f"拒绝覆盖未受管理的材质: {path}")
        else:
            asset = self.tools.create_asset(name, self.root + "/Materials", kind, factory)
        if asset is None:
            raise RuntimeError(f"无法创建资源 {path}")
        self.managed(asset)
        return asset

    def node(self, kind, **properties):
        node = self.lib.create_material_expression(self.material, getattr(u, "MaterialExpression"+kind))
        if node is None:
            raise RuntimeError(f"无法创建材质节点 {kind}")
        for k, v in properties.items():
            prop(node, k, v)
        return node, ""

    def connect(self, expr, target, input_name):
        names = [str(n) for n in self.lib.get_material_expression_input_names(target[0])]
        if input_name not in names:
            aliases = {"Coordinates":["UVs"], "A":["Base","Min"], "B":["Exp","Max"], "Input":["VectorInput","Vector","Input"]}
            input_name = next((n for n in aliases.get(input_name,[]) if n in names),
                              names[0] if len(names)==1 else input_name)
        if not self.lib.connect_material_expressions(expr[0], expr[1], target[0], input_name):
            raise RuntimeError(f"材质节点连接失败: {type(expr[0]).__name__} -> {type(target[0]).__name__}.{input_name}; inputs={names}")

    def op(self, kind, a, b=None):
        node = self.node(kind)
        self.connect(a, node, "A" if b else "Input")
        if b:
            self.connect(b, node, "B")
        return node

    def constant(self, value):
        if isinstance(value, list):
            if len(value) == 2:
                return self.node("Constant2Vector", r=value[0], g=value[1])
            value = value + [0] * (3-len(value))
            return self.node("Constant3Vector", constant=u.LinearColor(*value[:3], 1))
        return self.node("Constant", r=float(value))

    def scalar(self, name, default):
        return self.node("ScalarParameter", parameter_name=name, default_value=float(default))

    def texture(self, image, normal=False, data=False):
        key = image["id"] + ("_N" if normal else "_D" if data else "_C")
        if key in self.textures:
            return self.textures[key]
        name = "T_" + key
        path = self.root + "/Textures/" + name
        texture = u.load_asset(path)
        current_owner = u.EditorAssetLibrary.get_metadata_tag(texture, "MMDBridge.Owner") if texture else ""
        if texture and current_owner not in ("", self.owner):
            raise RuntimeError(f"拒绝覆盖外部贴图: {path}")
        digest = self.texture_hashes.get(image["path"])
        if digest is None:
            digest = file_hash(image["path"])
            self.texture_hashes[image["path"]] = digest
        if (texture and current_owner == self.owner and
                u.EditorAssetLibrary.get_metadata_tag(texture, "MMDBridge.SourceHash") == digest):
            self.textures[key] = texture
            return texture
        task = u.AssetImportTask()
        for k, v in dict(filename=image["path"], destination_path=self.root+"/Textures", destination_name=name,
                         automated=True, replace_existing=True, save=True).items():
            prop(task, k, v)
        self.tools.import_asset_tasks([task])
        texture = u.load_asset(path)
        if texture is None:
            raise RuntimeError(f"贴图导入失败 {image['path']}")
        prop(texture, "srgb", not normal and not data and image.get("color_space") not in ("Non-Color", "Linear", "Linear Rec.709"))
        if normal:
            prop(texture, "compression_settings", u.TextureCompressionSettings.TC_NORMALMAP)
            prop(texture, "flip_green_channel", True)
        elif data:
            prop(texture, "compression_settings", u.TextureCompressionSettings.TC_MASKS)
        self.managed(texture)
        u.EditorAssetLibrary.set_metadata_tag(texture, "MMDBridge.SourceHash", digest)
        u.EditorAssetLibrary.save_loaded_asset(texture)
        self.textures[key] = texture
        return texture

    def reuse(self, record):
        """Load a completed material checkpoint without rebuilding or recompiling it."""
        material_path = self.root + "/Materials/" + record["asset_name"]
        instance_path = self.root + "/Materials/MI_" + record["id"]
        material = u.load_asset(material_path)
        instance = u.load_asset(instance_path)
        master_route = (instance and
                        u.EditorAssetLibrary.get_metadata_tag(instance, "MMDBridge.MaterialRoute") == "user_master")
        if (not material and not master_route) or not instance:
            return False
        if any(u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner") != self.owner
               for asset in (material, instance)):
            return False
        self.assets[record["id"]] = instance_path
        if record.get("water"):
            self.water.add(record["id"])
        return True

    @staticmethod
    def _parameter_key(value):
        return "".join(character for character in str(value).lower() if character.isalnum())

    def _parameter_names(self, method, material):
        fn = getattr(self.lib, method, None)
        if not callable(fn):
            return []
        try:
            return [str(value) for value in fn(material)]
        except Exception:
            return []

    def _semantic_parameter(self, names, semantic, modifier=None):
        aliases = {
            "base_color": ("basecolor", "albedo", "diffuse"),
            "normal": ("normal",), "roughness": ("roughness", "rough"),
            "metallic": ("metallic", "metalness"), "opacity": ("opacity", "alpha"),
            "emission": ("emission", "emissive"), "ao": ("ao", "ambientocclusion"),
        }.get(semantic, (semantic,))
        modifier_aliases = {
            "scale": ("scale", "intensity", "strength", "multiply", "multiplier", "tint"),
            "offset": ("offset", "bias", "add"),
            "strength": ("strength", "intensity", "power", "scale"),
        }.get(modifier, (modifier,) if modifier else ())
        ranked = []
        for name in names:
            key = self._parameter_key(name)
            if not any(alias in key for alias in aliases):
                continue
            if modifier_aliases and not any(alias in key for alias in modifier_aliases):
                continue
            ranked.append((0 if key in aliases else len(key), name))
        return min(ranked, default=(0, None))[1]

    def _mi_channel(self, expression):
        kind = expression.get("op")
        if kind == "texture":
            if expression.get("uv", {"op":"uv"}).get("op") != "uv":
                return None
            return {"image": expression["image"], "modifier": None, "value": None}
        if kind == "constant":
            return {"image": None, "modifier": None, "value": expression.get("value")}
        if kind == "normal":
            result = self._mi_channel(expression["color"])
            if result:
                strength = self._mi_channel(expression.get("strength", {"op": "constant", "value": 1.0}))
                if not strength or strength.get("image"):
                    return None
                result["normal_strength"] = strength.get("value", 1.0)
            return result
        if kind == "math" and expression.get("mode") in ("MULTIPLY", "ADD"):
            left, right = self._mi_channel(expression["a"]), self._mi_channel(expression["b"])
            if not left or not right:
                return None
            textured = left if left["image"] else right if right["image"] else None
            constant = right if textured is left else left if textured is right else None
            if not textured or not constant or constant["image"]:
                return None
            textured["modifier"] = "scale" if expression["mode"] == "MULTIPLY" else "offset"
            textured["value"] = constant["value"]
            return textured
        return None

    @staticmethod
    def _neutral(channel, value):
        if channel in ("metallic", "transmission", "subsurface", "emission_strength"):
            return value == 0 or value == 0.0
        if channel == "opacity":
            return value == 1 or value == 1.0
        if channel == "emission" and isinstance(value, list):
            return not any(abs(float(item)) > 1e-6 for item in value[:3])
        return False

    def build_from_master(self, record):
        if not (self.cfg.get("use_scene_master_material", True) and
                record.get("master_material_candidate")):
            return None
        path = self.cfg.get("scene_master_material", "/Game/SelfMakeMaterial/Material/M_00")
        master = u.load_asset(path) if path else None
        if master is None:
            self.report["warnings"].append(f"场景母材质不存在，改用工具材质: {path}")
            return None
        texture_names = self._parameter_names("get_texture_parameter_names", master)
        scalar_names = self._parameter_names("get_scalar_parameter_names", master)
        vector_names = self._parameter_names("get_vector_parameter_names", master)
        switch_names = self._parameter_names("get_static_switch_parameter_names", master)
        if not texture_names and not scalar_names and not vector_names:
            self.report["warnings"].append(f"无法读取场景母材质参数，改用工具材质: {path}")
            return None
        assignments = []
        for channel, expression in record.get("channels", {}).items():
            if channel not in ("base_color", "normal", "roughness", "metallic", "opacity", "emission"):
                continue
            desc = self._mi_channel(expression)
            if desc is None:
                return None
            if desc["image"]:
                parameter = self._semantic_parameter(texture_names, channel)
                if not parameter:
                    return None
                assignments.append(("texture", parameter, desc["image"], channel))
                if desc.get("modifier"):
                    modifier = self._semantic_parameter(
                        vector_names if isinstance(desc["value"], list) else scalar_names,
                        channel, desc["modifier"])
                    neutral = (desc["modifier"] == "scale" and desc["value"] in (1, 1.0)) or (
                        desc["modifier"] == "offset" and desc["value"] in (0, 0.0))
                    if not modifier and not neutral:
                        return None
                    if modifier:
                        assignments.append(("vector" if isinstance(desc["value"], list) else "scalar",
                                            modifier, desc["value"], channel))
                if channel == "normal" and desc.get("normal_strength") not in (None, 1, 1.0):
                    strength_parameter = self._semantic_parameter(scalar_names, "normal", "strength")
                    if not strength_parameter:
                        return None
                    assignments.append(("scalar", strength_parameter,
                                        desc["normal_strength"], channel))
            elif not self._neutral(channel, desc["value"]):
                parameter = self._semantic_parameter(
                    vector_names if isinstance(desc["value"], list) else scalar_names, channel)
                if not parameter:
                    return None
                assignments.append(("vector" if isinstance(desc["value"], list) else "scalar",
                                    parameter, desc["value"], channel))
        if not assignments:
            return None
        # Validate every discovered channel switch before creating the child
        # instance.  Some engine versions expose switch enumeration but not the
        # instance setter; in that case using the master would silently leave a
        # texture channel disabled, so fall back to the generated tool material.
        switch_setter = getattr(
            self.lib, "set_material_instance_static_switch_parameter_value", None)
        switch_assignments = []
        for kind, _parameter, _value, channel in assignments:
            if kind != "texture":
                continue
            switch = self._semantic_parameter(switch_names, channel)
            if switch:
                if not callable(switch_setter):
                    return None
                switch_assignments.append(switch)
        instance = self.create("MI_" + record["id"], u.MaterialInstanceConstant,
                               u.MaterialInstanceConstantFactoryNew())
        self.lib.set_material_instance_parent(instance, master)
        for kind, parameter, value, channel in assignments:
            name = u.Name(parameter)
            if kind == "texture":
                texture = self.texture(value, normal=channel == "normal",
                                       data=channel in ("roughness", "metallic", "opacity", "ao"))
                self.lib.set_material_instance_texture_parameter_value(instance, name, texture)
            elif kind == "scalar":
                self.lib.set_material_instance_scalar_parameter_value(instance, name, float(value))
            else:
                values = list(value) + [1, 1, 1, 1]
                self.lib.set_material_instance_vector_parameter_value(
                    instance, name, u.LinearColor(*[float(item) for item in values[:4]]))
        for switch in switch_assignments:
            switch_setter(instance, u.Name(switch), True)
        self.lib.update_material_instance(instance)
        self.managed(instance)
        u.EditorAssetLibrary.set_metadata_tag(instance, "MMDBridge.SourceMaterial", record["id"])
        u.EditorAssetLibrary.set_metadata_tag(instance, "MMDBridge.MaterialRoute", "user_master")
        u.EditorAssetLibrary.set_metadata_tag(instance, "MMDBridge.ParentMaterial", path)
        u.EditorAssetLibrary.save_loaded_asset(instance)
        self.assets[record["id"]] = instance.get_path_name()
        self.report.setdefault("material_routes", {})[record["id"]] = {
            "route": "user_master", "parent": path,
            "assigned_parameters": [item[1] for item in assignments]}
        return instance

    def release_batch(self):
        """Drop Python references so UE can release imported render resources at GC."""
        self.material = None
        self.textures.clear()

    def asset(self, identity):
        value = self.assets[identity]
        return u.load_asset(value) if isinstance(value, str) else value

    def expr(self, expr, normal=False, data=False):
        kind = expr["op"]
        sub = lambda e: self.expr(e, data=data)
        if kind == "constant":
            return self.constant(expr["value"])
        if kind == "uv":
            return self.node("TextureCoordinate", coordinate_index=expr.get("index", 0))
        if kind == "mapping":
            # Blender V=0 is bottom; UE V=0 is top. Convert before and after Mapping.
            uv = sub(expr["uv"])
            b_uv = self.op("Add", self.op("Multiply", uv, self.constant([1, -1])), self.constant([0, 1]))
            def component(expression, red=False, green=False):
                mask = self.node("ComponentMask", r=red, g=green)
                self.connect(expression, mask, "Input")
                return mask
            def xy(expression):
                return component(expression, red=True, green=True)
            rotation = expr.get("rotation", {"op": "constant", "value": [0, 0, 0]})
            angle = rotation.get("value") if rotation.get("op") == "constant" else None
            if not isinstance(angle, list) or len(angle) < 3:
                raise RuntimeError("Mapping 的动态旋转未在 Blender 端烘焙")
            angle = float(angle[2])
            vector_type = expr.get("vector_type", "POINT")
            if vector_type == "TEXTURE":
                # Blender's Texture mode applies the inverse transform:
                # inverse translation, inverse rotation, then inverse scale.
                mapped = self.op("Subtract", b_uv, xy(sub(expr["offset"])))
                angle = -angle
            else:
                mapped = self.op("Multiply", b_uv, xy(sub(expr["scale"])))
            if abs(angle) > 1e-8:
                x = component(mapped, red=True)
                y = component(mapped, green=True)
                cosine, sine = self.constant(math.cos(angle)), self.constant(math.sin(angle))
                out_x = self.op("Subtract", self.op("Multiply", x, cosine), self.op("Multiply", y, sine))
                out_y = self.op("Add", self.op("Multiply", x, sine), self.op("Multiply", y, cosine))
                rotated = self.node("AppendVector")
                self.connect(out_x, rotated, "A")
                self.connect(out_y, rotated, "B")
                mapped = rotated
            if vector_type == "TEXTURE":
                mapped = self.op("Divide", mapped, xy(sub(expr["scale"])))
            elif vector_type == "POINT":
                mapped = self.op("Add", mapped, xy(sub(expr["offset"])))
            return self.op("Add", self.op("Multiply", mapped, self.constant([1, -1])), self.constant([0, 1]))
        if kind == "texture":
            tex = self.texture(expr["image"], normal or expr.get("normal_map", False), data)
            node = self.node("TextureSample", texture=tex, sampler_type=u.MaterialSamplerType.SAMPLERTYPE_NORMAL if normal or expr.get("normal_map") else
                             u.MaterialSamplerType.SAMPLERTYPE_MASKS if data else
                             u.MaterialSamplerType.SAMPLERTYPE_COLOR if tex.get_editor_property("srgb") else u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
            uv = self.expr(expr.get("uv", {"op":"uv"}))
            if expr.get("uv", {}).get("op") == "mapping":
                mask = self.node("ComponentMask", r=True, g=True)
                self.connect(uv, mask, "Input")
                uv = mask
            self.connect(uv, node, "Coordinates")
            if expr.get("extension") == "EXTEND":
                prop(node[0], "sampler_source", u.SamplerSourceMode.SSM_CLAMP_WORLD_GROUP_SETTINGS)
            if (normal or expr.get("normal_map")) and expr.get("uv", {}).get("index", 0) != 0:
                # A normal baked against UV1 cannot use the mesh's UV0 tangent
                # basis. Derive the basis from the actual sampling coordinates.
                function = u.load_asset("/Engine/Functions/Engine_MaterialFunctions01/Texturing/DeriveTangentBasis")
                if function is None:
                    raise RuntimeError("缺少 DeriveTangentBasis，不能安全使用独立 UV 法线")
                basis = self.node("MaterialFunctionCall", material_function=function)
                inputs = [str(n) for n in self.lib.get_material_expression_input_names(basis[0])]
                uv_input = next((n for n in inputs if "uv" in n.lower()), None)
                normal_input = next((n for n in inputs if "normal" in n.lower()), None)
                if not uv_input or not normal_input:
                    raise RuntimeError("DeriveTangentBasis 接口不匹配: " + str(inputs))
                self.connect(uv, basis, uv_input)
                self.connect((node[0], "RGB"), basis, normal_input)
                outputs = [str(n) for n in self.lib.get_material_expression_output_names(basis[0])]
                world_output = next((n for n in outputs if "normal" in n.lower()), None)
                if world_output is None:
                    raise RuntimeError("DeriveTangentBasis 未提供法线输出")
                transform = self.node("Transform", transform_source_type=u.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_WORLD,
                                      transform_type=u.MaterialVectorCoordTransform.TRANSFORM_TANGENT)
                self.connect((basis[0], world_output), transform, "Input")
                return self.op("Normalize", transform)
            return node[0], "A" if expr.get("output") == "Alpha" else "RGB"
        if kind == "normal":
            node = self.expr(expr["color"], normal=True, data=False)
            strength = self.expr(expr["strength"])
            scaled = self.op("Multiply", node, self.op("Add", self.op("Multiply", self.constant([1,1,0]), strength), self.constant([0,0,1])))
            return self.op("Normalize", scaled)
        if kind == "bump":
            normal = self.expr(expr["normal"], normal=True)
            height = self.expr(expr["height"], data=True)
            strength = self.op("Multiply", self.expr(expr["strength"]), self.expr(expr["distance"]))
            dx, dy = self.node("DDX"), self.node("DDY")
            self.connect(height, dx, "Value")
            self.connect(height, dy, "Value")
            if expr.get("invert"):
                strength = self.op("Multiply", strength, self.constant(-1))
            x = self.op("Multiply", dx, self.op("Multiply", strength, self.constant(-1)))
            y = self.op("Multiply", dy, self.op("Multiply", strength, self.constant(-1)))
            xy = self.node("AppendVector")
            self.connect(x, xy, "A")
            self.connect(y, xy, "B")
            derived = self.node("AppendVector")
            self.connect(xy, derived, "A")
            self.connect(self.constant(1), derived, "B")
            derived = self.op("Normalize", derived)
            # Angle-correct blending is preferable, but normalized tangent-vector
            # addition is deterministic and keeps both imported normal and height.
            return self.op("Normalize", self.op("Subtract",
                self.op("Add", normal, derived), self.constant([0,0,1])))
        if kind == "unary_math":
            mapping = {"ABSOLUTE": "Abs", "FLOOR": "Floor", "CEIL": "Ceil", "FRACT": "Frac", "SINE": "Sine", "COSINE": "Cosine"}
            result = self.op(mapping[expr["mode"]], sub(expr["a"]))
            if expr["mode"] in ("SINE", "COSINE"):
                prop(result[0], "period", math.tau)
            return self.op("Saturate", result) if expr.get("clamp") else result
        if kind == "math":
            mapping = {"MULTIPLY":"Multiply", "ADD":"Add", "SUBTRACT":"Subtract", "DIVIDE":"Divide",
                       "POWER":"Power", "MINIMUM":"Min", "MAXIMUM":"Max"}
            result = self.op(mapping[expr["mode"]], sub(expr["a"]), sub(expr["b"]))
            return self.op("Saturate", result) if expr.get("clamp") else result
        if kind == "mix":
            a, b, fac = sub(expr["a"]), sub(expr["b"]), sub(expr["factor"])
            if expr["mode"] != "MIX":
                mode = expr["mode"]
                if mode in ("MULTIPLY", "ADD", "SUBTRACT", "DIVIDE"):
                    b = self.op({"MULTIPLY":"Multiply", "ADD":"Add", "SUBTRACT":"Subtract",
                                 "DIVIDE":"Divide"}[mode], a, b)
                elif mode == "SCREEN":
                    b = self.op("OneMinus", self.op("Multiply", self.op("OneMinus", a), self.op("OneMinus", b)))
                elif mode == "DIFFERENCE":
                    b = self.op("Abs", self.op("Subtract", a, b))
                elif mode in ("DARKEN", "LIGHTEN"):
                    b = self.op("Min" if mode == "DARKEN" else "Max", a, b)
                elif mode == "OVERLAY":
                    low = self.op("Multiply", self.constant(2), self.op("Multiply", a, b))
                    high = self.op("OneMinus", self.op("Multiply", self.constant(2),
                        self.op("Multiply", self.op("OneMinus", a), self.op("OneMinus", b))))
                    threshold = self.op("Saturate", self.op("Multiply",
                        self.op("Subtract", a, self.constant(.5)), self.constant(10000)))
                    overlay = self.node("LinearInterpolate")
                    self.connect(low, overlay, "A")
                    self.connect(high, overlay, "B")
                    self.connect(threshold, overlay, "Alpha")
                    b = overlay
            node = self.node("LinearInterpolate")
            for source, name in ((a,"A"),(b,"B"),(fac,"Alpha")):
                self.connect(source,node,name)
            return self.op("Saturate", node) if expr.get("clamp") else node
        if kind == "invert":
            color = sub(expr["color"])
            inverse = self.op("OneMinus", color)
            node = self.node("LinearInterpolate")
            self.connect(color,node,"A")
            self.connect(inverse,node,"B")
            self.connect(sub(expr["factor"]),node,"Alpha")
            return node
        if kind == "ramp":
            elements = sorted(expr["elements"], key=lambda e:e["position"])
            result = self.constant(elements[0]["color"])
            factor = sub(expr["factor"])
            for prev, curr in zip(elements,elements[1:]):
                alpha = self.op("Saturate", self.op("Divide", self.op("Subtract", factor,self.constant(prev["position"])),
                    self.constant(max(1e-5,curr["position"]-prev["position"]))))
                if expr["interpolation"] == "CONSTANT":
                    alpha = self.op("Floor", alpha)
                lerp = self.node("LinearInterpolate")
                self.connect(result,lerp,"A")
                self.connect(self.constant(curr["color"]),lerp,"B")
                self.connect(alpha,lerp,"Alpha")
                result = lerp
            return result
        raise RuntimeError("未知材质表达式: " + kind)

    def water_normal(self, folder):
        # Deterministic tileable normal texture. No image library required in UE Python.
        target = Path(folder) / "wave_normal.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 128
        rows = bytearray()
        for y in range(size):
            rows.append(0)
            for x in range(size):
                nx = .22*math.cos(x/size*math.tau*3+y/size*math.tau*2)
                ny = .22*math.cos(x/size*math.tau-y/size*math.tau*4)
                nz = math.sqrt(1-nx*nx-ny*ny)
                rows.extend(int((v*.5+.5)*255) for v in (nx,ny,nz))
        def chunk(kind, data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        target.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,2,0,0,0))+
                           chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))
        image = {"id":"water_normal_v1", "path":str(target),"color_space":"Non-Color"}
        normals = []
        for n, defaults in enumerate(((.025,.01),(-.015,.02))):
            uv = self.node("TextureCoordinate")
            uv = self.op("Multiply",uv,self.scalar(f"WaveScale{n+1}",5+n*2))
            panner = self.node("Panner")
            self.connect(uv,panner,"Coordinate")
            self.connect(self.scalar("SequenceTime",0),panner,"Time")
            speed = self.node("AppendVector")
            self.connect(self.scalar(f"WaveSpeedX{n+1}",defaults[0]),speed,"A")
            self.connect(self.scalar(f"WaveSpeedY{n+1}",defaults[1]),speed,"B")
            self.connect(speed,panner,"Speed")
            sample = self.node("TextureSample",texture=self.texture(image,normal=True),sampler_type=u.MaterialSamplerType.SAMPLERTYPE_NORMAL)
            self.connect(panner,sample,"Coordinates")
            normals.append(sample)
        combined = self.op("Normalize",self.op("Add",*normals))
        scaled = self.op("Multiply",combined,self.op("Add",self.op("Multiply",self.constant([1,1,0]),self.scalar("WaveStrength",.3)),self.constant([0,0,1])))
        return self.op("Normalize",scaled)

    def build(self, record, cache_dir):
        instance = self.build_from_master(record)
        if instance is not None:
            return instance
        material = self.create(record["asset_name"],u.Material,u.MaterialFactoryNew())
        self.material = material
        self.lib.delete_all_material_expressions(material)
        channels = record["channels"]
        if record.get("surface") == "unsupported":
            self.report["warnings"].append(f"材质未还原: {record['name']}")
            self.lib.connect_material_property(self.constant([1,0,1])[0],"",u.MaterialProperty.MP_BASE_COLOR)
        transmission = channels.get("transmission")
        has_transmission = bool(transmission and (
            transmission.get("op") != "constant" or float(transmission.get("value", 0)) > 1e-4))
        glass = record.get("surface") == "glass" or record.get("water") or has_transmission
        opacity = channels.get("opacity")
        masked = bool(opacity and (opacity["op"] != "constant" or opacity.get("value",1) < 1))
        prop(material,"two_sided",masked)
        prop(material,"blend_mode",u.BlendMode.BLEND_TRANSLUCENT if glass else u.BlendMode.BLEND_MASKED if masked else u.BlendMode.BLEND_OPAQUE)
        if glass:
            prop(material,"translucency_lighting_mode",u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
        mapping = {"base_color":"MP_BASE_COLOR","roughness":"MP_ROUGHNESS","metallic":"MP_METALLIC",
                   "normal":"MP_NORMAL","emission":"MP_EMISSIVE_COLOR","opacity":"MP_OPACITY_MASK"}
        special_masks = record.get("special_masks", {})
        for key, target in mapping.items():
            if key not in channels or (glass and key == "opacity") or (record.get("water") and key in ("normal","roughness","base_color")):
                continue
            if special_masks and key in ("base_color", "roughness", "normal"):
                continue
            expression = self.expr(channels[key],data=key in ("roughness","metallic","opacity"))
            if key == "emission" and "emission_strength" in channels:
                expression = self.op("Multiply",expression,self.expr(channels["emission_strength"]))
            if not self.lib.connect_material_property(expression[0],expression[1],getattr(u.MaterialProperty,target)):
                raise RuntimeError(f"无法连接材质输出 {key}")
        if special_masks:
            def lerp(a, b, alpha):
                node = self.node("LinearInterpolate")
                self.connect(a, node, "A")
                self.connect(b, node, "B")
                self.connect(alpha, node, "Alpha")
                return node
            base = self.expr(channels.get("base_color", {"op":"constant", "value":[.18,.18,.18,1]}))
            rough = self.expr(channels.get("roughness", {"op":"constant", "value":.6}), data=True)
            normal = self.expr(channels.get("normal", {"op":"constant", "value":[0,0,1]}), normal=True)
            water_mask = (self.expr(special_masks["water"], data=True)
                          if "water" in special_masks else self.constant(0))
            line_mask = (self.expr(special_masks["parking_line"], data=True)
                         if "parking_line" in special_masks else self.constant(0))
            wet_tint = self.node("VectorParameter", parameter_name="WetTint",
                                 default_value=u.LinearColor(.42,.45,.48,1))
            wet_base = self.op("Multiply", base, wet_tint)
            final_base = lerp(base, wet_base, water_mask)
            line_color = self.node("VectorParameter", parameter_name="ParkingLineColor",
                                   default_value=u.LinearColor(.85,.82,.55,1))
            final_base = lerp(final_base, line_color, line_mask)
            final_rough = lerp(rough, self.scalar("WaterRoughness", .035), water_mask)
            final_rough = lerp(final_rough, self.scalar("ParkingLineRoughness", .42), line_mask)
            final_normal = lerp(normal, self.constant([0,0,1]), water_mask)
            for expression, target in ((final_base, u.MaterialProperty.MP_BASE_COLOR),
                                       (final_rough, u.MaterialProperty.MP_ROUGHNESS),
                                       (final_normal, u.MaterialProperty.MP_NORMAL)):
                if not self.lib.connect_material_property(expression[0], expression[1], target):
                    raise RuntimeError("无法连接 MyGO/混合表面材质输出")
            # UE's Specular input is normalized so 0.5 is roughly dielectric
            # IOR 1.5.  IOR 1.45 corresponds to about 0.42.
            specular = lerp(self.scalar("DrySpecular", .5), self.scalar("WaterIOR145Specular", .42), water_mask)
            self.lib.connect_material_property(specular[0], specular[1], u.MaterialProperty.MP_SPECULAR)
        if glass:
            self.lib.connect_material_property(self.scalar("Opacity",.35)[0],"",u.MaterialProperty.MP_OPACITY)
            ior = self.scalar("Refraction",1.333 if record.get("water") else 1.45)
            self.lib.connect_material_property(ior[0],"",u.MaterialProperty.MP_REFRACTION)
        if record.get("water"):
            color = self.node("VectorParameter",parameter_name="WaterColor",default_value=u.LinearColor(.03,.25,.3,1))
            self.lib.connect_material_property(color[0],"",u.MaterialProperty.MP_BASE_COLOR)
            self.lib.connect_material_property(self.scalar("Roughness",.08)[0],"",u.MaterialProperty.MP_ROUGHNESS)
            normal = self.water_normal(cache_dir)
            self.lib.connect_material_property(normal[0],normal[1],u.MaterialProperty.MP_NORMAL)
            self.water.add(record["id"])
        self.lib.layout_material_expressions(material)
        self.lib.recompile_material(material)
        instance = self.create("MI_"+record["id"],u.MaterialInstanceConstant,u.MaterialInstanceConstantFactoryNew())
        self.lib.set_material_instance_parent(instance,material)
        self.lib.update_material_instance(instance)
        u.EditorAssetLibrary.set_metadata_tag(instance,"MMDBridge.SourceMaterial",record["id"])
        u.EditorAssetLibrary.set_metadata_tag(material,"MMDBridge.MaterialStrategy",record.get("strategy","nodes"))
        u.EditorAssetLibrary.set_metadata_tag(instance,"MMDBridge.MaterialStrategy",record.get("strategy","nodes"))
        u.EditorAssetLibrary.save_loaded_asset(material)
        u.EditorAssetLibrary.save_loaded_asset(instance)
        self.assets[record["id"]] = instance.get_path_name()
        self.report.setdefault("material_routes", {})[record["id"]] = {
            "route": record.get("strategy", "nodes"), "parent": material.get_path_name()}
        return instance

    def assign(self, mesh, records):
        if isinstance(mesh,u.SkeletalMesh):
            slots = list(mesh.get_editor_property("materials"))
            preservation = validate_character_slots(
                [str(slot.get_editor_property("imported_material_slot_name")) for slot in slots], records)
            mappings = []
            for index,slot in enumerate(slots):
                name = str(slot.get_editor_property("imported_material_slot_name"))
                record = next((r for r in records if r["slot_name"] == name),None)
                if record:
                    slot.set_editor_property("material_interface",self.asset(record["id"]))
                    mappings.append({'ue_slot':index,'imported_name':name,'material_id':record['id'],
                                     'pmx_index':record.get('pmx_index'),'source_name':record['name']})
                else:
                    raise RuntimeError(f"未匹配角色材质槽: {name}")
            mesh.set_editor_property("materials",slots)
            self.report['checks']['material_slots'] = mappings
            self.report['checks']['material_slot_preservation'] = preservation
        else:
            for index, identity in enumerate(records):
                if identity and identity in self.assets:
                    mesh.set_material(index,self.asset(identity))
        u.EditorAssetLibrary.save_loaded_asset(mesh)

    def validate(self):
        return {key:(value if isinstance(value, str) else value.get_path_name())
                for key,value in self.assets.items()}
