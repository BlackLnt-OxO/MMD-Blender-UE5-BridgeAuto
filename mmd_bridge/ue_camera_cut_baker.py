# -*- coding: utf-8 -*-
"""
ue_camera_cut_baker.py — 由 MMD 相机动画拆分 / 烘焙真正的 Camera Cuts（UE5 Editor Python）

作者：ZCode（独立交付，不修改也不依赖 MMD_Blender_UE5_Autowork 内的任何文件）

用途
----
原始 MMD 相机动画里存在大量"硬切镜头"（699 = 构图A，700 = 构图B），但它是同一条连续
曲线。MRQ 打开 Temporal Sample Count > 1 时会对子帧求值，于是采到 699→700 之间的高速
插值，把两个机位累积进同一张图，产生重影。

本脚本把一条 MMD 相机序列拆成真正的 Camera Cuts：
  * 每镜生成一台独立 CineCameraActor（AUTO_Cam_Shot_XXX）
  * 逐帧（可含子帧）从**已求值**的源 MMD 相机读取世界 Transform 与镜头参数，烘焙到新相机
  * 每镜首帧与末段 key 强制 Constant/Hold，且 section 严格结束在 Cut 帧之前 → Frame Close / Open 都不会跨切点插值
  * 重建唯一的 MovieSceneCameraCutTrack，section 严格首尾相接 [0,700) [700,1260) ...

四个模式
--------
  MODE = "probe"   符号体检：逐个校验本脚本用到的 UE Python API 是否真实存在，不写任何数据
  MODE = "detect"  第二阶段自动检测 Cut（只读）：按局部中位数比值找疑似硬切，只输出候选
  MODE = "bake"    真正的拆分 + 烘焙 + 建 Camera Cuts（默认）
  MODE = "verify"   对已有结果做验收：结构断言 + 子帧 Hold 数值证明

运行方式
--------
  A) 先在 UE Sequencer 中打开目标 Level Sequence，再执行：Tools → Execute Python Script → 选本文件
  B) 无头：UnrealEditor-Cmd.exe <project>.uproject -run=pythonscript -script="<本文件>"
  外部 JSON 覆盖脚本内常量：设置环境变量 CAMCUT_CONFIG=<json 路径>（见 config.example.json）
  阻止自动执行（把它当模块导入时）：设置环境变量 CAMCUT_NO_AUTORUN=1

本文件结构（§1 为纯逻辑，不触碰 unreal，可离线单测；见 tests/test_cut_logic.py）
  §0 配置区
  §1 纯逻辑：镜头切分 / 采样时间表 / Hold 规则 / Rotator 解缠 / 检测算法
  §2 UE 适配层：枚举与属性容错访问
  §3 求值基础设施：打开序列 + 同步跳到任意子帧 + 读取源相机最终状态
  §4 幂等清理 / 备份
  §5 烘焙：建相机 + 9 通道 Transform + 镜头参数 + Hold 收尾
  §6 建 Camera Cuts
  §7 验收 verify
  §8 检测 detect
  §9 main 调度
"""

from __future__ import annotations

import datetime
import json
import math
import os
import traceback
from collections import namedtuple

try:
    import unreal
except ImportError:  # 离线单测环境；§1 纯逻辑不依赖 unreal
    unreal = None


# ============================================================================
# §0 配置区（题面要求的常量名全部保留；可用 CAMCUT_CONFIG 指向的 JSON 覆盖）
# ============================================================================

MODE = "bake"                      # "probe" | "detect" | "bake" | "verify"

# --- 输入 ---
# 兼容旧配置/报告保留；运行时优先使用当前已打开的 Level Sequence。
# 没有打开序列时脚本会直接提示并中止，不会静默读取这里的旧路径。
SOURCE_SEQUENCE = "/Game/MMDBridgeFinal/A_e87a49048496c3b5/Sequences/LS_MMD"
SOURCE_CAMERA = "MMD_Camera"     # 源相机：先按关卡 actor 标签匹配，再按 actor 名，再按 binding 名
# 源序列实际所在的关卡。脚本会在打开 Sequencer 前切到这个关卡，避免把生成相机
# 生到 Accepted/其他当前关卡，导致相机世界坐标与 MRQ 映射关卡不一致。
SOURCE_LEVEL = "/Game/MMDBridgeFinal/A_e87a49048496c3b5/Maps/MMD_Preview"

SEQUENCE_START = 0
# None = 用序列自身的播放结束帧（推荐，避免尾段没有相机覆盖）。
# 显式给整数时为**排他**上界：SEQUENCE_END = 3600 表示最后一帧是 3599。
SEQUENCE_END = None

# Cut = 700 表示：699 及以前属于前一镜，700 及以后属于下一镜。
# 镜头范围统一 [start, end)。
CUT_FRAMES = [
    13, 59, 87, 185, 210, 233, 263, 389, 447, 481, 512, 639, 700,
    730, 762, 772, 798, 824, 835, 858, 890, 945, 1015, 1082, 1124,
    1143, 1151, 1184, 1227, 1238, 1259, 1318, 1390, 1437, 1453, 1468,
    1479, 1515, 1645, 1703, 1725, 1763, 1828, 1853, 1890, 1953, 1975,
    1998, 2047, 2056, 2108, 2120, 2170, 2230, 2264, 2295, 2314, 2357,
    2371, 2428, 2485, 2547, 2557, 2583, 2609, 2620, 2643, 2675, 2730,
    2800, 2846, 2867, 2909, 2928, 2936, 2969, 3012, 3024, 3044, 3103,
    3175, 3222, 3238, 3253, 3264, 3300, 3430, 3488, 3510, 3550, 3597,
    3610, 3627, 3670, 3691, 3738, 3774, 3800, 3858, 3879, 3891, 3911,
    3941, 3973, 3985,
]

# --- 烘焙参数 ---
# 1 = 每整数帧一个 key（第一版，最稳）。
# 4 = 每帧 4 个子样本（0/.25/.5/.75），正常镜头内部保留原始子帧曲线；
#     Cut 前最后一帧的 699.25/699.5/699.75 会被强制为 699.0 的值（禁止 A→B 插值）。
BAKE_SUBSAMPLES = 1

DO_BAKE_LENS = True              # 烘焙 CurrentFocalLength / CurrentAperture
# 内部 key 的插值：'linear'（默认，逐帧采样的忠实表示，无过冲）| 'auto' | 'cubic'
KEY_INTERPOLATION = "linear"

# --- 输出控制 ---
GENERATED_PREFIX = "AUTO_Cam_Shot_"      # 生成物统一前缀，重复运行据此清理
GENERATED_TAG = "ZCodeCameraCutBaker"    # 生成 actor 的 tag，双保险识别
USE_SPAWNABLE = False             # False = Possessable（默认，稳定）；True = Spawnable（见 README）
# 镜头轨道写法：'auto' = possessable 用组件 possessable（已验证路径），spawnable 用 actor 属性路径
LENS_TRACK_MODE = "auto"          # 'auto' | 'component_possessable' | 'actor_property_path'

IN_PLACE = True                   # True = 直接改 SOURCE_SEQUENCE；False = 先复制成 <name>_Cut 再改副本
MAKE_BACKUP = True                 # 改之前把 LevelSequence 资产复制一份到 _CutBakerBackups/
BACKUP_FOLDER_NAME = "_CutBakerBackups"
SAVE_SEQUENCE = True              # 保存序列资产
SAVE_LEVEL = True                 # 生成相机是 level actor；bake 完成后保存关卡
VERIFY_AFTER_BAKE = True          # bake 结束后自动跑一遍验收
VERIFY_TOLERANCE_CM = 1e-4        # 子帧 Hold 数值证明的容差（厘米；严格应为 0）
VERIFY_TOLERANCE_DEG = 1e-4       # 源相机与生成相机朝向对照容差（度）

# --- 检测（MODE = "detect"，只读）---
DETECT_WINDOW = 30                # 局部中位数窗口：±N 帧
DETECT_ABS_POS_CM = 40.0          # 绝对阈值，避免静止相机的浮点抖动
DETECT_ABS_ROT_DEG = 25.0
DETECT_ABS_FOCAL_MM = 15.0
DETECT_REL_POS = 8.0              # 相对阈值：delta / median(邻域 delta)
DETECT_REL_ROT = 8.0
DETECT_REL_FOCAL = 8.0
DETECT_SUPPRESS = 2               # 非极大值抑制半径（帧），避免一次硬切报出多个相邻候选
DETECT_SAMPLE_STEP = 1            # 求值步长；>1 可加速但可能漏切

# --- 其它 ---
ALLOW_OTHER_ENGINE_VERSIONS = False   # 只接受 5.7.x（本脚本的 API 依据是 5.7 源码）
REPORT_DIR = None                 # None = <本脚本目录>/runs
LOG_PREFIX = "[CameraCutAuto]"
PROGRESS_EVERY = 200              # 每 N 帧打一次进度

CONFIG_KEYS = (
    "MODE", "SOURCE_SEQUENCE", "SOURCE_CAMERA", "SOURCE_LEVEL", "SEQUENCE_START", "SEQUENCE_END", "CUT_FRAMES",
    "BAKE_SUBSAMPLES", "DO_BAKE_LENS", "KEY_INTERPOLATION", "GENERATED_PREFIX", "GENERATED_TAG",
    "USE_SPAWNABLE", "LENS_TRACK_MODE", "IN_PLACE", "MAKE_BACKUP", "BACKUP_FOLDER_NAME",
    "SAVE_SEQUENCE", "SAVE_LEVEL", "VERIFY_AFTER_BAKE", "VERIFY_TOLERANCE_CM", "VERIFY_TOLERANCE_DEG",
    "DETECT_WINDOW", "DETECT_ABS_POS_CM", "DETECT_ABS_ROT_DEG", "DETECT_ABS_FOCAL_MM",
    "DETECT_REL_POS", "DETECT_REL_ROT", "DETECT_REL_FOCAL", "DETECT_SUPPRESS",
    "DETECT_SAMPLE_STEP", "ALLOW_OTHER_ENGINE_VERSIONS", "REPORT_DIR", "PROGRESS_EVERY",
)


# ============================================================================
# §1 纯逻辑（不触碰 unreal，可离线单测）
# ============================================================================

Shot = namedtuple("Shot", "index start end")                      # 范围 [start, end)
Sample = namedtuple("Sample", "index frame sub read_frame read_sub hold")

TRANSFORM_CHANNELS = (
    "Location.X", "Location.Y", "Location.Z",
    "Rotation.X", "Rotation.Y", "Rotation.Z",   # 注意：X=Roll, Y=Pitch, Z=Yaw
    "Scale.X", "Scale.Y", "Scale.Z",
)


class ConfigError(Exception):
    """输入配置不合法（Cut 帧越界 / 不递增 / 序列范围非法等）。"""


