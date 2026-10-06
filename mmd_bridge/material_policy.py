"""Deterministic scene material policy; independent of Blender and Unreal."""
import math
import re

MATERIAL_ANALYSIS_REVISION = 20


def scene_material_route(*, simple, repeat_count=1, category='small', transmitting=False):
    """Automatic scene policy; repetition is actual shared-material/instance usage."""
    if transmitting:
        return {'route':'hybrid','reason':'透光表面先尝试节点还原；失败或已确认外观不可靠时允许不透明 PBR 降级'}
    if simple:
        return {'route':'nodes','reason':'简单计算或已适配的字段直接生成材质节点'}
    if repeat_count >= 8:
        return {'route':'hybrid','reason':'重复使用至少 8 次，优先节点和共享字段缓存，避免逐实例烘焙'}
    return {'route':'pbr_bake','reason':'少量使用的复杂表面优先完整 PBR 烘焙',
            'layout':'chunked_object_atlas' if category=='chunked' else 'whole_object_atlas'}


def prefer_basic_pbr_bake(failure):
    """Escalate unresolved surface failures or conversion deadlines to PBR."""
    return failure.get('scope') in ('complete_surface', 'ue_material_compile', 'conversion_timeout')


def basic_pbr_retry_sources(records, report, source):
    """Use reported compiler failures/deadlines, never node-count/name rules."""
    from .core import stable_id
    failed_ids,failed_names = set(),set()
    for item in report.get('failures',[]):
        if item.get('stage')=='material' and any(reason in item.get('error','') for reason in ('原生材质编译失败', '原生材质转换超时', '原生材质转换失败')):
            if item.get('material_id'):failed_ids.add(item['material_id'])
            elif item.get('name'):failed_names.add(item['name'])
    routes = report.get('material_routes',{})
    lines = report.get('checks',{}).get('material_compile_errors',[])
    result = {}
    visual=report.get('checks',{}).get('material_visual_findings',{})
    for record in records:
        if record.get('pmx_index') is not None or record.get('basic_pbr_bake') or record.get('strategy_requested')=='nodes':continue
        parent = routes.get(record['id'],{}).get('parent','')
        parent_path=parent.split('.')[0]
        parent_failed=bool(parent_path and any(re.search(re.escape(parent_path)+r'(?=[.\s\'\":,;)]|$)',line) for line in lines))
        visual_failed=visual.get(record['id'],{}).get('result')=='appearance_mismatch'
        transmitting=bool(record.get('transmission_candidate') or 'transmission' in record.get('closure_features',[])
            or record.get('surface')=='glass' or record.get('thin_surface_transmission'))
        matched = record['id'] in failed_ids or record['name'] in failed_names or parent_failed or (visual_failed and transmitting)
        if not matched:continue
        reason = '透光表面外观已确认不可靠，降级为不透明 PBR' if visual_failed and transmitting else 'UE 完整材质转换／编译失败或超时；先保留失败报告，再烘焙 PBR 贴图'
        aliases = set(record.get('source_aliases',[])) | {record.get('source_name') or record['name']}
        for name in aliases:result[stable_id(source,name)]={'scope':'visual_transmission' if visual_failed else 'ue_material_compile','reason':reason,'source_name':name,'failed_record_id':record['id'],'opaque_fallback':transmitting}
    return result


def texture_role(image, *, normal=False, data=False):
    if normal:
        return "normal"
    if data:
        return "mask"
    if image.get("color_space") in ("Non-Color", "Linear", "Linear Rec.709"):
        return "linear_color"
    return "srgb"


def cutout_transmission(record):
    """Use thin-surface scattering only with mesh evidence and independent coverage."""
    channels = record.get("channels", {})
    alpha, transmission = channels.get("opacity"), channels.get("transmission")
    policy = record.get("surface_policy") or {}
    dimensions = sorted(abs(float(v)) for v in policy.get("size_m", []))
    # A tree's combined bounds are volumetric even though each leaf is a card.
    # An actual Translucent BSDF is evidence of a thin transmitting surface.
    thin = (record.get('thin_surface_transmission', False) or
            policy.get('mesh_surface', {}).get('thin_open_surface', False) or policy.get("planar", False) or
            (len(dimensions) == 3 and dimensions[0] <= dimensions[1] * .2))
    return bool(record.get("surface") == "pbr" and not record.get("water") and
                thin and alpha and alpha.get("op") != "constant" and transmission and
                (transmission.get("op") != "constant" or float(transmission.get("value", 0)) > 1e-4))


def glass_refraction_mode(record):
    """Screen-space IOR is unsuitable for a broad thin pane; retain IOR in its BSDF."""
    policy = record.get('surface_policy') or {}
    dimensions = sorted(abs(float(v)) for v in policy.get('size_m', []))
    pane = (len(dimensions) == 3 and dimensions[1] > 0 and dimensions[2] >= 1 and
            dimensions[0] <= dimensions[1] * .05)
    return 'pixel_normal_offset' if pane else 'index_of_refraction'


def classify_surface(size_m, uv_span=(1, 1), mapping_scale=(1, 1), override="auto", planar=False):
    if override not in ("auto", "small", "tiled", "chunked"):
        raise ValueError("mmd_bridge_surface_class 必须为 auto/small/tiled/chunked")
    dimensions = sorted(abs(float(v)) for v in size_m)
    if len(dimensions) != 3 or not all(math.isfinite(v) for v in dimensions):
        raise ValueError("无效的求值几何尺寸")
    repeat = max(abs(float(a) * float(b)) for a, b in zip(uv_span, mapping_scale))
    area = dimensions[1] * dimensions[2]
    large = dimensions[2] >= 4.0 or area >= 8.0
    tiled = large and (planar or (repeat > 1.25 and dimensions[0] <= dimensions[2] * 0.2))
    chunked = (large and not tiled and not planar and dimensions[2] >= 8.0 and
               dimensions[1] >= 3.0 and dimensions[2] >= 3.0 * dimensions[0])
    category = override if override != "auto" else "tiled" if tiled else "chunked" if chunked else "small"
    return {"category": category, "requested": override, "size_m": list(size_m),
            'bake_category':'chunked' if dimensions[2]>=8 or area>=64 else 'small',
            "uv_repeat": repeat, "planar": bool(planar),
            "reason": "对象显式覆盖" if override != "auto" else
                      "大面积且具有平铺坐标或平面表面" if category == "tiled" else
                      "大型立体物件分块烘焙" if category == "chunked" else "独立物件优先生成 PBR 节点，仅烘焙不兼容字段",
            "strategy": "hybrid" if category == "tiled" else "bake"}


def projected_uv(point, low, high, axes, padding=0.02):
    return tuple(padding + (1 - 2 * padding) * (point[i] - low[i]) / max(high[i] - low[i], 1e-9)
                 for i in axes)
