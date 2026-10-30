"""Snapshot actual UE materials and apply isolated copies to selected character slots."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import unreal as u

from .character_material_preset import FORMAT, VERSION, load, validate, signature, match_slots, job_preset
from .core import atomic_json, read_json

ARRAYS = {
    'scalar_parameter_values': 'ScalarParameterValue',
    'vector_parameter_values': 'VectorParameterValue',
    'double_vector_parameter_values': 'DoubleVectorParameterValue',
    'texture_parameter_values': 'TextureParameterValue',
    'texture_collection_parameter_values': 'TextureCollectionParameterValue',
    'parameter_collection_parameter_values': 'ParameterCollectionParameterValue',
    'runtime_virtual_texture_parameter_values': 'RuntimeVirtualTextureParameterValue',
    'sparse_volume_texture_parameter_values': 'SparseVolumeTextureParameterValue',
    'font_parameter_values': 'FontParameterValue',
}
STRUCTS = {'base_property_overrides': 'MaterialInstanceBasePropertyOverrides',
           'lightmass_settings': 'LightmassMaterialInterfaceSettings'}
BOOLEANS = ('override_subsurface_profile', 'override_specular_profile', 'override_toon_profile',
            'override_phys_material', 'override_blendable_location', 'override_blendable_priority')
REFERENCES = ('phys_material', 'subsurface_profile', 'neural_profile',
              'specular_profile_override', 'toon_profile_override')


def path(value):
    return value.get_path_name() if value else None


def get(obj, name, default=None):
    try:
        return obj.get_editor_property(name)
    except Exception:
        return default


def world():
    return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()


def record(material):
    if material:
        for name in ('MMDBridge.CustomRecord', 'MMDBridge.PBRRecord'):
            text = u.EditorAssetLibrary.get_metadata_tag(material, name)
            if text:
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    pass
    return {}


def slots(mesh, component=None):
    result = []
    saved = u.EditorAssetLibrary.get_metadata_tag(mesh, 'MMDBridge.MaterialPresetSlots')
    identities = json.loads(saved) if saved else []
    for index, entry in enumerate(mesh.get_editor_property('materials')):
        material = component.get_material(index) if component else entry.material_interface
        source = record(entry.material_interface) or record(material)
        note = source.get('character_material', {})
        source_model = note.get('custom', {}).get('source', {}).get('model_sha256') or note.get('pbr', {}).get('model_sha256')
        row = dict(index=index, slot_name=str(entry.material_slot_name),
            imported_slot_name=str(get(entry, 'imported_material_slot_name', '')),
            source_name=source.get('name', ''), pmx_index=source.get('pmx_index'),
            model_sha256=source_model, material=path(material))
        if not source and index < len(identities) and identities[index]['slot_name'] == row['slot_name']:
            for field in ('source_name', 'pmx_index', 'model_sha256'):
                row[field] = identities[index].get(field)
        result.append(row)
    return result


def describe(mesh, component=None, origin=None):
    rows = slots(mesh, component)
    return dict(id=path(component or mesh), mesh=path(mesh), component=path(component),
                label=(component.get_owner().get_actor_label() + ' / ' + component.get_name()) if component else mesh.get_name(),
                origin=origin or '场景角色' if component else origin or '骨骼网格体资产',
                slots=rows, signature=signature(rows))


def _animation_meshes(animation):
    skeleton = animation.get_editor_property('skeleton')
    if not skeleton:
        return []
    preview = u.AnimationLibrary.get_skeleton_preview_mesh(skeleton, False)
    # Some animation assets have no registered preview mesh. Only exact-skeleton meshes are candidates.
    registry = u.AssetRegistryHelpers.get_asset_registry()
    names = registry.get_referencers(skeleton.get_path_name().split('.')[0], u.AssetRegistryDependencyOptions())
    meshes = {}
    if preview:
        meshes[path(preview)] = preview
    for package in names:
        for asset_data in registry.get_assets_by_package_name(package):
            if str(asset_data.asset_class_path.asset_name) == 'SkeletalMesh':
                asset = asset_data.get_asset()
                if asset and asset.get_editor_property('skeleton') == skeleton:
                    meshes[path(asset)] = asset
    return list(meshes.values())


def selection():
    result = {}

    def add(value, origin=None):
        if isinstance(value, u.SkeletalMeshComponent):
            mesh = value.get_editor_property('skeletal_mesh_asset')
            if mesh:
                row = describe(mesh, value, origin)
                result[row['id']] = row
        elif isinstance(value, u.SkeletalMesh):
            row = describe(value, origin=origin)
            result.setdefault(row['id'], row)
        elif isinstance(value, u.AnimSequence):
            for mesh in _animation_meshes(value):
                add(mesh, '动画序列 ' + value.get_name() + ' → 网格材质（会影响使用该网格的其它对象）')
        elif isinstance(value, u.LevelSequence):
            for binding in value.get_bindings():
                for bound in value.locate_bound_objects(binding, world()):
                    add(bound, '关卡序列 ' + value.get_name())
        elif isinstance(value, u.LevelSequenceActor):
            sequence = value.get_sequence()
            if sequence:
                add(sequence)
        elif isinstance(value, u.Actor):
            for component in value.get_components_by_class(u.SkeletalMeshComponent):
                add(component, origin)

    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_selected_level_actors():
        add(actor)
    for asset in u.EditorUtilityLibrary.get_selected_assets():
        add(asset)
    if not result:
        raise ValueError('请在 UE 选中角色、骨骼网格体、动画序列或包含角色绑定的关卡序列')
    return list(result.values())


def resolve(target):
    if target.startswith('/Game/') and ':PersistentLevel.' not in target:
        mesh = u.load_asset(target)
        if isinstance(mesh, u.SkeletalMesh):
            return mesh, None
    component = u.find_object(None, target)
    if not isinstance(component, u.SkeletalMeshComponent):
        raise ValueError('目标角色已改变或不存在，请重新读取 UE 选择')
    mesh = component.get_editor_property('skeletal_mesh_asset')
    if not mesh:
        raise ValueError('选中组件没有骨骼网格体')
    # A generated USD component is not a persistent native material override target.
    for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors():
        if isinstance(actor, u.UsdStageActor) and hasattr(actor, 'get_source_prim_path'):
            if actor.get_source_prim_path(component):
                raise ValueError('该组件由 USD 生成，请选择独立 UE 骨骼网格体；此操作不写 USD 层')
    return mesh, component


def _check_root(root):
    """Unsupported static constructs are explicit failures, never silently dropped."""
    visited = set()

    def visit(material):
        if path(material) in visited:
            return
        visited.add(path(material))
        getter = (u.MaterialEditingLibrary.get_material_expressions if isinstance(material, u.Material)
                  else u.MaterialEditingLibrary.get_material_function_expressions)
        for node in getter(material):
            kind = node.get_class().get_name()
            if 'StaticComponentMaskParameter' in kind or 'MaterialAttributeLayers' in kind:
                raise ValueError('母材质包含当前 UE Python 无法完整读写的静态分量遮罩／材质层：' + path(root))
            if 'MaterialFunctionCall' in kind:
                function = get(node, 'material_function')
                if function:
                    visit(function)
    visit(root)


def snapshot_material(material, *, save_dependencies=False):
    if not isinstance(material, (u.Material, u.MaterialInstanceConstant)):
        raise ValueError('仅能保存持久化 Material／Material Instance Constant；动态运行时材质请先转为资产')
    chain, seen = [], set()
    current = material
    while isinstance(current, u.MaterialInstanceConstant):
        if path(current) in seen:
            raise ValueError('母材质继承存在循环')
        seen.add(path(current))
        item = dict(source=path(current), arrays={}, structs={}, flags={}, references={}, switches={}, profiles={})
        for name in ARRAYS:
            values = get(current, name, [])
            item['arrays'][name] = [value.export_text() for value in values]
        for name in STRUCTS:
            value = get(current, name)
            if value is not None:
                item['structs'][name] = value.export_text()
        for name in BOOLEANS:
            value = get(current, name)
            if value is not None:
                item['flags'][name] = value
        for name in REFERENCES:
            value = get(current, name)
            if value is not None:
                item['references'][name] = path(value)
                if isinstance(value, u.SubsurfaceProfile):
                    item['profiles'][name] = get(value, 'settings').export_text()
                if save_dependencies:
                    u.EditorAssetLibrary.save_loaded_asset(value)
        for name in u.MaterialEditingLibrary.get_static_switch_parameter_names(current):
            item['switches'][str(name)] = u.MaterialEditingLibrary.get_material_instance_static_switch_parameter_value(current, name)
        chain.append(item)
        if save_dependencies:
            u.EditorAssetLibrary.save_loaded_asset(current)
        current = get(current, 'parent')
    if not isinstance(current, u.Material):
        raise ValueError('母材质引用不存在: ' + path(material))
    _check_root(current)
    if save_dependencies:
        u.EditorAssetLibrary.save_loaded_asset(current)
    return dict(source=path(material), root=path(current), instances=list(reversed(chain)))


def capture(target, filename, expected=None):
    mesh, component = resolve(target)
    rows = slots(mesh, component)
    if expected and signature(rows) != expected:
        raise ValueError('读取选择后材质绑定已变化，请重新读取')
    materials = {}
    for row in rows:
        if row['material']:
            material = component.get_material(row['index']) if component else mesh.materials[row['index']].material_interface
            materials.setdefault(row['material'], snapshot_material(material, save_dependencies=True))
    data = validate(dict(format=FORMAT, version=VERSION,
        source=dict(project=u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path()),
                    mesh=path(mesh), component=path(component), engine=u.SystemLibrary.get_engine_version()),
        slots=rows, materials=materials,
        asset_references='UE parent materials and textures must exist in the destination project; no fallback material is substituted.'))
    atomic_json(Path(filename), data)
    return dict(file=str(Path(filename).resolve()), slots=len(rows), materials=len(materials),
                signature=signature(data), parent_materials=sorted({m['root'] for m in materials.values()}))


def _preflight(data):
    validate(data)
    for material in data['materials'].values():
        root = u.load_asset(material['root'])
        if not isinstance(root, u.Material):
            raise ValueError('目标工程缺少母材质，请先迁移原资产及依赖：' + material['root'])
        _check_root(root)
        for item in material['instances']:
            if set(item.get('arrays', {})) - ARRAYS.keys() or set(item.get('structs', {})) - STRUCTS.keys():
                raise ValueError('配置含有不支持的材质属性')
            if set(item.get('flags', {})) - set(BOOLEANS) or set(item.get('references', {})) - set(REFERENCES):
                raise ValueError('配置含有不支持的材质覆盖属性')
            references = list(item.get('references', {}).values())
            for values in item.get('arrays', {}).values():
                for text in values:
                    references.extend(reference for reference in re.findall(
                        r"/[A-Za-z0-9_]+/[^'\"\s,()]+\.[^'\"\s,()]+", text) if not reference.startswith('/Script/'))
            for reference in references:
                if not u.load_asset(reference):
                    raise ValueError('目标工程缺少贴图或材质依赖：' + reference)


def _struct(name, text):
    value = getattr(u, name)()
    value.import_text(text)
    return value


def build_materials(data, root):
    _preflight(data)
    tools = u.AssetToolsHelpers.get_asset_tools()
    result = {}
    for index, (identity, source) in enumerate(data['materials'].items()):
        parent = u.load_asset(source['root'])
        for depth, item in enumerate(source['instances']):
            name = f'MI_Slot{index:03d}_Layer{depth:02d}'
            instance = tools.create_asset(name, root, u.MaterialInstanceConstant, u.MaterialInstanceConstantFactoryNew())
            if not instance:
                raise RuntimeError('无法创建独立材质实例: ' + root + '/' + name)
            u.MaterialEditingLibrary.set_material_instance_parent(instance, parent)
            for field, texts in item.get('arrays', {}).items():
                instance.set_editor_property(field, [_struct(ARRAYS[field], text) for text in texts])
            for field, text in item.get('structs', {}).items():
                instance.set_editor_property(field, _struct(STRUCTS[field], text))
            for field, value in item.get('flags', {}).items():
                instance.set_editor_property(field, value)
            for field, reference in item.get('references', {}).items():
                asset = u.load_asset(reference)
                if field in item.get('profiles', {}):
                    profile_name = f'SSP_Slot{index:03d}_Layer{depth:02d}'
                    asset = tools.create_asset(profile_name, root, u.SubsurfaceProfile, u.SubsurfaceProfileFactory())
                    settings = get(asset, 'settings').copy()
                    settings.import_text(item['profiles'][field])
                    asset.set_editor_property('settings', settings)
                    u.EditorAssetLibrary.save_loaded_asset(asset)
                instance.set_editor_property(field, asset)
            for field, value in item.get('switches', {}).items():
                u.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value(instance, field, value)
            u.MaterialEditingLibrary.update_material_instance(instance)
            for field, texts in item.get('arrays', {}).items():
                actual = [value.export_text() for value in get(instance, field, [])]
                if actual != texts:
                    raise RuntimeError('材质参数回读不一致: ' + field)
            for field, value in item.get('switches', {}).items():
                if u.MaterialEditingLibrary.get_material_instance_static_switch_parameter_value(instance, field) != value:
                    raise RuntimeError('静态开关回读不一致: ' + field)
            u.EditorAssetLibrary.set_metadata_tag(instance, 'MMDBridge.MaterialPresetSource', identity)
            u.EditorAssetLibrary.save_loaded_asset(instance)
            parent = instance
        result[identity] = parent
    return result


def _save(meshes, save_level):
    for mesh in meshes:
        if not u.EditorAssetLibrary.save_loaded_asset(mesh):
            raise RuntimeError('无法保存目标骨骼网格体: ' + path(mesh))
    if save_level:
        settings = u.get_default_object(u.UsdProjectSettings)
        previous = settings.get_editor_property('show_save_layers_dialog_when_saving')
        settings.set_editor_property('show_save_layers_dialog_when_saving', u.UsdSaveDialogBehavior.NEVER_SAVE)
        try:
            if not u.EditorLoadingAndSavingUtils.save_map(world(), world().get_path_name().split('.')[0]):
                raise RuntimeError('无法保存目标角色所在关卡')
        finally:
            settings.set_editor_property('show_save_layers_dialog_when_saving', previous)


def _assign(mesh, component, values):
    if component:
        component.get_owner().modify(True)
        component.modify(True)
        for index, material in values.items():
            component.set_material(index, material)
    else:
        mesh.modify(True)
        entries = [entry.copy() for entry in mesh.get_editor_property('materials')]
        for index, material in values.items():
            entries[index].material_interface = material
        mesh.set_editor_property('materials', entries)


def apply_data(data, mesh, component=None, *, mapping=None, root=None, save=True, ledger=True):
    before = slots(mesh, component)
    matches = match_slots(data['slots'], before, mapping=mapping)
    revision = uuid.uuid4().hex[:12]
    root = root or '/Game/MMDBridge/MaterialPresets/R_' + revision
    materials = build_materials(data, root)
    sources = {row['index']: row for row in data['slots']}
    values, originals, changes = {}, {}, []
    for match in matches:
        slot = match['target']
        source = sources[match['source']]['material']
        values[slot] = materials[source] if source else None
        originals[slot] = component.get_material(slot) if component else mesh.materials[slot].material_interface
        changes.append(dict(slot=slot, original=path(originals[slot]), candidate=path(values[slot]),
                            candidate_signature=signature(snapshot_material(values[slot])) if values[slot] else None))
    ledger_path = Path(u.Paths.project_saved_dir()) / 'MMDBridge/character_material_preset_undo.json'
    note = dict(state='applying', target=path(component or mesh), world=path(world()), changes=changes, revision=revision)
    if ledger:
        atomic_json(ledger_path, note)
    try:
        with u.ScopedEditorTransaction('Apply character material preset'):
            if slots(mesh, component) != before:
                raise ValueError('预检后目标绑定发生变化，保留当前编辑')
            _assign(mesh, component, values)
            if not component:
                u.EditorAssetLibrary.set_metadata_tag(mesh, 'MMDBridge.MaterialPresetSlots', json.dumps(before, ensure_ascii=False))
            for slot, value in values.items():
                actual = component.get_material(slot) if component else mesh.materials[slot].material_interface
                if actual != value:
                    raise RuntimeError('材质槽覆盖未通过回读验证')
            if save:
                _save([] if component else [mesh], bool(component))
    except Exception:
        _assign(mesh, component, originals)
        if save:
            _save([] if component else [mesh], bool(component))
        if ledger:
            atomic_json(ledger_path, dict(note, state='rolled_back'))
        raise
    if ledger:
        atomic_json(ledger_path, dict(note, state='complete'))
    return dict(count=len(values), target=path(component or mesh), root=root, revision=revision,
                slot_mapping=matches, usd_saved=False)


def undo():
    filename = Path(u.Paths.project_saved_dir()) / 'MMDBridge/character_material_preset_undo.json'
    if not filename.is_file():
        raise ValueError('没有角色材质配置应用记录')
    note = read_json(filename)
    if note['state'] != 'complete':
        raise ValueError('该材质配置应用记录不能撤销')
    mesh, component = resolve(note['target'])
    if component and path(world()) != note['world']:
        raise ValueError('请打开应用配置时的关卡再撤销')
    values, candidates = {}, {}
    for row in note['changes']:
        current = component.get_material(row['slot']) if component else mesh.materials[row['slot']].material_interface
        state = signature(snapshot_material(current)) if current else None
        if path(current) != row['candidate'] or state != row['candidate_signature']:
            raise ValueError('材质已有后续手工编辑，保留当前结果；不能自动撤销')
        original = u.load_asset(row['original']) if row['original'] else None
        if row['original'] and not original:
            raise ValueError('原绑定材质不存在: ' + row['original'])
        values[row['slot']], candidates[row['slot']] = original, current
    try:
        with u.ScopedEditorTransaction('Undo character material preset'):
            _assign(mesh, component, values)
            _save([] if component else [mesh], bool(component))
    except Exception:
        _assign(mesh, component, candidates)
        raise
    atomic_json(filename, dict(note, state='undone'))
    return dict(count=len(values), target=note['target'])


def apply_import(job, mesh, root):
    data = job_preset(job)
    if data is None:
        return None
    return apply_data(data, mesh, root=root + '/MaterialPreset/R_' + uuid.uuid4().hex[:12], ledger=False)


def run(command):
    action = command['action']
    if action == 'material_preset_selection':
        return {'targets': selection()}
    if action == 'material_preset_capture':
        return capture(command['target'], command['filename'], command.get('expected'))
    if action == 'material_preset_apply':
        mesh, component = resolve(command['target'])
        if command.get('expected') and signature(slots(mesh, component)) != command['expected']:
            raise ValueError('读取选择后目标绑定已变化，请重新读取')
        return apply_data(load(command['filename']), mesh, component, mapping=command.get('mapping'))
    if action == 'material_preset_undo':
        return undo()
    raise ValueError('未知角色材质配置操作')