def plan_shots(cut_frames, start_frame, end_frame):
    """把整条序列按 Cut 帧切成若干镜头，返回 [Shot]，每个 Shot 的范围是 [start, end)。

    Cut = F 的语义：F 及以后属于下一镜，F-1 及以前属于上一镜。
    """
    if not isinstance(start_frame, int) or not isinstance(end_frame, int):
        raise ConfigError("SEQUENCE_START / SEQUENCE_END 必须是整数（SEQUENCE_END 为排他上界）")
    if end_frame <= start_frame:
        raise ConfigError(f"序列范围非法：start={start_frame} end={end_frame}（要求 end > start）")
    prev = None
    cuts = []
    for item in cut_frames or []:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"Cut 帧必须是整数：{item!r}")
        if not (start_frame < item < end_frame):
            raise ConfigError(
                f"Cut 帧 {item} 越界：必须落在 ({start_frame}, {end_frame}) 开区间内")
        if prev is not None and item <= prev:
            raise ConfigError(f"Cut 帧必须严格递增：{prev} → {item}")
        prev = item
        cuts.append(item)
    bounds = [start_frame] + cuts + [end_frame]
    return [Shot(i + 1, bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def sample_times(shot_start, shot_end, subsamples):
    """生成一镜内所有采样点（含 Cut 边界的强制 Hold 规则）。

    采样时间 t = shot_start + i/S，i ∈ [0, (end-start)*S)。最大 t = end - 1/S，
    所以**永远不会采到 end**（end 属于下一镜）。

    强制 Hold：t 所属整帧 >= end-1 的采样，一律改为读取 (end-1, 0.0) 的值。
      S=1：末尾那一个 key（就是 end-1 本身）天然等于该值。
      S>1：699.0 / 699.25 / 699.5 / 699.75 全部 = 699.0 的求值结果。
    这些采样同时会被标记 hold=True，烘焙时给它的 key 用 Constant 插值，杜绝过冲。
    """
    if subsamples < 1:
        raise ConfigError(f"BAKE_SUBSAMPLES 必须 >= 1，当前 {subsamples}")
    sub_count = int(subsamples)
    total = (shot_end - shot_start) * sub_count
    hold_frame = shot_end - 1
    out = []
    for i in range(total):
        frame = shot_start + i // sub_count
        sub = (i % sub_count) / float(sub_count)
        if frame >= hold_frame:
            out.append(Sample(i, frame, sub, hold_frame, 0.0, True))
        else:
            out.append(Sample(i, frame, sub, frame, sub, False))
    return out


def unwrap_rotator(prev, cur):
    """把 cur 的每个分量平移到离 prev 最近的一圈，返回新的三元组。

    否则 yaw 从 359° 跳到 1° 会被烘焙成反向扫 358°，快速运镜场景尤其明显。
    """
    cur = tuple(float(v) for v in cur)
    if prev is None:
        return cur
    out = []
    for p, c in zip(prev, cur):
        delta = c - p
        delta -= 360.0 * round(delta / 360.0)
        out.append(p + delta)
    return tuple(out)


def rotator_delta(a, b):
    """两个已解缠 Rotator 的差值（度，取各分量绝对值最大）。"""
    return max(abs(b[i] - a[i]) for i in range(3))


def vector_delta(a, b):
    """两个三维点的欧氏距离。"""
    return math.dist(a, b)


def median(values):
    """中位数（不依赖 statistics，便于对照测试）。"""
    data = sorted(values)
    n = len(data)
    if n == 0:
        return 0.0
    mid = n // 2
    return data[mid] if n % 2 else (data[mid - 1] + data[mid]) / 2.0


def window_median_ratios(deltas, window):
    """对每个 delta 求 delta / median(邻域 delta)（邻域不含自身）。

    静止相机的浮点抖动会让 median 变成 0 或极小值，此时返回一个大而有限的分数，
    避免 inf 破坏 JSON 序列化。
    """
    ratios = []
    count = len(deltas)
    for i, value in enumerate(deltas):
        lo = max(0, i - window)
        hi = min(count, i + window + 1)
        neighbours = [deltas[j] for j in range(lo, hi) if j != i]
        base = median(neighbours)
        if base > 1e-9:
            ratios.append(value / base)
        else:
            ratios.append(1e9 if value > 1e-9 else 0.0)
    return ratios


def detect_candidates(pos_deltas, rot_deltas, focal_deltas, first_transition_frame, cfg):
    """按"绝对阈值 + 局部中位数比值"找疑似硬切帧，返回 [(frame, score, detail)]。

    pos_deltas[i] 表示 帧(first+i) → 帧(first+i+1) 的变化量，所以候选帧号 = first + i + 1。
    """
    window = cfg["DETECT_WINDOW"]
    pos_ratio = window_median_ratios(pos_deltas, window)
    rot_ratio = window_median_ratios(rot_deltas, window)
    focal_ratio = window_median_ratios(focal_deltas, window)

    hits = []
    for i in range(len(pos_deltas)):
        reasons = []
        if pos_deltas[i] > cfg["DETECT_ABS_POS_CM"] and pos_ratio[i] > cfg["DETECT_REL_POS"]:
            reasons.append("position")
        if rot_deltas[i] > cfg["DETECT_ABS_ROT_DEG"] and rot_ratio[i] > cfg["DETECT_REL_ROT"]:
            reasons.append("rotation")
        if focal_deltas[i] > cfg["DETECT_ABS_FOCAL_MM"] and focal_ratio[i] > cfg["DETECT_REL_FOCAL"]:
            reasons.append("focal")
        if reasons:
            score = max(pos_ratio[i], rot_ratio[i], focal_ratio[i])
            hits.append((first_transition_frame + i + 1, score, {
                "position_delta_cm": pos_deltas[i], "position_ratio": pos_ratio[i],
                "rotation_delta_deg": rot_deltas[i], "rotation_ratio": rot_ratio[i],
                "focal_delta_mm": focal_deltas[i], "focal_ratio": focal_ratio[i],
                "reasons": reasons,
            }))

    # 非极大值抑制：一次硬切常在相邻 2~3 帧都被判为异常，只保留最强的那个。
    suppress = cfg["DETECT_SUPPRESS"]
    kept = []
    for frame, score, detail in sorted(hits, key=lambda item: -item[1]):
        if all(abs(frame - other[0]) > suppress for other in kept):
            kept.append((frame, score, detail))
    return sorted(kept, key=lambda item: item[0])


def tick_span_per_frame(tick_resolution, display_rate):
    """一个显示帧对应多少个 tick（用分数表示，避免浮点）。"""
    return int(tick_resolution[0]) * int(display_rate[1]), int(tick_resolution[1]) * int(display_rate[0])


def subframe_is_representable(tick_resolution, display_rate, subsamples):
    """检查 1/S 显示帧能否用整数 tick 精确表示（24000/30 = 800 tick/帧，S=4/8 可以，S=3 不行）。"""
    if subsamples <= 1:
        return True
    span_num, span_den = tick_span_per_frame(tick_resolution, display_rate)
    return (span_num % (span_den * int(subsamples))) == 0


def ticks_for_time(frame, sub, tick_resolution, display_rate):
    """把显示帧时间 (frame + sub) 换算成整数 tick（用于 add_key 的 TICK_RESOLUTION 形式）。"""
    span_num, span_den = tick_span_per_frame(tick_resolution, display_rate)
    return int(round((frame + sub) * span_num / float(span_den)))


def seconds_for_frame(frame, sub, display_rate):
    """显示帧时间 → 秒。set_global_position 用秒注入小数帧（源码 PlaybackPosition = Time*DisplayRate）。"""
    return (frame + sub) * display_rate[1] / float(display_rate[0])


def focal_from_fov(fov_deg, sensor_width_mm):
    """由水平 FOV 与传感器宽度反算焦距（mm）。源相机不是 CineCamera 时的降级路径。"""
    if sensor_width_mm <= 0 or fov_deg <= 0.0 or fov_deg >= 180.0:
        return None
    return (sensor_width_mm / 2.0) / math.tan(math.radians(fov_deg) / 2.0)


# ============================================================================
# §2 UE 适配层：容错访问（枚举成员名在不同小版本可能不同）
# ============================================================================

_ADD_KEY_FORM = None      # 记住 add_key 可用的调用形式，避免每帧反复试错
_INTERP_KWARG_OK = None   # add_key 是否接受 interpolation= 关键字
# 实测会丢子帧的形式；前若干个带子帧的 key 会读回校验真实时间，发现丢帧就永久拉黑该形式
_SUBFRAME_BAD_FORMS = set()
_SUBFRAME_VERIFY_LEFT = 6
_LOGGED_ENUMS = set()
_ENUM_CACHE = {}          # 枚举解析结果缓存：求值热路径上每写一个 key 都要查两次，不能每次 dir() 一遍


def log(message):
    if unreal is None:
        print(f"{LOG_PREFIX} {message}")
    else:
        unreal.log(f"{LOG_PREFIX} {message}")
        # UE's Python logger goes to the Output Log, not necessarily to
        # sys.stdout. The desktop bridge tees stdout into the task log, so
        # mirror the message there as well.
        print(f"{LOG_PREFIX} {message}")


def warn(message):
    if unreal is None:
        print(f"{LOG_PREFIX} WARN {message}")
    else:
        unreal.log_warning(f"{LOG_PREFIX} {message}")
        print(f"{LOG_PREFIX} WARN {message}")


def fail(message):
    if unreal is None:
        print(f"{LOG_PREFIX} ERROR {message}")
    else:
        unreal.log_error(f"{LOG_PREFIX} {message}")
        print(f"{LOG_PREFIX} ERROR {message}")


def enum_member(enum_name, *candidates):
    """按名字取 unreal 枚举成员，兼容不同小版本的成员拼写（RCIM_Constant / RCIM_CONSTANT ...）。

    取不到时返回 None 并只提示一次，让调用方走降级路径。结果会缓存 —— 这个函数在
    逐帧写 key 的热路径上，每次 dir() 扫一遍枚举会明显拖慢整条序列的烘焙。
    """
    if unreal is None:
        return None
    cache_key = (enum_name, candidates)
    if cache_key in _ENUM_CACHE:
        return _ENUM_CACHE[cache_key]
    resolved = _resolve_enum_member(enum_name, candidates)
    _ENUM_CACHE[cache_key] = resolved
    return resolved


def _resolve_enum_member(enum_name, candidates):
    enum = getattr(unreal, enum_name, None)
    if enum is None:
        if enum_name.startswith("E"):
            enum = getattr(unreal, enum_name[1:], None)
        else:
            enum = getattr(unreal, "E" + enum_name, None)
    if enum is None:
        if enum_name not in _LOGGED_ENUMS:
            _LOGGED_ENUMS.add(enum_name)
            warn(f"unreal.{enum_name} 不存在（该小版本可能未导出此枚举），改走降级路径")
        return None
    members = [a for a in dir(enum) if not a.startswith("_")]

    def normalise(text):
        return text.replace("_", "").upper()

    for candidate in candidates:
        if candidate in members:
            return getattr(enum, candidate)
    for candidate in candidates:
        target = normalise(candidate)
        for member in members:
            if normalise(member) == target:
                return getattr(enum, member)
    if enum_name not in _LOGGED_ENUMS:
        _LOGGED_ENUMS.add(enum_name)
        warn(f"unreal.{enum_name} 里找不到 {candidates}，可用成员：{sorted(members)}")
    return None


def prop(obj, name, default=None):
    """读属性：先 get_editor_property，再普通属性，都没有就返回 default。"""
    if obj is None:
        return default
    try:
        return obj.get_editor_property(name)
    except Exception:
        pass
    try:
        return getattr(obj, name)
    except Exception:
        return default


def set_prop(obj, name, value):
    """写属性，成功返回 True。"""
    if obj is None:
        return False
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception:
        pass
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def frame_number(value):
    return unreal.FrameNumber(int(value))


def rotator_degrees(rotation):
    """把 UE Rotator 或 Quaternion 转成 (roll, pitch, yaw)。

    ``Transform.rotation`` 在 UE Python 中通常是 ``unreal.Quat``，不是 Rotator。
    以前直接读取 roll/pitch/yaw，失败后静默得到 0，导致烘焙相机丢失朝向。
    """
    missing = object()
    roll = prop(rotation, "roll", missing)
    pitch = prop(rotation, "pitch", missing)
    yaw = prop(rotation, "yaw", missing)
    if missing not in (roll, pitch, yaw):
        return float(roll), float(pitch), float(yaw)

    # UE 的 Quat Python wrapper 提供 rotator() 时优先用引擎自己的转换，
    # 避免不同版本对欧拉角奇异点和轴顺序的差异。
    try:
        to_rotator = getattr(rotation, "rotator", None)
        if callable(to_rotator):
            converted = to_rotator()
            converted_roll = prop(converted, "roll", missing)
            converted_pitch = prop(converted, "pitch", missing)
            converted_yaw = prop(converted, "yaw", missing)
            if missing not in (converted_roll, converted_pitch, converted_yaw):
                return float(converted_roll), float(converted_pitch), float(converted_yaw)
    except Exception:
        pass

    # 无 rotator() 时的标准 UE 四元数(x,y,z,w)降级转换。
    x = prop(rotation, "x", missing)
    y = prop(rotation, "y", missing)
    z = prop(rotation, "z", missing)
    w = prop(rotation, "w", missing)
    if missing in (x, y, z, w):
        raise ConfigError(
            f"无法把 UE Transform.rotation 转成欧拉角：收到 {type(rotation).__name__}，"
            "既没有 roll/pitch/yaw，也没有可用的 quaternion x/y/z/w")

    import math
    x, y, z, w = map(float, (x, y, z, w))
    sin_roll = 2.0 * (w * x + y * z)
    cos_roll = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sin_roll, cos_roll)
    sin_pitch = 2.0 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2.0, sin_pitch) if abs(sin_pitch) >= 1.0 else math.asin(sin_pitch)
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(sin_yaw, cos_yaw)
    return tuple(math.degrees(value) for value in (roll, pitch, yaw))


# ============================================================================
# §3 求值基础设施
# ============================================================================

class Context:
    """一次运行的全部可变状态（序列 / 求值器 / 生成物登记 / 报告）。"""

    def __init__(self, config):
        self.cfg = config
        self.sequence = None
        self.display_rate = (30, 1)
        self.tick_resolution = (24000, 1)
        self.evaluator = None
        self.source_actor = None
        self.source_binding = None
        self.source_profile = {}
        self.generated = []          # [{"index", "name", "binding", "actor", "component", "channels", "shot"}]
        self.warnings = []
        self.notes = {}
        self.baked_lens_check = []   # [(shot_index, frame, sub, expected_focal)]

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
        warn(message)


class Evaluator:
    """把一个 LevelSequence 同步求值到任意（可含子帧的）时间点。

    这是本脚本唯一的时间驱动入口，依据（UE 5.7 源码）：
      LevelSequenceEditorBlueprintLibrary.set_global_position
        → FSequencer::SetGlobalTime → EvalRange = PlayPosition.JumpTo(NewTime)（不扫掠、不触发中间事件）
        → EvaluateInternal → FMovieSceneRootEvaluationTemplateInstance::EvaluateSynchronousBlocking
        → FMovieSceneEntitySystemRunner::Flush()
    整条链路在同一个调用栈内完成，所以"跳到帧 → 立刻读 Transform"不会读到上一帧缓存。
    """

    def __init__(self, context):
        self.ctx = context
        # 5.5 起"实例化 BlueprintFunctionLibrary"已弃用（真实 5.7.4 上实测会打 DeprecationWarning），
        # 官方建议直接调类方法；类方法直调已在真实 5.7.4 上验证可用。
        self.lse = getattr(unreal, "LevelSequenceEditorBlueprintLibrary", None)
        if self.lse is None:
            raise RuntimeError(
                "unreal.LevelSequenceEditorBlueprintLibrary 不存在（LevelSequenceEditor 插件未启用？）")
        self.can_subframe = hasattr(self.lse, "set_global_position")
        self.display_rate = context.display_rate
        self.time_unit = enum_member("MovieSceneTimeUnit", "DisplayRate", "DISPLAY_RATE")
        self.position_type = enum_member("MovieScenePositionType", "Time", "TIME")
        self.frame_position_type = enum_member("MovieScenePositionType", "Frame", "FRAME")
        self.jump_method = enum_member("UpdatePositionMethod", "Jump", "JUMP")
        self._last_seconds = None
        self._last_state = None
        self._count = 0
        # 帧形式（FrameNumber + sub_frame）在显示帧上精确；秒形式有 float32 带来的
        # ~2e-5 帧误差。两种都已验证可用，默认用精确的那种，失败自动回退。
        self.use_frame_params = None

    def warm_up(self):
        """结构性改动（建轨道 / 建 cut）之后强制整棵 Sequencer 树刷新并立即求值。"""
        try:
            self.lse.force_update()
            self._last_seconds = None
            self._last_state = None
        except Exception as exc:
            self.ctx.warn(f"force_update 失败（{exc}）；后续读取可能滞后于结构改动")

    def _build_params(self, frame, sub):
        """构造时间参数：优先精确的帧形式，退到秒形式。"""
        if self.use_frame_params is not False:
            try:
                params = unreal.MovieSceneSequencePlaybackParams()
                if self.frame_position_type is not None:
                    params.position_type = self.frame_position_type
                frame_time = unreal.FrameTime(unreal.FrameNumber(int(frame)))
                if sub:
                    frame_time.sub_frame = float(sub)   # 已实测可写
                params.frame = frame_time
                if self.use_frame_params is None:
                    self.use_frame_params = True
                    log("时间注入：帧形式（FrameNumber + sub_frame，精确）")
                if self.jump_method is not None:
                    params.update_method = self.jump_method
                return params
            except Exception as exc:
                if self.use_frame_params is None:
                    self.use_frame_params = False
                    log(f"时间注入：帧形式不可用（{exc}），回退秒形式")
                else:
                    self.use_frame_params = False
        params = unreal.MovieSceneSequencePlaybackParams()
        if self.position_type is not None:
            params.position_type = self.position_type
        params.time = seconds_for_frame(frame, sub, self.display_rate)
        if self.jump_method is not None:
            params.update_method = self.jump_method
        return params

    def jump_to(self, frame, sub=0.0):
        """跳到 (frame + sub) 并完成同步求值。"""
        if not self.can_subframe and sub:
            raise ConfigError(
                "当前引擎没有 LevelSequenceEditorBlueprintLibrary.set_global_position，"
                "只能按整数帧求值；请把 BAKE_SUBSAMPLES 设为 1")
        if self.can_subframe:
            # 同一时间点重复设置时，引擎内部会自行 ForceEvaluate，所以无需额外 force_update
            self.lse.set_global_position(self._build_params(frame, sub), self.time_unit)
        else:
            self.lse.set_current_time(int(frame))
        self._last_seconds = seconds_for_frame(frame, sub, self.display_rate)
        self._count += 1
        if self.ctx.cfg["PROGRESS_EVERY"] and self._count % self.ctx.cfg["PROGRESS_EVERY"] == 0:
            log(f"已求值 {self._count} 次，当前 frame={frame}+{sub:.3f}")


def open_sequence(sequence, evaluator):
    """打开序列并返回之前打开的那条（结束时可还原）。该库未打开序列时所有函数静默 no-op。"""
    previous = None
    try:
        previous = evaluator.lse.get_current_level_sequence()
    except Exception:
        pass
    if not evaluator.lse.open_level_sequence(sequence):
        raise RuntimeError(
            f"无法在 Sequencer 中打开序列 {sequence.get_path_name()}；"
            "本脚本依赖编辑器 Sequencer 的求值，必须能打开它")
    # 求值期间不要让 Camera Cut 抢走编辑器视口
    try:
        evaluator.saved_lock = evaluator.lse.is_camera_cut_locked_to_viewport()
        evaluator.lse.set_lock_camera_cut_to_viewport(False)
    except Exception:
        evaluator.saved_lock = None
    return previous


