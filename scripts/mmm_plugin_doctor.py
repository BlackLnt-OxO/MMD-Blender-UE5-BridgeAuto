"""MMM 插件加载诊断 —— 回答「插件为什么没上线」。

起因：把 DLL 放进 ``Plugins/`` 后启动 MMM，完全没有 telemetry。
从外部无法区分三种情况，本工具用插件的自诊断日志把它们分开：

.. code-block:: text

    plugin_startup.log 里有什么            →  结论
    ─────────────────────────────────────────────────────────
    （文件不存在）                         →  MMM 没加载这个 DLL
    只有「静态构造」                       →  DLL 加载了，但 MMM 没启用插件
    有「实例构造」但没有「Update()」       →  实例化了，但 Update 没被调用
    有「Update()」但没有 heartbeat         →  Update 跑了但写文件失败
    有 heartbeat                           →  正常，问题在别处

用法
----
    # 诊断隔离 Runtime（默认）
    ./.venv/Scripts/python.exe scripts/mmm_plugin_doctor.py

    # 诊断指定的 MMM 目录
    ./.venv/Scripts/python.exe scripts/mmm_plugin_doctor.py --target "F:\\...\\MikuMikuMoving64_v1292"

    # 顺便把当前构建的 DLL 装进去
    ./.venv/Scripts/python.exe scripts/mmm_plugin_doctor.py --install
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmd_bridge import mmm_protocol as proto          # noqa: E402
from mmd_bridge import mmm_runtime as rt              # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DLL = (REPO_ROOT / "plugins" / "mmm" / "MMDBridgeMMMPlugin"
             / "bin" / "Release" / rt.PLUGIN_DLL_NAME)


def say(text: str = "") -> None:
    print(text, flush=True)


def describe(path: Path) -> str:
    if not path.is_file():
        return "不存在"
    info = path.stat()
    stamp = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"{info.st_size} 字节，{stamp}"


def read_startup_log() -> tuple[Path, list[str]]:
    log = proto.bridge_dir() / "plugin_startup.log"
    if not log.is_file():
        return log, []
    try:
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    except (OSError, ValueError):
        return log, []
    return log, lines


def latest_session(lines: list[str]) -> list[str]:
    """只保留**最后一个进程**（= 当前 MMM 会话）的记录。

    日志是累积追加的，重启 MMM 后旧会话的记录还在。
    若整份日志一起看，上一次会话里的 ``Update()`` 会被当成"现在在跑" ——
    实测就因此得出了错误结论（报 UPDATE_RAN，其实那是上一次会话）。

    每行形如 ``2026-09-20 21:16:40.806  [pid 22520]  ...``。
    """
    if not lines:
        return []
    last_pid = None
    for line in reversed(lines):
        match = re.search(r"\[pid (\d+)\]", line)
        if match:
            last_pid = match.group(1)
            break
    if last_pid is None:
        return lines
    return [ln for ln in lines if f"[pid {last_pid}]" in ln]


#: telemetry 超过这个秒数没更新就认为"没在跑"。
#: 插件的心跳间隔是 250ms，快照是 1s —— 20 秒足够宽松，
#: 又能区分"上次会话留下的文件"和"现在正在刷新"。
TELEMETRY_MAX_AGE = 20.0


def telemetry_state(job_dir: Path | None, *,
                    max_age: float = TELEMETRY_MAX_AGE,
                    now: float | None = None) -> tuple[bool, str]:
    """看 telemetry 是否**正在**产生。返回 ``(在跑, 说明)``。

    **必须同时看存在性与新鲜度。** 只看文件是否存在会得出错误结论 ——
    实测踩过：重启 MMM 后旧任务的 6 个 telemetry 文件还在磁盘上，
    工具却报「RUNNING」，而实际上新进程连插件都还没加载完。

    **这一步必须先于读日志。** 只读自诊断日志同样会出错 ——
    日志为空（跑的是加埋点之前的旧 DLL）但 telemetry 正常刷新时，
    旧版工具报了「从未加载」。
    """
    if job_dir is None or not job_dir.is_dir():
        return False, "没有活动任务目录"

    names = ("heartbeat.json", "state.json", "model_snapshot.json")
    present = [n for n in names if (job_dir / n).is_file()]
    if not present:
        return False, "任务目录下没有任何 telemetry 文件"

    current = time.time() if now is None else now
    newest = max((job_dir / n).stat().st_mtime for n in present)
    age = current - newest
    stamp = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")

    if age > max_age:
        return False, (f"最近一次写入 {stamp}（{age:.0f}s 前）—— "
                       f"已停止刷新，插件当前不在上报")
    return True, f"最近一次写入 {stamp}（{age:.1f}s 前）"


def diagnose(target: Path, *, job_dir: Path | None = None) -> tuple[str, str]:
    """返回 ``(结论代码, 可读说明)``。

    判定顺序：
    1. **telemetry 优先于日志** —— telemetry 是插件的实际产出，日志只是线索；
    2. **只看当前会话的日志** —— 日志是累积的，上一次会话的 ``Update()``
       不能证明现在在跑（实测因此误报过）。
    """
    running, detail = telemetry_state(job_dir)
    if running:
        return (
            "RUNNING",
            f"插件正在上报 telemetry（{detail}）。\n"
            "  ⇒ 插件加载、实例化、Update 全部正常。")

    _, all_lines = read_startup_log()
    if not all_lines:
        return (
            "NOT_LOADED",
            "没有**新鲜**的 telemetry，也没有自诊断日志。\n"
            "  （磁盘上可能还留着上一次会话的文件，但那不代表现在在跑）\n"
            "  可能原因：\n"
            "    1) DLL 不在 <MMM>\\Plugins\\ 下\n"
            "    2) MMM 启动时 DLL 还不存在（先装 DLL 再启动）\n"
            "    3) MMM 的插件目录被配置到别处\n"
            "    4) 运行的 DLL 早于埋点版本（埋点前的构建不会写日志）")

    lines = latest_session(all_lines)
    text = "\n".join(lines)
    earlier = len(all_lines) - len(lines)

    if "Update() 首次调用" in text:
        return (
            "UPDATE_RAN",
            "本次会话里 Update() 已被调用，但没有新鲜 telemetry 落盘。\n"
            "  ⇒ 写文件失败：检查 job_dir 是否存在、是否有写权限，"
            "以及 active_job.json 是否可解析。")
    if "Enabled() 被调用" in text:
        return (
            "ENABLED_NO_UPDATE",
            "本次会话里插件已被 MMM 启用（Enabled() 被调用），但 Update() 还没跑。\n"
            "  ⇒ 稍等片刻再跑一次诊断；若持续如此，检查 MMM 是否被模态对话框阻塞。")
    if "实例构造" in text:
        return (
            "INSTANTIATED_NO_UPDATE",
            "MMM 创建了插件实例并调用了 Initialize()，但没有调用 Update()。\n"
            "  ⇒ MMM 只是在**探测**插件（用于填充插件列表），没有把它当作活跃插件。\n"
            "  **解决办法：在 MMM 里点一下插件的按钮（AutoBake）。**\n"
            "     实测点击后 MMM 会调用 Enabled()，随后 Update() 立即开始。")
    if "静态构造" in text:
        return (
            "LOADED_NOT_ENABLED",
            "DLL 已被加载、类型可解析，但 MMM **没有实例化插件**。\n"
            "  ⇒ 需要人工在 MMM 里启用插件（方案 §50 的分支）。")
    if earlier:
        return (
            "STALE_LOG_ONLY",
            f"本次会话只有 {len(lines)} 行日志（另有 {earlier} 行属于更早的会话）。\n"
            "  ⇒ 当前会话还没有可识别的进展，稍后再看。")
    return ("UNKNOWN", "日志存在但没有可识别的记录。")


def watch_bake(job_dir: Path, *, seconds: float = 3600.0,
               poll: float = 1.0) -> int:
    """实时显示物理烘焙进度。

    MMM 的物理烘焙**按正常播放速度推进**（实测：marker 逐帧前进），
    6754 帧 @30fps ≈ 3.75 分钟。所以「等」是常态，需要一个能看进度的窗口。

    进度来自插件上报的 ``marker_position`` 与 ``scene_state``。
    """
    import time

    say(f"监视烘焙进度：{job_dir}")
    say("（Ctrl-C 退出）")
    say("")

    start = time.time()
    last_marker = None
    last_change = time.time()
    entered_baking = False
    completed = False
    interval = None

    while time.time() - start < seconds:
        state = proto.read_state(job_dir)
        if not state:
            print("\r  等待插件上报…", end="", flush=True)
            time.sleep(poll)
            continue

        scene = state.get("scene_state", "?")
        marker = int(state.get("marker_position", 0) or 0)
        observed = bool(state.get("bake_observed"))
        done = bool(state.get("bake_completed_observed"))
        stalled = bool(state.get("bake_stalled"))

        if scene == "PhysicsBaking" and not entered_baking:
            entered_baking = True
            say(f"▶ 进入 PhysicsBaking（起始 marker={marker}）")

        if marker != last_marker:
            if last_marker is not None:
                interval = time.time() - last_change
            last_marker = marker
            last_change = time.time()

        elapsed = time.time() - start
        idle = time.time() - last_change
        rate = f"{interval:.2f}s/帧" if interval else "—"
        line = (f"\r  [{scene:15s}] marker={marker:<7d} "
                f"已用 {elapsed:6.1f}s  步进 {rate}  "
                f"静默 {idle:4.1f}s  "
                f"observed={observed} done={done}")
        print(line, end="", flush=True)

        if stalled:
            say("\n  ⚠ 插件报告 bake_stalled —— 可能卡住")

        if done:
            say("")
            say(f"✅ 烘焙完成（总耗时 {elapsed:.1f}s，最终 marker={marker}）")
            return 0

        time.sleep(poll)

    say("")
    say("（监视窗口结束）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MMM 插件加载诊断")
    parser.add_argument("--target", default="",
                        help="MMM 或 Runtime 目录（默认用隔离 Runtime）")
    parser.add_argument("--install", action="store_true",
                        help="顺便把当前构建的 DLL 安装到目标目录")
    parser.add_argument("--watch", action="store_true",
                        help="实时显示物理烘焙进度（按 Ctrl-C 退出）")
    args = parser.parse_args(argv)

    if args.target:
        target = Path(args.target)
        label = "指定目录"
    else:
        status = rt.runtime_status()
        target = Path(status["runtime_dir"])
        label = "隔离 Runtime"
        if not status["prepared"]:
            say(f"!! 隔离 Runtime 尚未准备：{target}")
            say("   先运行 mmm_e2e.py run --isolate，或用 --target 指定 MMM 目录。")
            return 2

    say("=" * 74)
    say("MMM 插件加载诊断")
    say("=" * 74)
    say(f"目标（{label}）: {target}")

    # -- 1. 插件 DLL ---------------------------------------------------
    installed = target / "Plugins" / rt.PLUGIN_DLL_NAME
    say("")
    say("[1] 插件 DLL")
    say(f"    目标位置 : {installed}")
    say(f"    状态     : {describe(installed)}")
    say(f"    构建产物 : {BUILD_DLL}")
    say(f"    构建状态 : {describe(BUILD_DLL)}")

    if args.install:
        if not BUILD_DLL.is_file():
            say("    !! 构建产物不存在，无法安装。请先构建：")
            say('       dotnet build plugins/mmm/MMDBridgeMMMPlugin/'
                'MMDBridgeMMMPlugin.csproj -c Release -p:MmmPath="<MMM 目录>"')
            return 2
        try:
            rt.install_plugin(target, BUILD_DLL)
            say("    → 已安装（旧文件已备份）")
        except rt.MmmRuntimeError as exc:
            say(f"    !! 安装失败：{exc.message}")
            return 2

    # -- 2. 握手文件 ---------------------------------------------------
    say("")
    say("[2] 握手文件（Python → 插件）")
    active = proto.read_active_job()
    if active is None:
        say(f"    {proto.active_job_path()}  —— 不存在或不可解析")
        say("    插件即使加载了也不会写 telemetry。")
    else:
        say(f"    job_id  : {active.job_id}")
        say(f"    nonce   : {active.nonce}")
        say(f"    job_dir : {active.job_dir}")
        say(f"    model   : {active.effective_model}")

    # -- 3. 插件自诊断日志 ---------------------------------------------
    log, lines = read_startup_log()
    say("")
    say("[3] 插件自诊断日志")
    say(f"    {log}")
    if not lines:
        say("    （无记录）")
    else:
        for line in lines[-25:]:
            say(f"    {line}")
        if len(lines) > 25:
            say(f"    …（共 {len(lines)} 行，只显示最后 25 行）")

    # -- 4. telemetry --------------------------------------------------
    say("")
    say("[4] telemetry 输出")
    if active is not None:
        job_dir = Path(active.job_dir)
        for name in ("heartbeat.json", "state.json", "model_snapshot.json",
                     "motion_snapshot.json", "display_frame_snapshot.json",
                     "bake_snapshot.json"):
            say(f"    {name:30s} {describe(job_dir / name)}")
    else:
        say("    （没有活动任务，跳过）")

    # -- 5. 结论 -------------------------------------------------------
    code, explanation = diagnose(target,
                                  job_dir=Path(active.job_dir) if active else None)
    say("")
    say("=" * 74)
    say(f"结论：{code}")
    say("=" * 74)
    say(explanation)
    if code == "RUNNING" and active is not None:
        model = proto.read_snapshot(active.job_dir, "model")
        bones = int(model.get("bone_count", 0) or 0)
        if bones == 0:
            say("")
            say("下一步：MMM 里还没有加载模型。请 Load Model 载入：")
            say(f"  {active.effective_model}")
    say("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
