"""Preserved path-tracing material baseline; shared mappings require native validation.

JSON expressions are data, never executable source.
"""
import math
import json
import re
import struct
import zlib
from pathlib import Path
import unreal as u
from .core import file_hash, read_json, atomic_json
from .material_slots import validate_character_slots
from .material_policy import texture_role, cutout_transmission, glass_refraction_mode, masked_surface
from .material_graph import signature, material_template, semantic_ir

BACKEND_VERSION=16


def prop(obj, name, value):
    obj.set_editor_property(name, value)
    return obj


def _field_texture_size(texture, cfg):
    """Limit GPU field resolution, retaining signed linear HDR values on disk."""
    tag = 'MMDBridge.FieldPreviousMaxTextureSize'
    previous = u.EditorAssetLibrary.get_metadata_tag(texture, tag)
    current = texture.get_editor_property('max_texture_size')
    if cfg.get('scene_import_profile') == 'render_optimized':
        if not previous:
            u.EditorAssetLibrary.set_metadata_tag(texture, tag, str(current))
        desired = min(current, 1024) if current > 0 else 1024
    elif previous:
        desired = int(previous)
        u.EditorAssetLibrary.set_metadata_tag(texture, tag, '')
    else:
        return False
    if current == desired:
        return False
    texture.set_editor_property('max_texture_size', desired)
    return True


def configure_baked_field_textures(scene, cfg, root, report, progress=None):
    """Also apply the policy to reused material checkpoints before Stage load."""
    images = {}
    field_ids = {field['image_id'] for record in scene.get('materials', [])
                 for field in record.get('baked_fields', []) if field.get('image_id')}
    def collect(value):
        if isinstance(value, dict):
            image = value.get('image')
            if isinstance(image, dict) and image.get('hdr') and (image.get('generated_bake') or image.get('id') in field_ids):
                images[image['id']] = image
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(scene.get('materials', []))
    checks = []
    for image in images.values():
        base = root + '/Textures/' + image.get('asset_name', 'T_' + image['id'])
        digest=file_hash(image['path'])
        for suffix in ('_C', '_D', '_N'):
            from .material_contract import asset_name_for_path
            name=image.get('asset_name','T_'+image['id'])+suffix+'_v2_'+digest[:12]
            path=root+'/Textures/'+asset_name_for_path(root+'/Textures',name,u.Paths.project_content_dir())
            texture = u.load_asset(path)
            if texture is None:
                path=base+suffix
                texture=u.load_asset(path)
            if not isinstance(texture, u.Texture2D):
                continue
            owner=u.EditorAssetLibrary.get_metadata_tag(texture, 'MMDBridge.Owner')
            if not owner:
                raise RuntimeError('数据贴图缺少工具归属标记: ' + path)
            protection=Materials(root,owner,report,cfg)
            protection.verify_asset(texture)
            previous = texture.get_editor_property('max_texture_size')
            changed = _field_texture_size(texture, cfg)
            if changed and not u.EditorAssetLibrary.save_loaded_asset(texture):
                raise RuntimeError('无法保存数据贴图尺寸预算: ' + path)
            if changed:protection.protect_asset(texture)
            checks.append({'path':texture.get_path_name(), 'previous_max_size':previous,
                           'max_size':texture.get_editor_property('max_texture_size'), 'changed':changed})
            if progress:
                progress(len(checks), len(images))
    report.setdefault('checks', {})['baked_field_gpu_budget'] = {'textures':checks,
        'profile':cfg.get('scene_import_profile','faithful'), 'source_images_unchanged':True,
        'needs_review':cfg.get('scene_import_profile')=='render_optimized'}
    return checks