def ensure_source_level(context, level_path):
    """确保源序列和生成的 level actors 都位于指定关卡。"""
    if not level_path:
        context.warn("SOURCE_LEVEL 为空：脚本将使用当前关卡，必须自行确认它与 MRQ 映射关卡一致")
        return
    try:
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = editor.get_editor_world()
        current = world.get_path_name() if world is not None else ""
    except Exception as exc:
        raise ConfigError(f"无法读取当前 UE 关卡，不能安全确认生成相机位置：{exc}") from exc

    expected = str(level_path).rstrip("/")
    expected_world = expected if "." in expected.rsplit("/", 1)[-1] else expected + "." + expected.rsplit("/", 1)[-1]
    if current == expected or current == expected_world:
        context.notes["source_level"] = expected
        context.notes["source_level_action"] = "already_loaded"
        log(f"源关卡已确认：{current}")
        return

    try:
        dirty = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
    except Exception:
        dirty = []
    if dirty:
        dirty_names = []
        for package in dirty:
            try:
                dirty_names.append(package.get_name())
            except Exception:
                dirty_names.append(str(package))
        raise ConfigError(
            "当前关卡有未保存修改，拒绝自动切换 SOURCE_LEVEL 以避免丢失内容："
            + ", ".join(dirty_names)
            + "。请先保存或关闭未保存关卡，再重新运行。")

    subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not subsystem.load_level(expected):
        raise ConfigError(f"无法加载 SOURCE_LEVEL：{expected}")
    try:
        loaded_world = editor.get_editor_world()
        loaded = loaded_world.get_path_name() if loaded_world is not None else ""
    except Exception:
        loaded = ""
    if loaded not in (expected, expected_world):
        raise ConfigError(f"SOURCE_LEVEL 加载后关卡仍不匹配：期望 {expected}，实际 {loaded}")
    context.notes["source_level"] = expected
    context.notes["source_level_action"] = "loaded"
    log(f"已切换到源关卡：{loaded}")


def restore_sequence_focus(evaluator, previous):
    try:
        if getattr(evaluator, "saved_lock", None) is not None:
            evaluator.lse.set_lock_camera_cut_to_viewport(evaluator.saved_lock)
    except Exception:
        pass
    try:
        if previous is not None:
            evaluator.lse.open_level_sequence(previous)
    except Exception:
        pass


def assert_subframe_evaluation(context):
    """FrameLocked 的 MovieScene 会把子帧时间 floor 到整帧，烘焙子样本会静默失真。"""
    movie_scene = context.sequence.get_movie_scene()
    mode = prop(movie_scene, "evaluation_type", None)
    with_sub = enum_member("MovieSceneEvaluationType", "WithSubFrames", "WITH_SUB_FRAMES")
    if mode is None or with_sub is None:
        context.warn("无法确认 MovieScene 的 evaluation_type；若烘焙子样本出现整帧化现象请手动检查")
        return
    if mode != with_sub:
        message = ("MovieScene evaluation_type 不是 WithSubFrames，子帧时间会被取整到整帧，"
                   "无法保证 Cut 边界行为。请在 Sequencer 播放设置里改为 With Sub Frames。")
        if context.cfg["BAKE_SUBSAMPLES"] > 1:
            raise ConfigError(message)
        context.warn(message)


# ----------------------------------------------------------------------------
# 读取源相机状态
# ----------------------------------------------------------------------------

def _camera_component(actor):
    if actor is None:
        return None
    component = None
    try:
        component = actor.get_component_by_class(unreal.CameraComponent)
    except Exception:
        component = None
    if component is None:
        for getter in ("get_cine_camera_component", "get_camera_component"):
            try:
                component = getattr(actor, getter)()
                if component:
                    break
            except Exception:
                continue
    return component


def _filmback_of(component):
    filmback = prop(component, "filmback", None)
    if filmback is None:
        return None
    return {
        "sensor_width": float(prop(filmback, "sensor_width", 36.0)),
        "sensor_height": float(prop(filmback, "sensor_height", 20.25)),
        "sensor_aspect_ratio": float(prop(filmback, "sensor_aspect_ratio", 1.777778)),
    }


