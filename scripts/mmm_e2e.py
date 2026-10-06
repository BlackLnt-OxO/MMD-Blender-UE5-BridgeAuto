"""Stage 2.9 验收工具：MMM 独立端到端（不依赖 Blender / UE）。

方案 §56 Stage 2.9 的原文只有一句：

    PMX + source VMD → effective PMX → baked.vmd → validation PASS

本工具把它拆成 **12 项可判定的验收**，每一步都对应一个真实产物，
最后一项是硬 Gate（Phase 1 Validator）。

用法
----
    # 主验收（方案 §58-A）：final.pmx + Prhythmatic
    ./.venv/Scripts/python.exe scripts/mmm_e2e.py run ^
        --pmx "F:\\MMD_Model\\Miku\\Sano_YYB·Era·Miku（二分裙）\\final.pmx" ^
        --vmd "F:\\MMD_Motion\\mobiusP_Prhythmatic_motion_ver3.0\\Prhythmatic攝晍梡3.0.vmd" ^
        --mmm "F:\\MikuMikuDance Exe\\MikuMikuMoving64_v1292" --isolate

    # 只重跑最终裁判（第 12 项）
    ./.venv/Scripts/python.exe scripts/mmm_e2e.py verify --job <任务目录>

关于人工操作
------------
第 6~11 项目前需要在 MMM 里**手动**完成 Load Model / Load Motion / Bake / Export
—— UI Automation 是 Stage 2.7/2.8 的产物，本阶段还没有。
这符合方案 §38：**人工接管只替代 UIA，绝不替代结果验证**。
所以下面每一步的**判定都是自动的**，你只需要点按钮。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmd_bridge import mmm_protocol as proto            # noqa: E402
from mmd_bridge import mmm_runtime as rt                # noqa: E402
from mmd_bridge.character_prep import (                 # noqa: E402
    CharacterPrepError,
    prepare_character_model,
)
from mmd_bridge.effective_inputs import write_effective_inputs   # noqa: E402
from mmd_bridge.vmd_alias import (                      # noqa: E402
    ALIAS_VMD_FILE_NAME,
    read_alias_map,
)
from mmd_bridge.vmd_name_strategy import ALIAS_MAP_FILE_NAME   # noqa: E402
from mmd_bridge.vmd_reader import read_vmd              # noqa: E402
from mmd_bridge.vmd_validator import (                  # noqa: E402
    validate_baked_vmd,
    write_validation_json,
)

PLUGIN_BUILD = (Path(__file__).resolve().parents[1] / "plugins" / "mmm"
                / "MMDBridgeMMMPlugin" / "bin" / "Release" / rt.PLUGIN_DLL_NAME)


# --------------------------------------------------------------------------
# 验收清单
# --------------------------------------------------------------------------

@dataclass
class Check:
    key: str
    name: str
    ok: bool = False
    skipped: bool = False
    detail: str = ""

    @property
    def mark(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, key: str, name: str, ok: bool, detail: str = "",
            skipped: bool = False) -> Check:
        check = Check(key=key, name=name, ok=ok, skipped=skipped, detail=detail)
        self.checks.append(check)
        return check

    @property
    def passed(self) -> bool:
        return all(c.ok or c.skipped for c in self.checks) and bool(self.checks)

    def render(self) -> str:
        lines = ["", "=" * 78, "Stage 2.9 验收结果", "=" * 78]
        for index, check in enumerate(self.checks, 1):
            lines.append(f"  [{check.mark:4s}] {index:2d}. {check.name}")
            if check.detail:
                lines.append(f"           {check.detail}")
        lines.append("=" * 78)
        lines.append(f"  结论：{'✅ PASS' if self.passed else '❌ FAIL'}"
                     f"（{sum(1 for c in self.checks if c.ok)}/{len(self.checks)} 通过）")
        lines.append("=" * 78)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def say(text: str = "") -> None:
    """打印一行。**带时间戳** —— 否则事后无法判断各阶段耗时，
    优化就变成了猜。"""
    print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)


def wait_for_file_stable(path: Path, *, timeout: float = 600.0,
                         quiet: float = 2.0, poll: float = 0.5) -> bool:
    """等文件出现且大小稳定 ``quiet`` 秒。返回是否等到。"""
    deadline = time.time() + timeout
    last_size = -1
    stable_since = None
    while time.time() < deadline:
        if path.is_file():
            size = path.stat().st_size
            if size != last_size:
                last_size = size
                stable_since = time.time()
            elif stable_since and (time.time() - stable_since) >= quiet and size > 0:
                return True
        time.sleep(poll)
    return False


def stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def find_baked_vmd(mmm_dir: Path) -> Path | None:
    """定位导出的烘焙动作。

    优先精确的 ``baked.vmd``；但 MMM 的"导出动作"默认会**沿用原文件名再加后缀**，
    操作者往往直接确认默认名。因此接受任何**名字含 baked 的 VMD**
    （大小写不敏感），挑最新的一个。

    找不到时返回 ``None`` —— 绝不猜一个不相干的 VMD 来"验证通过"。
    """
    exact = mmm_dir / "baked.vmd"
    if exact.is_file() and exact.stat().st_size > 0:
        return exact
    candidates = [p for p in mmm_dir.glob("*.vmd")
                  if "baked" in p.name.lower() and p.stat().st_size > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def wait_for_baked_vmd(mmm_dir: Path, *, timeout: float = 600.0,
                       quiet: float = 2.0, poll: float = 0.5) -> Path | None:
    """等烘焙动作出现并稳定下来。返回找到的文件，超时返回 ``None``。"""
    deadline = time.time() + timeout
    last: Path | None = None
    last_size = -1
    stable_since = None
    while time.time() < deadline:
        found = find_baked_vmd(mmm_dir)
        if found is not None:
            size = found.stat().st_size
            if found != last or size != last_size:
                last, last_size, stable_since = found, size, time.time()
            elif stable_since and (time.time() - stable_since) >= quiet:
                return found
        time.sleep(poll)
    return find_baked_vmd(mmm_dir)


def confirm_step(title: str, lines: list[str], *,
                 timeout: float = 600.0) -> bool:
    """要求人工确认后才继续（方案 §26「人工确认模型和动作正常」）。

    这是**唯一一个无法用 telemetry 代替**的判定：MMM 里模型有没有炸、
    动作有没有错位，只有人看得出来。所以它必须是一个显式的 Gate，
    而不是"没报错就默认没问题"。

    非交互环境（stdin 不是终端）下**跳过并明确告知**，
    绝不静默当成已确认。
    """
    say("")
    say("=" * 78)
    say(f"人工确认：{title}")
    say("=" * 78)
    for line in lines:
        say(f"  {line}")
    say("")

    if not stdin_is_interactive():
        say("⚠ 当前不是交互式终端，跳过人工确认。")
        say("  在 MMM 里请自行目视检查模型与动作；本工具仍会做全部自动判定。")
        return True

    say("请在 MMM 中目视确认上述内容无误，然后按 Enter 继续（输入 s 放弃）：")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("")
            return False
        if answer in ("s", "stop", "n", "no"):
            return False
        return True
    say("确认超时。")
    return False


def wait_for_bake(job_dir: Path, *, timeout: float = 1800.0,
                  poll: float = 0.5) -> dict:
    """盯 state.json，直到观察到 PhysicsBaking 完整跃迁。"""
    deadline = time.time() + timeout
    seen_entering = False
    last_line = ""
    while time.time() < deadline:
        state = proto.read_state(job_dir)
        if state:
            observed = bool(state.get("bake_observed"))
            completed = bool(state.get("bake_completed_observed"))
            stalled = bool(state.get("bake_stalled"))
            scene = state.get("scene_state", "?")
            marker = state.get("marker_position", 0)
            if observed and not seen_entering:
                seen_entering = True
                say(f"\n  ▶ 已观察到进入 PhysicsBaking（scene_state={scene}）")
            line = (f"\r  [{scene:15s}] marker={marker:<7} "
                    f"bake_observed={observed} completed={completed}")
            if line != last_line:
                print(line, end="", flush=True)
                last_line = line
            if stalled:
                say("\n  ⚠ 检测到 bake_stalled —— Bake 可能卡住")
            if completed:
                say("")
                return state
        time.sleep(poll)
    say("")
    return proto.read_state(job_dir)


# --------------------------------------------------------------------------
# 主体
# --------------------------------------------------------------------------

def prepare(pmx: Path, vmd: Path, mmm_dir: Path, *, isolate: bool,
            lipsync: Path | None = None) -> dict:
    """第 1~5 项：预处理 + Runtime + 插件 + 启动 + 上线。

    ``lipsync`` 是可选的表情 VMD。人工流程里它在主动作之后导入，
    本函数只负责**准备**（含别名转换）；真正导入 MMM 由 UIA 或人工完成。
    """
    report = Report()
    say("\n[1/5] PMX 预处理…")
    job_root = None
    try:
        # 先建任务目录，让 ProcessedPMX 与 manifest 落在里面
        stamp = time.strftime("%Y%m%d-%H%M%S")
        import os
        import uuid
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        job_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        job_root = local / "MMDBridge" / "jobs" / job_id
        pmx_dir = job_root / "pmx"
        pmx_dir.mkdir(parents=True, exist_ok=True)

        prep, text = prepare_character_model(
            pmx, source_vmd=vmd, source_lipsync=lipsync, output_dir=pmx_dir)
    except CharacterPrepError as exc:
        report.add("prep", "PMX 自动前处理", False, f"{exc.code}: {exc.message}")
        return {"report": report, "job_root": job_root}
    except Exception as exc:                       # noqa: BLE001
        report.add("prep", "PMX 自动前处理", False, f"{type(exc).__name__}: {exc}")
        return {"report": report, "job_root": job_root}

    report.add("prep", "PMX 自动前处理", True,
               f"源缺失 {prep.missing_physics_bone_count} → "
               f"修复后 {prep.missing_after_repair}"
               f"；effective={'ProcessedPMX' if prep.repaired else '原 PMX'}")
    report.add("representable", "物理骨 VMD 名称路由",
               True,
               f"期望骨骼 {len(prep.expected_bones)} 根"
               f"（修复骨 {len(prep.repair_expected_bones)} + "
               f"探针骨 {len(prep.bake_probe_bones)}）")

    # VMD 名称路由（方案《VMD 骨骼名限制自动 Alias》）。
    # 名称写不进 VMD 不再是拒绝理由 —— 生成别名工作模型继续走。
    if prep.uses_vmd_alias:
        reasons = "、".join(f"{k}×{v}"
                            for k, v in (prep.alias_summary.get("reasons") or {}).items())
        report.add("alias", "VMD 名称别名（自动生成）", True,
                   f"{len(prep.alias_index_map)} 根骨改用别名"
                   f"（{reasons}）；工作模型 "
                   f"{Path(prep.alias_model).name}")

    # 表情 VMD（可选）。人工流程里它在主动作之后导入 MMM ——
    # 以前这一步在 MMM 侧完全没有，只有 Blender 侧会用到它。
    if lipsync is not None and Path(lipsync).is_file():
        lipsync_for_mmm = prep.alias_lipsync or str(lipsync)
        report.add("lipsync", "表情 VMD 已准备", True,
                   f"{Path(lipsync).name}"
                   + ("（已转别名）" if prep.alias_lipsync else ""))

    if prep.mmm_skipped:
        report.add("bypass", "零物理骨旁路", True,
                   f"{prep.mmm_skipped_reason} —— 无需 MMM，Stage 2.9 不适用")
        return {"report": report, "job_root": job_root, "prep": prep,
                "source_lipsync": str(lipsync) if lipsync else "",
                "bypassed": True}

    say("[2/5] 准备隔离 Runtime…")
    try:
        runtime = rt.prepare_runtime(mmm_dir) if isolate else mmm_dir
        status = rt.runtime_status()
        detail = (f"{status['runtime_dir']}"
                  if isolate else f"直接使用 {mmm_dir}")
        report.add("runtime", "隔离 Runtime 就绪", True, detail)
    except rt.MmmRuntimeError as exc:
        report.add("runtime", "隔离 Runtime 就绪", False,
                   f"{exc.code}: {exc.message}")
        return {"report": report, "job_root": job_root, "prep": prep}

    say("[3/5] 安装插件…")
    try:
        installed = rt.install_plugin(runtime, PLUGIN_BUILD)
        report.add("plugin_install", "插件已安装", True, str(installed))
    except rt.MmmRuntimeError as exc:
        report.add("plugin_install", "插件已安装", False,
                   f"{exc.code}: {exc.message}")
        return {"report": report, "job_root": job_root, "prep": prep}

    say("[4/5] 建立任务并启动 MMM…")
    mmm_job_dir = job_root / "mmm"
    mmm_job_dir.mkdir(parents=True, exist_ok=True)
    source = read_vmd(vmd)
    start, end = source.bone_frame_range
    # 走别名时，MMM 必须加载**别名模型 + 别名动作** ——
    # 否则它找不到那些骨骼，动作会静默丢失（方案 §10.2 / §14）。
    mmm_model = prep.alias_model or prep.effective_model
    mmm_motion = prep.alias_motion or str(vmd)
    if prep.uses_vmd_alias and not prep.alias_motion:
        say("   ⚠ 别名模型已生成，但没有别名动作 —— MMM 里那些骨骼将没有动作")
    request = proto.JobRequest(
        job_id=job_id, job_dir=str(mmm_job_dir),
        effective_model=mmm_model, source_vmd=mmm_motion,
        manifest_path=str(pmx_dir / "repair_manifest.json"),
        start_frame=start, end_frame=end)
    proto.write_active_job(request)

    exe = runtime / "MikuMikuMoving64.exe"
    if not exe.is_file():
        report.add("plugin_online", "插件上线", False, f"找不到 {exe}")
        return {"report": report, "job_root": job_root, "prep": prep}
    subprocess.Popen([str(exe)], cwd=str(runtime))

    # **MMM 不会自动启用插件。**
    # 它启动时只构造一个临时实例做探测然后 Dispose（用于填充插件列表），
    # 此时 Update() 永不调用、没有任何 telemetry。
    # 这一步必须在这里做 —— 放到状态机里就太晚了：
    # 本函数自己就在等插件上线，会先卡住（实测踩过）。
    _enable_plugin_if_needed(mmm_job_dir, request)

    say("[5/5] 等待插件上线…")
    try:
        heartbeat = proto.wait_for_plugin(
            mmm_job_dir, job_id=request.job_id, nonce=request.nonce,
            timeout=120.0)
    except proto.PluginNotReady as exc:
        report.add("plugin_online", "插件上线", False, exc.message)
        return {"report": report, "job_root": job_root, "prep": prep,
                "request": request}
    report.add("plugin_online", "插件上线", True,
               f"pid={heartbeat.pid} version={heartbeat.plugin_version}")

    return {"report": report, "job_root": job_root, "prep": prep,
            "source_lipsync": str(lipsync) if lipsync else "",
            "request": request, "source": source}


def _commit_effective(ctx: dict, motion: str | Path) -> None:
    prep = ctx["prep"]
    write_effective_inputs(
        ctx["job_root"], source_model=prep.source_model,
        source_vmd=prep.effective_motion,
        effective_model=prep.alias_model or prep.effective_model,
        effective_motion=str(motion), repaired=prep.repaired,
        mmm_skipped=prep.mmm_skipped,
        mmm_skipped_reason=prep.mmm_skipped_reason,
        expected_bone_count=len(prep.expected_bones),
        effective_lipsync=prep.alias_lipsync or ctx.get("source_lipsync", ""),
        uses_vmd_alias=prep.uses_vmd_alias,
        alias_map=prep.alias_map_path, alias_model=prep.alias_model)


def observe_and_verify(ctx: dict, *, timeout: float,
                       confirm: bool = True) -> Report:
    """第 6~12 项：等人工操作 → 观察 telemetry → 最终裁判。"""
    report: Report = ctx["report"]
    prep = ctx["prep"]
    request = ctx["request"]
    source = ctx["source"]
    job_root: Path = ctx["job_root"]
    mmm_dir = Path(request.job_dir)

    say("")
    say("=" * 78)
    say("现在请在已启动的 MMM 里手动完成：")
    say(f"  ① Load Model  →  {prep.effective_model}")
    say(f"  ② Load Motion →  {request.source_vmd}")
    say("  ③ Physics Bake（等待完成）")
    say(f"  ④ Export Motion → {mmm_dir / 'baked.vmd'}")
    say("=" * 78)
    say("程序会自动判定每一步，你只需操作。")
    say("")

    # -- 6. 模型加载 ---------------------------------------------------
    deadline = time.time() + timeout
    model = {}
    while time.time() < deadline:
        model = proto.read_snapshot(mmm_dir, "model")
        if model.get("bone_count"):
            break
        time.sleep(0.5)
    bone_count = int(model.get("bone_count", 0) or 0)
    report.add("model", "模型加载（内部确认）", bone_count > 0,
               f"active_model={model.get('active_model_name', '')!r} "
               f"bones={bone_count}")

    # -- 7. 修复闭环 ---------------------------------------------------
    frames = proto.read_snapshot(mmm_dir, "display_frame")
    in_frame = set(frames.get("bones_in_physicsbake") or [])
    expected = set(prep.repair_expected_bones)
    if not expected:
        report.add("displayframe", "PhysicsBakeFix 闭环", True,
                   "本次无需修复，跳过新增骨断言", skipped=True)
    else:
        missing = sorted(expected - in_frame)
        report.add("displayframe", "PhysicsBakeFix 闭环", not missing,
                   f"期望 {len(expected)} 根，实际命中 "
                   f"{len(expected) - len(missing)} 根"
                   + (f"；缺失样例 {missing[:4]}" if missing else ""))

    # -- 8. Marker -----------------------------------------------------
    state = proto.read_state(mmm_dir)
    marker = state.get("marker_position")
    report.add("marker", "MarkerPosition 已设置",
               marker is not None and int(marker) >= source.bone_frame_range[0],
               f"marker={marker}，期望起点 {source.bone_frame_range[0]}")

    # -- 9. 动作加载 ---------------------------------------------------
    motion = proto.read_snapshot(mmm_dir, "motion")
    with_frames = int(motion.get("bones_with_frames", 0) or 0)
    report.add("motion", "动作加载（内部确认）", with_frames > 0,
               f"有帧骨骼 {with_frames}/{motion.get('bone_count', '?')}，"
               f"最大帧 {motion.get('max_frame', '?')}")

    # -- 9.5 人工确认（方案 §26）---------------------------------------
    # 唯一无法用 telemetry 代替的判定：模型有没有炸、动作有没有错位，
    # 只有人看得出来。必须显式确认，不能"没报错就当没问题"。
    hit = len(in_frame & expected) if expected else 0
    lines = [
        f"模型    : {prep.effective_model}",
        f"动作    : {request.source_vmd}",
        "",
        f"MMM 报告：活动模型 {model.get('active_model_name', '?')!r}，"
        f"骨骼 {bone_count} 根",
        f"         动作有帧骨骼 {with_frames} 根，最大帧 "
        f"{motion.get('max_frame', '?')}",
    ]
    if expected:
        lines.append(f"         修复骨命中 {hit}/{len(expected)} 根")
    lines += [
        "",
        "请目视确认：模型外形/材质正常、骨架未错位、动作姿势与源 VMD 一致。",
    ]
    confirmed = True
    if confirm:
        confirmed = confirm_step("模型与动作是否正常", lines, timeout=timeout)
    report.add("manual_confirm", "人工确认模型与动作正常", confirmed,
               "已确认" if confirmed else "操作者判定不正常或超时")
    if not confirmed:
        say("\n操作者未确认，停止后续 Bake。")
        return report

    # -- 10. Bake 观测 -------------------------------------------------
    say("等待 Physics Bake（请在 MMM 中执行 Bake）…")
    state = wait_for_bake(mmm_dir, timeout=timeout)
    observed = bool(state.get("bake_observed"))
    completed = bool(state.get("bake_completed_observed"))
    report.add("bake", "Bake 状态被真实观测", observed and completed,
               f"bake_observed={observed} bake_completed_observed={completed}")

    # -- 11. 导出 ------------------------------------------------------
    say("等待烘焙动作文件（请在 MMM 中 Export Motion）…")
    say(f"  目标目录：{mmm_dir}")
    say("  文件名含 baked 即可（例如 baked.vmd 或 <原名>baked.vmd）")
    baked = wait_for_baked_vmd(mmm_dir, timeout=timeout)
    size = baked.stat().st_size if baked else 0
    report.add("export", "烘焙动作已导出且稳定", baked is not None,
               f"{baked}（{size} 字节）" if baked
               else f"{mmm_dir} 下未找到名字含 baked 的 VMD")
    if baked is None:
        report.add("validator", "VMD 四层 Validator", False,
                   "没有可验证的烘焙动作文件")
        return report

    # -- 12. 最终裁判（硬 Gate）---------------------------------------
    say(f"运行 Phase 1 Validator：{baked.name}")
    bake_range = source.bone_frame_range
    vreport, text = validate_baked_vmd(
        baked, source_path=request.source_vmd,
        expected_bone_names=prep.expected_bones,
        bake_range=bake_range)
    write_validation_json(vreport, job_root / "mmm" / "validation.json")
    report.add("validator", "VMD 四层 Validator（硬 Gate）", vreport.ok,
               f"status={vreport.status} "
               f"verified={vreport.verified_bone_count}/"
               f"{vreport.expected_bone_count}")
    say("")
    say(text)
    if vreport.ok and report.passed:
        _commit_effective(ctx, baked)
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _enable_plugin_if_needed(job_dir: Path, request) -> None:
    """若插件尚未上线，点一下插件按钮把它启用。

    **先查 heartbeat 再决定点不点** —— 那个按钮是**开关**，
    插件已经在跑时点它会把插件关掉。

    失败不致命：交给后面的 ``wait_for_plugin`` 超时，
    那样错误里会带"等了多少秒"，比"找不到按钮"更有诊断价值。
    """
    try:
        from mmd_bridge.mmm_ui import MmmUiDriver, MmmUiError
    except ImportError:                            # pragma: no cover
        return
    try:
        already, _why = proto.heartbeat_is_current(
            proto.read_heartbeat(job_dir),
            job_id=request.job_id, nonce=request.nonce)
    except Exception:                              # noqa: BLE001
        already = False
    if already:
        say("   插件已自行上线，无需点击")
        return

    # **只建一次驱动，让它内部等窗口出现。**
    #
    # 早期版本在重试循环里每次新建 `MmmUiDriver(timeout=1.0)`，
    # 而每次附加都要走一遍窗口查找 —— 实测 3 次尝试花了 133 秒
    # （单次约 44 秒，远超设定的 1 秒），表现是
    # "MMM 开着但一分钟都不去点插件"。
    #
    # 正确做法：一个驱动 + 足够长的 timeout。
    # 附加是廉价的快路径重试（每次约 0.5 秒），
    # 附加成功后 `find()` 会继续等控件出现 —— 那才是真正该等的东西。
    try:
        MmmUiDriver(timeout=90.0, poll=0.3).enable_plugin()
        say("   已点击「常驻」组的插件按钮")
    except MmmUiError as exc:
        say(f"   ⚠ 没能点到插件按钮：{exc.message[:120]}")
        say("      若 MMM 里插件未启用，请手动点一下「常驻」组的 AutoBake")
    except Exception as exc:                       # noqa: BLE001
        say(f"   ⚠ 点击插件按钮时出错：{type(exc).__name__}: {exc}")


def cmd_run(args) -> int:
    pmx = Path(args.pmx)
    vmd = Path(args.vmd)
    mmm_dir = Path(args.mmm)

    for path, what in ((pmx, "PMX"), (vmd, "VMD")):
        if not path.is_file():
            say(f"!! 找不到{what}：{path}")
            return 2

    ctx = prepare(pmx, vmd, mmm_dir, isolate=args.isolate,
                  lipsync=Path(args.lipsync) if args.lipsync else None)
    report: Report = ctx["report"]

    if ctx.get("bypassed"):
        try:
            read_vmd(ctx["prep"].effective_motion)
            if report.passed:
                _commit_effective(ctx, ctx["prep"].effective_motion)
        except Exception as exc:
            report.add("source_vmd", "源 VMD 验证", False, str(exc))
        say(report.render())
        return 0 if report.passed else 1
    # 前置阶段全部通过才进入观察；用 key 判断，不依赖列表位置。
    ready = all(c.ok or c.skipped for c in report.checks) and \
        any(c.key == "plugin_online" and c.ok for c in report.checks)
    if not ready:
        say(report.render())
        say("\n前置步骤失败，未进入观察阶段。")
        return 2

    if args.no_wait:
        say("\n（--no-wait：跳过观察，仅打印任务信息）")
        say(f"任务目录：{ctx['job_root']}")
        say(f"MMM 任务目录：{ctx['request'].job_dir}")
        return 0

    if args.auto:
        return _run_auto(ctx, args, report)

    observe_and_verify(ctx, timeout=args.timeout, confirm=not args.no_confirm)
    say(report.render())

    job_root = ctx["job_root"]
    say(f"\n任务目录：{job_root}")
    say(f"validation.json：{job_root / 'mmm' / 'validation.json'}")
    return 0 if report.passed else 1


def _run_auto(ctx: dict, args, report: Report) -> int:
    """无人值守模式：由状态机驱动 UIA 走完 MMM 阶段（方案 §5）。

    与人工模式的区别只在**第 4 环**（加载/烘焙/导出）——
    前面三环和最后的 Validator 完全共用。
    """
    from mmd_bridge.mmm_auto import (
        MmmAutoRunner, MmmStageTimeouts,
    )
    from mmd_bridge.mmm_ui import MmmUiDriver

    prep = ctx["prep"]
    request = ctx["request"]
    job_root = ctx["job_root"]

    say("\n[auto] 交给状态机驱动 MMM（不再需要人工操作）…")
    states: list[str] = []

    def on_state(state, detail):
        states.append(state.value)
        say(f"  [{state.value}] {detail}")

    runner = MmmAutoRunner(
        job_dir=request.job_dir,
        request=request,
        expected_model=prep.alias_model or prep.effective_model,
        expected_bones=list(prep.expected_bones),
        expected_lipsync=prep.alias_lipsync or (
            str(args.lipsync) if getattr(args, "lipsync", "") else ""),
        output_vmd=Path(args.output) if getattr(args, "output", "")
        else job_root / "mmm" / "baked.vmd",
        ui=MmmUiDriver(timeout=args.timeout),
        timeouts=MmmStageTimeouts(),
        on_state=on_state,
    )
    result = runner.run()

    if not result.ok:
        report.add("auto", "MMM 自动流程", False,
                   f"{result.state}: {result.error}")
        say(report.render())
        say(f"\n❌ 失败于 {result.state}：{result.error}")
        say(f"任务目录（含诊断产物）：{job_root}")
        # 方案 §19：失败时保留全部中间产物，不要只给一句话。
        # **这一步绝不能自己崩** —— 否则真正的错误会被二次异常吃掉
        # （实测踩过：diagnostics 里有不可序列化的对象，写诊断时抛 TypeError，
        #   把"失败在哪一步"整个盖住）。
        try:
            (job_root / "mmm" / "auto_result.json").write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:                   # noqa: BLE001
            say(f"   ⚠ 写诊断文件失败（不影响上面的错误结论）：{exc}")
        return 1

    report.add("auto", "MMM 自动流程", True,
               f"导出 {Path(result.baked_motion).name}")

    # 导出成功后跑最终裁判（与人工模式同一个 Validator）。
    say("\n[auto] 运行最终裁判…")
    baked = Path(result.baked_motion)
    manifest = json.loads(
        (job_root / "pmx" / "repair_manifest.json").read_text(encoding="utf-8"))
    expected = sorted(set(manifest.get("added_bone_names") or [])
                      | set(manifest.get("bake_probe_bone_names") or []))
    alias = read_alias_map(job_root / "pmx" / ALIAS_MAP_FILE_NAME)
    original_to_alias = alias.get("original_to_alias") or {}
    if original_to_alias:
        expected = sorted(original_to_alias.get(n, n) for n in expected)
    source_for_layer_b = (job_root / "pmx" / ALIAS_VMD_FILE_NAME)
    vmd_for_layer_b = str(source_for_layer_b) if source_for_layer_b.is_file() \
        else request.source_vmd

    baked_doc = read_vmd(baked)
    vreport, text = validate_baked_vmd(
        baked, source_path=vmd_for_layer_b or None,
        expected_bone_names=expected,
        bake_range=baked_doc.bone_frame_range)
    write_validation_json(vreport, job_root / "mmm" / "validation.json")
    report.add("validator", "VMD 四层 Validator（硬 Gate）", vreport.ok,
               f"status={vreport.status} "
               f"verified={vreport.verified_bone_count}/"
               f"{vreport.expected_bone_count}")
    say("")
    say(text)
    if vreport.ok and report.passed:
        _commit_effective(ctx, baked)

    say(report.render())
    say(f"\n任务目录：{job_root}")
    say(f"最终 VMD：{baked}")
    say(f"validation.json：{job_root / 'mmm' / 'validation.json'}")
    say(f"状态轨迹：{' → '.join(states)}")
    return 0 if report.passed else 1


def cmd_verify(args) -> int:
    """只重跑最终裁判 —— 用已有产物，不需要再开 MMM。"""
    job_root = Path(args.job)
    manifest_path = job_root / "pmx" / "repair_manifest.json"
    mmm_dir = job_root / "mmm"
    if not manifest_path.is_file():
        say(f"!! 任务目录不完整：{job_root}")
        say("   需要 pmx/repair_manifest.json")
        return 2

    baked = Path(args.baked) if args.baked else find_baked_vmd(mmm_dir)
    if baked is None or not baked.is_file():
        say(f"!! 在 {mmm_dir} 下找不到烘焙动作文件")
        say("   需要名字含 baked 的 .vmd（例如 baked.vmd 或 <原名>baked.vmd）")
        say("   也可以用 --baked 显式指定路径。")
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = sorted(set(manifest.get("added_bone_names") or [])
                      | set(manifest.get("bake_probe_bone_names") or []))
    if not expected:
        say("!! 期望清单为空 —— 拒绝出具有效判定（空集恒真）")
        return 2

    # 走别名时，烘焙后的 VMD 里存的是**别名**，不是原骨骼名。
    # 拿原名去比对会全部匹配不上（而且看起来像"烘焙失败"）。
    alias = read_alias_map(job_root / "pmx" / ALIAS_MAP_FILE_NAME)
    original_to_alias = alias.get("original_to_alias") or {}
    if original_to_alias:
        before = len(expected)
        expected = sorted(original_to_alias.get(name, name) for name in expected)
        say(f"ℹ 该任务使用 VMD 别名：{len(alias['alias_to_original'])} 根骨改用别名，"
            f"期望清单已相应转换（{before} 项）")

    # 层 B 需要 source VMD。manifest 里记的是源动作路径（不是 PMX）；
    # 旧任务没有该字段时允许用 --vmd 补，缺了就明确告知层 B 被跳过。
    source_vmd = args.vmd or manifest.get("source_vmd") or ""
    # 走别名时必须用**别名版**源动作 —— 否则层 B 会认为"轨道全丢了"。
    alias_motion = job_root / "pmx" / ALIAS_VMD_FILE_NAME
    if original_to_alias and alias_motion.is_file():
        source_vmd = str(alias_motion)
    if not source_vmd or not Path(source_vmd).is_file():
        say(f"⚠ 找不到 source VMD（manifest.source_vmd={source_vmd!r}）")
        say("  层 B「原 Motion 保留」将被跳过，判定不完整。")
        say("  可用 --vmd 指定源动作后重跑。")
        source_vmd = ""

    baked_doc = read_vmd(baked)
    report, text = validate_baked_vmd(
        baked, source_path=source_vmd or None,
        expected_bone_names=expected,
        bake_range=baked_doc.bone_frame_range)
    write_validation_json(report, job_root / "mmm" / "validation.json")
    say(text)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2.9 MMM 独立端到端验收")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑完整验收（需要人工在 MMM 中操作）")
    p_run.add_argument("--pmx", required=True)
    p_run.add_argument("--vmd", required=True)
    p_run.add_argument("--lipsync", default="",
                       help="可选的表情 VMD（人工流程里在主动作之后导入）")
    p_run.add_argument("--mmm", required=True, help="MMM 安装目录")
    p_run.add_argument("--isolate", action="store_true",
                       help="复制到隔离 Runtime 后运行（推荐）")
    p_run.add_argument("--timeout", type=float, default=1800.0,
                       help="等待人工操作的总超时（秒）")
    p_run.add_argument("--no-wait", action="store_true",
                       help="只做前置步骤并打印任务信息")
    p_run.add_argument("--no-confirm", action="store_true",
                       help="跳过人工确认 Gate（仅用于自动化；正常验收不要用）")
    p_run.add_argument("--auto", action="store_true",
                       help="无人值守：用 UIA 状态机走完加载/烘焙/导出")
    p_run.add_argument("--output", default="",
                       help="最终 baked VMD 的输出路径（默认 <job>/mmm/baked.vmd）")

    p_verify = sub.add_parser("verify", help="只重跑最终裁判")
    p_verify.add_argument("--job", required=True, help="任务目录")
    p_verify.add_argument("--vmd", default="",
                          help="源动作 VMD（层 B 用；manifest 未记录时必填）")
    p_verify.add_argument("--baked", default="",
                          help="显式指定烘焙动作路径（默认自动找名字含 baked 的 VMD）")

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "verify":
        return cmd_verify(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
