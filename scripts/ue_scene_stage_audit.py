"""Task-local, native UE reload evidence; does not perform conversion."""
import collections
import json
import os
import time
import traceback
import sys
import types
from pathlib import Path
import unreal as u
RUN = Path(os.environ['MMDBRIDGE_ACCEPTANCE_RUN'])
PROJECT = Path(os.environ['MMDBRIDGE_ACCEPTANCE_PROJECT']).resolve()
from mmd_bridge.core import atomic_json, read_json
receipt_path = RUN / 'ue_audit_response.json'
state = {'last': read_json(receipt_path).get('id') if receipt_path.is_file() else None, 'next': 0.0}
def inspect(job):
    report = read_json(job / 'ue_report.json')
    manifest = read_json(job / 'manifest.json')
    actors = u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors()
    stages = [a for a in actors if isinstance(a, u.UsdStageActor)]
    rows, missing, meshes, materials = [], [], {}, {}
    generated={}; unresolved=[]; mismatches=[]
    prototypes={p['id']:p for p in manifest['scene'].get('prototypes',[])}
    for item in manifest['scene'].get('instances',[]):
        path=item['prim_path']
        component=next((value for stage in stages
            for value in [stage.get_generated_component(path+'/Mesh') or stage.get_generated_component(path)] if value),None)
        if not isinstance(component,u.StaticMeshComponent):
            unresolved.append(path);continue
        generated[component.get_path_name()]=(component,item)
        mesh=component.static_mesh
        expected=prototypes[item['prototype_id']]['triangles']
        if mesh and mesh.get_num_triangles(0)!=expected:
            mismatches.append({'prim':path,'expected_triangles':expected,'actual_triangles':mesh.get_num_triangles(0)})
    for component,item in generated.values():
            actor=component.get_owner()
            mesh = component.static_mesh
            slots = [component.get_material(i) for i in range(component.get_num_materials())]
            row = {'component': component.get_path_name(), 'label': actor.get_actor_label(),
                   'prim_path':item['prim_path'],
                   'mesh': mesh.get_path_name() if mesh else None,
                   'materials': [m.get_path_name() if m else None for m in slots]}
            if isinstance(component, u.InstancedStaticMeshComponent):
                row['instances'] = component.get_instance_count()
            if mesh:
                if row['mesh'] not in meshes:
                    body = mesh.get_editor_property('body_setup')
                    subsystem=u.get_editor_subsystem(u.StaticMeshEditorSubsystem)
                    build=subsystem.get_lod_build_settings(mesh,0)
                    nanite=subsystem.get_nanite_settings(mesh)
                    meshes[row['mesh']] = {'lod0_triangles': mesh.get_num_triangles(0),
                        'nanite_triangles':mesh.get_num_nanite_triangles(),
                        'nanite_enabled':nanite.get_editor_property('enabled'),
                        'remove_degenerates':build.get_editor_property('remove_degenerates'),
                        'uv_channels':subsystem.get_num_uv_channels(mesh,0),
                        'material_slots': len(mesh.get_editor_property('static_materials')),
                        'collision_trace': str(body.get_editor_property('collision_trace_flag')) if body else None}
            if not mesh or any(m is None for m in slots):
                missing.append(row)
            for material in slots:
                if material:
                    materials[material.get_path_name()] = material.get_class().get_name()
            rows.append(row)
    roots = []
    for stage in stages:
        root = stage.get_editor_property('root_layer').file_path
        absolute = Path(root) if Path(root).is_absolute() else PROJECT.parent / root
        roots.append({'actor': stage.get_path_name(), 'root': root, 'exists': absolute.is_file()})
    asset_checks = {}
    for key in ('level', 'sequence', 'render_preset'):
        path = report['assets'].get(key)
        asset_checks[key] = {'path': path, 'exists': bool(path and u.EditorAssetLibrary.does_asset_exist(path))}
    for path in report['assets'].get('materials', {}).values():
        asset_checks[path] = {'exists': bool(u.load_asset(path))}
    for path in report['assets'].get('source_packages',[]):
        if path.startswith('/Game/'):
            asset_checks[path]={'exists':bool(u.load_asset(path))}
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    current = world.get_path_name().split('.')[0] if world else None
    errors = []
    if current != report['assets']['level']:
        errors.append('Generated level is not the current world')
    if not stages or not all(r['exists'] for r in roots):
        errors.append('USD Stage or root layer missing')
    if not rows:
        errors.append('No generated mesh components')
    if missing:
        errors.append('Mesh or material slots missing')
    if unresolved:errors.append('Exported USD instance components missing: '+str(len(unresolved)))
    if not all(c['exists'] for c in asset_checks.values()):
        errors.append('Saved asset missing')
    from mmd_bridge import ue_host
    from mmd_bridge.core import file_hash
    runtime_hashes={path.name:file_hash(path) for path in Path(ue_host.__file__).parent.glob('*.py')}
    return {'complete': not errors, 'errors': errors, 'pid': os.getpid(),
        'engine_version':u.SystemLibrary.get_engine_version(),
        'runtime_code_hashes':runtime_hashes,
        'project': str(PROJECT), 'level': current, 'stage_roots': roots,
        'actor_classes': dict(collections.Counter(a.get_class().get_name() for a in actors)),
        'mesh_components': len(rows), 'instanced_count': sum(r.get('instances', 0) for r in rows),
        'resolved_export_instances':len(manifest['scene'].get('instances',[]))-len(unresolved),
        'unresolved_prim_paths':unresolved,'geometry_mismatches':mismatches,
        'geometry_status':'needs_review' if mismatches else 'counts_match',
        'geometry_counts_match':not mismatches,
        'meshes': meshes, 'materials': materials, 'components': rows, 'missing_components': missing,
        'assets': asset_checks, 'export_stats': manifest['scene'].get('stats', {}),
        'source_packages_checked':len(report['assets'].get('source_packages',[])),
        'export_warnings': manifest['scene'].get('warnings', []),
        'gpu': __import__('mmd_bridge.process', fromlist=['gpu_memory_info']).gpu_memory_info()}
