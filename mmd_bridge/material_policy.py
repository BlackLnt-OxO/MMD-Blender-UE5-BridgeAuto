"""Deterministic scene material policy; independent of Blender and Unreal."""
import math


def classify_surface(size_m, uv_span=(1, 1), mapping_scale=(1, 1), override="auto", planar=False):
    if override not in ("auto", "small", "tiled"):
        raise ValueError("mmd_bridge_surface_class 必须为 auto/small/tiled")
    dimensions = sorted(abs(float(v)) for v in size_m)
    if len(dimensions) != 3 or not all(math.isfinite(v) for v in dimensions):
        raise ValueError("无效的求值几何尺寸")
    repeat = max(abs(float(a) * float(b)) for a, b in zip(uv_span, mapping_scale))
    area = dimensions[1] * dimensions[2]
    large = dimensions[2] >= 4.0 or area >= 8.0
    tiled = large and (planar or (repeat > 1.25 and dimensions[0] <= dimensions[2] * 0.2))
    category = override if override != "auto" else "tiled" if tiled else "small"
    return {"category": category, "requested": override, "size_m": list(size_m),
            "uv_repeat": repeat, "planar": bool(planar),
            "reason": "对象显式覆盖" if override != "auto" else
                      "大面积且具有平铺坐标或平面表面" if tiled else "独立物件采用 PBR 通道烘焙",
            "strategy": "hybrid" if category == "tiled" else "bake"}


def projected_uv(point, low, high, axes, padding=0.02):
    return tuple(padding + (1 - 2 * padding) * (point[i] - low[i]) / max(high[i] - low[i], 1e-9)
                 for i in axes)
