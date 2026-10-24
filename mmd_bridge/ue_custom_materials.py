"""Read user parents without editing them; write explicit mappings to owned MIs."""
import copy
import json
import math

import unreal as u

from .character_custom_materials import asset_path, plan
from .character_profiles import fingerprint
from .core import file_hash
from .ue_character_materials import dependency_signature, enable_character_usage


def _root(parent):
    visited = set()
    while isinstance(parent, u.MaterialInstanceConstant):
        if parent.get_path_name() in visited:
            raise ValueError('母材质继承存在循环')
        visited.add(parent.get_path_name())
        parent = parent.get_editor_property('parent')
    if not isinstance(parent, u.Material):
        raise ValueError('仅支持 Material 和 Material Instance Constant')
    return parent


def _definitions(root):
    definitions, unsupported, visited, spaces = {}, set(), set(), {}

    def visit(material):
        if material.get_path_name() in visited:
            return
        visited.add(material.get_path_name())
        getter = (u.MaterialEditingLibrary.get_material_expressions if isinstance(material, u.Material)
                  else u.MaterialEditingLibrary.get_material_function_expressions)
        try:
            nodes = getter(material)
        except TypeError:
            unsupported.add('当前不能展开此材质函数实例参数')
            return
        for node in nodes:
            kind = node.get_class().get_name()
            if 'MaterialAttributeLayers' in kind:
                unsupported.add('材质层参数暂不能唯一定位')
            if 'Parameter' in kind:
                try:
                    name = str(node.get_editor_property('parameter_name'))
                except Exception:
                    continue
                if 'Texture' in kind:
                    reason = ''
                    if 'Cube' in kind or 'Volume' in kind or 'VirtualTexture' in kind or 'Array' in kind:
                        reason = '当前仅支持普通二维纹理参数'
                    try:
                        sampler = str(node.get_editor_property('sampler_type')).upper()
                        if 'VIRTUAL' in sampler or 'EXTERNAL' in sampler:
                            reason = '虚拟纹理参数暂不支持'
                        color_space = ('normal' if 'NORMAL' in sampler else 'data' if any(
                            key in sampler for key in ('LINEAR', 'MASKS', 'ALPHA')) else 'srgb')
                        spaces.setdefault(name, set()).add(color_space)
                    except Exception:
                        pass
                    definitions.setdefault(name, []).append(reason)
            if 'MaterialFunctionCall' in kind:
                function = node.get_editor_property('material_function')
                if function:
                    visit(function)
    visit(root)
    return definitions, unsupported, spaces


def inventory(path):
    path = asset_path(path)
    parent = u.load_asset(path)
    if not isinstance(parent, (u.Material, u.MaterialInstanceConstant)):
        raise ValueError('母材质不存在或类型不支持: ' + path)
    root = _root(parent)
    definitions, unsupported, spaces = _definitions(root)
    lib = u.MaterialEditingLibrary
    parameters = []
    for kind in ('texture', 'scalar', 'vector'):
        names = list(getattr(lib, 'get_' + kind + '_parameter_names')(parent))
        for name in sorted(set(str(n) for n in names)):
            method = ('get_material_instance_' if isinstance(parent, u.MaterialInstanceConstant)
                      else 'get_material_default_') + kind + '_parameter_value'
            value = getattr(lib, method)(parent, name)
            reason = '；'.join(sorted(unsupported)) if unsupported else ''
            if kind == 'texture':
                reason = next((r for r in definitions.get(name, []) if r), reason)
                if unsupported:
                    reason = '；'.join(sorted(unsupported))
                if value is not None and not isinstance(value, u.Texture2D):
                    reason = '默认纹理不是 Texture2D'
                if value is not None:
                    try:
                        if value.get_editor_property('virtual_texture_streaming'):
                            reason = '虚拟纹理参数暂不支持'
                    except Exception:
                        pass
                value = value.get_path_name() if value else None
            elif kind == 'vector':
                value = [value.r, value.g, value.b, value.a]
            if kind == 'texture' and len(spaces.get(name, set())) > 1:
                reason = '同名纹理参数具有冲突的采样类型'
            parameters.append({'name': name, 'kind': kind, 'default': value,
                               'color_space': next(iter(spaces[name])) if kind == 'texture' and len(spaces.get(name, set())) == 1 else None,
                               'supported': not reason, 'reason': reason})
    result = {'parent': path, 'parameters': parameters,
              'dependency': dependency_signature(None, parent),
              'engine': u.SystemLibrary.get_engine_version()}
    result['signature'] = fingerprint(result)
    return result