def resolve_object_reference(value):
    """把 UE 的 SoftObjectPtr / SoftObjectPath 尽量解析为 UObject。"""
    if value is None:
        return None
    for method_name in ("get", "load_synchronous", "resolve_object"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                resolved = method()
                if resolved is not None:
                    return resolved
            except Exception:
                pass
    return value


def object_identity(value):
    """用于验收 UObject 引用；优先用稳定的 UE 对象路径。"""
    value = resolve_object_reference(value)
    if value is None:
        return None
    getter = getattr(value, "get_path_name", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            pass
    return str(value)


def read_source_state(context):
    """读取**已求值**的源相机最终状态：世界 Transform + 最终镜头参数。"""
    actor = context.source_actor
    transform = actor.get_actor_transform()
    location = prop(transform, "translation")
    rotation = prop(transform, "rotation")
    component = _camera_component(actor)

    state = {
        "loc": (float(prop(location, "x", 0.0)), float(prop(location, "y", 0.0)), float(prop(location, "z", 0.0))),
        # 世界旋转；通道映射见 §5：Rotation.X=Roll, Rotation.Y=Pitch, Rotation.Z=Yaw
        "rot": rotator_degrees(rotation),
        "focal": None,
        "focal_source": "none",
        "aperture": None,
        "focus": None,
        "focus_settings": None,
        "focus_method": None,
        "focus_actor": None,
        "focus_offset": None,
        "field_of_view": None,
        "ortho_width": None,
        "projection": "persp",
        "filmback": None,
        "aspect_ratio": None,
        "constrain_aspect_ratio": None,
    }
    if component is None:
        return state

    filmback = _filmback_of(component)
    state["filmback"] = filmback
    state["aspect_ratio"] = prop(component, "aspect_ratio", None)
    state["constrain_aspect_ratio"] = prop(component, "constrain_aspect_ratio", None)
    state["field_of_view"] = prop(component, "field_of_view", None)

    projection = prop(component, "projection_mode", None)
    ortho = enum_member("CameraProjectionMode", "Orthographic", "ORTHOGRAPHIC")
    if projection is not None and ortho is not None and projection == ortho:
        state["projection"] = "ortho"
        state["ortho_width"] = prop(component, "ortho_width", None)

    if isinstance(component, unreal.CineCameraComponent):
        state["focal"] = prop(component, "current_focal_length", None)
        state["focal_source"] = "cine"
        state["aperture"] = prop(component, "current_aperture", None)
        focus_settings = prop(component, "focus_settings", None)
        # FocusSettings 不是只有 ManualFocusDistance：MMD/UE 中常见的是
        # Tracking + TrackingFocusSettings.ActorToTrack。把整个结构和关键
        # 子属性都带出来，后面才能在新相机上保持原来的聚焦对象与模式。
        state["focus_settings"] = focus_settings
        state["focus_method"] = prop(focus_settings, "focus_method", None)
        state["focus"] = prop(focus_settings, "manual_focus_distance", None)
        tracking = prop(focus_settings, "tracking_focus_settings", None)
        state["focus_actor"] = resolve_object_reference(prop(tracking, "actor_to_track", None))
        state["focus_offset"] = prop(tracking, "relative_offset", None)
    else:
        # 普通 CameraActor：读 FOV 反算焦距，保持最终画面一致（不重算 MMD 侧数学）
        fov = prop(component, "field_of_view", None)
        state["field_of_view"] = fov
        if fov is not None and filmback:
            state["focal"] = focal_from_fov(fov, filmback["sensor_width"])
            state["focal_source"] = "fov-derived" if state["focal"] is not None else "none"
    return state


def sample_state(context, evaluator, frame, sub=0.0, cache=None):
    """跳到目标帧 → 同步求值 → 读取最终世界 Transform 与相机参数（不走缓存则重新求值）。"""
    if cache is not None:
        key = (frame, round(sub, 6))
        if key in cache:
            return cache[key]
    evaluator.jump_to(frame, sub)
    state = read_source_state(context)
    if cache is not None:
        cache[(frame, round(sub, 6))] = state
    return state


# ============================================================================
# §4 源相机解析 / 扫描 / 幂等清理 / 备份
# ============================================================================

def _actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def all_level_actors():
    try:
        return list(_actor_subsystem().get_all_level_actors())
    except Exception:
        return []


def resolve_source_camera(context, hint):
    """把 SOURCE_CAMERA 解析成 (binding, actor)。

    顺序：关卡 actor 标签 → actor 名 → 序列 binding 名（binding 命中时用 Sequencer 已求值的绑定对象）。
    """
    sequence = context.sequence
    binding = None
    for item in sequence.get_bindings():
        try:
            if item.get_name() == hint:
                binding = item
                break
        except Exception:
            continue
    if binding is not None:
        context.notes["source_binding_name"] = binding.get_name()

    actor = None
    exact_label, exact_name = [], []
    for candidate in all_level_actors():
        try:
            label = candidate.get_actor_label()
        except Exception:
            label = ""
        try:
            name = candidate.get_name()
        except Exception:
            name = ""
        if label == hint:
            exact_label.append(candidate)
        elif name == hint:
            exact_name.append(candidate)
    if len(exact_label) == 1:
        actor = exact_label[0]
        context.notes["source_resolved_by"] = "actor_label"
    elif len(exact_name) == 1:
        actor = exact_name[0]
        context.notes["source_resolved_by"] = "actor_name"

    if actor is None and binding is not None:
        # 已求值状态下取绑定对象（spawnable 也能拿到编辑器预览 actor）
        try:
            bound = context.evaluator.lse.get_bound_objects(sequence.get_binding_id(binding))
            for item in bound or []:
                if isinstance(item, unreal.CameraActor) or _camera_component(item) is not None:
                    actor = item
                    context.notes["source_resolved_by"] = "bound_objects"
                    break
        except Exception as exc:
            context.warn(f"get_bound_objects 失败：{exc}")

    if actor is None:
        # Independent sampled-camera imports need not use the historic label.
        # Scope fallback to the opened sequence, excluding our baked cameras;
        # never select an arbitrary camera from the level.
        animated = {}
        for candidate_binding in sequence.get_bindings():
            if not any(isinstance(t, unreal.MovieScene3DTransformTrack)
                       for t in candidate_binding.get_tracks()):
                continue
            objects = context.evaluator.lse.get_bound_objects(sequence.get_binding_id(candidate_binding))
            for candidate in objects or []:
                if (_camera_component(candidate) is not None
                        and not _is_generated_object(context, candidate)):
                    animated[object_identity(candidate)] = (candidate_binding, candidate)
        if len(animated) == 1:
            binding, actor = next(iter(animated.values()))
            context.notes['source_binding_name'] = binding.get_name()
            context.notes['source_resolved_by'] = 'unique_animated_camera'
        elif len(animated) > 1:
            raise ConfigError('当前序列包含多个动画相机，请明确指定 SOURCE_CAMERA')

    if actor is None:
        candidates = sorted({a.get_actor_label() for a in all_level_actors()
                             if _camera_component(a) is not None})
        raise ConfigError(
            f"找不到源相机 {hint!r}（binding={'命中' if binding else '未命中'}）。"
            f"关卡里的相机 actor：{candidates}")
    if binding is None:
        context.warn(f"序列里没有名为 {hint!r} 的 binding，仅按关卡 actor 解析；重复运行不会误删它，"
                     "但它不会被识别为源相机")
    return binding, actor


def scan_source_profile(context, evaluator, plan):
    """少量采样，判定镜头参数是"全程常量"还是"逐帧变化"，决定静态写值还是建轨道。

    只取少量探测点（各镜首/尾帧 + 序列首尾），并在报告里注明采样点数量。
    """
    probes = []
    for shot in plan:
        probes.append((shot.start, 0.0))
        probes.append((shot.end - 1, 0.0))
    probes = sorted(set(probes))
    profile = {"probe_frames": [frame for frame, _ in probes], "focal": [], "aperture": [],
               "focus": [], "focus_settings": [], "focus_actor": [], "focus_method": [], "focus_offset": [],
               "filmback": [], "projection": [], "ortho_width": [],
               "aspect_ratio": [], "constrain_aspect_ratio": [], "focal_source": set()}
    cache = {}
    for frame, sub in probes:
        state = sample_state(context, evaluator, frame, sub, cache)
        profile["focal"].append(state["focal"])
        profile["aperture"].append(state["aperture"])
        profile["focus"].append(state["focus"])
        profile["focus_settings"].append(state.get("focus_settings"))
        profile["focus_actor"].append(state.get("focus_actor"))
        profile["focus_method"].append(state.get("focus_method"))
        profile["focus_offset"].append(state.get("focus_offset"))
        profile["filmback"].append(state["filmback"])
        profile["projection"].append(state["projection"])
        profile["ortho_width"].append(state["ortho_width"])
        profile["aspect_ratio"].append(state["aspect_ratio"])
        profile["constrain_aspect_ratio"].append(state["constrain_aspect_ratio"])
        profile["focal_source"].add(state["focal_source"])

    def varies(values, tol=1e-6):
        clean = [v for v in values if v is not None]
        if not clean:
            return False
        if isinstance(clean[0], dict):
            keys = clean[0].keys()
            return any(varies([v[k] for v in clean if v is not None], tol) for k in keys)
        try:
            return (max(clean) - min(clean)) > tol
        except Exception:
            # UE enum / UObject / Vector 等不是可直接 max-min 的数值，
            # 统一按稳定字符串或对象路径比较。
            def signature(value):
                getter = getattr(value, "get_path_name", None)
                if callable(getter):
                    try:
                        return (type(value).__name__, getter())
                    except Exception:
                        pass
                return (type(value).__name__, str(value))
            first = signature(clean[0])
            return any(signature(value) != first for value in clean[1:])

    profile["focal_varies"] = varies(profile["focal"])
    profile["aperture_varies"] = varies(profile["aperture"])
    profile["focus_varies"] = varies(profile["focus"])
    profile["focus_settings_present"] = any(value is not None for value in profile["focus_settings"])
    profile["focus_actor_present"] = any(value is not None for value in profile["focus_actor"])
    profile["focus_actor_varies"] = varies(profile["focus_actor"])
    profile["focus_method_varies"] = varies(profile["focus_method"])
    profile["focus_offset_varies"] = varies(profile["focus_offset"])
    profile["filmback_varies"] = varies(profile["filmback"])
    profile["aspect_varies"] = (varies(profile["aspect_ratio"])
                                or varies(profile["constrain_aspect_ratio"]))
    profile["focal_source"] = sorted(profile["focal_source"])
    if "ortho" in profile["projection"]:
        context.notes["projection_route"] = "CameraActor + ProjectionMode / OrthoWidth / FieldOfView"
        context.warn("正交/混合投影使用普通 Camera 的投影轨道；CineCamera 专属光圈及跟踪聚焦需单独复核")
    if profile["filmback_varies"]:
        context.warn("源相机 filmback（sensor 宽高）在序列中发生变化，本脚本只烘焙首帧的 filmback；"
                     "如需动画请手动补轨道。")
    if profile["focal_varies"] and profile["focal_source"] == ["fov-derived"]:
        context.warn("源相机是普通 CameraActor，焦距由 FOV 反算得到（非直接读取 CineCamera 的焦距）。")
    if not profile["focal_varies"]:
        context.notes["focal_constant"] = profile["focal"][0] if profile["focal"] else None
    return profile


def _binding_key(binding):
    try:
        return str(binding.get_id())
    except Exception:
        return ""


def _is_generated_object(context, obj):
    """判断一个对象（actor 或组件）是不是本脚本生成物。

    binding 的名字在 UE 里取的是"actor 的 object name"，而脚本只设了 actor label，
    所以**不能只靠名字前缀**判断，否则重复运行时旧 binding 会残留。这里同时看
    label / object name 前缀、tag、以及组件的 owner。
    """
    if obj is None:
        return False
    prefix = context.cfg["GENERATED_PREFIX"]
    tag = context.cfg["GENERATED_TAG"]

    def names_of(target):
        values = []
        for getter in ("get_actor_label", "get_name"):
            try:
                value = getattr(target, getter)()
                if value:
                    values.append(str(value))
            except Exception:
                pass
        return values

    if any(name.startswith(prefix) for name in names_of(obj)):
        return True
    try:
        if tag in [str(t) for t in (obj.tags or [])]:
            return True
    except Exception:
        pass
    # 组件 possessable：看它的 owner actor
    try:
        owner = obj.get_owner()
    except Exception:
        owner = None
    if owner is not None:
        return _is_generated_object(context, owner)
    return False


def cleanup_generated(context):
    """删除上一次运行生成的一切（binding / actor / Camera Cut Track），不动源相机。

    两趟处理：先认出生成物，再先删子（镜头组件的 possessable）后删父（镜头相机的 possessable），
    避免留下指向已销毁 actor 的悬空 binding。
    """
    sequence = context.sequence
    prefix = context.cfg["GENERATED_PREFIX"]
    tag = context.cfg["GENERATED_TAG"]
    removed_bindings, removed_tracks, removed_actors = [], [], []

    generated, generated_keys = [], set()
    for binding in list(sequence.get_bindings()):
        try:
            name = binding.get_name()
        except Exception:
            name = ""
        template = None
        try:
            template = binding.get_object_template()
        except Exception:
            template = None
        if name.startswith(prefix) or _is_generated_object(context, template):
            generated.append(binding)
            generated_keys.add(_binding_key(binding))

    # 父 binding 属于生成物的（例如镜头参数所在的组件 possessable）也要删
    children = []
    for binding in list(sequence.get_bindings()):
        if any(_binding_key(binding) == _binding_key(item) for item in generated):
            continue
        try:
            parent = binding.get_parent()
        except Exception:
            parent = None
        if parent is not None and _binding_key(parent) in generated_keys:
            children.append(binding)
            generated_keys.add(_binding_key(binding))

    for binding in children + generated:
        try:
            removed_bindings.append(binding.get_name())
        except Exception:
            removed_bindings.append("<unnamed>")
        binding.remove()

    for track in list(sequence.get_tracks()):
        if isinstance(track, unreal.MovieSceneCameraCutTrack):
            sequence.remove_track(track)
            removed_tracks.append(type(track).__name__)

    subsystem = _actor_subsystem()
    for actor in list(all_level_actors()):
        names = []
        try:
            names.append(actor.get_actor_label() or "")
        except Exception:
            pass
        try:
            names.append(actor.get_name() or "")
        except Exception:
            pass
        try:
            tags = [str(t) for t in (actor.tags or [])]
        except Exception:
            tags = []
        if any(n.startswith(prefix) for n in names) or tag in tags:
            subsystem.destroy_actor(actor)
            removed_actors.append(names[0] or names[-1] if names else "<unnamed>")

    if removed_bindings or removed_tracks or removed_actors:
        log(f"清理旧生成物：bindings={removed_bindings} camera_cut_tracks={removed_tracks} "
            f"actors={removed_actors}")
    context.notes["cleanup"] = {"bindings": removed_bindings, "cut_tracks": removed_tracks,
                                "actors": removed_actors}


def make_backup(context, source_path, stage):
    """把 LevelSequence 资产复制一份，便于回滚，并标记产生备份的阶段。"""
    if not context.cfg["MAKE_BACKUP"]:
        return None
    folder, name = source_path.rsplit("/", 1)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    destination_dir = f"{folder}/{context.cfg['BACKUP_FOLDER_NAME']}"
    try:
        unreal.EditorAssetLibrary.make_directory(destination_dir)
        destination = f"{destination_dir}/{name}_{stage}_{stamp}"
        if unreal.EditorAssetLibrary.does_asset_exist(destination):
            warn(f"备份目标已存在，跳过：{destination}")
            return None
        asset = unreal.EditorAssetLibrary.duplicate_asset(source_path, destination)
        if asset:
            unreal.EditorAssetLibrary.save_asset(destination)
            log(f"已备份原序列：{destination}")
            return destination
        warn(f"备份失败（duplicate_asset 返回空）：{source_path} → {destination}")
    except Exception as exc:
        warn(f"备份失败：{exc}")
    return None


def maybe_duplicate_for_edit(context, source_path):
    """IN_PLACE=False 时复制出 <name>_Cut 作为工作副本。"""
    if context.cfg["IN_PLACE"]:
        return source_path
    folder, name = source_path.rsplit("/", 1)
    destination = f"{folder}/{name}_Cut"
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(destination):
            unreal.EditorAssetLibrary.delete_asset(destination)
        asset = unreal.EditorAssetLibrary.duplicate_asset(source_path, destination)
        if asset is None:
            raise RuntimeError("duplicate_asset 返回空")
        unreal.EditorAssetLibrary.save_asset(destination)
        log(f"IN_PLACE=False：工作副本 = {destination}（原始序列保持不变）")
        return destination
    except Exception as exc:
        raise RuntimeError(f"创建工作副本失败：{exc}") from exc


# ============================================================================
# §5 烘焙
# ============================================================================

CFG_KEYS_CACHE = {}
_WARNED = set()


def context_free_warn(message):
    if message not in _WARNED:
        _WARNED.add(message)
        warn(message)


def key_interpolation_member():
    name = str(CFG_KEYS_CACHE.get("KEY_INTERPOLATION", "linear")).lower()
    if name in ("auto", "smartauto", "smart_auto"):
        return enum_member("MovieSceneKeyInterpolation", "Auto", "AUTO", "SmartAuto", "SMART_AUTO")
    if name == "cubic":
        return enum_member("MovieSceneKeyInterpolation", "Cubic", "CUBIC")
    if name != "linear":
        context_free_warn(f"未知 KEY_INTERPOLATION={name!r}，回退 linear")
    return enum_member("MovieSceneKeyInterpolation", "Linear", "LINEAR")


def get_channel(section, name):
    """按名字取 section 上的通道。位置索引会因 TransformMask 的 Weight 位而整体错位，必须按名取。"""
    for argument in (unreal.Name(name), name):
        try:
            channel = section.get_channel(argument)
        except Exception:
            channel = None
        if channel is not None:
            return channel
    return None


def _add_key_form_usable(form, sub):
    if form == "plain":
        return sub == 0.0
    if form in ("kwargs", "positional"):
        return _display_rate_unit() is not None
    if form == "tick":
        return _tick_resolution_unit() is not None
    return False


def _key_time_matches(key, frame, sub):
    """校验刚写入的 key 是否真的落在 (frame + sub) 上。

    必须校验的原因（真实 UE 5.7.4 上实测）：add_key 的 `sub_frame=` 参数**会被忽略**，
    key 会静默落到整帧上。若不校验，BAKE_SUBSAMPLES>1 会悄悄退化成整帧烘焙，
    而且每个子帧采到的值会覆盖式地挤到同一个整数帧 key 上（等于把曲线平移了最多 3/4 帧）。
    """
    try:
        actual = key_display_time(key)
    except Exception:
        return True     # 读不回来就假定正常，避免误伤
    return abs((actual[0] + actual[1]) - (frame + sub)) < 1e-4


def add_channel_key(channel, frame, sub, value, interpolation, tick_resolution, display_rate):
    """往通道写一个 key，兼容不同小版本 add_key 的签名。

    真实 UE 5.7.4 上实测结论：
      * `add_key(FrameNumber(f), v)` 整数帧可用 —— 形式 plain
      * `add_key(FrameNumber(f), v, sub_frame=s, time_unit=DISPLAY_RATE)` **子帧会被丢掉**，
        key 静默落到整帧（所以子帧优先用 tick 形式）
      * `add_key(FrameNumber(ticks), v, time_unit=TICK_RESOLUTION)` 精确 —— 形式 tick
    带子帧时优先 tick；每次换形式后都会读回校验前几个 key 的真实时间，不匹配就换下一种，
    并在报告里明确警告，绝不静默降级。
    """
    global _ADD_KEY_FORM, _INTERP_KWARG_OK, _SUBFRAME_VERIFY_LEFT
    frame = int(frame)
    sub = float(sub)
    value = float(value)
    if _INTERP_KWARG_OK is False:
        interpolation = None

    def call(form, use_interpolation):
        interp = interpolation if use_interpolation else None
        if form == "plain":
            if interp is None:
                return channel.add_key(frame_number(frame), value)
            return channel.add_key(frame_number(frame), value, interpolation=interp)
        if form == "kwargs":
            kwargs = {"sub_frame": sub, "time_unit": _display_rate_unit()}
            if interp is not None:
                kwargs["interpolation"] = interp
            return channel.add_key(frame_number(frame), value, **kwargs)
        if form == "positional":
            args = (frame_number(frame), value, sub, _display_rate_unit())
            if interp is not None:
                args = args + (interp,)
            return channel.add_key(*args)
        if form == "tick":
            ticks = ticks_for_time(frame, sub, tick_resolution, display_rate)
            kwargs = {"time_unit": _tick_resolution_unit()}
            if interp is not None:
                kwargs["interpolation"] = interp
            return channel.add_key(frame_number(ticks), value, **kwargs)
        raise AssertionError(form)

    # 子帧：tick 形式是唯一经过实测确认不丢子帧的写法，放第一位
    if sub == 0.0:
        order = ["plain", "tick", "kwargs"]
    else:
        order = ["tick", "kwargs", "positional"]
    order = [form for form in order
             if _add_key_form_usable(form, sub) and form not in _SUBFRAME_BAD_FORMS]
    if _ADD_KEY_FORM in order:
        order = [_ADD_KEY_FORM] + [form for form in order if form != _ADD_KEY_FORM]

    last_error = None
    for form in order:
        tries = [True, False] if interpolation is not None else [False]
        for use_interpolation in tries:
            if use_interpolation and _INTERP_KWARG_OK is False:
                continue
            try:
                key = call(form, use_interpolation)
            except TypeError as exc:
                if use_interpolation and "interpolation" in str(exc).lower():
                    # 关键字名不同：退到不带它的形式，Hold 不受影响
                    _INTERP_KWARG_OK = False
                    context_free_warn(
                        "add_key 不接受 interpolation= 关键字，已改用默认插值"
                        "（Cut 边界的 Constant Hold 由 set_interpolation_mode 完成，不受影响）")
                    continue
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                break
            if key is None:
                last_error = RuntimeError("add_key 返回空")
                break
            # 子帧走的是"实测校验"，只在前若干个 key 上做，之后信任该形式
            if sub != 0.0 and _SUBFRAME_VERIFY_LEFT > 0:
                _SUBFRAME_VERIFY_LEFT -= 1
                if not _key_time_matches(key, frame, sub):
                    _SUBFRAME_BAD_FORMS.add(form)
                    context_free_warn(
                        f"add_key 的 {form} 形式会把子帧 key 归到整帧（实测 {frame}+{sub} 写成了"
                        f"{key_display_time(key)}），已改走其它形式；"
                        "若所有形式都如此，请把 BAKE_SUBSAMPLES 设为 1")
                    last_error = RuntimeError(f"{form} 形式丢掉子帧")
                    break
            if _ADD_KEY_FORM != form:
                _ADD_KEY_FORM = form
                log(f"add_key 采用形式：{form}")
            return key
    hint = ""
    if sub != 0.0:
        hint = (f"；本机引擎的 add_key 无法把子帧 key 写到 {frame}+{sub}"
                f"（会落到整帧）。这是 BAKE_SUBSAMPLES>1 的硬前提，"
                f"请把 BAKE_SUBSAMPLES 设为 1 重新运行")
    raise RuntimeError(f"无法写入 key（frame={frame} sub={sub}）：{last_error}{hint}")


def add_object_channel_key(channel, frame, sub, value, tick_resolution, display_rate):
    """给 ObjectPath 通道写 key。ActorToTrack 不能走 add_channel_key 的 float 转换。"""
    frame = int(frame)
    sub = float(sub)
    forms = ["plain", "tick"] if sub == 0.0 else ["tick", "kwargs", "positional"]
    last_error = None
    for form in forms:
        try:
            if form == "plain":
                key = channel.add_key(frame_number(frame), value)
            elif form == "tick":
                key = channel.add_key(frame_number(ticks_for_time(frame, sub, tick_resolution, display_rate)),
                                      value, time_unit=_tick_resolution_unit())
            elif form == "kwargs":
                key = channel.add_key(frame_number(frame), value, sub_frame=sub,
                                      time_unit=_display_rate_unit())
            else:
                key = channel.add_key(frame_number(frame), value, sub, _display_rate_unit())
            if key is None:
                raise RuntimeError("add_key 返回空")
            if sub != 0.0 and not _key_time_matches(key, frame, sub):
                last_error = RuntimeError(f"{form} 形式丢掉子帧")
                continue
            return key
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"无法写入对象 key（frame={frame} sub={sub}）：{last_error}")


def _display_rate_unit():
    return enum_member("MovieSceneTimeUnit", "DisplayRate", "DISPLAY_RATE")


def _tick_resolution_unit():
    return enum_member("MovieSceneTimeUnit", "TickResolution", "TICK_RESOLUTION")


def key_display_time(key):
    """key 的时间 → (整数显示帧, 子帧)。"""
    time = key.get_time()
    number = prop(prop(time, "frame_number", time), "value", None)
    if number is None:
        number = int(time)
    sub = float(prop(time, "sub_frame", 0.0) or 0.0)
    return int(number), sub


def create_shot_camera(context, name):
    """建一台镜头相机并返回登记字典。Possessable（默认）或 Spawnable。"""
    cfg = context.cfg
    sequence = context.sequence
    component = None
    actor = None
    mode = "possessable"
    ordinary = "ortho" in context.source_profile.get("projection", [])
    camera_class = unreal.CameraActor if ordinary else unreal.CineCameraActor

    if cfg["USE_SPAWNABLE"]:
        mode = "spawnable"
        binding = None
        try:
            binding = sequence.add_spawnable_from_class(camera_class)
        except Exception as exc:
            context.warn(f"add_spawnable_from_class 调用失败（{exc}），尝试 LevelSequenceEditorSubsystem")
            binding = None
        if binding is None or not _binding_valid(binding):
            try:
                subsystem = unreal.get_editor_subsystem(unreal.LevelSequenceEditorSubsystem)
                binding = subsystem.add_spawnable_from_class(sequence, camera_class)
            except Exception as exc:
                raise RuntimeError(f"Spawnable 创建失败，请改用 USE_SPAWNABLE=False：{exc}") from exc
        if binding is None or not _binding_valid(binding):
            raise RuntimeError("Spawnable 创建失败（binding 无效），请改用 USE_SPAWNABLE=False")
        actor = binding.get_object_template()
        if actor is None:
            raise RuntimeError("Spawnable 的 object template 为空，无法烘焙")
        component = _camera_component(actor)
    else:
        actor = _actor_subsystem().spawn_actor_from_class(camera_class, unreal.Vector())
        if actor is None:
            raise RuntimeError("spawn_actor_from_class(CineCameraActor) 返回空")
        try:
            actor.tags = [unreal.Name(cfg["GENERATED_TAG"])]
        except Exception:
            pass
        binding = sequence.add_possessable(actor)

    try:
        actor.set_actor_label(name)
    except Exception:
        pass
    if component is None:
        component = _camera_component(actor)
    if component is None:
        raise RuntimeError(f"{name}: 拿不到 CameraComponent")

    # 让模板/actor 落在原点，避免叠加自身变换（生成轨道写的是绝对相对变换）
    try:
        actor.set_actor_transform(unreal.Transform(), False, False)
    except Exception:
        pass

    record = {"name": name, "mode": mode, "binding": binding, "actor": actor,
              "ordinary_camera": ordinary,
              "component": component, "channels": {}, "lens_tracks": {}, "shot": None,
              "lens_check": None}
    return record


def _binding_valid(binding):
    try:
        return bool(binding.is_valid())
    except Exception:
        return binding is not None


def create_transform_track(context, record, start, end):
    """给镜头相机建 3D Transform 轨道与 section，并缓存 9 个通道。

    通道代理是懒创建的（UMovieSceneSection::CacheChannelProxy），不需要 Sequencer UI。
    scale 只写一个常量 key：MMD 相机不驱动缩放，动画缩放还会放大子组件。
    """
    binding = record["binding"]
    track = binding.add_track(unreal.MovieScene3DTransformTrack)
    if track is None:
        raise RuntimeError(f"{record['name']}: 无法创建 MovieScene3DTransformTrack")
    section = track.add_section()
    section.set_range(int(start), int(end))
    channels = {}
    for name in TRANSFORM_CHANNELS:
        channel = get_channel(section, name)
        if channel is None:
            raise RuntimeError(f"{record['name']}: 取不到通道 {name}（section 未挂到 MovieScene，或 TransformMask 被改动）")
        channels[name] = channel
    record["channels"] = channels
    record["transform_section"] = section
    record["transform_track"] = track
    return channels


def create_lens_tracks(context, record, start, end, profile):
    """建镜头参数轨道。

    CurrentFocalLength / CurrentAperture 是组件上的直接 float 属性，用 MovieSceneFloatTrack。
    ManualFocusDistance 和 TrackingFocusSettings.ActorToTrack 也建立动画轨道，
    这样即使首尾帧相同、但中间发生变化，也不会被少量 probe 漏掉。
    """
    cfg = context.cfg
    if not cfg["DO_BAKE_LENS"]:
        return
    component = record["component"]
    binding = record["binding"]
    mode = cfg["LENS_TRACK_MODE"]
    if mode == "auto":
        mode = "actor_property_path" if record["mode"] == "spawnable" else "component_possessable"

    if mode == "component_possessable":
        if record["mode"] == "spawnable":
            context.warn("spawnable 无法对组件建 possessable，已回退 actor_property_path")
            mode = "actor_property_path"
        else:
            target = context.sequence.add_possessable(component)
            target.set_parent(binding)
    else:
        target = binding

    def add_float(name, path):
        track = target.add_track(unreal.MovieSceneFloatTrack)
        if track is None:
            return None
        try:
            track.set_property_name_and_path(unreal.Name(name), path)
        except Exception as exc:
            context.warn(f"{record['name']}: set_property_name_and_path({name}) 失败：{exc}")
        section = track.add_section()
        section.set_range(int(start), int(end))
        channels = section.get_all_channels()
        if not channels:
            context.warn(f"{record['name']}: {name} 轨道没有通道，参数不会被动画")
            return None
        record["lens_tracks"][name] = {"track": track, "section": section, "channel": channels[0]}
        return record["lens_tracks"][name]

    def add_object(name, path):
        track_class = getattr(unreal, "MovieSceneObjectPropertyTrack", None)
        if track_class is None:
            context.warn(f"{record['name']}: 当前 UE Python 没有 MovieSceneObjectPropertyTrack，"
                         "无法逐帧保留聚焦对象；将至少保留首帧 FocusSettings")
            return None
        try:
            track = target.add_track(track_class)
            track.set_property_name_and_path(unreal.Name(name), path)
            # 某些 UE 小版本需要显式告诉 ObjectPropertyTrack 它是 Actor 引用。
            extensions = getattr(unreal, "MovieScenePropertyTrackExtensions", None)
            set_class = getattr(extensions, "set_object_property_class", None)
            actor_class = getattr(unreal, "Actor", None)
            if callable(set_class) and actor_class is not None:
                try:
                    set_class(track, actor_class)
                except Exception:
                    pass
            section = track.add_section()
            section.set_range(int(start), int(end))
            channels = section.get_all_channels()
            if not channels:
                context.warn(f"{record['name']}: {name} 对象轨道没有通道")
                return None
            record["lens_tracks"][name] = {"track": track, "section": section,
                                            "channel": channels[0], "object": True}
            return record["lens_tracks"][name]
        except Exception as exc:
            context.warn(f"{record['name']}: 无法建立 {name} 对象轨道：{exc}；"
                         "将至少保留首帧 FocusSettings")
            return None

    prefix = ("CameraComponent." if record.get("ordinary_camera") else "CineCameraComponent.") if mode == "actor_property_path" else ""
    if record.get("ordinary_camera"):
        add_float("FieldOfView", prefix + "FieldOfView")
        add_float("OrthoWidth", prefix + "OrthoWidth")
        track = target.add_track(unreal.MovieSceneByteTrack)
        track.set_property_name_and_path("ProjectionMode", prefix + "ProjectionMode")
        section = track.add_section()
        section.set_range(int(start), int(end))
        record["lens_tracks"]["ProjectionMode"] = {"track": track, "section": section,
            "channel": section.get_all_channels()[0], "object": True}
        return
    add_float("CurrentFocalLength", prefix + "CurrentFocalLength")
    # 即便 probe 的首尾值相同，也不能证明镜头中间没有变化；统一逐采样写入，
    # 常量值自然会得到常量曲线，动画值则完整保留。
    add_float("CurrentAperture", prefix + "CurrentAperture")
    if any(value is not None for value in profile.get("focus", [])):
        add_float("ManualFocusDistance", prefix + "FocusSettings.ManualFocusDistance")
    if profile.get("focus_settings_present"):
        add_object("TrackingFocusActor", prefix + "FocusSettings.TrackingFocusSettings.ActorToTrack")


def apply_static_camera_values(context, record, profile, state):
    """写入新相机的初始镜头状态，尤其是完整 FocusSettings（含跟踪对象）。"""
    component = record["component"]
    filmback = state.get("filmback")
    if filmback:
        current = prop(component, "filmback", None)
        if current is not None:
            for key, value in filmback.items():
                try:
                    setattr(current, key, value)
                except Exception:
                    pass
            set_prop(component, "filmback", current)
    if state.get("aspect_ratio") is not None:
        set_prop(component, "aspect_ratio", state["aspect_ratio"])
    if state.get("constrain_aspect_ratio") is not None:
        set_prop(component, "constrain_aspect_ratio", state["constrain_aspect_ratio"])
    if not profile.get("focal_varies") and profile.get("focal"):
        set_prop(component, "current_focal_length", profile["focal"][0])
    if state.get("aperture") is not None:
        set_prop(component, "current_aperture", state["aperture"])
    source_focus = state.get("focus_settings")
    if source_focus is not None:
        # 用官方 setter 优先；直接 set_editor_property 作为小版本兼容回退。
        setter = getattr(component, "set_focus_settings", None)
        copied = False
        if callable(setter):
            try:
                setter(source_focus)
                copied = True
            except Exception:
                pass
        if not copied:
            copied = set_prop(component, "focus_settings", source_focus)
        if not copied:
            context.warn(f"{record['name']}: 无法复制完整 FocusSettings，聚焦对象可能丢失")
        # 某些 Python 绑定对嵌套结构体的直接赋值不完整，再显式补写关键字段。
        focus_settings = prop(component, "focus_settings", None)
        if focus_settings is not None:
            if state.get("focus_method") is not None:
                set_prop(focus_settings, "focus_method", state["focus_method"])
            if state.get("focus") is not None and not profile.get("focus_varies"):
                set_prop(focus_settings, "manual_focus_distance", state["focus"])
            tracking = prop(focus_settings, "tracking_focus_settings", None)
            source_tracking = prop(source_focus, "tracking_focus_settings", None)
            if tracking is not None and source_tracking is not None:
                if state.get("focus_actor") is not None:
                    set_prop(tracking, "actor_to_track", state["focus_actor"])
                if state.get("focus_offset") is not None:
                    set_prop(tracking, "relative_offset", state["focus_offset"])
                set_prop(focus_settings, "tracking_focus_settings", tracking)
            set_prop(component, "focus_settings", focus_settings)


def finish_shot_hold(context, record, shot, samples):
    """Cut 边界的双侧 Hold：首帧与末段 key Constant，通道前后外推 Constant。

    这是"任何子帧都不能看到 A→B 插值"的第二、第三层保护：
      层1 采样本身不含 end（range 语义）
      层2 末段 key 用 RCIM_Constant → (end-1, end) 区间恒等于 A
      层3 post-infinity extrapolation = Constant → 即使越过末键也不会 Bezier/线性外推
    """
    constant_interp = enum_member("RichCurveInterpMode", "RCIM_Constant", "RCIM_CONSTANT")
    constant_extra = enum_member("RichCurveExtrapolation", "RCCE_Constant", "RCCE_CONSTANT")
    if constant_interp is None:
        context.warn("unreal.RichCurveInterpMode.RCIM_CONSTANT 不可用；"
                     "末键无法强制 Constant，只能依赖 section 边界（风险更高，请检查 MRQ 结果）")
    hold_from = shot.end - 1
    report = {"hold_keys": 0, "start_hold_keys": 0, "channels": 0,
              "pre_extrapolation_set": 0, "extrapolation_set": 0,
              "interp_available": constant_interp is not None}
    all_channels = list(record["channels"].values())
    for entry in record["lens_tracks"].values():
        # ObjectPath（聚焦对象）本身是离散引用，不支持数值曲线的
        # interpolation / extrapolation；它会保持当前对象直到下一个 key。
        if not entry.get("object"):
            all_channels.append(entry["channel"])
    for channel in all_channels:
        report["channels"] += 1
        keys = list(channel.get_keys())
        if not keys:
            continue
        first_key = min(keys, key=key_display_time)
        last_key = max(keys, key=key_display_time)
        if constant_interp is not None:
            try:
                # Frame Close 下，切点后的新相机首帧不允许从切点前的历史值插值进来。
                first_key.set_interpolation_mode(constant_interp)
                report["start_hold_keys"] += 1
            except Exception as exc:
                context.warn(f"{record['name']}: 设置首帧 key 的 Constant 插值失败：{exc}")
        for key in keys:
            frame, _ = key_display_time(key)
            if frame >= hold_from and constant_interp is not None:
                try:
                    key.set_interpolation_mode(constant_interp)
                    report["hold_keys"] += 1
                except Exception as exc:
                    context.warn(f"{record['name']}: 设置末段 key 的 Constant 插值失败：{exc}")
        if constant_interp is not None:
            try:
                last_key.set_interpolation_mode(constant_interp)
            except Exception:
                pass
        if constant_extra is not None:
            try:
                channel.set_pre_infinity_extrapolation(constant_extra)
                report["pre_extrapolation_set"] += 1
            except Exception as exc:
                context.warn(f"{record['name']}: 设置 pre-extrapolation 失败：{exc}")
        if constant_extra is not None:
            try:
                channel.set_post_infinity_extrapolation(constant_extra)
                report["extrapolation_set"] += 1
            except Exception as exc:
                context.warn(f"{record['name']}: 设置 post-extrapolation 失败：{exc}")
    record["hold_report"] = report
    return report


def bake_shot(context, evaluator, shot, record, samples):
    """逐采样烘焙一镜：读源相机最终世界 Transform/镜头参数 → 写新相机的 key。"""
    cfg = context.cfg
    channels = record["channels"]
    interp = key_interpolation_member()
    tick_resolution = context.tick_resolution
    display_rate = context.display_rate
    cache = {}
    previous_rotation = None
    written = 0

    # scale 不参与动画：只写一个常量，避免继承任何缩放
    for axis in "XYZ":
        add_channel_key(channels[f"Scale.{axis}"], shot.start, 0.0, 1.0, interp,
                        tick_resolution, display_rate)

    for sample in samples:
        state = sample_state(context, evaluator, sample.read_frame, sample.read_sub, cache)
        rotation = unwrap_rotator(previous_rotation, state["rot"])
        previous_rotation = rotation
        location = state["loc"]

        for axis, value in zip("XYZ", location):
            add_channel_key(channels[f"Location.{axis}"], sample.frame, sample.sub, value, interp,
                            tick_resolution, display_rate)
        # 注意通道语义：Rotation.X = Roll, Rotation.Y = Pitch, Rotation.Z = Yaw
        for axis, value in zip("XYZ", rotation):
            add_channel_key(channels[f"Rotation.{axis}"], sample.frame, sample.sub, value, interp,
                            tick_resolution, display_rate)
        if cfg["DO_BAKE_LENS"]:
            write_lens_keys(context, record, sample, state, interp, tick_resolution, display_rate)
        written += 1

    record["sample_count"] = written
    record["last_sample"] = (samples[-1].frame, samples[-1].sub) if samples else None
    return written


def write_lens_keys(context, record, sample, state, interp, tick_resolution, display_rate):
    if state.get("projection") == "ortho" and not record.get("ordinary_camera"):
        raise RuntimeError("检测到采样中途的正交切换，已停止，不能用透视近似代替；源相机保留")
    if record.get("ordinary_camera"):
        for name, key in (("FieldOfView", "field_of_view"), ("OrthoWidth", "ortho_width")):
            entry = record["lens_tracks"].get(name)
            if entry and state.get(key) is not None:
                add_channel_key(entry["channel"], sample.frame, sample.sub, state[key], interp, tick_resolution, display_rate)
        if sample.sub == 0:
            record["lens_tracks"]["ProjectionMode"]["channel"].add_key(
                unreal.FrameNumber(sample.frame), 1 if state.get("projection") == "ortho" else 0)
        return
    focal_entry = record["lens_tracks"].get("CurrentFocalLength")
    if focal_entry is not None and state.get("focal") is not None:
        add_channel_key(focal_entry["channel"], sample.frame, sample.sub, state["focal"], interp,
                        tick_resolution, display_rate)
    aperture_entry = record["lens_tracks"].get("CurrentAperture")
    if aperture_entry is not None and state.get("aperture") is not None:
        add_channel_key(aperture_entry["channel"], sample.frame, sample.sub, state["aperture"], interp,
                        tick_resolution, display_rate)
    focus_entry = record["lens_tracks"].get("ManualFocusDistance")
    if focus_entry is not None and state.get("focus") is not None:
        add_channel_key(focus_entry["channel"], sample.frame, sample.sub, state["focus"], interp,
                        tick_resolution, display_rate)
    actor_entry = record["lens_tracks"].get("TrackingFocusActor")
    if actor_entry is not None:
        add_object_channel_key(actor_entry["channel"], sample.frame, sample.sub,
                               state.get("focus_actor"), tick_resolution, display_rate)


# ============================================================================
# §6 Camera Cuts
# ============================================================================

def build_camera_cuts(context, plan, records):
    """删除旧 cut track，重建唯一的 Camera Cut Track：section 严格首尾相接，无 Gap / Overlap。"""
    sequence = context.sequence
    for track in list(sequence.get_tracks()):
        if isinstance(track, unreal.MovieSceneCameraCutTrack):
            sequence.remove_track(track)
    cut_track = sequence.add_track(unreal.MovieSceneCameraCutTrack)
    if cut_track is None:
        raise RuntimeError("无法创建 MovieSceneCameraCutTrack")
    sections = []
    for shot, record in zip(plan, records):
        section = cut_track.add_section()
        if section is None:
            raise RuntimeError(f"Shot{shot.index}: 无法创建 camera cut section")
        section.set_range(int(shot.start), int(shot.end))
        binding_id = sequence.get_binding_id(record["binding"])
        if binding_id is None:
            raise RuntimeError(f"Shot{shot.index}: get_binding_id 返回空")
        section.set_camera_binding_id(binding_id)
        sections.append(section)
    context.notes["camera_cut_sections"] = [
        {"shot": shot.index, "camera": record["name"], "start": shot.start, "end": shot.end}
        for shot, record in zip(plan, records)
    ]
    # 硬切说明：UMovieSceneCameraCutTrack::bCanBlend 构造默认 false，且 5.7 源码确认
    # 相邻不重叠 section 在无 easing 时 BlendTime == 0，即瞬时硬切、sub-frame 精度解析。
    # bCanBlend 是裸 UPROPERTY（无 setter、非 Blueprint），Python 改不了 —— 默认值正是我们要的。
    log(f"Camera Cut Track 已重建，{len(sections)} 个 section（要求 bCanBlend=False 才有硬切）")
    return cut_track, sections


# ============================================================================
# §7 验收
# ============================================================================

def verify(context, evaluator, plan, records, samples_by_shot):
    """结构断言 + 子帧 Hold 数值证明。

    核心验收条件：Cut=F 时，A 相机在 [F-1, F) 的所有子帧求值结果必须完全一致，
    B 相机从 F 起由自身曲线驱动。这样 MRQ 的 Temporal Sample Count=4 在 699 只会得到
    AAAAA 的 A，在 700 只会得到 B。
    """
    cfg = context.cfg
    tolerance = float(cfg["VERIFY_TOLERANCE_CM"])
    subsamples = int(cfg["BAKE_SUBSAMPLES"])
    result = {"pass": True, "tolerance_cm": tolerance, "subsamples": subsamples,
              "checks": [], "failures": []}

    def check(name, ok, detail):
        entry = {"check": name, "ok": bool(ok), "detail": detail}
        result["checks"].append(entry)
        if not ok:
            result["pass"] = False
            result["failures"].append(f"{name}: {detail}")

    # --- 结构 1：唯一 Camera Cut Track ---
    cut_tracks = [t for t in context.sequence.get_tracks()
                  if isinstance(t, unreal.MovieSceneCameraCutTrack)]
    check("唯一 CameraCutTrack", len(cut_tracks) == 1, f"找到 {len(cut_tracks)} 条")

    # --- 结构 2：section 严格首尾相接、范围与计划一致 ---
    if cut_tracks:
        sections = list(cut_tracks[0].get_sections())
        actual = sorted(
            (int(s.get_start_frame()), int(s.get_end_frame()), s) for s in sections)
        expected = [(shot.start, shot.end) for shot in plan]
        got = [(a, b) for a, b, _ in actual]
        check("cut section 范围", got == expected, f"期望 {expected} 实际 {got}")
        adjacent = all(actual[i][1] == actual[i + 1][0] for i in range(len(actual) - 1))
        check("cut section 无 Gap/Overlap", adjacent, "相邻 section 端点未严格相接")

    # --- 结构 3：每镜不存在 >= end 的 key，末键为 Constant，且 key 恰好覆盖到预期末样本 ---
    for shot, record in zip(plan, records):
        samples = samples_by_shot.get(shot.index) or []
        if not samples:
            check(f"Shot{shot.index} 采样表可用", False, "缺少采样表，无法核对越界 key")
            continue
        expected_last = (samples[-1].frame, samples[-1].sub)
        offending = []
        hold_ok = True
        start_hold_ok = True
        coverage = True
        for name, channel in _iter_channels(record):
            keys = list(channel.get_keys())
            for key in keys:
                frame, sub = key_display_time(key)
                if (frame, sub) >= (shot.end, 0.0):
                    offending.append(f"{name}@{frame}+{sub}")
            if not keys:
                continue
            last = max(keys, key=key_display_time)
            first = min(keys, key=key_display_time)
            # Scale 只写一个常量 key，不参与逐帧覆盖
            if not name.startswith("Scale.") and key_display_time(last) != expected_last:
                coverage = False
            try:
                mode = last.get_interpolation_mode()
            except Exception:
                mode = None
            constant = enum_member("RichCurveInterpMode", "RCIM_Constant", "RCIM_CONSTANT")
            if constant is not None and mode is not None and mode != constant:
                hold_ok = False
            if shot.start != plan[0].start and constant is not None:
                try:
                    first_mode = first.get_interpolation_mode()
                except Exception:
                    first_mode = None
                if first_mode is not None and first_mode != constant:
                    start_hold_ok = False
        check(f"Shot{shot.index} 无越界 key", not offending, f"越界 key：{offending[:6]}")
        check(f"Shot{shot.index} key 覆盖到末样本", coverage,
              f"期望末键 {expected_last}，存在未覆盖到末样本的通道")
        check(f"Shot{shot.index} 末键 Constant", hold_ok, "存在末键插值不是 Constant 的通道")
        if shot.start != plan[0].start:
            check(f"Shot{shot.index} 首键 Constant", start_hold_ok,
                  "切点后的首键插值不是 Constant，Frame Close 可能发生跨切点插值")

    # --- 结构 4：镜头参数轨道真的生效（读回求值后的 new camera 组件属性） ---
    # --- 结构 4.5：逐点对照源相机与生成相机的世界位姿 ---
    # 这条检查专门抓住 Quaternion 被误当成 Rotator、或生成相机落在错误世界中的问题。
    source_actor = getattr(context, "source_actor", None)
    if source_actor is not None:
        rotation_tolerance = float(cfg.get("VERIFY_TOLERANCE_DEG", 1e-4))
        for shot, record in zip(plan, records):
            probe_frames = sorted(set((shot.start, min(shot.start + 1, shot.end - 1), shot.end - 1)))
            for frame in probe_frames:
                evaluator.jump_to(frame, 0.0)
                expected = read_source_state(context)
                actual = _world_state(record["actor"])
                location_error = vector_delta(expected["loc"], actual["loc"])
                rotation_error = _wrapped_rotator_delta(expected["rot"], actual["rot"])
                check(f"Shot{shot.index} 位姿对照@{frame}",
                      location_error <= tolerance and rotation_error <= rotation_tolerance,
                      f"位置误差 {location_error:.9f} cm，旋转误差 {rotation_error:.9f} deg")

    # --- 结构 5：镜头参数轨道真的生效（读回求值后的 new camera 组件属性） ---
    # 这条检查把"属性路径是否真的绑定到组件上"从猜测变成运行时实测：
    # actor_property_path（spawnable 路径）与 component_possessable 两种写法都靠它兜底。
    for shot, record in zip(plan, records):
        if record.get("ordinary_camera"):
            for frame in sorted(set((shot.start, shot.end - 1))):
                evaluator.jump_to(frame, 0.0)
                expected = read_source_state(context)
                actual_mode = prop(record["component"], "projection_mode", None)
                ortho = enum_member("CameraProjectionMode", "Orthographic", "ORTHOGRAPHIC")
                check(f"Shot{shot.index} 投影模式@{frame}",
                      (actual_mode == ortho) == (expected["projection"] == "ortho"), str(actual_mode))
                key = "ortho_width" if expected["projection"] == "ortho" else "field_of_view"
                actual_value = prop(record["component"], key, None)
                expected_value = expected.get(key)
                check(f"Shot{shot.index} {key}@{frame}", actual_value is not None and expected_value is not None and
                      abs(float(actual_value) - float(expected_value)) < .01, str(actual_value))
        entry = record["lens_tracks"].get("CurrentFocalLength")
        expected_focal = record.get("expected_focal_at_probe")
        if entry is None or expected_focal is None:
            continue
        evaluator.jump_to(shot.start, 0.0)
        actual_focal = prop(record["component"], "current_focal_length", None)
        if actual_focal is None:
            check(f"Shot{shot.index} 焦距轨道生效", False, "读不到 current_focal_length")
        else:
            ok = abs(float(actual_focal) - float(expected_focal)) < 1e-2
            check(f"Shot{shot.index} 焦距轨道生效", ok,
                  f"期望 {expected_focal:.4f} mm，实际 {actual_focal:.4f} mm")

        # 光圈与 FocusSettings 不能只看轨道是否存在，要读回求值后的组件。
        # 这里至少核对每镜首/尾帧；逐帧轨道覆盖则由后面的 key 覆盖检查保证。
        for frame in sorted(set((shot.start, shot.end - 1))):
            evaluator.jump_to(frame, 0.0)
            expected = read_source_state(context)
            actual_aperture = prop(record["component"], "current_aperture", None)
            expected_aperture = expected.get("aperture")
            if expected_aperture is not None and actual_aperture is not None:
                check(f"Shot{shot.index} 光圈生效@{frame}",
                      abs(float(actual_aperture) - float(expected_aperture)) < 1e-3,
                      f"期望 {float(expected_aperture):.6f}，实际 {float(actual_aperture):.6f}")

            expected_focus = expected.get("focus_settings")
            actual_focus = prop(record["component"], "focus_settings", None)
            if expected_focus is not None and actual_focus is not None:
                expected_method = object_identity(prop(expected_focus, "focus_method", None))
                actual_method = object_identity(prop(actual_focus, "focus_method", None))
                if expected_method is not None:
                    check(f"Shot{shot.index} 聚焦模式生效@{frame}",
                          expected_method == actual_method,
                          f"期望 {expected_method}，实际 {actual_method}")
                expected_tracking = prop(expected_focus, "tracking_focus_settings", None)
                actual_tracking = prop(actual_focus, "tracking_focus_settings", None)
                expected_actor = object_identity(prop(expected_tracking, "actor_to_track", None))
                actual_actor = object_identity(prop(actual_tracking, "actor_to_track", None))
                if expected_actor is not None or actual_actor is not None:
                    check(f"Shot{shot.index} 聚焦对象生效@{frame}",
                          expected_actor == actual_actor,
                          f"期望 {expected_actor}，实际 {actual_actor}")

    # --- 核心：子帧 Hold 数值证明（等价于 MRQ 的 AAAA / BBBB）---
    records_by_shot = {record["shot"]: record for record in records}
    for index, shot in enumerate(plan):
        if index + 1 >= len(plan):
            break
        next_shot = plan[index + 1]
        cut = next_shot.start
        if cut != shot.end:
            check(f"Cut{cut} 相邻性", False, f"Shot{shot.index} end={shot.end} != {cut}")
            continue
        base_frame, base_sub = cut - 1, 0.0
        evaluator.jump_to(base_frame, base_sub)
        base = _world_state(records_by_shot[shot.index]["actor"])
        max_delta = 0.0
        # 即使 BAKE_SUBSAMPLES=1，也必须固定检查 1/4 帧；否则原先的
        # range(1, 1) 会让所谓的子帧验收一次都不执行。
        hold_subs = sorted(set([0.25] + [k / float(subsamples) for k in range(1, subsamples)]))
        for sub in hold_subs:
            evaluator.jump_to(base_frame, sub)
            current = _world_state(records_by_shot[shot.index]["actor"])
            max_delta = max(max_delta, _state_delta(base, current))
        check(f"Cut{cut} 上一镜子帧 Hold", max_delta <= tolerance,
              f"[{cut-1}, {cut}) 内含 1/4 帧在内的子帧相对 {cut-1} 最大变化 "
              f"{max_delta:.9f} cm/deg（应为 0）")

        # 对照：确认这不是"整镜冻结" —— 前一帧到末帧仍有正常运动
        evaluator.jump_to(base_frame - 1, 0.0)
        before = _world_state(records_by_shot[shot.index]["actor"])
        motion = _state_delta(before, base)
        forward = 0.0
        evaluator.jump_to(cut, 0.0)
        start_state = _world_state(records_by_shot[next_shot.index]["actor"])
        for sub in hold_subs:
            evaluator.jump_to(cut, sub)
            forward = max(forward, _state_delta(
                start_state, _world_state(records_by_shot[next_shot.index]["actor"])))
        result["checks"].append({
            "check": f"Cut{cut} 上下文", "ok": True,
            "detail": f"上一镜末帧相邻的正常运动 {motion:.4f}，"
                      f"下一镜 [{cut}, {cut+1}) 内自身运动 {forward:.4f}（B 未被冻结）",
        })
    return result


def _iter_channels(record):
    for name, channel in record["channels"].items():
        yield name, channel
    for name, entry in record["lens_tracks"].items():
        yield name, entry["channel"]


def _world_state(actor):
    transform = actor.get_actor_transform()
    location = prop(transform, "translation")
    rotation = prop(transform, "rotation")
    return {
        "loc": (float(prop(location, "x", 0.0)), float(prop(location, "y", 0.0)),
                float(prop(location, "z", 0.0))),
        "rot": rotator_degrees(rotation),
    }


def _state_delta(a, b):
    return max(vector_delta(a["loc"], b["loc"]), rotator_delta(a["rot"], b["rot"]))


def _wrapped_rotator_delta(a, b):
    """比较两个可能跨越 ±180/360 的欧拉角，避免等价角度被误判为大旋转。"""
    return max(abs(((float(b[i]) - float(a[i]) + 180.0) % 360.0) - 180.0) for i in range(3))


# ============================================================================
# §8 检测（MODE = "detect"，只读）
# ============================================================================

def run_detect(context, evaluator, plan):
    """逐帧求值源相机，按"绝对阈值 + 局部中位数比值"输出疑似 Cut。

    相对阈值的意义：快速运镜本身也可能有大位移，但它是"附近一段时间的正常运动量"，
    delta / median(邻域) 会接近 1；硬切则会高出一到两个数量级。
    """
    cfg = context.cfg
    step = max(1, int(cfg["DETECT_SAMPLE_STEP"]))
    states = []
    total = 0
    for shot in plan:
        total += shot.end - shot.start
    done = 0
    previous_rotation = None
    for shot in plan:
        for frame in range(shot.start, shot.end, step):
            state = sample_state(context, evaluator, frame, 0.0)
            rotation = unwrap_rotator(previous_rotation, state["rot"])
            previous_rotation = rotation
            states.append((frame, state["loc"], rotation, state["focal"]))
            done += 1
            if done % max(1, cfg["PROGRESS_EVERY"] // step) == 0:
                log(f"检测采样 {done}/{total}（frame={frame}）")

    pos_deltas, rot_deltas, focal_deltas = [], [], []
    for i in range(1, len(states)):
        _, loc_a, rot_a, focal_a = states[i - 1]
        _, loc_b, rot_b, focal_b = states[i]
        pos_deltas.append(vector_delta(loc_a, loc_b))
        rot_deltas.append(rotator_delta(rot_a, rot_b))
        focal_deltas.append(abs((focal_b or 0.0) - (focal_a or 0.0)))

    if step != 1:
        context.warn(f"DETECT_SAMPLE_STEP={step}，候选帧按步长外推，帧号可能不精确")
    candidates = detect_candidates(pos_deltas, rot_deltas, focal_deltas,
                                  states[0][0] if states else 0, cfg)
    frames = [frame for frame, _, _ in candidates]
    print("")
    print(f"Possible Camera Cuts: {frames}")
    print("")
    for frame, score, detail in candidates:
        print(f"  frame {frame}: score={score:.1f} reasons={detail['reasons']} "
              f"pos={detail['position_delta_cm']:.2f}cm(x{detail['position_ratio']:.1f}) "
              f"rot={detail['rotation_delta_deg']:.2f}deg(x{detail['rotation_ratio']:.1f}) "
              f"focal={detail['focal_delta_mm']:.2f}mm(x{detail['focal_ratio']:.1f})")
    print("")
    log("检测仅为只读结果：请人工确认后把帧号填进 CUT_FRAMES，再用 MODE='bake' 执行。")
    return {"candidates": frames,
            "details": [{"frame": f, "score": s, "detail": d} for f, s, d in candidates],
            "probe_count": len(states), "step": step}


# ============================================================================
# §9 probe / main
# ============================================================================

PROBE_ITEMS = (
    # (显示名, 取值路径, 用途)；路径含 "." 时按 Class.method / instance.method 逐级 getattr
    ("unreal.LevelSequenceEditorBlueprintLibrary", "unreal.LevelSequenceEditorBlueprintLibrary", "求值入口（LevelSequenceEditor 插件）"),
    ("open_level_sequence", "LSE.open_level_sequence", "必须能打开序列，否则该库所有函数静默 no-op"),
    ("set_global_position", "LSE.set_global_position", "子帧跳转（5.4+；更旧版本只有 set_current_time(int)）"),
    ("force_update", "LSE.force_update", "结构性改动后强制整树刷新并立即求值"),
    ("get_bound_objects", "LSE.get_bound_objects", "解析 spawnable 源相机"),
    ("set_lock_camera_cut_to_viewport", "LSE.set_lock_camera_cut_to_viewport", "求值期间避免视口被 Camera Cut 抢走"),
    ("unreal.MovieSceneSequencePlaybackParams", "unreal.MovieSceneSequencePlaybackParams", "时间参数结构体（用秒注入小数帧）"),
    ("unreal.MovieScenePositionType", "unreal.MovieScenePositionType", "Frame / Time 选择"),
    ("unreal.MovieSceneTimeUnit", "unreal.MovieSceneTimeUnit", "DisplayRate / TickResolution"),
    ("unreal.MovieScene3DTransformTrack", "unreal.MovieScene3DTransformTrack", "相机 Transform 轨道"),
    ("unreal.MovieScene3DTransformSection", "unreal.MovieScene3DTransformSection", "Transform section（通道懒创建）"),
    ("unreal.MovieSceneScriptingDoubleChannel", "unreal.MovieSceneScriptingDoubleChannel", "Transform 通道是 Double（不是 Float）"),
    ("unreal.MovieSceneScriptingDoubleKey", "unreal.MovieSceneScriptingDoubleKey", "逐 key 设置插值模式"),
    ("unreal.MovieSceneFloatTrack", "unreal.MovieSceneFloatTrack", "焦距 / 光圈轨道"),
    ("unreal.MovieSceneObjectPropertyTrack", "unreal.MovieSceneObjectPropertyTrack", "逐帧保持聚焦对象"),
    ("unreal.MovieSceneScriptingObjectPathChannel", "unreal.MovieSceneScriptingObjectPathChannel", "聚焦对象引用通道"),
    ("unreal.MovieSceneCameraCutTrack", "unreal.MovieSceneCameraCutTrack", "Camera Cuts 轨道"),
    ("unreal.MovieSceneCameraCutSection", "unreal.MovieSceneCameraCutSection", "cut section"),
    ("unreal.CineCameraActor", "unreal.CineCameraActor", "镜头相机"),
    ("unreal.CineCameraComponent", "unreal.CineCameraComponent", "镜头参数来源"),
    ("unreal.EditorActorSubsystem", "unreal.EditorActorSubsystem", "生成 / 销毁关卡 actor"),
    ("unreal.EditorAssetLibrary", "unreal.EditorAssetLibrary", "备份 / 保存资产"),
    ("unreal.LevelSequenceEditorSubsystem", "unreal.LevelSequenceEditorSubsystem", "spawnable 回退路径"),
    ("unreal.RichCurveInterpMode", "unreal.RichCurveInterpMode", "末键 Constant（Hold 第二层）"),
    ("unreal.RichCurveExtrapolation", "unreal.RichCurveExtrapolation", "post 外推 Constant（Hold 第三层）"),
    ("unreal.CameraProjectionMode", "unreal.CameraProjectionMode", "识别源相机是否正交"),
    # --- 以下是最容易"写错就静默失败"的方法级符号，逐个体检 ---
    ("LevelSequence.add_possessable", "unreal.LevelSequence.add_possessable", "possess 关卡 actor"),
    ("LevelSequence.add_track", "unreal.LevelSequence.add_track", "建 master track（Camera Cuts）"),
    ("LevelSequence.get_bindings", "unreal.LevelSequence.get_bindings", "遍历 binding"),
    ("LevelSequence.get_binding_id", "unreal.LevelSequence.get_binding_id", "Camera Cut 绑定 id"),
    ("LevelSequence.remove_track", "unreal.LevelSequence.remove_track", "删除旧 Camera Cut Track"),
    ("MovieSceneTrackExtensions.add_section", "unreal.MovieScene3DTransformTrack.add_section", "建 section"),
    ("MovieSceneSectionExtensions.set_range", "unreal.MovieScene3DTransformSection.set_range", "section 范围 [start,end)"),
    ("MovieSceneSectionExtensions.get_channel", "unreal.MovieScene3DTransformSection.get_channel", "按名字取通道（索引会错位）"),
    ("MovieSceneCameraCutSection.set_camera_binding_id", "unreal.MovieSceneCameraCutSection.set_camera_binding_id", "指定该 cut 的相机"),
    ("DoubleChannel.add_key", "unreal.MovieSceneScriptingDoubleChannel.add_key", "写 Transform key"),
    ("DoubleChannel.set_pre_infinity_extrapolation", "unreal.MovieSceneScriptingDoubleChannel.set_pre_infinity_extrapolation", "首帧边界 Hold"),
    ("DoubleChannel.set_post_infinity_extrapolation", "unreal.MovieSceneScriptingDoubleChannel.set_post_infinity_extrapolation", "Hold 的第三层保护"),
    ("DoubleKey.set_interpolation_mode", "unreal.MovieSceneScriptingDoubleKey.set_interpolation_mode", "Hold 的第二层保护（Constant）"),
    ("PropertyTrack.set_property_name_and_path", "unreal.MovieSceneFloatTrack.set_property_name_and_path", "焦距轨道属性绑定"),
    ("ObjectPathChannel.add_key", "unreal.MovieSceneScriptingObjectPathChannel.add_key", "写入逐帧聚焦对象"),
    ("BindingProxy.add_track", "unreal.MovieSceneBindingProxy.add_track", "在 binding 上建轨道"),
    ("BindingProxy.remove", "unreal.MovieSceneBindingProxy.remove", "幂等清理"),
    ("BindingProxy.get_object_template", "unreal.MovieSceneBindingProxy.get_object_template", "spawnable 模板 actor"),
)


def _has(obj, name):
    if obj is None:
        return False
    try:
        return hasattr(obj, name)
    except Exception:
        return False


def _resolve_probe_path(path):
    """把 "unreal.Foo.bar" / "LSE.bar" 解析成 (目标对象, 属性名)，失败返回 (None, 末段名)。"""
    parts = path.split(".")
    target = None
    if parts[0] == "unreal":
        target = unreal
        parts = parts[1:]
    elif parts[0] == "LSE":
        # 5.5+ 不要实例化 BlueprintFunctionLibrary，直接在类上查方法
        target = getattr(unreal, "LevelSequenceEditorBlueprintLibrary", None)
        if target is None:
            return None, parts[-1]
        parts = parts[1:]
    for part in parts[:-1]:
        target = getattr(target, part, None)
        if target is None:
            return None, parts[-1]
    return target, parts[-1]


def run_probe():
    """符号体检：不写任何数据，只报告本脚本依赖的 API 是否真实存在。"""
    report = {"engine_version": None, "items": [], "missing": [], "enum_members": {}}
    if unreal is None:
        print("probe 模式必须在 UE 编辑器内运行")
        return report
    report["engine_version"] = unreal.SystemLibrary.get_engine_version()
    for label, path, purpose in PROBE_ITEMS:
        target, name = _resolve_probe_path(path)
        ok = target is not None and _has(target, name)
        report["items"].append({"symbol": label, "ok": ok, "purpose": purpose})
        if not ok:
            report["missing"].append({"symbol": label, "purpose": purpose})

    # 关键枚举成员：不同小版本拼写可能不同
    for enum_name, members in (("RichCurveInterpMode", ("RCIM_Constant", "RCIM_CONSTANT")),
                               ("RichCurveExtrapolation", ("RCCE_Constant", "RCCE_CONSTANT")),
                               ("MovieSceneKeyInterpolation", ("Linear", "LINEAR", "Constant")),
                               ("MovieSceneTimeUnit", ("DisplayRate", "DISPLAY_RATE")),
                               ("MovieSceneEvaluationType", ("WithSubFrames", "WITH_SUB_FRAMES")),
                               ("MovieScenePositionType", ("Time", "TIME")),
                               ("CameraProjectionMode", ("Orthographic", "ORTHOGRAPHIC"))):
        enum = getattr(unreal, enum_name, None)
        if enum is None:
            report["enum_members"][enum_name] = None
            continue
        available = sorted(a for a in dir(enum) if not a.startswith("_"))
        resolved = None
        for member in members:
            if member in available:
                resolved = member
                break
        report["enum_members"][enum_name] = {"resolved": resolved, "available": available}
        if resolved is None:
            report["missing"].append({"symbol": f"unreal.{enum_name}.{members}", "purpose": "枚举成员"})

    print("")
    print("=== CameraCutAuto probe ===")
    print(f"engine: {report['engine_version']}")
    for item in report["items"]:
        print(f"  [{'OK ' if item['ok'] else 'MISS'}] {item['symbol']:<52} {item['purpose']}")
    for enum_name, info in report["enum_members"].items():
        if info is None:
            print(f"  [MISS] unreal.{enum_name}")
        else:
            mark = "OK " if info["resolved"] else "MISS"
            print(f"  [{mark}] unreal.{enum_name} -> {info['resolved']}  {info['available'][:8]}")
    print("")
    if report["missing"]:
        print(f"缺失 {len(report['missing'])} 项，请对照 README 的替代实现表：")
        for item in report["missing"]:
            print(f"  - {item['symbol']}（{item['purpose']}）")
    else:
        print("全部符号存在。")
    print("")
    return report


def load_config():
    """读取脚本内常量，允许 CAMCUT_CONFIG 指向的 JSON 覆盖。"""
    config = {key: globals()[key] for key in CONFIG_KEYS}
    path = os.environ.get("CAMCUT_CONFIG")
    if not path:
        return config, None
    with open(path, "r", encoding="utf-8") as handle:
        override = json.load(handle)
    for key, value in override.items():
        if key.startswith("_"):      # 允许用 "_说明" 之类的键在 JSON 里写注释
            continue
        if key not in config:
            warn(f"配置文件里的未知键被忽略：{key}")
            continue
        config[key] = value
    log(f"已从 {path} 覆盖 {len(override)} 项配置")

    # JSON 里的数字可能是浮点，统一收紧成整数，避免 set_range 之类静默取整
    config["MODE"] = str(config["MODE"]).strip().lower()
    if config["MODE"] not in ("probe", "detect", "bake", "verify"):
        raise ConfigError(f"MODE 必须是 probe/detect/bake/verify，当前 {config['MODE']!r}")
    config["SEQUENCE_START"] = int(config["SEQUENCE_START"])
    config["SEQUENCE_END"] = None if config["SEQUENCE_END"] is None else int(config["SEQUENCE_END"])
    config["CUT_FRAMES"] = [int(item) for item in (config["CUT_FRAMES"] or [])]
    config["BAKE_SUBSAMPLES"] = int(config["BAKE_SUBSAMPLES"])
    config["PROGRESS_EVERY"] = int(config["PROGRESS_EVERY"])
    config["DETECT_SAMPLE_STEP"] = int(config["DETECT_SAMPLE_STEP"])
    config["DETECT_SUPPRESS"] = int(config["DETECT_SUPPRESS"])
    config["DETECT_WINDOW"] = int(config["DETECT_WINDOW"])
    return config, path


def script_directory():
    """本脚本所在目录（被 exec 执行时 __file__ 可能不存在，退到项目 Saved 目录）。"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        fallback = prop(unreal.Paths, "project_saved_dir", None)
        try:
            fallback = unreal.Paths.project_saved_dir()
        except Exception:
            fallback = os.getcwd()
        return os.path.join(fallback, "ZCodeCameraCutBaker")


def report_directory(config):
    base = config.get("REPORT_DIR")
    if not base:
        base = os.path.join(script_directory(), "runs")
    os.makedirs(base, exist_ok=True)
    return base


def write_report(name, payload):
    path = os.path.join(report_directory(CFG_KEYS_CACHE), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
    log(f"报告：{path}")
    return path


def run_bake(config, context):
    cfg = config
    sequence = context.sequence
    plan = plan_shots(cfg["CUT_FRAMES"], cfg["SEQUENCE_START"], cfg["resolved_end"])
    log(f"镜头计划：{[f'[{s.start},{s.end})' for s in plan]}")

    # 备份必须在任何写操作之前完成；main 已经做过则跳过（backup_path 已存在）。
    if cfg["MAKE_BACKUP"] and not cfg.get("backup_path"):
        cfg["backup_path"] = make_backup(context, cfg["SOURCE_SEQUENCE"], "Bake")

    evaluator = context.evaluator
    profile = scan_source_profile(context, evaluator, plan)
    context.source_profile = profile
    log(f"源相机扫描：focal_varies={profile['focal_varies']} "
        f"focal_source={profile['focal_source']} filmback_varies={profile['filmback_varies']} "
        f"probe_frames={profile['probe_frames']}")

    cleanup_generated(context)
    evaluator.warm_up()

    records = []
    samples_by_shot = {}
    for shot in plan:
        name = f"{cfg['GENERATED_PREFIX']}{shot.index:03d}"
        log(f"Shot{shot.index:03d} [{shot.start}, {shot.end}) → {name}")
        record = create_shot_camera(context, name)
        record["shot"] = shot.index
        create_transform_track(context, record, shot.start, shot.end)
        create_lens_tracks(context, record, shot.start, shot.end, profile)
        apply_static_camera_values(context, record, profile,
                                   sample_state(context, evaluator, shot.start, 0.0))
        evaluator.warm_up()   # 新建轨道是结构性改动，先让 Sequencer 吃进去再求值
        samples = sample_times(shot.start, shot.end, cfg["BAKE_SUBSAMPLES"])
        samples_by_shot[shot.index] = samples
        baked = bake_shot(context, evaluator, shot, record, samples)
        hold = finish_shot_hold(context, record, shot, samples)
        # 记录一个"焦距轨道是否生效"的对照点，验收时读回比较
        probe_state = sample_state(context, evaluator, shot.start, 0.0)
        record["expected_focal_at_probe"] = probe_state.get("focal")
        record["baked_samples"] = baked
        record["hold"] = hold
        context.generated.append(record)
        records.append(record)
        log(f"  {name}: {baked} keys/通道 × {len(record['channels']) + len(record['lens_tracks'])} 通道，"
            f"末段 Constant key={hold['hold_keys']}，末次采样={record['last_sample']}")

    evaluator.warm_up()
    build_camera_cuts(context, plan, records)
    evaluator.warm_up()

    verification = None
    if cfg["VERIFY_AFTER_BAKE"]:
        verification = verify(context, evaluator, plan, records, samples_by_shot)
        log(f"验收：{'PASS' if verification['pass'] else 'FAIL'}")

    if cfg["SAVE_SEQUENCE"]:
        try:
            unreal.EditorAssetLibrary.save_loaded_asset(sequence, False)
            log(f"已保存序列：{sequence.get_path_name()}")
        except Exception as exc:
            context.warn(f"保存序列失败：{exc}")
    if cfg["SAVE_LEVEL"]:
        try:
            unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
            log("已保存关卡")
        except Exception as exc:
            context.warn(f"保存关卡失败：{exc}")
    else:
        context.warn("生成相机是 level actor：脚本没有自动保存关卡。"
                     "请确认画面后自行 Ctrl+S 保存关卡，否则重启后绑定会失效。"
                     "（无头批量运行可设 SAVE_LEVEL=True）")

    return {
        "mode": "bake",
        "engine_version": unreal.SystemLibrary.get_engine_version(),
        "sequence": sequence.get_path_name(),
        "sequence_working_copy": cfg.get("working_sequence_path"),
        "backup": cfg.get("backup_path"),
        "display_rate": list(context.display_rate),
        "tick_resolution": list(context.tick_resolution),
        "bake_subsamples": cfg["BAKE_SUBSAMPLES"],
        "key_interpolation": cfg["KEY_INTERPOLATION"],
        "use_spawnable": cfg["USE_SPAWNABLE"],
        "source_camera": context.source_actor.get_actor_label(),
        "source_resolved_by": context.notes.get("source_resolved_by"),
        "source_profile": {key: (sorted(value) if isinstance(value, set) else value)
                           for key, value in profile.items()},
        "shots": [{
            "index": shot.index, "name": record["name"], "start": shot.start, "end": shot.end,
            "samples": record.get("baked_samples"),
            "last_sample": record.get("last_sample"),
            "mode": record["mode"],
        } for shot, record in zip(plan, records)],
        "hold_report": [record.get("hold_report") for record in records],
        "cleanup": context.notes.get("cleanup"),
        "camera_cut_sections": context.notes.get("camera_cut_sections"),
        "verification": verification,
        "warnings": context.warnings,
        "notes": {k: v for k, v in context.notes.items()},
    }


def run_verify_only(config, context):
    sequence = context.sequence
    plan = plan_shots(config["CUT_FRAMES"], config["SEQUENCE_START"], config["resolved_end"])
    records = []
    for shot in plan:
        name = f"{config['GENERATED_PREFIX']}{shot.index:03d}"
        binding = None
        for item in sequence.get_bindings():
            if item.get_name() == name:
                binding = item
                break
        if binding is None:
            raise ConfigError(f"找不到生成相机 {name}，请先运行 MODE='bake'")
        actor = binding.get_object_template()
        if actor is None:
            for candidate in all_level_actors():
                if (candidate.get_actor_label() or "") == name:
                    actor = candidate
                    break
        if actor is None:
            raise ConfigError(f"找不到 {name} 的 actor（possessable 需关卡里存在同名 actor）")
        record = {"name": name, "actor": actor, "component": _camera_component(actor),
                  "channels": {}, "lens_tracks": {}, "shot": shot.index, "binding": binding}
        # 验收需要的通道直接从序列里读回，避免依赖本次运行的内存状态
        section = _transform_section_of(binding)
        if section is not None:
            for channel_name in TRANSFORM_CHANNELS:
                channel = section.get_channel(unreal.Name(channel_name))
                if channel is not None:
                    record["channels"][channel_name] = channel
        for track in binding.get_tracks():
            object_track = getattr(unreal, "MovieSceneObjectPropertyTrack", None)
            if (isinstance(track, unreal.MovieSceneFloatTrack)
                    or (object_track is not None and isinstance(track, object_track))):
                prop_name = str(track.get_property_name())
                for sec in track.get_sections():
                    channels = sec.get_all_channels()
                    if channels:
                        record["lens_tracks"][prop_name] = {"track": track, "section": sec,
                                                            "channel": channels[0]}
        record["expected_focal_at_probe"] = None
        records.append(record)
    samples_by_shot = {shot.index: sample_times(shot.start, shot.end, config["BAKE_SUBSAMPLES"])
                       for shot in plan}
    return verify(context, context.evaluator, plan, records, samples_by_shot)


def _transform_section_of(binding):
    for track in binding.get_tracks():
        if isinstance(track, unreal.MovieScene3DTransformTrack):
            sections = track.get_sections()
            if sections:
                return sections[0]
    return None


def _asset_path(asset):
    try:
        return asset.get_path_name()
    except Exception:
        return str(asset)


def _open_level_sequence_candidates():
    """返回当前编辑器中已打开的 LevelSequence 资产，去重并把当前焦点放在最前。"""
    candidates = []
    current = None
    lse_class = getattr(unreal, "LevelSequenceEditorBlueprintLibrary", None)
    if lse_class is not None:
        try:
            current = lse_class.get_current_level_sequence()
        except Exception:
            current = None

    assets = []
    subsystem_class = getattr(unreal, "AssetEditorSubsystem", None)
    if subsystem_class is not None:
        try:
            subsystem = unreal.get_editor_subsystem(subsystem_class)
            assets = list(subsystem.get_all_edited_assets() or [])
        except Exception as exc:
            log(f"无法枚举已打开资产编辑器：{exc}")

    level_sequence_class = getattr(unreal, "LevelSequence", None)
    if level_sequence_class is not None:
        assets = [asset for asset in assets if isinstance(asset, level_sequence_class)]
    if current is not None and all(_asset_path(asset) != _asset_path(current) for asset in assets):
        assets.insert(0, current)

    seen = set()
    for asset in assets:
        path = _asset_path(asset)
        if path not in seen:
            seen.add(path)
            candidates.append(asset)
    if current is not None:
        candidates.sort(key=lambda asset: 0 if _asset_path(asset) == _asset_path(current) else 1)
    return candidates, current


def choose_open_level_sequence():
    """优先当前 Sequencer；多个已打开序列时用 UE 对话框逐个让用户确认。"""
    candidates, current = _open_level_sequence_candidates()
    if not candidates:
        raise ConfigError(
            "没有检测到已打开的 Level Sequence。请先在 UE 的 Sequencer 中打开目标关卡序列，"
            "然后重新执行脚本。")
    if len(candidates) == 1:
        selected = candidates[0]
        log(f"自动选择已打开序列：{_asset_path(selected)}")
        return selected

    dialog = getattr(unreal, "EditorDialog", None)
    show_message = getattr(dialog, "show_message", None) if dialog is not None else None
    message_type = enum_member("AppMsgType", "YesNo", "YES_NO")
    return_yes = enum_member("AppReturnType", "Yes", "YES")
    return_no = enum_member("AppReturnType", "No", "NO")
    if not callable(show_message) or message_type is None or return_yes is None:
        paths = "\n".join(f"  {index}. {_asset_path(asset)}"
                           for index, asset in enumerate(candidates, 1))
        raise ConfigError("检测到多个已打开 Level Sequence，但当前 UE 没有可用选择对话框：\n" + paths)

    current_path = _asset_path(current) if current is not None else None
    for index, asset in enumerate(candidates, 1):
        path = _asset_path(asset)
        marker = "（当前 Sequencer 焦点）" if path == current_path else ""
        result = show_message(
            "CameraCutAuto：选择关卡序列",
            f"检测到多个已打开的 Level Sequence，请确认是否使用第 {index}/{len(candidates)} 个：\n\n"
            f"{path}\n{marker}\n\n点击 Yes 使用此序列，点击 No 查看下一个。",
            message_type,
            return_no,
        )
        if result == return_yes:
            log(f"用户选择序列：{path}")
            return asset
    raise ConfigError("未选择任何 Level Sequence，脚本已中止，未写入数据。")


def existing_cut_layout(sequence):
    """Inspect without modifying native/upstream cuts; no automatic destructive rebake."""
    tracks = [t for t in sequence.get_tracks() if isinstance(t, unreal.MovieSceneCameraCutTrack)]
    if len(tracks) != 1:
        return None
    sections = sorted(tracks[0].get_sections(), key=lambda section: section.get_start_frame())
    if len(sections) < 2:
        return None
    # Our generated sequence is eligible for the existing verify implementation.
    if any(binding.get_name().startswith("AUTO_Cam_Shot_") for binding in sequence.get_bindings()):
        return None
    intervals = [(s.get_start_frame(), s.get_end_frame()) for s in sections]
    ids = [str(prop(s.get_camera_binding_id(), "guid", None)) for s in sections]
    contiguous = (intervals[0][0] == sequence.get_playback_start() and
                  intervals[-1][1] == sequence.get_playback_end() and
                  all(a < b for a, b in intervals) and
                  all(a[1] == b[0] for a, b in zip(intervals, intervals[1:])))
    return {"skipped_existing_cuts": True, "sequence": sequence.get_path_name(),
            "intervals": intervals, "contiguous": contiguous, "distinct_cameras": len(set(ids)),
            "requires_review": True,
            "message": "已有 Camera Cuts，已保留且未重复拆分；结构检查不等于 MRQ 子帧验收"}


def main():
    if unreal is None:
        raise RuntimeError("必须在 UE 编辑器的 Python 环境里运行本脚本")
    try:
        config, config_path = load_config()
    except Exception as exc:
        fail(f"配置读取失败：{exc}")
        raise
    CFG_KEYS_CACHE.clear()
    CFG_KEYS_CACHE.update(config)
    _ENUM_CACHE.clear()

    engine_version = unreal.SystemLibrary.get_engine_version()
    log(f"引擎 {engine_version} / 模式 {config['MODE']} / 配置 {config_path or '脚本内常量'}")
    if not engine_version.startswith(("5.7.", "5.8.")) and not config["ALLOW_OTHER_ENGINE_VERSIONS"]:
        fail(f"CameraCutAuto 目标为 UE 5.8，历史兼容 UE 5.7；当前 {engine_version}。"
             "如确认要试跑，请设 ALLOW_OTHER_ENGINE_VERSIONS=True，并先跑 MODE='probe'。")
        raise ConfigError("不支持的 UE 次版本")

    if config["MODE"] == "probe":
        write_report("probe.json", run_probe())
        return

    probe = run_probe()
    if probe["missing"]:
        fail(f"probe 有 {len(probe['missing'])} 项缺失，已中止（未写任何数据）。详见上方输出。")
        raise ConfigError("UE CameraCutAuto API 检查失败")

    context = Context(config)
    selected_sequence = choose_open_level_sequence()
    existing = existing_cut_layout(selected_sequence)
    if existing:
        write_report(f"{config['MODE']}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json", existing)
        log(existing["message"])
        return
    source_path = _asset_path(selected_sequence)
    config["SOURCE_SEQUENCE"] = source_path
    context.notes["selected_open_sequence"] = source_path
    try:
        working_path = maybe_duplicate_for_edit(context, source_path)
        config["working_sequence_path"] = working_path
        sequence = unreal.EditorAssetLibrary.load_asset(working_path)
        if sequence is None:
            raise RuntimeError(f"加载序列失败：{working_path}")
        context.sequence = sequence
        context.display_rate = _rate_tuple(sequence.get_display_rate())
        context.tick_resolution = _rate_tuple(sequence.get_tick_resolution())
        log(f"display_rate={context.display_rate} tick_resolution={context.tick_resolution}")

        if config["BAKE_SUBSAMPLES"] > 1 and not subframe_is_representable(
                context.tick_resolution, context.display_rate, config["BAKE_SUBSAMPLES"]):
            raise ConfigError(
                f"BAKE_SUBSAMPLES={config['BAKE_SUBSAMPLES']} 无法用当前 tick_resolution="
                f"{context.tick_resolution} 精确表示（需要每显示帧的 tick 数能被 S 整除）。"
                "请改用 1/2/4/5/8/10 等可整除的值，或调整序列的 tick resolution。")

        if config["SEQUENCE_END"] is None:
            config["resolved_end"] = int(sequence.get_playback_end())
            log(f"SEQUENCE_END 留空 → 使用序列播放结束帧 {config['resolved_end']}（排他上界）")
        else:
            config["resolved_end"] = int(config["SEQUENCE_END"])
            playback_end = int(sequence.get_playback_end())
            if config["resolved_end"] < playback_end:
                context.warn(f"SEQUENCE_END={config['resolved_end']} 小于序列播放结束 {playback_end}："
                             f"最后 {playback_end - config['resolved_end']} 帧没有任何 Camera Cut，"
                             "MRQ 在尾段会退回默认相机。")

        plan_shots(config["CUT_FRAMES"], config["SEQUENCE_START"], config["resolved_end"])

        ensure_source_level(context, config["SOURCE_LEVEL"])
        evaluator = Evaluator(context)
        context.evaluator = evaluator
        previous = open_sequence(sequence, evaluator)
        assert_subframe_evaluation(context)
        evaluator.warm_up()
        context.source_binding, context.source_actor = resolve_source_camera(
            context, config["SOURCE_CAMERA"])
        log(f"源相机：{context.source_actor.get_actor_label()} "
            f"(by {context.notes.get('source_resolved_by')})")

        if config["MODE"] == "detect":
            # 检测不看 CUT_FRAMES，整段扫一遍
            detect_plan = plan_shots([], config["SEQUENCE_START"], config["resolved_end"])
            payload = run_detect(context, evaluator, detect_plan)
            payload.update({"mode": "detect", "engine_version": engine_version,
                            "sequence": sequence.get_path_name(),
                            "thresholds": {k: config[k] for k in (
                                "DETECT_WINDOW", "DETECT_ABS_POS_CM", "DETECT_ABS_ROT_DEG",
                                "DETECT_ABS_FOCAL_MM", "DETECT_REL_POS", "DETECT_REL_ROT",
                                "DETECT_REL_FOCAL", "DETECT_SUPPRESS", "DETECT_SAMPLE_STEP")},
                            "warnings": context.warnings})
            write_report(f"detect_{datetime.datetime.now():%Y%m%d_%H%M%S}.json", payload)
            evaluator.jump_to(config["SEQUENCE_START"], 0.0)
            restore_sequence_focus(evaluator, previous)
            return

        if config["MODE"] == "verify":
            if config["MAKE_BACKUP"]:
                config["backup_path"] = make_backup(context, source_path, "Verify")
            result = run_verify_only(config, context)
            payload = {"mode": "verify", "engine_version": engine_version,
                       "sequence": sequence.get_path_name(), "verification": result,
                       "warnings": context.warnings}
            write_report(f"verify_{datetime.datetime.now():%Y%m%d_%H%M%S}.json", payload)
            print("")
            print(f"VERIFY {'PASS' if result['pass'] else 'FAIL'}（{len(result['checks'])} 项检查）")
            for item in result["checks"]:
                print(f"  [{'OK ' if item['ok'] else 'FAIL'}] {item['check']}: {item['detail']}")
            print("")
            restore_sequence_focus(evaluator, previous)
            return

        if config["MAKE_BACKUP"]:
            config["backup_path"] = make_backup(context, source_path, "Bake")
        payload = run_bake(config, context)
        evaluator.jump_to(config["SEQUENCE_START"], 0.0)
        stamp = f"{datetime.datetime.now():%Y%m%d_%H%M%S}"
        write_report(f"bake_{stamp}.json", payload)
        if payload.get("verification"):
            print("")
            print(f"VERIFY {'PASS' if payload['verification']['pass'] else 'FAIL'}")
            for item in payload["verification"]["checks"]:
                print(f"  [{'OK ' if item['ok'] else 'FAIL'}] {item['check']}: {item['detail']}")
            print("")
        restore_sequence_focus(evaluator, previous)
        log("完成。注意：源 MMD 相机与其轨道未被修改，可随时改 CUT_FRAMES 重新烘焙。")
    except Exception:
        fail(traceback.format_exc())
        raise


def _rate_tuple(rate):
    return (int(prop(rate, "numerator", 30)), int(prop(rate, "denominator", 1)))


if unreal is not None and os.environ.get("CAMCUT_NO_AUTORUN") != "1":
    main()
