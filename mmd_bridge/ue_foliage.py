"""Distance preservation for tool-owned masked surfaces, based on their graph."""
import unreal as u
from pathlib import Path
from .core import read_json


def restore_stage_components(actor):
    """Reapply generated component policy after USD regeneration in editor/PIE."""
    from .ue_shader_data import restore
    restore(actor)
    file = Path(actor.get_editor_property('root_layer').file_path)
    if not file.is_absolute():
        file = Path(u.Paths.project_dir()) / file
    file = file.resolve()
    content = Path(u.Paths.project_content_dir()).resolve()
    if not file.is_relative_to(content) or not (file.parent/'package.json').is_file():
        return None
    record = read_json(file.parent/'package.json')
    if not record.get('asset_id'):
        return None
    root = '/Game/' + file.parent.parent.parent.relative_to(content).as_posix()
    components, meshes, seen = [], {}, set()
    for path in record.get('actor_labels', {}):
        component = actor.get_generated_component(path+'/Mesh') or actor.get_generated_component(path)
        if component is None:
            continue
        children = ([component] if isinstance(component,u.StaticMeshComponent)
                    else component.get_children_components(True))
        for child in children:
            if not isinstance(child,u.StaticMeshComponent) or child.get_path_name() in seen:
                continue
            seen.add(child.get_path_name())
            mesh = child.get_editor_property('static_mesh')
            if mesh:
                components.append(child)
                meshes[mesh.get_path_name()] = mesh
    return configure_distance_preservation({}, {}, root, list(meshes.values()), components=components)


def restore_runtime_components(actor, masked_materials):
    from .ue_shader_data import restore
    restore(actor)
    """Restore owned masked components in -game without editor material data."""
    masked = {path.split('.')[0] for path in masked_materials}
    if not masked:
        return 0
    components = actor.get_components_by_class(u.StaticMeshComponent)
    # USD generated components may belong to generated child actors instead.
    for child in actor.get_attached_actors():
        components.extend(child.get_components_by_class(u.StaticMeshComponent))
    file = Path(actor.get_editor_property('root_layer').file_path)
    if not file.is_absolute():
        file = Path(u.Paths.convert_relative_path_to_full(u.Paths.project_dir())) / file
    if (file.parent / 'package.json').is_file():
        for path in read_json(file.parent / 'package.json').get('actor_labels', {}):
            generated = actor.get_generated_component(path + '/Mesh') or actor.get_generated_component(path)
            if generated:
                components.extend(([generated] if isinstance(generated, u.StaticMeshComponent)
                                   else generated.get_children_components(True)))
    seen, changed = set(), 0
    for component in components:
        if not isinstance(component, u.StaticMeshComponent) or component.get_path_name() in seen:
            continue
        seen.add(component.get_path_name())
        matches = False
        for material in component.get_materials():
            if material is None:
                continue
            parent = material.get_editor_property('parent') if isinstance(material, u.MaterialInstance) else material
            if parent and parent.get_path_name().split('.')[0] in masked:
                matches = True
                break
        if not matches:
            continue
        for name, value in (('never_distance_cull', True), ('allow_cull_distance_volume', False),
                            ('min_draw_distance', 0.), ('ld_max_draw_distance', 0.)):
            component.set_editor_property(name, value)
        if isinstance(component, u.InstancedStaticMeshComponent):
            component.set_cull_distances(0, 0)
        if isinstance(component, u.HierarchicalInstancedStaticMeshComponent):
            component.set_editor_property('enable_density_scaling', False)
        changed += 1
    return changed


def coverage_images(expression):
    """Masks whose shader cutoff is also the image's alpha cutoff.

    Changing RGB mip values or the inputs of a noise/mix/ramp graph changes the
    material itself. Those graphs require final shader coverage validation, not
    a guessed threshold applied to every upstream texture.
    """
    value = expression
    while isinstance(value, dict):
        operation = value.get('op')
        if operation == 'to_scalar' and abs(sum(value.get('weights', [1 / 3] * 3)) - 1) < 1e-6:
            value = value.get('value')
        elif (operation == 'clamp' and value.get('min') == {'op': 'constant', 'value': 0}
              and value.get('max') == {'op': 'constant', 'value': 1}):
            value = value.get('value')
        elif operation == 'math' and value.get('mode') in ('MINIMUM', 'MAXIMUM'):
            # Generic mask extraction expresses saturate(alpha) as min(max(a,0),1).
            # Alpha of the raster formats accepted below is already in [0,1].
            limit = 0 if value['mode'] == 'MAXIMUM' else 1
            constant = {'op': 'constant', 'value': limit}
            if value.get('a') == constant:
                value = value.get('b')
            elif value.get('b') == constant:
                value = value.get('a')
            else:
                return []
        elif operation == 'texture' and value.get('output') == 'Alpha':
            image = value['image']
            if not image.get('hdr', False) and not str(image.get('path', '')).lower().endswith(('.exr', '.hdr')):
                return [(image, 'alpha')]
            return []
        else:
            return []
    return []