def verify_record(cfg, record):
    note = record.get('character_material', {}).get('custom')
    if not note or note.get('version') != 1:
        raise ValueError('缺少自选映射准备记录')
    expected = plan(cfg, note['source'])
    if expected['signature'] != note['signature']:
        raise ValueError('自选映射或来源已改变，请重新准备材质')
    actual = inventory(note['parent'])
    if actual['signature'] != note['inventory_signature']:
        raise ValueError('UE 母材质或参数默认值在读取后发生变化，请重新读取参数')
    for image in record.get('textures', {}).values():
        if file_hash(image['path']) != image['sha256']:
            raise ValueError('准备后的贴图内容发生变化')
    return note, actual


def build(manager, record):
    note, actual = verify_record(manager.cfg, record)
    parent = u.load_asset(note['parent'])
    result = manager.create('MI_' + record.get('asset_name', record['id']), u.MaterialInstanceConstant,
                            u.MaterialInstanceConstantFactoryNew(), record=record)
    lib = manager.lib
    lib.clear_all_material_instance_parameters(result)
    lib.set_material_instance_parent(result, parent)
    assigned = {}
    for name, image in record.get('textures', {}).items():
        texture = manager.texture(image, normal=image.get('normal', False), data=image['color_space'] != 'sRGB')
        lib.set_material_instance_texture_parameter_value(result, name, texture)
        if lib.get_material_instance_texture_parameter_value(result, name) != texture:
            raise RuntimeError('Texture Parameter 设置失败: ' + name)
        assigned[name] = texture.get_path_name()
    for name, binding in note['numeric'].items():
        value = binding['value']
        setter = getattr(lib, 'set_material_instance_' + binding['kind'] + '_parameter_value')
        if binding['kind'] == 'vector':
            value = u.LinearColor(*value[:3], 1)
        setter(result, name, value)
        readback = getattr(lib, 'get_material_instance_' + binding['kind'] + '_parameter_value')(result, name)
        matches = (all(math.isclose(getattr(readback, key), getattr(value, key), rel_tol=1e-5, abs_tol=1e-6)
                       for key in ('r', 'g', 'b', 'a')) if binding['kind'] == 'vector' else
                   math.isclose(readback, value, rel_tol=1e-5, abs_tol=1e-6))
        if not matches:
            raise RuntimeError('PMX 数值参数设置失败: ' + name)
    enable_character_usage(result)
    lib.update_material_instance(result)
    manager.managed(result)
    for key, value in {'CustomRecord': json.dumps(record, ensure_ascii=False),
                       'CustomBinding': note['signature'], 'CustomParentSignature': actual['signature'],
                       'CustomRoot': manager.root, 'ParentMaterial': note['parent'],
                       'MaterialRoute': 'custom_mapped_v1'}.items():
        u.EditorAssetLibrary.set_metadata_tag(result, 'MMDBridge.' + key, value)
    if not u.EditorAssetLibrary.save_loaded_asset(result):
        raise RuntimeError('自选材质实例保存失败')
    manager.protect_asset(result)
    manager.assets[record['id']] = result.get_path_name()
    info = {'route': 'custom_mapped_v1', 'parent': note['parent'], 'assigned_parameters': assigned,
            'numeric_parameters': note['numeric'], 'retained_defaults': note['retained_defaults'],
            'ambiguous_numeric': note['ambiguous_numeric'], 'binding_hash': note['signature']}
    manager.report.setdefault('material_routes', {})[record['id']] = info
    # Opacity scalars cannot make an opaque parent translucent or masked.
    if note['source']['diffuse'][3] < 1 and _root(parent).get_editor_property('blend_mode') == u.BlendMode.BLEND_OPAQUE:
        warning = '自选父材质为不透明，无法保证 PMX 透明／隐藏状态: ' + record['name']
        record['requires_visual_review'] = True
        manager.report.setdefault('warnings', []).append(warning)
    return result


def reuse(manager, record, instance):
    if not instance:
        return False
    note, actual = verify_record(manager.cfg, record)
    tag = u.EditorAssetLibrary.get_metadata_tag
    if tag(instance, 'MMDBridge.CustomBinding') != note['signature'] or tag(instance, 'MMDBridge.CustomParentSignature') != actual['signature']:
        return False
    manager.verify_asset(instance)
    if asset_path(instance.get_editor_property('parent').get_path_name()) != note['parent']:
        raise ValueError('保留手工修改的父材质绑定')
    manager.assets[record['id']] = instance.get_path_name()
    manager.report.setdefault('material_routes', {})[record['id']] = {
        'route': 'custom_mapped_v1', 'parent': note['parent'], 'binding_hash': note['signature'], 'reused': True}
    return True
