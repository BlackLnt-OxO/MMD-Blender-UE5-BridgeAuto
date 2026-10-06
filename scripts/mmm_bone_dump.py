"""Phase 0：把插件 dump 的骨骼关键帧与 MMM 官方导出的 VMD 逐字节对照。

要回答的三个问题（方案 §9 / §10 / §11）
---------------------------------------
1. ``InterpolatePoint.X/Y`` 的数值域是 **0~127** 还是 **0~1**？
2. VMD 插值区那 64 字节里，**是否还编码了 Physics ON/OFF 标志**？
   —— 若没有，8 个 InterpolatePoint 就能完整重建；若有，就重建不出来。
3. Bake 的结果写进了**哪一层** MotionLayer？

这三项**都不能猜**。做法是把插件报的值与官方导出对齐 ——
官方导出是 ground truth，对不上就是我们的理解错了。

用法
----
    # ① 写请求（默认取 repair_manifest 里的物理骨，最多 8 根）
    ./.venv/Scripts/python.exe scripts/mmm_bone_dump.py request --job <job_root>

    # ② 在 MMM 里 Bake 一次，插件会自动 dump

    # ③ 对照
    ./.venv/Scripts/python.exe scripts/mmm_bone_dump.py compare \\
        --job <job_root> --native "<MMM 导出的 vmd>"
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmd_bridge.vmd_reader import read_vmd          # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_NAME = "dump_bones.txt"
DUMP_NAME = "bone_dump.tsv"
MARKER_NAME = "bone_dump.done"
FORCE_NAME = "dump_now.txt"

#: 默认 dump 多少根骨 —— 够做对照即可，多了文件会很大。
DEFAULT_BONE_LIMIT = 8


def say(text: str = "") -> None:
    print(text, flush=True)


# --------------------------------------------------------------------------
# ① 写请求
# --------------------------------------------------------------------------

def load_physics_bones(job_root: Path) -> list[str]:
    """从 repair_manifest 取物理骨名（修复骨 + 探针骨）。

    **不猜。** 方案 §5 明确要求由 Python 提供清单，
    插件不得通过 BoneFlags / 显示枠 / 名字含 skirt 之类的方式自行推断。
    """
    manifest = job_root / "pmx" / "repair_manifest.json"
    if not manifest.is_file():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names: list[str] = []
    for key in ("added_bone_names", "bake_probe_bone_names"):
        for name in payload.get(key) or []:
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def cmd_request(args) -> int:
    job_root = Path(args.job)
    mmm_dir = job_root / "mmm"
    mmm_dir.mkdir(parents=True, exist_ok=True)

    names = [n.strip() for n in (args.bones or "").split(",") if n.strip()]
    if not names:
        names = load_physics_bones(job_root)
    if not names:
        say("!! 没有拿到骨骼名。")
        say("   repair_manifest.json 里没有 added_bone_names / bake_probe_bone_names，")
        say("   也没有用 --bones 指定。")
        return 2

    limit = args.limit
    selected = names[:limit]
    (mmm_dir / REQUEST_NAME).write_text("\n".join(selected) + "\n",
                                        encoding="utf-8")
    # 清掉旧标记，让插件重新 dump 一次。
    (mmm_dir / MARKER_NAME).unlink(missing_ok=True)
    force = mmm_dir / FORCE_NAME
    if args.now:
        force.write_text("1", encoding="utf-8")
    else:
        force.unlink(missing_ok=True)

    say(f"已写入 {mmm_dir / REQUEST_NAME}")
    say(f"  共 {len(names)} 根物理骨，取前 {len(selected)} 根：")
    for name in selected:
        say(f"    {name}")
    say("")
    if args.now:
        say("已写 dump_now.txt —— 插件会在 1 秒内 dump，无需 Bake。")
        say("（前提：MMM 里已经载入模型与动作，且插件处于启用状态）")
    else:
        say("下一步：在 MMM 里完成一次 Bake。插件会在 Bake 完成后自动 dump。")
    return 0


# --------------------------------------------------------------------------
# ② 读 dump
# --------------------------------------------------------------------------

def read_dump(path: Path) -> dict:
    """解析 TSV。返回 ``{bone: {layer: [frame_dict, ...]}}``。"""
    bones: dict[str, dict[int, list[dict]]] = {}
    meta: dict[str, str] = {}
    current_bone: str | None = None
    current_layer: int | None = None
    missing: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        parts = raw.split("\t")
        tag = parts[0]
        if tag == "#MMDBRIDGE_BONE_DUMP":
            meta["format"] = parts[1] if len(parts) > 1 else "?"
        elif tag.startswith("#"):
            if len(parts) > 1:
                meta[tag[1:]] = parts[1]
        elif tag == "MISSING":
            missing.append(parts[1])
        elif tag == "BONE":
            current_bone = parts[1]
            bones.setdefault(current_bone, {})
            current_layer = None
        elif tag == "LAYER" and current_bone is not None:
            current_layer = int(parts[1])
            bones[current_bone].setdefault(current_layer, [])
        elif tag == "F" and current_bone is not None and current_layer is not None:
            numbers = [float(v) for v in parts[2:]]
            bones[current_bone][current_layer].append({
                "frame": int(float(parts[1])),
                "pos": tuple(numbers[0:3]),
                "rot": tuple(numbers[3:7]),
                "interp": [tuple(numbers[7 + i * 2:9 + i * 2]) for i in range(8)],
            })

    return {"meta": meta, "bones": bones, "missing": missing}


# --------------------------------------------------------------------------
# ③ 对照
# --------------------------------------------------------------------------

def vmd_interpolation_bytes(document, bone_name: str, frame: int) -> bytes | None:
    """取原生 VMD 里某骨某帧的 64 字节插值区。"""
    track = document.bone_tracks.get(bone_name)
    if track is None:
        return None
    for item in track.frames:
        if item.frame == frame:
            return bytes(item.interpolation)
    return None


def build_interpolation(points: list[tuple[float, float]],
                        scale: float) -> bytes:
    """按 MMD 的 64 字节布局，从 8 个 InterpolatePoint 构造插值区。

    **布局：4 个 16 字节块，每块一条曲线，值在块内偏移 0/4/8/12。**

    .. code-block:: text

        block 0 (byte  0-15) = X 曲线：x1@0  y1@4  x2@8  y2@12
        block 1 (byte 16-31) = Y 曲线
        block 2 (byte 32-47) = Z 曲线
        block 3 (byte 48-63) = R 曲线

    **权威依据是 mmd_tools 的读法**（``vendor/blender_mmd_tools-4.5.13``
    的 ``importer.py``）：

    .. code-block:: python

        indices = tuple(convert_interpolation((0, 16, 32))) + (48,) * 4
        self.__setInterpolation(interp[idx : idx + 16 : 4], prev_kp, kp)

    ``interp[idx:idx+16:4]`` 是**步长 4** 的切片，正好取到块内偏移 0/4/8/12。

    ⚠ **曾经误判为"转置布局"**（以为 Y 在 byte 1/5/9/13）。
    在 ``Classic`` 动作的判别样本上暴露了错误：该帧 Z 曲线含值 ``95``，
    它**只出现在 byte 32**；按转置解读 Z 读的是 byte 2/6/10/14，
    根本取不到 —— 而块式解读能取到。

    之前"转置布局在前 16 字节 548/548 吻合"是**巧合**：
    大多数动作的 X/Y 曲线相同，前 16 字节里两种解读取值一致，分不开。

    块内其余 12 字节是 MMD 写入时的移位残留，mmd_tools 与 MMD 都不读，填 0。
    """
    xa, xb, ya, yb, za, zb, ra, rb = points

    def b(value: float) -> int:
        return max(0, min(255, int(round(value * scale))))

    out = bytearray(64)
    for block, (a, c) in enumerate(((xa, xb), (ya, yb), (za, zb), (ra, rb))):
        base = block * 16
        out[base + 0] = b(a[0])     # x1
        out[base + 4] = b(a[1])     # y1
        out[base + 8] = b(c[0])     # x2
        out[base + 12] = b(c[1])    # y2
    return bytes(out)


def per_byte_diff(points: list[tuple[float, float]],
                  native: bytes, scale: float) -> list[int]:
    """返回 16 个字节位置里哪些不一致 —— 用于定位布局或数值域的问题。"""
    candidate = build_interpolation(points, scale)
    return [i for i in range(16)
            if i < len(native) and candidate[i] != native[i]]


def compare_interpolation(points: list[tuple[float, float]],
                          native: bytes) -> tuple[bool, str]:
    """判断哪种数值域能重建出原生字节。"""
    if len(native) < 64:
        return False, "原生插值区不足 64 字节"
    for scale, label in ((1.0, "0~127（API 已给整数域）"),
                         (127.0, "0~1（需乘 127）")):
        if build_interpolation(points, scale) == native:
            return True, f"数值域 = {label}"
    # 都不匹配时，报告差异最大的位置
    best = None
    for scale in (1.0, 127.0):
        candidate = build_interpolation(points, scale)
        diff = sum(1 for a, c in zip(candidate, native) if a != c)
        if best is None or diff < best[1]:
            best = (scale, diff)
    return False, (f"两种数值域都重建不出原生字节"
                   f"（最接近的是 scale={best[0]}，仍有 {best[1]}/64 字节不同）")


def cmd_compare(args) -> int:
    job_root = Path(args.job)
    mmm_dir = job_root / "mmm"
    dump_path = Path(args.dump) if args.dump else mmm_dir / DUMP_NAME
    if not dump_path.is_file():
        say(f"!! 找不到 dump：{dump_path}")
        say("   先跑 request，再在 MMM 里 Bake 一次。")
        return 2

    dump = read_dump(dump_path)
    meta = dump["meta"]
    say("=" * 74)
    say("Phase 0 对照：插件 dump vs MMM 官方导出")
    say("=" * 74)
    for key in ("job", "model", "bone_count", "bones_written"):
        if key in meta:
            say(f"  {key:14s}: {meta[key]}")
    say(f"  dump 骨骼数     : {len(dump['bones'])}")
    if dump["missing"]:
        say(f"  在 MMM 中找不到 : {dump['missing']}")

    native_path = Path(args.native)
    if not native_path.is_file():
        say(f"\n!! 找不到官方导出：{native_path}")
        return 2
    say(f"\n正在解析官方导出：{native_path.name} …")
    native = read_vmd(native_path)

    say("")
    say("-" * 74)
    say(f"{'骨骼':22s} {'层':>3s} {'dump帧':>7s} {'原生帧':>7s} "
        f"{'位置':>6s} {'旋转':>6s} {'插值':>6s}")
    say("-" * 74)

    interp_verdicts: dict[str, int] = {}
    checked = 0
    for bone_name, layers in dump["bones"].items():
        native_track = native.bone_tracks.get(bone_name)
        native_frames = ({f.frame for f in native_track.frames}
                         if native_track else set())
        for layer_index, frames in sorted(layers.items()):
            dump_frames = {f["frame"] for f in frames}
            common = sorted(dump_frames & native_frames)[: args.samples]

            pos_ok = rot_ok = interp_ok = 0
            for item in frames:
                if item["frame"] not in set(common):
                    continue
                native_frame = next(f for f in native_track.frames
                                    if f.frame == item["frame"])
                if all(abs(a - b) < 1e-4
                       for a, b in zip(item["pos"], native_frame.position)):
                    pos_ok += 1
                if all(abs(a - b) < 1e-4
                       for a, b in zip(item["rot"], native_frame.rotation)):
                    rot_ok += 1
                good, verdict = compare_interpolation(
                    item["interp"], bytes(native_frame.interpolation))
                if good:
                    interp_ok += 1
                    interp_verdicts[verdict] = interp_verdicts.get(verdict, 0) + 1

            n = len(common) or 1
            say(f"{bone_name[:22]:22s} {layer_index:>3d} "
                f"{len(dump_frames):>7d} {len(native_frames):>7d} "
                f"{pos_ok:>3d}/{n:<2d} {rot_ok:>3d}/{n:<2d} {interp_ok:>3d}/{n:<2d}")
            checked += 1

    say("-" * 74)
    say("")
    say("=" * 74)
    say("结论")
    say("=" * 74)
    if interp_verdicts:
        for verdict, count in sorted(interp_verdicts.items(),
                                     key=lambda kv: -kv[1]):
            say(f"  插值区可重建：{verdict}（{count} 帧样本）")
        say("  ⇒ 8 个 InterpolatePoint 足以重建 64 字节插值区。")
    else:
        say("  ⚠ 没有任何一帧的插值区被重建出来。")
        say("    可能原因：")
        say("      1) 数值域不是 0~127 也不是 0~1（需要看原始数值）")
        say("      2) 64 字节里确实编码了额外信息（Physics ON/OFF 标志，方案 §10）")
        say("      3) 布局假设错了")
        say("    → 请把 dump 的前几帧原始数值发出来，人工看一遍。")
    say(f"\n  共对照 {checked} 个 (骨骼, 层) 组合。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0 骨骼 dump 与对照")
    sub = parser.add_subparsers(dest="command", required=True)

    p_req = sub.add_parser("request", help="写 dump 请求")
    p_req.add_argument("--job", required=True, help="任务根目录")
    p_req.add_argument("--bones", default="", help="逗号分隔的骨骼名（默认取 manifest）")
    p_req.add_argument("--limit", type=int, default=DEFAULT_BONE_LIMIT)
    p_req.add_argument("--now", action="store_true",
                       help="不等 Bake，立刻 dump（插值测试用，需动作已载入）")
    p_req.set_defaults(func=cmd_request)

    p_cmp = sub.add_parser("compare", help="与官方导出对照")
    p_cmp.add_argument("--job", required=True)
    p_cmp.add_argument("--native", required=True, help="MMM 官方导出的 VMD")
    p_cmp.add_argument("--dump", default="", help="显式指定 dump 文件")
    p_cmp.add_argument("--samples", type=int, default=50,
                       help="每根骨对照多少帧样本")
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