class Materials:
    def __init__(self, root, owner, report, cfg=None, check_resources=None):
        self.root, self.owner, self.report, self.cfg = root, owner, report, (cfg or {})
        self.tools = u.AssetToolsHelpers.get_asset_tools()
        self.lib = u.MaterialEditingLibrary
        self.assets = {}
        self.textures = {}
        self.texture_hashes = {}
        self.water = set()
        self.material = None
        self._expression_material = None
        self._expression_cache = {}
        self.check_resources = check_resources
        self.function_definitions = {}
        self.functions = {}
        self.function_ports = None
        self._parameter_defaults = {}
        self._texture_parameter_roles = set()
        self._created_this_pass = set()
        self._pending_created = set()

    def asset_file(self, asset):
        package=asset.get_path_name().split('.')[0]
        if not package.startswith('/Game/'):
            raise RuntimeError('无法验证工程外资产: '+package)
        return Path(u.Paths.project_content_dir())/(package[6:]+'.uasset')

    def asset_ledger(self):
        return Path(u.Paths.project_saved_dir())/'MMDBridge'/(signature([self.root,self.owner])+'_assets.json')

    def protect_asset(self, asset):
        """Remember bytes after save, outside the asset so the digest is stable."""
        path=self.asset_file(asset);ledger=self.asset_ledger()
        if not path.is_file():raise RuntimeError('资产尚未保存: '+str(path))
        records=read_json(ledger) if ledger.is_file() else {}
        records[asset.get_path_name()]=file_hash(path)
        atomic_json(ledger,records)
        getattr(self,'_pending_created',set()).discard(asset.get_path_name().split('.')[0])

    def checkpoint_interrupted_build(self):
        """Commit only this converter's unfinished new assets before GPU pause."""
        saved=[]
        for path in list(self._pending_created):
            asset=u.load_asset(path)
            if asset is None or u.EditorAssetLibrary.get_metadata_tag(asset,'MMDBridge.Owner')!=self.owner:
                continue
            u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.BuildState','interrupted')
            if not u.EditorAssetLibrary.save_loaded_asset(asset):
                raise RuntimeError('中断材质构建无法保存检查点: '+path)
            self.protect_asset(asset);saved.append(path)
        return saved

    def verify_asset(self, asset):
        if asset.get_outer() in u.EditorLoadingAndSavingUtils.get_dirty_content_packages():
            raise RuntimeError('保留尚未保存的用户编辑，请保存或使用新的输出目录: '+asset.get_path_name())
        path=self.asset_file(asset)
        ledger=self.asset_ledger()
        expected=(read_json(ledger) if ledger.is_file() else {}).get(asset.get_path_name())
        if not expected or not path.is_file() or file_hash(path)!=expected:
            raise RuntimeError('保留用户修改或未登记的资产，请使用新的输出目录: '+asset.get_path_name())

    def managed(self, asset):
        current=u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner")
        if current not in ("", self.owner):
            raise RuntimeError(f"资源归属冲突: {asset.get_path_name()}")
        if current != self.owner:
            u.EditorAssetLibrary.set_metadata_tag(asset, "MMDBridge.Owner", self.owner)

    def create(self, name, kind, factory):
        folder = self.root + ('/Sequences' if kind == getattr(u, 'MoviePipelinePrimaryConfig', None) else '/Materials')
        from .material_contract import asset_name_for_path
        name = asset_name_for_path(folder, name, u.Paths.project_content_dir())
        path = folder + "/" + name
        asset = u.load_asset(path)
        if asset:
            if u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner") != self.owner:
                raise RuntimeError(f"拒绝覆盖未受管理的材质: {path}")
            if path not in self._created_this_pass:
                self.verify_asset(asset)
        else:
            asset = self.tools.create_asset(name, folder, kind, factory)
            if asset is not None:
                self._created_this_pass.add(path)
                self._pending_created.add(path)
        if asset is None:
            raise RuntimeError(f"无法创建资源 {path}")
        self.managed(asset)
        if isinstance(asset,u.Material):
            self._expression_material = asset
            self._expression_cache = {}
        return asset

    def node(self, kind, **properties):
        factory = (self.lib.create_material_expression_in_function if isinstance(self.material,u.MaterialFunction)
                   else self.lib.create_material_expression)
        node = factory(self.material, getattr(u, "MaterialExpression"+kind))
        if node is None:
            raise RuntimeError(f"原生材质转换失败：无法创建材质节点 {kind}")
        for k, v in properties.items():
            prop(node, k, v)
        return node, ""

    def function_input(self, expression, normal=False, data=False):
        if self.function_ports is None:raise RuntimeError('函数输入出现在函数之外')
        key=(expression['id'],bool(normal),bool(data))
        if key not in self.function_ports:
            spec=next(i for i in self._function_definition['inputs'] if i['id']==expression['id'])
            enum={'VALUE':'SCALAR','INT':'SCALAR','BOOLEAN':'SCALAR','RGBA':'VECTOR3','VECTOR':'VECTOR3','SHADER':'SUBSTRATE','TEXTURE2D':'TEXTURE2D'}[spec['type']]
            name='P_'+spec['id']+('_N' if normal else '_D' if data else '_C')
            node=self.node('FunctionInput',input_name=name,
                input_type=getattr(u.FunctionInputType,'FUNCTION_INPUT_'+enum),sort_priority=len(self.function_ports))
            self.function_ports[key]={'node':node,'name':name,'id':spec['id'],'normal':bool(normal),'data':bool(data)}
        return self.function_ports[key]['node']

    def function_call(self, expression, normal=False, data=False):
        identity=expression['function']
        if identity not in self.function_definitions:raise RuntimeError('缺少材质函数定义: '+identity)
        definition=self.function_definitions[identity]
        variant=signature([BACKEND_VERSION,identity,bool(normal),bool(data),getattr(self,'_coverage_mip_bias',None),
                           self.cfg.get('thin_surface_scattering','legacy_diffusion')])
        if variant not in self.functions:
            from .material_contract import asset_name_for_path
            folder=self.root+'/Materials/Functions'
            function_name=asset_name_for_path(folder,'MF_'+variant,u.Paths.project_content_dir())
            path=folder+'/'+function_name
            function=u.load_asset(path)
            ports=[]
            if function:
                self.verify_asset(function)
                if u.EditorAssetLibrary.get_metadata_tag(function,'MMDBridge.FunctionSignature')!=variant:
                    raise RuntimeError('拒绝覆盖未验证或用户修改的函数: '+path)
                ports=json.loads(u.EditorAssetLibrary.get_metadata_tag(function,'MMDBridge.FunctionPorts'))
            else:
                function=self.tools.create_asset(function_name,folder,
                                                u.MaterialFunction,u.MaterialFunctionFactoryNew())
                if function is None:raise RuntimeError('无法创建材质函数: '+path)
                self.managed(function)
                u.EditorAssetLibrary.set_metadata_tag(function,'MMDBridge.BuildState','building')
                saved=(self.material,self._expression_material,self._expression_cache,self.function_ports,
                       getattr(self,'_function_definition',None))
                try:
                    self.material=function;self._expression_material=function;self._expression_cache={}
                    self.function_ports={};self._function_definition=definition
                    body=self.closure(definition['body']) if definition['output_type']=='SHADER' else self.expr(definition['body'],normal=normal,data=data)
                    output=self.node('FunctionOutput',output_name='Result',sort_priority=0)
                    self.connect(body,output,'A')
                    ports=[{k:v for k,v in p.items() if k!='node'} for p in self.function_ports.values()]
                    self.lib.update_material_function(function)
                    self.managed(function)
                    u.EditorAssetLibrary.set_metadata_tag(function,'MMDBridge.FunctionSignature',variant)
                    u.EditorAssetLibrary.set_metadata_tag(function,'MMDBridge.FunctionPorts',json.dumps(ports))
                    u.EditorAssetLibrary.set_metadata_tag(function,'MMDBridge.SourceGroup',definition.get('name',''))
                    u.EditorAssetLibrary.set_metadata_tag(function,'MMDBridge.BuildState','complete')
                    if not u.EditorAssetLibrary.save_loaded_asset(function):raise RuntimeError('函数保存失败: '+path)
                    self.protect_asset(function)
                except Exception:
                    # Delete only this attempt's new, incomplete asset. Previously
                    # saved functions and user edits never enter this branch.
                    u.EditorAssetLibrary.delete_loaded_asset(function)
                    raise
                finally:
                    self.material,self._expression_material,self._expression_cache,self.function_ports,self._function_definition=saved
            self.functions[variant]=(function,ports)
            self.report.setdefault('material_functions',{})[variant]={'path':path,'definition':identity,'ports':ports}
        function,ports=self.functions[variant]
        node=self.node('MaterialFunctionCall',material_function=function)
        for port in ports:
            binding=expression['args'][int(port['id'])]
            if binding.get('op')=='constant' and isinstance(self.material,u.Material):
                name='Preset_'+signature(expression)[:8]+'_'+port['id']
                value=binding['value']
                if isinstance(value,list):
                    source=self.node('VectorParameter',parameter_name=name,default_value=u.LinearColor(*(value+[0,0,0,1])[:3],value[3] if len(value)>3 else 1))
                else:source=self.scalar(name,value)
            else:source=self.expr(binding,normal=port['normal'],data=port['data'])
            self.connect(source,node,port['name'])
        return node[0],'Result'

    def clear_expressions(self, material):
        # Rebuilding the same UMaterial after a recoverable failure must not
        # reuse references to nodes deleted by the preceding attempt.
        self._expression_material = material
        self._expression_cache = {}
        # An open editor can compile each intermediate, disconnected graph.
        editor = u.get_editor_subsystem(u.AssetEditorSubsystem)
        editor.close_all_editors_for_asset(material)
        # UE 5.8 iterates a view while removing expressions. Verify that the
        # complete old graph was removed before constructing the replacement.
        previous = self.lib.get_num_material_expressions(material)
        while previous:
            self.lib.delete_all_material_expressions(material)
            remaining = self.lib.get_num_material_expressions(material)
            if remaining >= previous:
                raise RuntimeError('无法清空原有材质节点: ' + material.get_path_name())
            previous = remaining

    def connect(self, expr, target, input_name):
        names = [str(n) for n in self.lib.get_material_expression_input_names(target[0])]
        if input_name not in names:
            aliases = {"Coordinates":["UVs"], "A":["Base","Min"], "B":["Exp","Max"], "Input":["VectorInput","Vector","Input"],
                       'AGreaterThanB':['A > B'], 'AEqualsB':['A == B'], 'ALessThanB':['A < B'],
                       'MipBias':['Bias'],'MipLevel':['Level'], 'TextureObject':['Tex']}
            canonical = re.sub(r'[^a-z0-9]','',input_name.lower())
            input_name = next((n for n in names if re.sub(r'[^a-z0-9]','',n.lower()) == canonical),
                              next((n for n in aliases.get(input_name,[]) if n in names),
                              names[0] if len(names)==1 else input_name))
        if not self.lib.connect_material_expressions(expr[0], expr[1], target[0], input_name):
            raise RuntimeError(f"原生材质转换失败：材质节点连接失败: {type(expr[0]).__name__} -> {type(target[0]).__name__}.{input_name}; inputs={names}")

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

    def safe_divide(self, a, b):
        # Blender returns zero for a zero divisor. Guard the divisor itself so
        # speculative shader evaluation never introduces Inf/NaN.
        divisor=self.node('If',equals_threshold=0.0)
        for source,name in ((b,'A'),(self.constant(0),'B'),(b,'AGreaterThanB'),
                            (self.constant(1),'AEqualsB'),(b,'ALessThanB')):
            self.connect(source,divisor,name)
        result=self.node('If',equals_threshold=0.0)
        quotient=self.op('Divide',a,divisor)
        for source,name in ((b,'A'),(self.constant(0),'B'),(quotient,'AGreaterThanB'),
                            (self.constant(0),'AEqualsB'),(quotient,'ALessThanB')):
            self.connect(source,result,name)
        return result

    def scalar(self, name, default):
        return self.node("ScalarParameter", parameter_name=name, default_value=float(default))

    def texture(self, image, normal=False, data=False):
        if image.get('_parameter'):
            self._texture_parameter_roles.add((image['_parameter'],bool(normal),bool(data)))
            image=self._parameter_defaults[image['_parameter']]['value']
        role = texture_role(image, normal=normal, data=data)
        coverage = image.get('coverage_mips')
        coverage_suffix = '_Mask_' + signature(coverage)[:8] if coverage else ''
        digest=self.texture_hashes.get(image['path'])
        if digest is None:
            digest=file_hash(image['path']);self.texture_hashes[image['path']]=digest
        key = image["id"] + digest + ("_N" if normal else "_D" if data else "_C") + coverage_suffix
        if key in self.textures:
            return self.textures[key]
        name = image.get("asset_name", "T_" + image["id"]) + ("_N" if normal else "_D" if data else "_C")+'_v2_'+digest[:12] + coverage_suffix
        from .material_contract import asset_name_for_path
        name=asset_name_for_path(self.root+'/Textures',name,u.Paths.project_content_dir())
        path = self.root + "/Textures/" + name
        texture = u.load_asset(path)
        current_owner = u.EditorAssetLibrary.get_metadata_tag(texture, "MMDBridge.Owner") if texture else ""
        if texture and current_owner != self.owner:
            raise RuntimeError(f"拒绝覆盖外部贴图: {path}")
        if texture:self.verify_asset(texture)
        cached = (texture and current_owner == self.owner and
                  u.EditorAssetLibrary.get_metadata_tag(texture, "MMDBridge.SourceHash") == digest)
        if not cached:
            if self.check_resources:
                self.check_resources()
            task = u.AssetImportTask()
            for k, v in dict(filename=image["path"], destination_path=self.root+"/Textures", destination_name=name,
                             automated=True, replace_existing=True, save=True).items():
                prop(task, k, v)
            self.tools.import_asset_tasks([task])
            texture = u.load_asset(path)
            if texture is None:
                # Interchange may choose a different final name. Resolve its
                # actual result, never guess a filename or claim foreign assets.
                actual_paths = list(task.imported_object_paths)
                if len(actual_paths) == 1 and str(actual_paths[0]).startswith(self.root+'/Textures/'):
                    texture = u.load_asset(actual_paths[0])
                    self.report.setdefault('checks',{}).setdefault('texture_import_paths',[]).append(
                        {'requested':path,'actual':str(actual_paths[0]),'source':image['path']})
                if texture is None:
                    raise RuntimeError(f"贴图导入失败 {image['path']}; imported={actual_paths}")
        hdr = image.get('hdr',False) or Path(image['path']).suffix.lower() in ('.exr','.hdr')
        srgb = role == "srgb" and not hdr
        compression = (u.TextureCompressionSettings.TC_HDR if hdr else
                       u.TextureCompressionSettings.TC_NORMALMAP if role == "normal" else
                       u.TextureCompressionSettings.TC_BC7 if role != "srgb" else None)
        changed = not cached or texture.get_editor_property("srgb") != srgb
        if coverage:
            thresholds = [0.,0.,0.,0.]
            thresholds[coverage['channel']] = coverage['threshold']
            thresholds = u.Vector4(*thresholds)
            for key,value in {'do_scale_mips_for_alpha_coverage':True,'alpha_coverage_thresholds':thresholds}.items():
                changed |= texture.get_editor_property(key) != value
                prop(texture,key,value)
        if image.get('lookup'):
            # A curve table is shader data: streaming a coarser mip changes the
            # operator itself, even on a distant object.
            for key,value in dict(never_stream=True,mip_gen_settings=u.TextureMipGenSettings.TMGS_NO_MIPMAPS,
                                  filter=u.TextureFilter.TF_BILINEAR,max_texture_size=0).items():
                changed |= texture.get_editor_property(key)!=value
                prop(texture,key,value)
        if hdr and image.get('generated_bake'):
            changed |= _field_texture_size(texture, self.cfg)
        if compression is not None:
            changed |= texture.get_editor_property("compression_settings") != compression
        if changed:
            prop(texture, "srgb", srgb)
            if compression is not None:
                prop(texture, "compression_settings", compression)
            if role == "normal":
                prop(texture, "flip_green_channel", True)
        self.managed(texture)
        if not cached:
            u.EditorAssetLibrary.set_metadata_tag(texture, "MMDBridge.SourceHash", digest)
        if changed:
            if not u.EditorAssetLibrary.save_loaded_asset(texture):raise RuntimeError('贴图保存失败: '+path)
        self.protect_asset(texture)
        self.textures[key] = texture
        return texture

    def reuse(self, record):
        """Load a completed material checkpoint without rebuilding or recompiling it."""
        from .material_contract import asset_name_for_path
        folder=self.root+'/Materials'
        material_path=folder+'/'+asset_name_for_path(folder,record['asset_name'],u.Paths.project_content_dir())
        instance_path=folder+'/'+asset_name_for_path(folder,'MI_'+record.get('asset_name',record['id']),u.Paths.project_content_dir())
        material = u.load_asset(material_path)
        instance = u.load_asset(instance_path)
        if instance and u.EditorAssetLibrary.get_metadata_tag(instance,'MMDBridge.SharedParent'):
            self.verify_asset(instance)
            material=instance.get_editor_property('parent')
            self.verify_asset(material)
        if self.cfg.get('restore_scene_appearance', False) and cutout_transmission(record):
            record['requires_visual_review'] = True
            if not material or u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.CutoutTransmission') != '1':
                return False
        if record.get("pmx_index") is not None and self.cfg.get("character_backend") == "mmdbridge_fbx":
            _, desired, _ = self.character_master(record)
            if not instance or u.EditorAssetLibrary.get_metadata_tag(instance, "MMDBridge.ParentMaterial") != desired:
                return False
            self.assets[record["id"]] = instance_path
            return True
        master_route = (instance and
                        u.EditorAssetLibrary.get_metadata_tag(instance, "MMDBridge.MaterialRoute") == "user_master")
        if (not material and not master_route) or not instance:
            return False
        if not master_route and u.EditorAssetLibrary.get_metadata_tag(instance,'MMDBridge.BackendVersion') != str(BACKEND_VERSION):
            return False
        if record.get('surface_graph') and u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.SurfaceGraph') != '2':
            return False
        if (record.get('surface_graph') and record.get('thin_surface_transmission') and
                u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.ThinSurfaceScattering') !=
                self.cfg.get('thin_surface_scattering','legacy_diffusion')):
            return False
        if record.get('coverage_source') == 'shader_mix':
            transmission = record['channels'].get('transmission')
            has_transmission = bool(transmission and (transmission.get('op') != 'constant' or
                                      float(transmission.get('value', 0)) > 1e-4))
            glass = record.get('surface') == 'glass' or record.get('water') or (
                has_transmission and not cutout_transmission(record))
            glass=glass or record.get('continuous_opacity',False) or bool(set(record.get('closure_features',[])) & {'transmission','straight_transmission'})
            expected_blends = ((u.BlendMode.BLEND_TRANSLUCENT, u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE)
                               if glass else (u.BlendMode.BLEND_MASKED,))
            if (master_route or u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.ShaderCoverage') != '1' or
                    material.get_editor_property('blend_mode') not in expected_blends):
                return False
        if any(u.EditorAssetLibrary.get_metadata_tag(asset, "MMDBridge.Owner") != self.owner
               for asset in (material, instance)):
            return False
        if (not master_route and material.get_editor_property('blend_mode') in (
                u.BlendMode.BLEND_TRANSLUCENT, u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE) and
                material.get_editor_property('translucency_lighting_mode') !=
                u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING):
            return False
        if (self.cfg.get('restore_scene_appearance',False) and record.get('surface')=='glass' and
                not record.get('water') and u.SystemLibrary.get_console_variable_int_value('r.Substrate')):
            front=self.lib.get_material_property_input_node(material,u.MaterialProperty.MP_FRONT_MATERIAL)
            if (not isinstance(front,u.MaterialExpressionSubstrateSlabBSDF) or
                    material.get_editor_property('blend_mode') != u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE):
                return False
            mode = glass_refraction_mode(record)
            if u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.RefractionMode') != mode:
                return False
            if u.EditorAssetLibrary.get_metadata_tag(material,'MMDBridge.GlassColor') != '3' or not material.get_editor_property('two_sided'):
                return False
            if mode == 'thin_raster':
                record['requires_visual_review'] = True
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
            "base_color": ("basecolor", "albedo", "diffuse", "基础色", "基础颜色", "底色", "漫反射"),
            "normal": ("normal", "法线"), "roughness": ("roughness", "rough", "粗糙度"),
            "metallic": ("metallic", "metalness", "金属度"),
            "opacity": ("opacity", "alpha", "透明度", "不透明度"),
            "emission": ("emission", "emissive", "自发光"),
            "ao": ("ao", "ambientocclusion", "环境光遮蔽", "环境遮蔽"),
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

    @staticmethod
    def _eye_material(record):
        names = [record.get("name", ""), record.get("source_name", "")]
        mmd = record.get("mmd", {})
        names.extend((mmd.get("name_j", ""), mmd.get("name_e", "")))
        text = " ".join(names).lower()
        if re.search(r"eye.?brow|eye.?lash|眉|睫毛|まゆ|まつげ", text):
            return False
        return bool(re.search(r"(^|[^a-z])(eye|eyes|eyeball|iris|pupil)([^a-z]|$)|眼|瞳|虹彩", text))

    def character_master(self, record):
        path = self.cfg.get("eye_master_material" if self._eye_material(record)
                            else "character_master_material")
        master = u.load_asset(path) if path else None
        if isinstance(master, u.MaterialInterface):
            return master, path, 'character_master'
        toon = (hasattr(u, 'MaterialExpressionSubstrateToonBSDF') and
                u.SystemLibrary.get_console_variable_int_value('r.Substrate') == 1 and
                u.SystemLibrary.get_console_variable_int_value('r.Substrate.ProjectGBufferFormat') == 0)
        name = 'M_Character_Toon_Default' if toon else 'M_Character_PBR_Default'
        master_path = self.root + '/Materials/' + name
        master = u.load_asset(master_path)
        if master and u.EditorAssetLibrary.get_metadata_tag(master, 'MMDBridge.Owner') != self.owner:
            raise RuntimeError('默认角色母材质归属冲突: ' + master_path)
        if not master or u.EditorAssetLibrary.get_metadata_tag(master, 'MMDBridge.CharacterDefault') != '1':
            master = self.create(name, u.Material, u.MaterialFactoryNew())
            previous = self.material
            self.material = master
            try:
                self.clear_expressions(master)
                prop(master, 'blend_mode', u.BlendMode.BLEND_MASKED)
                prop(master, 'two_sided', True)
                white = u.load_asset('/Engine/EngineResources/WhiteSquareTexture')
                base = self.node('TextureSampleParameter2D', parameter_name='BaseColor', texture=white)
                tint = self.node('VectorParameter', parameter_name='BaseColorTint', default_value=u.LinearColor(1, 1, 1, 1))
                color = self.op('Multiply', (base[0], 'RGB'), tint)
                rough = self.scalar('Roughness', .65)
                metallic = self.scalar('Metallic', 0)
                specular = self.scalar('Specular', .2)
                normal = self.node('TextureSampleParameter2D', parameter_name='Normal',
                    texture=u.load_asset('/Engine/EngineMaterials/DefaultNormal'),
                    sampler_type=u.MaterialSamplerType.SAMPLERTYPE_NORMAL)
                mask = self.op('Multiply', (base[0], 'A'), self.scalar('Opacity', 1))
                if toon:
                    profile = self.create('TP_Character_Default', u.ToonProfile, u.ToonProfileFactory())
                    u.EditorAssetLibrary.save_loaded_asset(profile)
                    self.protect_asset(profile)
                    shader = self.node('SubstrateToonBSDF', toon_profile=profile)
                    for source, pin in ((color, 'BaseColor'), (rough, 'Roughness'),
                                        (metallic, 'Metallic'), (specular, 'Specular'), (normal, 'Normal')):
                        self.connect(source, shader, pin)
                    self.lib.connect_material_property(shader[0], shader[1], u.MaterialProperty.MP_FRONT_MATERIAL)
                else:
                    for source, pin in ((color, 'MP_BASE_COLOR'), (rough, 'MP_ROUGHNESS'),
                                        (metallic, 'MP_METALLIC'), (specular, 'MP_SPECULAR'), (normal, 'MP_NORMAL')):
                        self.lib.connect_material_property(source[0], source[1], getattr(u.MaterialProperty, pin))
                self.lib.connect_material_property(mask[0], mask[1], u.MaterialProperty.MP_OPACITY_MASK)
                self.lib.set_material_usage(master, u.MaterialUsage.MATUSAGE_SKELETAL_MESH)
                self.lib.recompile_material(master)
                u.EditorAssetLibrary.set_metadata_tag(master, 'MMDBridge.CharacterDefault', '1')
                u.EditorAssetLibrary.set_metadata_tag(master, 'MMDBridge.MaterialRoute', 'official_substrate_toon' if toon else 'native_pbr')
                u.EditorAssetLibrary.save_loaded_asset(master)
                self.protect_asset(master)
            finally:
                self.material = previous
        route = 'official_substrate_toon_fallback' if toon else 'native_pbr_fallback'
        self.report.setdefault('checks', {})['character_default'] = {
            'route': route, 'master': master_path, 'requested_master': path,
            'substrate': u.SystemLibrary.get_console_variable_int_value('r.Substrate'),
            'gbuffer_format': u.SystemLibrary.get_console_variable_int_value('r.Substrate.ProjectGBufferFormat')}
        return master, master_path, route

    def build_character(self, record):
        master, path, route = self.character_master(record)
        texture_names = self._parameter_names("get_texture_parameter_names", master)
        switch_names = self._parameter_names("get_static_switch_parameter_names", master)
        images = {}
        base = record.get("textures", {}).get("base_color") or record.get("channels", {}).get("base_color", {}).get("image")
        if base:
            images["base_color"] = base
        for channel in ("roughness", "metallic", "ao", "normal", "opacity"):
            expr = record.get("channels", {}).get(channel, {})
            if expr.get("op") == "texture" and expr.get("image"):
                images[channel] = expr["image"]
        if "base_color" not in images:
            raise RuntimeError(f"角色材质缺少基础贴图: {record['name']}")
        parameter_map = {}
        for channel in images:
            if (route != 'character_master' and channel == 'opacity' and
                    images[channel]['id'] == images['base_color']['id']):
                # The generated master already samples this texture's alpha.
                continue
            parameter = self._semantic_parameter(texture_names, channel)
            if not parameter:
                if channel == "base_color":
                    raise RuntimeError(f"母材质 {path} 未提供 Albedo/BaseColor 贴图参数")
                self.report["warnings"].append(f"角色材质 {record['name']} 的 {channel} 未匹配母材质参数")
                continue
            parameter_map[channel] = parameter
        setter = getattr(self.lib, "set_material_instance_static_switch_parameter_value", None)
        switches = [self._semantic_parameter(switch_names, channel) for channel in parameter_map]
        if any(switches) and not callable(setter):
            raise RuntimeError(f"UE 无法启用母材质贴图开关: {path}")
        instance = self.create("MI_" + record.get("asset_name", record["id"]), u.MaterialInstanceConstant,
                               u.MaterialInstanceConstantFactoryNew())
        self.lib.set_material_instance_parent(instance, master)
        for channel, parameter in parameter_map.items():
            tex = self.texture(images[channel], normal=channel == "normal",
                               data=channel in ("roughness", "metallic", "ao", "opacity"))
            self.lib.set_material_instance_texture_parameter_value(instance, u.Name(parameter), tex)
        for switch in filter(None, switches):
            setter(instance, u.Name(switch), True)
        if route != 'character_master':
            diffuse = record.get('mmd', {}).get('diffuse_color', [1, 1, 1])
            self.lib.set_material_instance_vector_parameter_value(instance, 'BaseColorTint', u.LinearColor(*diffuse[:3], 1))
            opacity = record.get('channels', {}).get('opacity', {})
            if opacity.get('op') == 'constant':
                self.lib.set_material_instance_scalar_parameter_value(instance, 'Opacity', float(opacity['value']))
        self.lib.update_material_instance(instance)
        self.managed(instance)
        u.EditorAssetLibrary.set_metadata_tag(instance, "MMDBridge.MaterialRoute", route)
        u.EditorAssetLibrary.set_metadata_tag(instance, "MMDBridge.ParentMaterial", path)
        u.EditorAssetLibrary.save_loaded_asset(instance)
        self.protect_asset(instance)
        self.assets[record["id"]] = instance.get_path_name()
        self.report.setdefault("material_routes", {})[record["id"]] = {
            "route": route, "parent": path, "assigned_parameters": parameter_map}
        return instance

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
        instance = self.create("MI_" + record.get("asset_name", record["id"]), u.MaterialInstanceConstant,
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
        self.protect_asset(instance)
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
        self.check_conversion_budget()
        # Shared Blender subgraphs should remain shared UE expressions. In
        # particular foliage albedo is consumed by both diffuse and SSS.
        if getattr(self, '_expression_material', None) != self.material:
            self._expression_material = self.material
            self._expression_cache = {}
        key = (normal, data, getattr(self, '_coverage_mip_bias', None),
               json.dumps(expr,sort_keys=True,separators=(',',':')))
        if key not in self._expression_cache:
            self._expression_cache[key] = self.expression(expr,normal=normal,data=data)
        return self._expression_cache[key]

    def coverage_expr(self, expression):
        """Separate opacity sampling from color/roughness, including shared graphs."""
        from .ue_render_policy import foliage_distance_cvars
        scale = self.cfg.get('foliage_distance_scale', 2.)
        foliage_distance_cvars(scale)  # Validate the same range as viewport/MRQ.
        # Scale only a direct scalar mask's mip chain at its actual cutoff. A
        # nonlinear composite mask needs separate coverage validation. Keep a
        # distinct resource so albedo and roughness sharing it remain unchanged.
        image = expression.get('image',{})
        if expression.get('op') == 'texture' and image and not image.get('_parameter') and expression.get('output') in ('Alpha','Value'):
            getter = getattr(self.material,'get_editor_property',None)
            threshold = float(getter('opacity_mask_clip_value')) if getter else 1/3
            expression = {**expression,'image':{**image,'coverage_mips':{
                'channel':3 if expression['output'] == 'Alpha' else 0,'threshold':threshold}}}
        previous = getattr(self, '_coverage_mip_bias', None)
        self._coverage_mip_bias = -math.log2(scale)
        try:
            return self.expr(expression, data=True)
        finally:
            self._coverage_mip_bias = previous

    def preserve_mask_distance(self, material, record):
        """Upgrade an existing owned scene material without changing its color graph."""
        if (not material.get_path_name().startswith(self.root + '/') or
                u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.Owner') != self.owner):
            return None
        scale = self.cfg.get('foliage_distance_scale', 2.)
        version = '1:' + str(float(scale))
        if u.EditorAssetLibrary.get_metadata_tag(material, 'MMDBridge.MaskDistance') == version:
            return {'path':material.get_path_name(), 'changed':False, 'mip_bias':-math.log2(scale)}
        self.verify_asset(material)
        previous = self.material
        self.material = material
        try:
            mask = self.coverage_expr(record['channels']['opacity'])
            if not self.lib.connect_material_property(mask[0], mask[1], u.MaterialProperty.MP_OPACITY_MASK):
                raise RuntimeError('无法连接距离保留遮罩: ' + material.get_path_name())
            self.lib.delete_unused_expressions(material)
            errors = self.lib.recompile_material(material)
            if errors:
                raise RuntimeError('距离保留遮罩编译失败: ' + '; '.join(str(e) for e in errors))
            u.EditorAssetLibrary.set_metadata_tag(material, 'MMDBridge.MaskDistance', version)
            if not u.EditorAssetLibrary.save_loaded_asset(material):
                raise RuntimeError('无法保存距离保留遮罩: ' + material.get_path_name())
            self.protect_asset(material)
            return {'path':material.get_path_name(), 'changed':True, 'mip_bias':-math.log2(scale)}
        finally:
            self.material = previous

    def expression(self, expr, normal=False, data=False):
        if expr['op'] == 'backfacing':
            return self.op('Multiply',self.op('OneMinus',self.node('TwoSidedSign')),self.constant(.5))
        if expr['op'] == 'checker':
            # Cycles checker uses all three axes, including its boundary epsilon.
            p = self.op('Multiply', self.expr(expr['vector'], data=True), self.expr(expr['scale'], data=True))
            p = self.op('Multiply', self.op('Add', p, self.constant(.000001)), self.constant(.999999))
            integer = self.op('Abs', self.op('Floor', p))
            axes = []
            for index in range(3):
                part = self.node('ComponentMask', r=index == 0, g=index == 1, b=index == 2)
                self.connect(integer, part, 'Input'); axes.append(part)
            parity = self.op('Multiply', self.op('Frac', self.op('Multiply',
                self.op('Add', self.op('Add', axes[0], axes[1]), axes[2]), self.constant(.5))), self.constant(2))
            if expr.get('antialias'):
                width=self.op('Add',self.op('Abs',self.op('DDX',p)),self.op('Abs',self.op('DDY',p)))
                signs=[]
                def integral(v):
                    return self.op('OneMinus',self.op('Abs',self.op('Subtract',self.constant(1),
                        self.op('Multiply',self.constant(2),self.op('Frac',self.op('Multiply',v,self.constant(.5)))))))
                for axis in range(3):
                    parts=[]
                    for value in (p,width):
                        mask=self.node('ComponentMask',r=axis==0,g=axis==1,b=axis==2);self.connect(value,mask,'Input');parts.append(mask)
                    position,footprint=parts
                    half=self.op('Multiply',footprint,self.constant(.5))
                    average=self.op('Divide',self.op('Subtract',integral(self.op('Add',position,half)),integral(self.op('Subtract',position,half))),
                                    self.op('Max',footprint,self.constant(1e-5)))
                    exact=self.op('Subtract',self.constant(1),self.op('Multiply',self.constant(2),
                        self.op('Frac',self.op('Multiply',axes[axis],self.constant(.5)))))
                    choose=self.node('If',equals_threshold=0.)
                    for value,name in ((footprint,'A'),(self.constant(1e-5),'B'),(average,'AGreaterThanB'),(exact,'AEqualsB'),(exact,'ALessThanB')):
                        self.connect(value,choose,name)
                    signs.append(choose)
                parity=self.op('Saturate',self.op('Subtract',self.constant(.5),self.op('Multiply',self.constant(.5),
                    self.op('Multiply',self.op('Multiply',signs[0],signs[1]),signs[2]))))
            if expr['output'] == 'Fac':
                return parity
            result = self.node('LinearInterpolate')
            for source, name in ((self.expr(expr['b'], data=data), 'A'),
                                 (self.expr(expr['a'], data=data), 'B'), (parity, 'Alpha')):
                self.connect(source, result, name)
            return result
        kind = expr["op"]
        sub = lambda e: self.expr(e, data=data)
        if kind == "constant":
            return self.constant(expr["value"])
        if kind == 'parameter':
            value=self._parameter_defaults[expr['name']]['value']
            if expr['type']=='scalar':return self.scalar(expr['name'],value)
            node=self.node('VectorParameter',parameter_name=expr['name'],default_value=u.LinearColor(*(value+[0,0,0])[:3],value[3] if len(value)>3 else 1))
            if expr.get('size')==2:
                mask=self.node('ComponentMask',r=True,g=True);self.connect(node,mask,'Input');return mask
            return node
        if kind == 'function_input':return self.function_input(expr,normal,data)
        if kind == 'function_call':return self.function_call(expr,normal,data)
        if kind=='texture_object':
            texture=self.texture(expr['image'],normal,data)
            name=expr['image'].get('_parameter','Texture_'+expr['image']['id'] if 'id' in expr['image'] else 'Texture')
            name+=('_N' if normal else '_D' if data else '_C')
            return self.node('TextureObjectParameter',parameter_name=name,texture=texture) if isinstance(self.material,u.Material) else self.node('TextureObject',texture=texture)
        if kind == 'mesh_scalar':
            uv=self.node('TextureCoordinate',coordinate_index=expr['uv_index'])
            channel=expr.get('component',0)
            mask=self.node('ComponentMask',r=channel==0,g=channel==1);self.connect(uv,mask,'Input')
            return self.op('Subtract',self.constant(1),mask) if channel==1 else mask
        if kind == 'object_random':
            default=self.node('ScalarParameter',parameter_name='SourceObjectRandom',default_value=0.,
                use_custom_primitive_data=True,primitive_data_index=expr['primitive_index'])
            value=self.node('PerInstanceCustomData',data_index=expr['instance_index'],const_default_value=0.)
            self.connect(default,value,'DefaultValue')
            interpolated=self.node('VertexInterpolator');self.connect(value,interpolated,'Input')
            return interpolated
        if kind == 'vertex_color':
            raw=self.node('VertexColor')
            if expr.get('output')=='Alpha':return raw[0],'A'
            parts=[]
            # UE 5.8's USD translator stores linear displayColor as sRGB vertex
            # bytes. Decode the RGB once; alpha is a linear scalar throughout.
            for channel in ('R','G','B'):
                value=(raw[0],channel);result=self.node('If',equals_threshold=0.)
                high=self.op('Power',self.op('Divide',self.op('Add',value,self.constant(.055)),self.constant(1.055)),self.constant(2.4))
                low=self.op('Divide',value,self.constant(12.92))
                for v,n in ((value,'A'),(self.constant(.04045),'B'),(high,'AGreaterThanB'),(low,'AEqualsB'),(low,'ALessThanB')):self.connect(v,result,n)
                parts.append(result)
            return self.op('AppendVector',self.op('AppendVector',parts[0],parts[1]),parts[2])
        if kind in ('lookup','rgb_curve'):
            texture=self.texture(expr['image'])
            width=expr['image']['width']
            def sample(value,channel):
                x=self.op('Add',self.op('Multiply',self.op('Saturate',value),self.constant((width-1)/width)),self.constant(.5/width))
                uv=self.op('AppendVector',x,self.constant(.5))
                node=self.node('TextureSample',texture=texture,sampler_type=u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR,
                    sampler_source=u.SamplerSourceMode.SSM_CLAMP_WORLD_GROUP_SETTINGS,
                    mip_value_mode=u.TextureMipValueMode.TMVM_MIP_LEVEL)
                self.connect(self.constant(0),node,'MipLevel');self.connect(uv,node,'Coordinates')
                return node[0],channel
            if kind=='lookup':return sample(self.expr(expr['factor'],data=True),'A' if expr.get('alpha') else 'RGB')
            original=self.expr(expr['color'],data=False)
            def component(vector,index):
                node=self.node('ComponentMask',r=index==0,g=index==1,b=index==2)
                self.connect(vector,node,'Input');return node
            def curve(value,index):
                result=sample(value,('R','G','B','A')[index])
                low=self.op('Add',self.constant(expr['endpoints'][0][index]),self.op('Multiply',value,self.constant(-expr['slopes'][0][index]*1000)))
                high=self.op('Add',self.constant(expr['endpoints'][1][index]),self.op('Multiply',self.op('Subtract',value,self.constant(1)),self.constant(expr['slopes'][1][index]*1000)))
                a=self.node('If',equals_threshold=0.)
                for v,n in ((value,'A'),(self.constant(0),'B'),(result,'AGreaterThanB'),(result,'AEqualsB'),(low,'ALessThanB')):self.connect(v,a,n)
                b=self.node('If',equals_threshold=0.)
                for v,n in ((value,'A'),(self.constant(1),'B'),(high,'AGreaterThanB'),(a,'AEqualsB'),(a,'ALessThanB')):self.connect(v,b,n)
                return b
            scale=[1/(white-black) if white!=black else 0 for black,white in zip(expr['black'],expr['white'])]
            # Cycles' curve table does not apply CurveMapping black/white levels.
            # Keep legacy records' behavior, while explicit new records match the
            # source renderer verified by the native curve pixel regression.
            adjusted=(self.op('Multiply',self.op('Subtract',original,self.constant(expr['black'])),self.constant(scale))
                      if expr.get('levels_applied',True) else original)
            parts=[curve(component(adjusted,i),i) if expr.get('precomposed') else
                   curve(curve(component(adjusted,i),3),i) for i in range(3)]
            color=self.op('AppendVector',self.op('AppendVector',parts[0],parts[1]),parts[2])
            result=self.node('LinearInterpolate')
            for v,n in ((original,'A'),(color,'B'),(self.expr(expr['factor'],data=True),'Alpha')):self.connect(v,result,n)
            return result
        if kind == 'to_scalar':
            return self.op('DotProduct',self.expr(expr['value'],data=False),self.constant(expr['weights']))
        if kind in ('hsv', 'brightness_contrast', 'gamma', 'separate', 'combine'):
            # Color operations always consume linear RGB, even when their output
            # feeds a scalar/data channel. Keep the source image's color space.
            def color(e):
                return self.expr(e, data=False)
            def component(value, index):
                mask = self.node('ComponentMask', r=index==0, g=index==1, b=index==2)
                self.connect(value,mask,'Input')
                return mask
            def combine(parts):
                return self.op('AppendVector',self.op('AppendVector',parts[0],parts[1]),parts[2])
            if kind == 'combine':
                result = combine([color(e) for e in expr['components']])
                return self.op('HsvToRgb',result) if expr['mode']=='HSV' else result
            original = color(expr['color'])
            if kind == 'separate':
                return component(self.op('RgbToHsv',original) if expr['mode']=='HSV' else original,expr['index'])
            if kind == 'hsv':
                hsv = self.op('RgbToHsv',original)
                h = self.op('Frac',self.op('Add',component(hsv,0),
                    self.op('Add',color(expr['hue']),self.constant(.5))))
                s = self.op('Saturate',self.op('Multiply',component(hsv,1),color(expr['saturation'])))
                v = self.op('Multiply',component(hsv,2),color(expr['value']))
                adjusted = self.op('HsvToRgb',combine([h,s,v]))
                result = self.node('LinearInterpolate')
                for source,name in ((original,'A'),(adjusted,'B'),(color(expr['factor']),'Alpha')):
                    self.connect(source,result,name)
                return self.op('Max',result,self.constant(0))
            if kind == 'brightness_contrast':
                contrast = color(expr['contrast'])
                scale = self.op('Add',contrast,self.constant(1))
                offset = self.op('Subtract',color(expr['brightness']),self.op('Multiply',contrast,self.constant(.5)))
                return self.op('Max',self.op('Add',self.op('Multiply',original,scale),offset),self.constant(0))
            # Blender gamma leaves nonpositive components unchanged and returns
            # white for gamma=0; UE Power clamps its base, so guard explicitly.
            gamma = color(expr['gamma'])
            parts=[]
            for index in range(3):
                c=component(original,index)
                positive=self.node('If',equals_threshold=0.0)
                for source,name in ((c,'A'),(self.constant(0),'B'),
                        (self.op('Power',self.op('Max',c,self.constant(1e-8)),gamma),'AGreaterThanB'),
                        (c,'AEqualsB'),(c,'ALessThanB')):
                    self.connect(source,positive,name)
                parts.append(positive)
            result=self.node('If')
            for source,name in ((gamma,'A'),(self.constant(0),'B'),
                    (combine(parts),'AGreaterThanB'),(self.constant([1,1,1]),'AEqualsB'),
                    (combine(parts),'ALessThanB')):
                self.connect(source,result,name)
            prop(result[0],'equals_threshold',0.0)
            return result
        if kind == 'fresnel':
            ior = self.expr(expr['ior'],data=True)
            ratio = self.op('Divide',self.op('Subtract',ior,self.constant(1)),self.op('Add',ior,self.constant(1)))
            result = self.node('Fresnel')
            self.connect(self.op('Multiply',ratio,ratio),result,'BaseReflectFractionIn')
            if 'normal' in expr:
                transform = self.node('Transform',transform_source_type=u.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_TANGENT,
                                      transform_type=u.MaterialVectorCoordTransform.TRANSFORM_WORLD)
                self.connect(self.expr(expr['normal'],normal=True),transform,'Input')
                self.connect(transform,result,'Normal')
            else:
                self.connect(self.node('VertexNormalWS'),result,'Normal')
            return result
        if kind == "uv":
            return self.node("TextureCoordinate", coordinate_index=expr.get("index", 0))
        if kind == 'blender_uv':
            uv=sub(expr['uv'])
            converted=self.op('Add', self.op('Multiply',uv,self.constant([1,-1])),self.constant([0,1]))
            return self.op('AppendVector',converted,self.constant(0))
        if kind == 'mapping_3d':
            def component(vector,index):
                result=self.node('ComponentMask',r=index==0,g=index==1,b=index==2)
                self.connect(vector,result,'Input');return result
            def combine(parts):return self.op('AppendVector',self.op('AppendVector',parts[0],parts[1]),parts[2])
            def divide(a,b):return combine([self.safe_divide(component(a,i),component(b,i)) for i in range(3)])
            vector,scale,rotation=sub(expr['uv']),sub(expr['scale']),sub(expr['rotation'])
            mode=expr['vector_type']
            if mode=='TEXTURE':vector=self.op('Subtract',vector,sub(expr['offset']))
            elif mode=='NORMAL':vector=divide(vector,scale)
            else:vector=self.op('Multiply',vector,scale)
            # Blender Euler XYZ means Rz * Ry * Rx. Texture uses the inverse,
            # applying inverse Z, Y, X in that order, not negated XYZ order.
            for axis in ((2,1,0) if mode=='TEXTURE' else (0,1,2)):
                angle=component(rotation,axis)
                if mode=='TEXTURE':angle=self.op('Multiply',angle,self.constant(-1))
                cosine=self.op('Cosine',angle);sine=self.op('Sine',angle)
                prop(cosine[0],'period',math.tau);prop(sine[0],'period',math.tau)
                parts=[component(vector,i) for i in range(3)]
                a,b=((1,2),(2,0),(0,1))[axis]
                x,y=parts[a],parts[b]
                parts[a]=self.op('Subtract',self.op('Multiply',x,cosine),self.op('Multiply',y,sine))
                parts[b]=self.op('Add',self.op('Multiply',x,sine),self.op('Multiply',y,cosine))
                vector=combine(parts)
            if mode=='TEXTURE':return divide(vector,scale)
            if mode=='POINT':return self.op('Add',vector,sub(expr['offset']))
            if mode=='NORMAL':
                length=self.op('SquareRoot',self.op('DotProduct',vector,vector))
                return combine([self.safe_divide(component(vector,i),length) for i in range(3)])
            return vector
        if kind == "mapping":
            # UV enters Blender Mapping as (u, v, 0). Use the qualified XYZ
            # implementation so editable/linked rotation keeps all three axes.
            converted={**expr,'op':'mapping_3d','uv':{'op':'blender_uv','uv':expr['uv']},
                       'rotation':expr.get('rotation',{'op':'constant','value':[0,0,0]}),
                       'vector_type':expr.get('vector_type','POINT')}
            mapped=self.expr(converted,data=data)
            xy=self.node('ComponentMask',r=True,g=True);self.connect(mapped,xy,'Input')
            return self.op('Add',self.op('Multiply',xy,self.constant([1,-1])),self.constant([0,1]))
        if kind == "texture":
            if expr.get('texture'):
                obj=self.expr(expr['texture'],normal=normal or expr.get('normal_map',False),data=data)
                sampler=(u.MaterialSamplerType.SAMPLERTYPE_NORMAL if normal or expr.get('normal_map') else
                    u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR if data or expr.get('hdr') or expr.get('color_space')=='Non-Color' else u.MaterialSamplerType.SAMPLERTYPE_COLOR)
                node=self.node('TextureSample',sampler_type=sampler);self.connect(obj,node,'TextureObject')
                tex=None
            else:tex = self.texture(expr["image"], normal or expr.get("normal_map", False), data)
            properties={}
            if expr.get('image',{}).get('_parameter'):
                properties['parameter_name']=expr['image']['_parameter']+('_N' if normal or expr.get('normal_map') else '_D' if data else '_C')
            if tex:node = self.node("TextureSampleParameter2D" if properties else "TextureSample", **properties,texture=tex, sampler_type=u.MaterialSamplerType.SAMPLERTYPE_NORMAL if normal or expr.get("normal_map") else
                             u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR if data else
                             u.MaterialSamplerType.SAMPLERTYPE_COLOR if tex.get_editor_property("srgb") else u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
            bias = getattr(self, '_coverage_mip_bias', None)
            # Spatial EXR fields retain their original sampling. Only raster
            # mask images need extra detail; their texture mip chain stays enabled.
            image = expr.get('image',{'hdr':expr.get('hdr'), 'path':'.exr' if expr.get('hdr') else '.png'})
            if image.get('_parameter'):image=self._parameter_defaults[image['_parameter']]['value']
            if (bias is not None and not normal and not expr.get('normal_map') and
                    not image.get('hdr') and Path(image['path']).suffix.lower() not in ('.exr','.hdr')):
                prop(node[0], 'mip_value_mode', u.TextureMipValueMode.TMVM_MIP_BIAS)
                self.connect(self.constant(bias), node, 'MipBias')
            uv = self.expr(expr.get("uv", {"op":"uv"}))
            # Blender's Vector socket is float3; UE's texture coordinates are
            # float2, including vectors produced by Combine XYZ or baked fields.
            mask = self.node("ComponentMask", r=True, g=True)
            self.connect(uv, mask, "Input")
            uv = mask
            def blender_vector(e):
                if isinstance(e,list):return any(blender_vector(item) for item in e if isinstance(item,(list,dict)))
                if e.get('op') in ('mapping_3d','blender_uv'):return True
                if e.get('op')=='texture':return False
                return any(blender_vector(v) for v in e.values() if isinstance(v,list) or isinstance(v,dict) and 'op' in v)
            if expr.get('coordinate_space')=='blender' or blender_vector(expr.get('uv',{})):
                uv=self.op('Add',self.op('Multiply',uv,self.constant([1,-1])),self.constant([0,1]))
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
            return node[0], "A" if expr.get("output") == "Alpha" else "R" if data or expr.get('output') == 'Value' else "RGB"
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
            mapping = {"ABSOLUTE": "Abs", "FLOOR": "Floor", "CEIL": "Ceil", "FRACT": "Frac", "SINE": "Sine", "COSINE": "Cosine", 'SQRT':'SquareRoot','SIGN':'Sign'}
            a = sub(expr['a'])
            mode = expr['mode']
            if mode == 'ROUND':
                result = self.op('Floor', self.op('Add',a,self.constant(.5)))
            elif mode in ('RADIANS','DEGREES'):
                result = self.op('Multiply',a,self.constant(math.pi/180 if mode=='RADIANS' else 180/math.pi))
            else:
                result = self.op(mapping[mode],self.op('Max',a,self.constant(0)) if mode=='SQRT' else a)
            if expr["mode"] in ("SINE", "COSINE"):
                prop(result[0], "period", math.tau)
            return self.op("Saturate", result) if expr.get("clamp") else result
        if kind == "math":
            mapping = {"MULTIPLY":"Multiply", "ADD":"Add", "SUBTRACT":"Subtract", "DIVIDE":"Divide",
                       "POWER":"Power", "MINIMUM":"Min", "MAXIMUM":"Max"}
            a,b=sub(expr['a']),sub(expr['b'])
            if expr['mode'] in ('LESS_THAN','GREATER_THAN'):
                result=self.node('If',equals_threshold=0.0)
                for source,name in ((a,'A'),(b,'B'),(self.constant(int(expr['mode']=='GREATER_THAN')),'AGreaterThanB'),
                        (self.constant(0),'AEqualsB'),(self.constant(int(expr['mode']=='LESS_THAN')),'ALessThanB')):
                    self.connect(source,result,name)
            elif expr['mode']=='DIVIDE':
                result=self.safe_divide(a,b)
            else:
                result = self.op(mapping[expr["mode"]], a,b)
            return self.op("Saturate", result) if expr.get("clamp") else result
        if kind == 'clamp':
            low,high=sub(expr['min']),sub(expr['max'])
            if expr.get('sort'): low,high=self.op('Min',low,high),self.op('Max',low,high)
            return self.op('Min',self.op('Max',sub(expr['value']),low),high)
        if kind == 'map_range':
            low,high=sub(expr['to_min']),sub(expr['to_max'])
            fac=self.safe_divide(self.op('Subtract',sub(expr['value']),sub(expr['from_min'])),
                self.op('Subtract',sub(expr['from_max']),sub(expr['from_min'])))
            mode=expr['mode']
            if mode in ('SMOOTHSTEP','SMOOTHERSTEP'):
                fac=self.op('Saturate',fac)
                square=self.op('Multiply',fac,fac)
                if mode=='SMOOTHSTEP':
                    fac=self.op('Multiply',square,self.op('Subtract',self.constant(3),self.op('Multiply',fac,self.constant(2))))
                else:
                    fac=self.op('Multiply',self.op('Multiply',square,fac),
                        self.op('Add',self.op('Multiply',fac,self.op('Subtract',self.op('Multiply',fac,self.constant(6)),self.constant(15))),self.constant(10)))
            elif mode=='STEPPED':
                steps=sub(expr['steps'])
                fac=self.safe_divide(self.op('Floor',self.op('Multiply',fac,self.op('Add',steps,self.constant(1)))),steps)
            result=self.op('Add',low,self.op('Multiply',fac,self.op('Subtract',high,low)))
            return self.op('Min',self.op('Max',result,self.op('Min',low,high)),self.op('Max',low,high)) if expr.get('clamp') else result
        if kind == "mix":
            a, b, fac = sub(expr["a"]), sub(expr["b"]), sub(expr["factor"])
            if expr.get('clamp_factor'):fac=self.op('Saturate',fac)
            if expr['mode'] in ('HUE','SATURATION','VALUE','COLOR'):
                a,b=self.expr(expr['a'],data=False),self.expr(expr['b'],data=False)
                hsv_a,hsv_b=self.op('RgbToHsv',a),self.op('RgbToHsv',b)
                def part(source,index):
                    mask=self.node('ComponentMask',r=index==0,g=index==1,b=index==2)
                    self.connect(source,mask,'Input');return mask
                def lerp(x,y):
                    result=self.node('LinearInterpolate')
                    for source,name in ((x,'A'),(y,'B'),(fac,'Alpha')):self.connect(source,result,name)
                    return result
                components=[part(hsv_a,i) for i in range(3)]
                mode=expr['mode']
                if mode in ('HUE','COLOR'):components[0]=part(hsv_b,0)
                if mode=='COLOR':components[1]=part(hsv_b,1)
                if mode=='SATURATION':components[1]=lerp(components[1],part(hsv_b,1))
                if mode=='VALUE':components[2]=lerp(components[2],part(hsv_b,2))
                adjusted=self.op('HsvToRgb',self.op('AppendVector',
                    self.op('AppendVector',components[0],components[1]),components[2]))
                if mode!='VALUE':
                    gate=self.node('If',equals_threshold=0.0)
                    saturation=part(hsv_a if mode=='SATURATION' else hsv_b,1)
                    for source,name in ((saturation,'A'),(self.constant(0),'B'),(adjusted,'AGreaterThanB'),
                                        (a,'AEqualsB'),(adjusted,'ALessThanB')):self.connect(source,gate,name)
                    adjusted=gate
                result=lerp(a,adjusted) if mode in ('HUE','COLOR') else adjusted
                return self.op('Saturate',result) if expr.get('clamp') else result
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
        raise RuntimeError("原生材质转换失败：未知材质表达式: " + kind)

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

    def thin_transmission(self, slab, color):
        """Thin BSDF compatibility: keep coverage separate from back-light scattering.

        The helper and slab use the same engine default thickness, as UE's legacy
        TwoSidedFoliage conversion does. Color is transmittance, never a cm distance.
        legacy_diffusion is an explicit comparison/fallback, not an equivalent BSDF.
        """
        mode = self.cfg.get('thin_surface_scattering','legacy_diffusion')
        if mode == 'legacy_diffusion':
            prop(slab[0],'sub_surface_type',u.MaterialSubSurfaceType.MSS_DIFFUSION)
            self.connect(color,slab,'SSS MFP')
        elif mode == 'two_sided_wrap':
            prop(slab[0],'sub_surface_type',u.MaterialSubSurfaceType.MSS_TWO_SIDED_WRAP)
            mfp = self.node('SubstrateTransmittanceToMFP')
            self.connect(self.op('Saturate',color),mfp,'Transmittance Color')
            self.connect(mfp,slab,'SSS MFP')
        else:
            raise ValueError('无效的薄表面散射模式: '+str(mode))

    def closure(self, graph):
        self.check_conversion_budget()
        kind = graph['kind']
        if kind=='function_call':return self.function_call(graph)
        if kind in ('mix','add'):
            node = self.node('SubstrateHorizontalMixing' if kind == 'mix' else 'SubstrateAdd',use_parameter_blending=False)
            self.connect(self.closure(graph['a']),node,'Background' if kind == 'mix' else 'A')
            self.connect(self.closure(graph['b']),node,'Foreground' if kind == 'mix' else 'B')
            if kind == 'mix':
                self.connect(self.op('Saturate',self.expr(graph['factor'],data=True)),node,'Mix')
            return node
        channels = graph['channels']
        if kind == 'bsdf_transparent':
            # Unlit BSDF cannot mix with lit closures; zero Fresnel keeps straight transmission.
            slab = self.node('SubstrateSlabBSDF',sub_surface_type=u.MaterialSubSurfaceType.MSS_SIMPLE_VOLUME)
            for port in ('Diffuse Albedo','F0','F90','Roughness'):
                self.connect(self.constant(0),slab,port)
            mfp = self.node('SubstrateTransmittanceToMFP')
            self.connect(self.op('Saturate',self.expr(channels['base_color'])),mfp,'TransmittanceColor')
            self.connect(mfp,slab,'SSS MFP')
            return slab
        from .material_contract import active_field
        has_feature = lambda key:key in graph['features'] if 'features' in graph else active_field(channels.get(key))
        field = lambda key,default:self.expr(channels.get(key,{'op':'constant','value':default}),data=key not in ('base_color','specular_tint','coat_tint','emission'))
        slab = self.node('SubstrateSlabBSDF')
        base = field('base_color',[0,0,0])
        if kind == 'bsdf_principled':
            metal = field('metallic',0)
            diffuse = self.op('Multiply',base,self.op('OneMinus',metal))
            ior = field('ior',1.5)
            ratio = self.op('Divide',self.op('Subtract',ior,self.constant(1)),self.op('Add',ior,self.constant(1)))
            dielectric = self.op('Multiply',self.op('Multiply',ratio,ratio),self.op('Multiply',field('specular',.5),self.constant(2)))
            dielectric = self.op('Multiply',dielectric,field('specular_tint',[1,1,1]))
            f0 = self.node('LinearInterpolate')
            for source,name in ((dielectric,'A'),(base,'B'),(metal,'Alpha')):
                self.connect(source,f0,name)
            if has_feature('transmission'):
                diffuse = self.op('Multiply', diffuse, self.op('OneMinus', field('transmission',0)))
                prop(slab[0], 'sub_surface_type', u.MaterialSubSurfaceType.MSS_SIMPLE_VOLUME)
                mfp = self.node('SubstrateTransmittanceToMFP')
                transmitted = self.op('Multiply', self.op('Saturate', base), self.op('Saturate', field('transmission',0)))
                self.connect(transmitted, mfp, 'TransmittanceColor')
                self.connect(mfp, slab, 'SSS MFP')
            elif has_feature('subsurface'):
                prop(slab[0], 'sub_surface_type', u.MaterialSubSurfaceType.MSS_DIFFUSION)
                radius = self.op('Multiply', field('subsurface_radius',[1,.2,.1]),
                    self.op('Multiply', field('subsurface_scale',.05), self.constant(100)))
                self.connect(radius, slab, 'SSS MFP')
                # Weight is a closure mix, not an alteration of the scattering radius.
                opaque_channels = {**channels, 'subsurface': {'op':'constant','value':0}, 'coat': {'op':'constant','value':0}}
                opaque = self.closure({'kind':kind,'channels':opaque_channels,'features':[]})
                scattering_mix = self.node('SubstrateHorizontalMixing', use_parameter_blending=False)
                self.connect(opaque, scattering_mix, 'Background')
                self.connect(slab, scattering_mix, 'Foreground')
                self.connect(self.op('Saturate',field('subsurface',0)), scattering_mix, 'Mix')
        elif kind in ('bsdf_glass','bsdf_refraction'):
            diffuse = self.constant(0)
            ior = field('ior',1.5)
            ratio = self.op('Divide',self.op('Subtract',ior,self.constant(1)),self.op('Add',ior,self.constant(1)))
            f0 = self.op('Multiply', self.op('Multiply',ratio,ratio), base)
            self.connect(base, slab, 'F90')
            prop(slab[0], 'sub_surface_type', u.MaterialSubSurfaceType.MSS_SIMPLE_VOLUME)
            mfp = self.node('SubstrateTransmittanceToMFP')
            self.connect(self.op('Saturate',base), mfp, 'TransmittanceColor')
            self.connect(mfp, slab, 'SSS MFP')
        elif kind in ('bsdf_glossy','bsdf_metallic'):
            diffuse,f0 = self.constant(0),base
            if kind == 'bsdf_glossy':
                self.connect(base,slab,'F90')
        elif kind in ('bsdf_diffuse','bsdf_translucent'):
            diffuse,f0 = base,self.constant(0)
            self.connect(self.constant(0),slab,'F90')
            if kind=='bsdf_translucent':
                self.thin_transmission(slab,base)
        elif kind == 'emission':
            diffuse,f0 = self.constant(0),self.constant(0)
            self.connect(self.constant(0),slab,'F90')
        else:
            raise RuntimeError('原生材质转换失败：不支持的表面闭包：'+kind)
        self.connect(diffuse,slab,'Diffuse Albedo')
        self.connect(f0,slab,'F0')
        self.connect(field('roughness',.5),slab,'Roughness')
        if 'normal' in channels:
            self.connect(self.expr(channels['normal'],normal=True),slab,'Normal')
        if 'emission' in channels:
            from .scene_appearance import RADIANCE_TO_UE
            emissive = self.op('Multiply',field('emission',[0,0,0]),field('emission_strength',1))
            self.connect(self.op('Multiply',emissive,self.constant(RADIANCE_TO_UE)),slab,'Emissive Color')
        result = scattering_mix if kind == 'bsdf_principled' and 'scattering_mix' in locals() else slab
        if kind == 'bsdf_principled' and has_feature('coat'):
            coat = self.node('SubstrateSlabBSDF',sub_surface_type=u.MaterialSubSurfaceType.MSS_SIMPLE_VOLUME)
            self.connect(self.constant(0),coat,'Diffuse Albedo')
            ior = field('coat_ior',1.5)
            ratio = self.op('Divide',self.op('Subtract',ior,self.constant(1)),self.op('Add',ior,self.constant(1)))
            self.connect(self.op('Multiply',ratio,ratio),coat,'F0')
            self.connect(field('coat_roughness',.03),coat,'Roughness')
            mfp = self.node('SubstrateTransmittanceToMFP')
            self.connect(field('coat_tint',[1,1,1]),mfp,'TransmittanceColor'); self.connect(mfp,coat,'SSS MFP')
            if 'coat_normal' in channels or 'normal' in channels:
                self.connect(self.expr(channels.get('coat_normal',channels.get('normal')),normal=True),coat,'Normal')
            coverage = self.node('SubstrateWeight')
            self.connect(coat,coverage,'A'); self.connect(self.op('Saturate',field('coat',0)),coverage,'Weight')
            layer = self.node('SubstrateVerticalLayering',use_parameter_blending=False)
            self.connect(coverage,layer,'Top'); self.connect(result,layer,'Bottom')
            result = layer
        return result

    def connect_refraction(self, ior, mode):
        prop(self.material,'refraction_method',u.RefractionMode.RM_INDEX_OF_REFRACTION)
        value = ior
        if mode == 'thin_raster':
            # PNO disables PT refraction at shader compile time; retain IOR mode and switch only its scalar.
            ray = self.node('RayTracingQualitySwitch')
            self.connect(self.constant(1),ray,'Normal');self.connect(ior,ray,'RayTraced')
            value = self.node('PathTracingQualitySwitch')
            self.connect(ray,value,'Normal');self.connect(ior,value,'PathTraced')
        if not self.lib.connect_material_property(value[0],value[1],u.MaterialProperty.MP_REFRACTION):
            raise RuntimeError('原生材质转换失败：无法连接源 IOR 折射')

    def build(self, record, cache_dir):
        import time
        timeout = float(self.cfg.get('material_conversion_timeout', 120.0))
        self._conversion_deadline = time.perf_counter() + timeout if timeout > 0 and not record.get('basic_pbr_bake') and record.get('pmx_index') is None else None
        try:
            result = self._build(record, cache_dir)
            self.check_conversion_budget()
            return result
        finally:
            self._conversion_deadline = None

    def check_conversion_budget(self):
        import time
        deadline = getattr(self, '_conversion_deadline', None)
        if deadline is not None and time.perf_counter() >= deadline:
            raise RuntimeError('原生材质转换超时：超过单材质时间预算')

    def _build(self, record, cache_dir):
        if record.get('surface_graph'):
            from .material_contract import add_issue, finalize_restoration
            gbuffer = u.SystemLibrary.get_console_variable_int_value('r.Substrate.ProjectGBufferFormat')
            self.report.setdefault('checks',{})['material_capabilities'] = {
                'substrate_enabled':bool(u.SystemLibrary.get_console_variable_int_value('r.Substrate')),
                'gbuffer_format':gbuffer,'global_settings_changed':False}
            if gbuffer == 0 and (record.get('closure_features') or record['surface_graph']['kind'] in ('mix','add','function_call')):
                add_issue(record, 'substrate_gbuffer_simplification',
                    '当前 Blendable GBuffer 会合并闭包并限制部分 F90/MFP 响应；保留节点结构但渲染需视觉复核',severity='approximation')
                finalize_restoration(record)
        for definition in record.get('function_definitions',[]):
            old=self.function_definitions.get(definition['id'])
            if old and semantic_ir(old['body'])!=semantic_ir(definition['body']):raise RuntimeError('材质函数签名冲突')
            self.function_definitions[definition['id']]=definition
        if record.get("pmx_index") is not None and self.cfg.get("character_backend") == "mmdbridge_fbx":
            return self.build_character(record)
        instance = None if record.get('basic_pbr_bake', {}).get('status') == 'baked' else self.build_from_master(record)
        if instance is not None:
            return instance
        if record.get('surface_graph') and 'transmission' in record.get('closure_features',[]):
            from .material_graph import FunctionLibrary
            from .material_contract import closure_refraction, add_issue
            library=FunctionLibrary();library.definitions=self.function_definitions
            record['refraction_ior']=closure_refraction(library.expand(record['surface_graph']))
            if record['refraction_ior'] and record['refraction_ior'].get('op')!='constant':
                add_issue(record,'refraction_mixture_projection',
                    '多个透射闭包的 IOR 合成到 UE 单一折射输入，需视觉复核',severity='approximation')
        if record.get('surface_graph') and self.cfg.get('share_scene_parents',True):
            opacity=record.get('channels',{}).get('opacity')
            state={'masked':masked_surface(record),
                   'refraction_mode':glass_refraction_mode(record) if 'transmission' in record.get('closure_features',[]) else None,
                   'backend_version':BACKEND_VERSION,
                   'thin_surface_scattering':self.cfg.get('thin_surface_scattering','legacy_diffusion'),
                   'two_sided':bool(record.get('thin_surface_transmission',False)),
                   'coverage_scale':float(self.cfg.get('foliage_distance_scale',2.)),
                   'cutout':cutout_transmission(record)}
            if record.get('continuous_opacity'):state['continuous_opacity']=True
            record=material_template(record,state)
            self._parameter_defaults=record['_bindings']
            self._texture_parameter_roles=set()
            path=self.root+'/Materials/M_Shared_'+record['_parent_key']
            parent=u.load_asset(path)
            if parent and u.EditorAssetLibrary.get_metadata_tag(parent,'MMDBridge.BuildState')!='interrupted':
                self.verify_asset(parent)
                self._texture_parameter_roles={tuple(p) for p in json.loads(u.EditorAssetLibrary.get_metadata_tag(parent,'MMDBridge.TextureParameters') or '[]')}
                return self.finish_material(record,parent,compile_parent=False)
        material = self.create('M_Shared_'+record['_parent_key'] if record.get('_parent_key') else record["asset_name"],u.Material,u.MaterialFactoryNew())
        self.material = material
        self.clear_expressions(material)
        if record.get('surface_graph'):
            if not u.SystemLibrary.get_console_variable_int_value('r.Substrate'):
                raise RuntimeError('原生材质转换失败：多闭包还原需要启用 UE Substrate 并重启编辑器')
            opacity=record.get('channels',{}).get('opacity')
            masked=record['_render_state']['masked'] if record.get('_render_state') else masked_surface(record)
            physical_transmission = 'transmission' in record.get('closure_features', [])
            transmissive = physical_transmission or 'straight_transmission' in record.get('closure_features', []) or record.get('continuous_opacity',False)
            prop(material,'blend_mode',u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE if transmissive else u.BlendMode.BLEND_MASKED if masked else u.BlendMode.BLEND_OPAQUE)
            prop(material,'two_sided',transmissive or masked or record.get('thin_surface_transmission',False))
            if transmissive:
                prop(material,'translucency_lighting_mode',u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
                prop(material,'translucency_pass',u.MaterialTranslucencyPass.MTP_BEFORE_DOF)
                mode = glass_refraction_mode(record) if physical_transmission else 'straight_transmission'
                prop(material,'refraction_method',u.RefractionMode.RM_INDEX_OF_REFRACTION if physical_transmission else u.RefractionMode.RM_NONE)
                u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.RefractionMode',mode)
                coverage=self.expr(opacity,data=True) if opacity else self.constant(1)
                self.lib.connect_material_property(coverage[0],coverage[1],u.MaterialProperty.MP_OPACITY)
                if physical_transmission:
                    ior=self.expr(record.get('refraction_ior') or record.get('channels',{}).get('ior',{'op':'constant','value':1.5}),data=True)
                    self.connect_refraction(ior,mode)
            if masked and not transmissive:
                coverage=self.coverage_expr(opacity)
                if not self.lib.connect_material_property(coverage[0],coverage[1],u.MaterialProperty.MP_OPACITY_MASK):
                    raise RuntimeError('原生材质转换失败：闭包图覆盖率连接失败')
            front = self.closure(record['surface_graph'])
            if not self.lib.connect_material_property(front[0],front[1],u.MaterialProperty.MP_FRONT_MATERIAL):
                raise RuntimeError('原生材质转换失败：无法连接表面闭包图')
            u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.SurfaceGraph','2')
            if cutout_transmission(record):u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.CutoutTransmission','1')
            return self.finish_material(record,material)
        channels = record["channels"]
        if record.get("surface") == "unsupported":
            self.report["warnings"].append(f"材质未还原: {record['name']}")
            self.lib.connect_material_property(self.constant([1,0,1])[0],"",u.MaterialProperty.MP_BASE_COLOR)
        transmission = channels.get("transmission")
        has_transmission = bool(transmission and (
            transmission.get("op") != "constant" or float(transmission.get("value", 0)) > 1e-4))
        cutout = (self.cfg.get('restore_scene_appearance', False) or
                  record.get('coverage_source') == 'shader_mix') and cutout_transmission(record)
        glass = record.get("surface") == "glass" or record.get("water") or (has_transmission and not cutout)
        opacity = channels.get("opacity")
        masked = bool(opacity and (opacity["op"] != "constant" or opacity.get("value",1) < 1))
        prop(material,"two_sided",masked or glass)
        prop(material,"blend_mode",u.BlendMode.BLEND_TRANSLUCENT if glass else u.BlendMode.BLEND_MASKED if masked else u.BlendMode.BLEND_OPAQUE)
        if cutout:
            prop(material,'shading_model',u.MaterialShadingModel.MSM_TWO_SIDED_FOLIAGE)
            base = self.expr(channels.get('base_color', {'op':'constant','value':[.18,.18,.18]}))
            transmitted = self.op('Multiply',base,self.expr(transmission,data=True))
            if not self.lib.connect_material_property(transmitted[0],transmitted[1],u.MaterialProperty.MP_SUBSURFACE_COLOR):
                raise RuntimeError('无法连接遮罩表面的透光颜色')
            record['requires_visual_review'] = True
            self.report['warnings'].append('遮罩与透光分别保留；薄表面透光使用原生双面散射近似，需要视觉复核: '+record['name'])
            u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.CutoutTransmission','1')
        if glass:
            prop(material,"translucency_lighting_mode",u.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
        mapping = {"base_color":"MP_BASE_COLOR","roughness":"MP_ROUGHNESS","metallic":"MP_METALLIC", "specular":"MP_SPECULAR",
                   "normal":"MP_NORMAL","emission":"MP_EMISSIVE_COLOR","opacity":"MP_OPACITY_MASK"}
        special_masks = record.get("special_masks", {})
        for key, target in mapping.items():
            if key not in channels or (glass and key == "opacity") or (record.get("water") and key in ("normal","roughness","base_color")):
                continue
            if special_masks and key in ("base_color", "roughness", "normal"):
                continue
            expression = (self.coverage_expr(channels[key]) if key == 'opacity' and masked else
                          self.expr(channels[key],data=key in ("roughness","metallic","opacity")))
            if key == "emission" and "emission_strength" in channels:
                expression = self.op("Multiply",expression,self.expr(channels["emission_strength"]))
            if key == 'emission' and self.cfg.get('restore_scene_appearance',False):
                from .scene_appearance import RADIANCE_TO_UE
                expression=self.op('Multiply',expression,self.constant(RADIANCE_TO_UE))
            if not self.lib.connect_material_property(expression[0],expression[1],getattr(u.MaterialProperty,target)):
                raise RuntimeError(f"原生材质转换失败：无法连接材质输出 {key}")
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
            source_glass=(self.cfg.get('restore_scene_appearance',False) and record.get('surface')=='glass' and
                          not record.get('water') and u.SystemLibrary.get_console_variable_int_value('r.Substrate'))
            coverage = (self.expr(opacity,data=True) if self.cfg.get('restore_scene_appearance',False) and opacity else
                        self.scalar("Opacity",1 if source_glass else .35))
            self.lib.connect_material_property(coverage[0],coverage[1],u.MaterialProperty.MP_OPACITY)
            ior = (self.expr(channels['ior']) if source_glass and 'ior' in channels else
                   self.scalar("Refraction",1.333 if record.get("water") else 1.45))
            self.lib.connect_material_property(ior[0],"",u.MaterialProperty.MP_REFRACTION)
            if source_glass:
                prop(material,'blend_mode',u.BlendMode.BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE)
                prop(material,'translucency_pass',u.MaterialTranslucencyPass.MTP_BEFORE_DOF)
                mode = glass_refraction_mode(record)
                self.connect_refraction(ior,mode)
                u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.RefractionMode',mode)
                if mode == 'thin_raster':
                    record['requires_visual_review'] = True
                    self.report['warnings'].append('薄板实时预览取消屏幕几何偏移；光追和 PT 保留源 IOR，细节需要视觉复核: '+record['name'])
                slab=self.node('SubstrateSlabBSDF',sub_surface_type=u.MaterialSubSurfaceType.MSS_SIMPLE_VOLUME)
                self.connect(self.constant(0),slab,'Diffuse Albedo')
                ratio=self.op('Divide',self.op('Subtract',ior,self.constant(1)),self.op('Add',ior,self.constant(1)))
                # Glass BSDF's Color weights both reflection and transmission in Cycles.
                tint=self.expr(channels['base_color'])
                self.connect(self.op('Multiply',self.op('Multiply',ratio,ratio),tint),slab,'F0')
                self.connect(tint,slab,'F90')
                self.connect(self.expr(channels.get('roughness',{'op':'constant','value':0})),slab,'Roughness')
                mfp=self.node('SubstrateTransmittanceToMFP')
                self.connect(tint,mfp,'TransmittanceColor')
                self.connect(mfp,slab,'SSS MFP')
                u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.GlassColor','3')
                if 'normal' in channels:
                    self.connect(self.expr(channels['normal'],normal=True),slab,'Normal')
                if not self.lib.connect_material_property(slab[0],slab[1],u.MaterialProperty.MP_FRONT_MATERIAL):
                    raise RuntimeError('无法连接原生玻璃透射 BSDF')
        if record.get("water"):
            color = self.node("VectorParameter",parameter_name="WaterColor",default_value=u.LinearColor(.03,.25,.3,1))
            self.lib.connect_material_property(color[0],"",u.MaterialProperty.MP_BASE_COLOR)
            self.lib.connect_material_property(self.scalar("Roughness",.08)[0],"",u.MaterialProperty.MP_ROUGHNESS)
            normal = self.water_normal(cache_dir)
            self.lib.connect_material_property(normal[0],normal[1],u.MaterialProperty.MP_NORMAL)
            self.water.add(record["id"])
        return self.finish_material(record,material)

    def finish_material(self,record,material,compile_parent=True):
        if compile_parent:
            # USD prototypes can be rendered by instanced components and Nanite.
            # Missing usage permutations otherwise silently draw the default material.
            prop(material,'used_with_instanced_static_meshes',True)
            if material.get_editor_property('blend_mode') in (u.BlendMode.BLEND_OPAQUE,u.BlendMode.BLEND_MASKED):
                prop(material,'used_with_nanite',True)
        if compile_parent and record.get('surface_graph') and record.get('thin_surface_transmission'):
            prop(material,'used_with_nanite',True)
            prop(material,'used_with_instanced_static_meshes',True)
            u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.ThinSurfaceScattering',
                self.cfg.get('thin_surface_scattering','legacy_diffusion'))
            record['requires_visual_review']=True
        if (compile_parent and material.get_editor_property('blend_mode') == u.BlendMode.BLEND_MASKED and
                record.get('channels', {}).get('opacity')):
            u.EditorAssetLibrary.set_metadata_tag(material, 'MMDBridge.MaskDistance',
                '1:' + str(float(self.cfg.get('foliage_distance_scale', 2.))))
        if compile_parent and record.get('coverage_source') == 'shader_mix':
            u.EditorAssetLibrary.set_metadata_tag(material, 'MMDBridge.ShaderCoverage', '1')
        if compile_parent:
            self.lib.delete_unused_expressions(material)
            self.lib.layout_material_expressions(material)
            errors = self.lib.recompile_material(material)
            if errors:
                raise RuntimeError('原生材质编译失败 '+record['name']+': '+ '; '.join(str(e) for e in errors))
        from .material_contract import asset_name_for_path
        existing=u.load_asset(self.root+'/Materials/'+asset_name_for_path(self.root+'/Materials',
            'MI_'+record.get('asset_name',record['id']),u.Paths.project_content_dir()))
        if existing and record.get('_parent_key'):self.verify_asset(existing)
        instance = self.create("MI_"+record.get("asset_name", record["id"]),u.MaterialInstanceConstant,u.MaterialInstanceConstantFactoryNew())
        self.lib.set_material_instance_parent(instance,material)
        for name,binding in record.get('_bindings',{}).items():
            value=binding['value']
            if binding['type']=='scalar':self.lib.set_material_instance_scalar_parameter_value(instance,name,float(value))
            elif binding['type']=='vector':self.lib.set_material_instance_vector_parameter_value(instance,name,u.LinearColor(*(value+[0,0,0])[:3],value[3] if len(value)>3 else 1))
            else:
                for port,normal,data in sorted(self._texture_parameter_roles):
                    if port==name:
                        texture=self.texture(value,normal,data)
                        self.lib.set_material_instance_texture_parameter_value(instance,name+('_N' if normal else '_D' if data else '_C'),texture)
        self.lib.update_material_instance(instance)
        u.EditorAssetLibrary.set_metadata_tag(instance,"MMDBridge.SourceMaterial",record["id"])
        u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.BackendVersion',str(BACKEND_VERSION))
        if compile_parent:u.EditorAssetLibrary.set_metadata_tag(material,"MMDBridge.MaterialStrategy",record.get("strategy","nodes"))
        u.EditorAssetLibrary.set_metadata_tag(instance,"MMDBridge.MaterialStrategy",record.get("strategy","nodes"))
        if record.get('_parent_key') and compile_parent:
            u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.TextureParameters',json.dumps(sorted(self._texture_parameter_roles)))
        if compile_parent:
            u.EditorAssetLibrary.set_metadata_tag(material,'MMDBridge.BuildState','complete')
            if not u.EditorAssetLibrary.save_loaded_asset(material):raise RuntimeError('母材质保存失败')
        u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.BuildState','complete')
        if not u.EditorAssetLibrary.save_loaded_asset(instance):raise RuntimeError('材质实例保存失败')
        self.protect_asset(material);self.protect_asset(instance)
        if record.get('_parent_key'):
            self.protect_asset(material);self.protect_asset(instance)
            u.EditorAssetLibrary.set_metadata_tag(instance,'MMDBridge.SharedParent',record['_parent_key'])
            u.EditorAssetLibrary.save_loaded_asset(instance);self.protect_asset(instance)
        self.assets[record["id"]] = instance.get_path_name()
        self.report.setdefault("material_routes", {})[record["id"]] = {
            "route": record.get("strategy", "nodes"), "parent": material.get_path_name(),
            "shared_parent":bool(record.get('_parent_key')), "parent_reused":not compile_parent}
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