def configure_distance_preservation(scene, report, root, meshes, before_each=None,
                                    components=(), material_builder=None):
    """Preserve covered mip pixels and thin Nanite area without forcing LOD 0."""
    changes = {'textures': [], 'meshes': [], 'complex_masks_need_review': []}
    changes.update(mask_sampling=[], components_checked=0, components_changed=0)
    texture_requests = {}
    for record in scene.get('materials', []):
        path = report.get('assets', {}).get('materials', {}).get(record['id'])
        if not path or not path.startswith(root + '/'):
            continue
        material = u.load_asset(path)
        if not material:
            continue
        parent = material.get_editor_property('parent') if isinstance(material, u.MaterialInstanceConstant) else material
        if not parent or parent.get_editor_property('blend_mode') != u.BlendMode.BLEND_MASKED:
            continue
        threshold = float(parent.get_editor_property('opacity_mask_clip_value'))
        opacity = record.get('channels', {}).get('opacity', {})
        if material_builder and opacity:
            if before_each:
                before_each()
            result = material_builder.preserve_mask_distance(parent, record)
            if result:
                changes['mask_sampling'].append(result)
        sources = coverage_images(opacity)
        for image, channel in sources:
            # Resolve the same content-versioned asset that the shader samples.
            tex_path = (material_builder.texture(image, data=True).get_path_name() if material_builder else
                        root + '/Textures/' + image.get('asset_name', 'T_' + image['id']) + '_D')
            texture_requests.setdefault(tex_path, {}).setdefault(channel, set()).add(threshold)
        if not sources and opacity.get('op') != 'constant':
            changes['complex_masks_need_review'].append(record['id'])
    for path, requests in texture_requests.items():
        if before_each:
            before_each()
        texture = u.load_asset(path)
        if not isinstance(texture, u.Texture2D):
            raise RuntimeError('覆盖贴图缺失: ' + path)
        if material_builder:
            material_builder.verify_asset(texture)
        old = texture.get_editor_property('alpha_coverage_thresholds')
        thresholds = [old.x, old.y, old.z, old.w]
        if any(len(values) > 1 for values in requests.values()):
            changes.setdefault('shared_texture_cutoffs_need_review', []).append(path)
            continue
        for channel, values in requests.items():
            thresholds[3] = next(iter(values))
        enabled = texture.get_editor_property('do_scale_mips_for_alpha_coverage')
        previous = [old.x, old.y, old.z, old.w]
        changed = not enabled or previous != thresholds
        if changed:
            texture.set_editor_property('do_scale_mips_for_alpha_coverage', True)
            texture.set_editor_property('alpha_coverage_thresholds', u.Vector4(*thresholds))
            if not u.EditorAssetLibrary.save_loaded_asset(texture):
                raise RuntimeError('无法保存覆盖贴图: ' + path)
            if material_builder:
                material_builder.protect_asset(texture)
        changes['textures'].append({'path': path, 'changed': changed,
            'previous_enabled': enabled, 'previous_thresholds': previous,
            'thresholds': thresholds})
    seen, covered_meshes = set(), set()
    for mesh in meshes:
        path = mesh.get_path_name()
        if path in seen or not path.startswith(root + '/'):
            continue
        seen.add(path)
        covered = False
        for slot in mesh.get_editor_property('static_materials'):
            material = slot.material_interface
            if not material:
                continue
            parent = material.get_editor_property('parent') if isinstance(material, u.MaterialInstanceConstant) else material
            if parent and parent.get_editor_property('blend_mode') == u.BlendMode.BLEND_MASKED:
                covered = True
                break
        if not covered:
            continue
        covered_meshes.add(path)
        settings = mesh.get_editor_property('nanite_settings')
        if not settings.get_editor_property('enabled'):
            continue
        if before_each:
            before_each()
        if hasattr(u, 'NaniteShapePreservation'):
            previous = settings.get_editor_property('shape_preservation')
            target = u.NaniteShapePreservation.PRESERVE_AREA
            # A user-selected voxel representation also preserves shape.
            changed = previous == u.NaniteShapePreservation.NONE
            if changed:
                settings.set_editor_property('shape_preservation', target)
        else:
            previous = settings.get_editor_property('preserve_area')
            changed = not previous
            if changed:
                settings.set_editor_property('preserve_area', True)
        if changed:
            u.get_editor_subsystem(u.StaticMeshEditorSubsystem).set_nanite_settings(mesh, settings, True)
            if not u.EditorAssetLibrary.save_loaded_asset(mesh):
                raise RuntimeError('无法保存 Nanite 形状保持设置: ' + path)
        changes['meshes'].append({'path': path, 'changed': changed, 'previous': str(previous)})
    seen_components = set()
    for component in components:
        if not isinstance(component, u.StaticMeshComponent):
            continue
        path = component.get_path_name()
        if path in seen_components:
            continue
        seen_components.add(path)
        mesh = component.get_editor_property('static_mesh')
        if not mesh or mesh.get_path_name() not in covered_meshes:
            continue
        if before_each and changes['components_checked'] % 512 == 0:
            before_each()
        changes['components_checked'] += 1
        changed = False
        for name, value in (('never_distance_cull',True),('allow_cull_distance_volume',False),
                            ('min_draw_distance',0.),('ld_max_draw_distance',0.)):
            if component.get_editor_property(name) != value:
                component.set_editor_property(name,value)
                changed = True
        if isinstance(component, u.InstancedStaticMeshComponent):
            if (component.get_editor_property('instance_start_cull_distance') or
                    component.get_editor_property('instance_end_cull_distance')):
                component.set_cull_distances(0,0)
                changed = True
        if isinstance(component, u.HierarchicalInstancedStaticMeshComponent):
            if component.get_editor_property('enable_density_scaling'):
                component.set_editor_property('enable_density_scaling',False)
                changed = True
        changes['components_changed'] += int(changed)
    changes['density_policy']={'scope':root,'distance_cull':False,
        'density_scaling':False,'reload_restore':True,
        'complex_coverage_status':'needs_review' if changes['complex_masks_need_review'] else 'configured'}
    return changes