def tick(delta):
    if time.monotonic() < state['next']:
        return
    state['next'] = time.monotonic() + 1
    request_path = RUN / 'ue_audit_request.json'
    if not request_path.is_file() or u.AssetRegistryHelpers.get_asset_registry().is_loading_assets():
        return
    request = read_json(request_path)
    if request['id'] == state['last']:
        return
    result = {'id': request['id'], 'pid': os.getpid(), 'complete': False}
    exiting = request['kind'] in ('inspect_and_exit', 'close', 'reload')
    try:
        actual = Path(u.Paths.convert_relative_path_to_full(u.Paths.get_project_file_path())).resolve()
        if actual != PROJECT:
            raise RuntimeError('Acceptance audit project mismatch')
        if request['kind'] == 'reload' and state.get('loading') != request['id']:
            state['loading'] = request['id']
            report = read_json(Path(request['job']) / 'ue_report.json')
            if not u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level']):
                raise RuntimeError('Saved level could not be reopened')
            from mmd_bridge.ue_host import restore_scene_actor_labels
            from mmd_bridge.ue_appearance import restore_viewport_look
            restore_scene_actor_labels()
            restore_viewport_look(u.get_editor_subsystem(u.EditorActorSubsystem))
            state['next'] = time.monotonic() + 5
            return
        if request['kind'] != 'close':
            result.update(inspect(Path(request['job'])))
            if request['kind'] == 'reload':
                signature = (result['mesh_components'], result['instanced_count'], result['actor_classes'])
                if state.get('signature') != signature:
                    state['signature'], state['stable'] = signature, 0
                    return
                state['stable'] = state.get('stable', 0) + 1
                if state['stable'] < 3:
                    return
        else:
            result['complete'] = True
        if exiting and u.EditorLoadingAndSavingUtils.get_dirty_map_packages():
            if request.get('job'):
                report = read_json(Path(request['job']) / 'ue_report.json')
                world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
                if world.get_path_name().split('.')[0] != report['assets']['level']:
                    raise RuntimeError('Refusing to close an unrelated dirty map')
                if not u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level():
                    raise RuntimeError('Generated map save failed before restart')
            else:
                raise RuntimeError('Refusing to close a dirty map without task identity')
    except Exception:
        result.update(complete=False, error=traceback.format_exc())
        exiting = False
    state['last'] = request['id']
    atomic_json(Path(request['output']), result)
    atomic_json(RUN / 'ue_audit_response.json', result)
    if exiting:
        u.SystemLibrary.quit_editor()
runtime = sys.modules.setdefault('_mmdb_scene_acceptance_audit_runtime', types.ModuleType('_mmdb_scene_acceptance_audit_runtime'))
if getattr(runtime, 'tick', None) is not None:
    u.unregister_slate_post_tick_callback(runtime.tick)
TICK = u.register_slate_post_tick_callback(tick)
runtime.tick = TICK
atomic_json(RUN / 'ue_audit_ready.json', {'pid': os.getpid(), 'project': str(PROJECT)})
