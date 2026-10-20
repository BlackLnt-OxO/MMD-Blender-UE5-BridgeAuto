"""Versioned, explicit new-task geometry policy; legacy jobs remain unchanged."""
import hashlib
import json
from pathlib import Path
from .blender_contour import VERSION as CONTOUR_VERSION

VERSION = 2
TARGET_TYPES = ('skin', 'hosiery')
MODE_VERSION = 4
OVERLAY_VERSION = 1
MODES = {
    'original': '不添加修改器',
    'weld_weighted': '焊接＋加权法向',
    'local_subdivision': '焊接＋加权法向＋身体及贴身物一级细分（约2倍顶点）',
    'all_subdivision': '焊接＋加权法向＋全部件一级细分（约5倍顶点）',
}


def supported(cfg):
    return (cfg.get('character_backend') == 'mmdbridge_fbx'
            and cfg.get('character_material_profile') == 'character_pbr')


def _candidate_inputs(cfg, model, model_hash):
    """Only inputs that can change purpose or alpha-layer detection enter cache."""
    from .core import file_hash
    from .pmx_reader import read_pmx
    def relevant(value):
        return {k:v for k,v in value.items() if k in ('type','alpha_mode','textures') or
                (k=='params' and 'Opacity' in v)} | ({'params':{'Opacity':value['params']['Opacity']}}
                if 'Opacity' in value.get('params',{}) else {})
    overrides=cfg.get('character_material_overrides',{}).get(model_hash,{})
    source={}
    if model.is_file():
        try:
            relative=read_pmx(model).textures
        except ValueError:
            relative=[] # Unit fixtures may use a non-PMX identity file.
        for name in relative:
            path=(model.parent/name.replace('\\','/')).resolve()
            source[str(path)]=file_hash(path) if path.is_file() else 'missing'
    selected={key:relevant(value) for key,value in overrides.items()}
    for value in [*selected.values(),relevant(cfg.get('character_pbr_options',{}))]:
        for binding in value.get('textures',{}).values():
            path=Path(binding.get('path',''))
            if path.is_file():source[str(path.resolve())]=file_hash(path)
    inputs={'source_textures':source,'material_inputs':selected,
            'global_inputs':relevant(cfg.get('character_pbr_options',{}))}
    return hashlib.sha256(json.dumps(inputs,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def geometry_policy(cfg):
    mode = cfg.get('character_geometry_mode')
    if mode is not None and supported(cfg):
        if mode not in MODES:
            raise ValueError('未知角色几何处理方案')
        # Freeze the old interpreter: missing mode still produces its original
        # version-2 signature and never expands an old skin/hosiery selection.
        legacy = dict(cfg, character_geometry_mode=None,
                      character_subdivision_enabled=False, character_geometry_candidate_scope='')
        result = geometry_policy(legacy)
        result.update(version=MODE_VERSION, mode=mode,
                      subdivision_enabled=mode.endswith('_subdivision'),
                      level=1 if mode.endswith('_subdivision') else 0)
        result['overlay_repair'] = {
            'version': OVERLAY_VERSION, 'offset_m': .0001,
            'detection_inputs_sha256': _candidate_inputs(cfg, Path(cfg.get('model', '')), result['source_model_sha256']),
            'overrides': cfg.get('character_geometry_overrides', {}).get(result['source_model_sha256'], {})}
        if mode.endswith('_subdivision'):
            from .character_geometry_roles import EXTENDED_VERSION
            from .blender_surface_repair import VERSION as REPAIR_VERSION
            result['candidate'] = {
                'scope': 'local' if mode == 'local_subdivision' else 'all',
                'role_version': EXTENDED_VERSION, 'repair_version': REPAIR_VERSION,
                'detection_inputs_sha256': result['overlay_repair']['detection_inputs_sha256'],
                'overrides': result['overlay_repair']['overrides']}
        return result
    if ('character_subdivision_enabled' not in cfg
            or cfg.get('character_backend') != 'mmdbridge_fbx'
            or cfg.get('character_material_profile') != 'character_pbr'):
        return None
    enabled = cfg['character_subdivision_enabled']
    if type(enabled) is not bool:
        raise ValueError('character_subdivision_enabled must be boolean')
    model = Path(cfg.get('model', ''))
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest() if model.is_file() else ''
    # Other characters and purely material parameters do not change this mesh.
    overrides = cfg.get('character_material_overrides', {}).get(model_hash, {})
    overrides = json.dumps({k: v['type'] for k, v in overrides.items() if v.get('type', 'auto') != 'auto'},
                           sort_keys=True, ensure_ascii=False)
    result = {'version': VERSION, 'contour_version': CONTOUR_VERSION, 'subdivision_enabled': enabled, 'level': 1,
            'boundary_policy': 'protected', 'target_types': list(TARGET_TYPES),
            'weld_distance_m': .0001, 'weighted_mode': 'FACE_AREA', 'weighted_weight': 50,
            'keep_sharp': True, 'source_model_sha256': model_hash,
            'global_type': cfg.get('character_pbr_options', {}).get('type', 'auto'),
            'material_overrides_sha256': hashlib.sha256(overrides.encode()).hexdigest()}
    if cfg.get('character_geometry_candidate_scope'):
        from .character_geometry_roles import VERSION as ROLE_VERSION
        from .blender_surface_repair import VERSION as REPAIR_VERSION
        result['candidate'] = {'scope': cfg['character_geometry_candidate_scope'],
            'role_version': ROLE_VERSION, 'repair_version': REPAIR_VERSION,
            'detection_inputs_sha256':_candidate_inputs(cfg,model,model_hash),
            'overrides': cfg.get('character_geometry_overrides', {}).get(model_hash, {})}
    return result


def resolved_geometry_signature(receipt):
    if not receipt:
        return None
    policy=receipt.get('geometry_policy') or {}
    if policy.get('candidate'):
        payload={'scope':receipt['scope'],'roles':receipt['geometry_roles'],
                 'meshes':[{'targets':row['target_pmx_indices'],'attachments':row['attachments'],
                            'reviews':row['attachment_reviews']} for row in receipt['meshes']]}
    elif policy.get('overlay_repair'):
        overlay=receipt.get('overlay_repair')
        if overlay is None:return None
        payload={'version':overlay['version'],'offset_m':overlay.get('offset_m'),
                 'meshes':[{'attachments':row['attachments'],'reviews':row['attachment_reviews']}
                           for row in overlay['meshes']]}
    else:return None
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def candidate_review_items(receipt):
    if not receipt:
        return []
    policy=receipt.get('geometry_policy') or {}
    if not policy.get('candidate'):
        items=[];overlay=receipt.get('overlay_repair') or {}
        for row in overlay.get('meshes',[]):
            for entry in row.get('attachment_reviews',[]):
                items.append({'stage':'character_geometry','name':'原槽 '+str(entry['pmx_index']),'reason':entry['reason']})
            for entry in row.get('attachments',[]):
                if not entry.get('complete'):
                    items.append({'stage':'character_geometry','name':'原槽 '+str(entry['pmx_index']),
                                  'reason':entry.get('reason','贴层映射未通过')})
        if overlay.get('appearance_status') == 'not_reviewed':
            items.append({'stage':'character_geometry','name':'共面贴层外移','reason':'需要原生画面复核；数据导入和材质编译不等于外观通过'})
        return items
    items=[]
    for row in receipt.get('meshes',[]):
        for entry in row.get('attachment_reviews',[]):
            items.append({'stage':'character_geometry','name':'原槽 '+str(entry['pmx_index']),'reason':entry['reason']})
        for entry in row.get('attachments',[]):
            if not entry.get('complete'):
                items.append({'stage':'character_geometry','name':'原槽 '+str(entry['pmx_index']),
                              'reason':entry.get('reason','贴层映射未通过')})
    if receipt.get('scope')=='local':
        for entry in receipt.get('geometry_roles',[]):
            if entry.get('needs_review'):
                items.append({'stage':'character_geometry','name':entry['name'],'reason':'；'.join(entry['reasons'])})
    if receipt.get('appearance_status','not_reviewed') not in ('passed','not_applicable'):
        items.append({'stage':'character_geometry','name':'表面修复候选','reason':'需要原生画面复核；数据导入和材质编译不等于外观通过'})
    return items


def check_geometry_checkpoint(saved, cfg, receipt=None):
    if saved.get('geometry_policy') != geometry_policy(cfg):
        raise ValueError('角色几何设置或处理版本已变化，请新建任务；原还原点保留')
    policy=saved.get('geometry_policy') or {}
    if receipt is not None and (policy.get('candidate') or policy.get('overlay_repair')):
        signature=resolved_geometry_signature(receipt)
        if not signature or saved.get('geometry_resolved_sha256') != signature:
            raise ValueError('几何用途、目标槽或贴层映射记录已变化，请新建任务；原还原点保留')


def target_material_indices(material_names, records):
    by_name = {r['asset_name']: r.get('character_material', {}).get('pbr', {}).get('type') for r in records}
    return [i for i, name in enumerate(material_names) if by_name.get(name) in TARGET_TYPES]


def _prepare_character_geometry(meshes, records, cfg):
    policy = geometry_policy(cfg)
    if policy is None:
        return meshes, {'geometry_policy': None, 'mode': 'legacy_unchanged'}
    overlay = None
    if policy.get('mode') in ('original','weld_weighted'):
        from .blender_surface_repair import prepare_overlay_offsets
        meshes, overlay = prepare_overlay_offsets(meshes, records, cfg, policy)
    if policy.get('mode') == 'original':
        return meshes, {'geometry_policy': policy, 'mode': 'original',
                        'normal_finish': {'mode': 'none'}, 'topology_changed': False,
                        'overlay_repair': overlay}
    if policy.get('candidate'):
        from .blender_surface_repair import prepare_surface_candidate
        return prepare_surface_candidate(meshes, records, cfg, policy)
    from .blender_contour import prepare_contour
    from .blender_mesh_finish import prepare_comparison
    processed = []
    contours = []
    types = {r['asset_name']: r.get('character_material', {}).get('pbr', {}).get('type', 'generic') for r in records}
    for mesh in meshes:
        indices = target_material_indices([m.name if m else '' for m in mesh.data.materials], records)
        used = {p.material_index for p in mesh.data.polygons}
        indices = [i for i in indices if i in used]
        if policy['subdivision_enabled'] and indices:
            results, contour = prepare_contour([mesh], 'subdivision', indices, material_types=types)
        else:
            results, contour = [mesh], {'mode': 'none', 'reason': 'no_target' if not indices else 'disabled'}
        processed.extend(results)
        contour['experimental'] = False
        contours.append(contour)
    results, normal = prepare_comparison(processed, .0001)
    normal['experimental'] = False
    receipt = {'geometry_policy': policy, 'contours': contours, 'normal_finish': normal}
    if overlay is not None:receipt['overlay_repair'] = overlay
    return results, receipt


def mesh_counts(meshes):
    slots = {}; vertices = faces = triangles = 0
    for mesh in meshes:
        data = mesh.data
        vertices += len(data.vertices); faces += len(data.polygons)
        for polygon in data.polygons:
            triangles += max(0,len(polygon.vertices)-2)
            mat = data.materials[polygon.material_index] if polygon.material_index < len(data.materials) else None
            key = mat.name if mat else '<unassigned>'
            row = slots.setdefault((mesh.name,key), {'material':key,'vertices':set(),'triangles':0})
            row['vertices'].update(polygon.vertices); row['triangles'] += max(0,len(polygon.vertices)-2)
    return {'vertices':vertices,'faces':faces,'triangles':triangles,
            'parts':[dict(mesh=obj,material=name,vertices=len(row['vertices']),triangles=row['triangles'])
                     for (obj,name),row in slots.items()]}


def prepare_character_geometry(meshes, records, cfg):
    before = mesh_counts(meshes)
    results, receipt = _prepare_character_geometry(meshes, records, cfg)
    after = mesh_counts(results)
    receipt['counts'] = {'before':before,'after':after,
                         'vertex_ratio':after['vertices']/max(1,before['vertices']),
                         'triangle_ratio':after['triangles']/max(1,before['triangles'])}
    return results, receipt
