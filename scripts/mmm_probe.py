"""Stage 2.2 验证工具：让 MMM 插件上线并读取它的 telemetry。

用法
----
先构建插件（见 plugins/mmm/MMDBridgeMMMPlugin/README.md），然后：

    # 1) 安装 DLL 到 MMM，并启动 MMM（会备份已有 DLL）
    ./.venv/Scripts/python.exe scripts/mmm_probe.py install --mmm "F:\\MikuMikuDance Exe\\MikuMikuMoving64_v1292"

    # 2) 一键：安装 + 建任务 + 启动 MMM + 观察 telemetry
    ./.venv/Scripts/python.exe scripts/mmm_probe.py run --mmm "F:\\...\\MikuMikuMoving64_v1292"

    # 3) 只看已有任务的 telemetry
    ./.venv/Scripts/python.exe scripts/mmm_probe.py watch --job <job_dir>

`run` 会打印一个任务目录。启动 MMM 后**手动打开 final.pmx**，
脚本会把插件报上来的模型信息、表示枠内容、动作帧统计实时打印出来。

注意
----
本工具直接安装到**你指定的 MMM 目录**。方案 §14 要求正式流程使用隔离的
MMM Runtime；本工具只用于 Stage 2.2 的首次验证，不进入正式主流程。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmd_bridge import mmm_protocol as proto          # noqa: E402

PLUGIN_DLL_NAME = "MMDBridgeMMMPlugin.dll"
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_BUILD = (REPO_ROOT / "plugins" / "mmm" / "MMDBridgeMMMPlugin"
                / "bin" / "Release" / PLUGIN_DLL_NAME)

MMM_VERSION = "1.2.9.2"


def _local_appdata() -> Path:
    import os
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def runtime_dir(version: str = MMM_VERSION) -> Path:
    """隔离 Runtime 的目标目录（方案 §14 的简化预览）。"""
    return _local_appdata() / "MMDBridge" / "mmm_runtime" / version / "runtime"


def prepare_runtime(mmm_dir: Path, *, force: bool = False) -> Path:
    """把 MMM 整体复制到隔离目录，返回副本路径。

    这是方案 §14「不要自动化用户日常 MMM」的最小实现：首次验证插件时，
    万一插件导致 MMM 异常，受影响的是副本而不是用户正在用的那套。
    """
    target = runtime_dir()
    exe_name = mmm_dir.name  # 仅用于提示

    if target.is_dir() and not force:
        exe = target / "MikuMikuMoving64.exe"
        if exe.is_file():
            print(f"复用已有隔离 Runtime: {target}")
            return target
        print(f"隔离目录已存在但缺少 exe，重新复制: {target}")

    size_mb = sum(f.stat().st_size for f in mmm_dir.rglob("*") if f.is_file()) / 1e6
    print(f"复制 MMM → {target}")
    print(f"  来源: {mmm_dir}（约 {size_mb:.0f} MB，请稍候）")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(mmm_dir, target, dirs_exist_ok=True)
    print(f"  完成。隔离 Runtime 中的 exe: {target / 'MikuMikuMoving64.exe'}")
    print(f"  （源目录 {exe_name} 未被修改）")
    return target



# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def install_dll(mmm_dir: Path, *, source: Path | None = None) -> Path:
    """把插件 DLL 复制到 MMM 的 Plugins 目录，备份同名旧文件。"""
    dll = source or PLUGIN_BUILD
    if not dll.is_file():
        raise SystemExit(
            f"找不到插件 DLL：{dll}\n"
            "请先构建：dotnet build plugins/mmm/MMDBridgeMMMPlugin/MMDBridgeMMMPlugin.csproj -c Release "
            '-p:MmmPath="<你的 MMM 目录>"')

    plugins = mmm_dir / "Plugins"
    if not plugins.is_dir():
        raise SystemExit(f"找不到 MMM 的 Plugins 目录：{plugins}")

    target = plugins / PLUGIN_DLL_NAME
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_suffix(f".dll.bak-{stamp}")
        shutil.copy2(target, backup)
        print(f"已备份旧 DLL -> {backup.name}")

    shutil.copy2(dll, target)
    print(f"已安装: {target}")
    return target


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------

def make_job(model: str = "", vmd: str = "", *,
             end_frame: int = 7000) -> proto.JobRequest:
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job_dir = _local_appdata() / "MMDBridge" / "jobs" / job_id / "mmm"
    job_dir.mkdir(parents=True, exist_ok=True)
    request = proto.JobRequest(
        job_id=job_id, job_dir=str(job_dir),
        effective_model=model, source_vmd=vmd,
        start_frame=0, end_frame=end_frame)
    proto.write_active_job(request)
    return request


# --------------------------------------------------------------------------
# watch
# --------------------------------------------------------------------------

def _fmt(value, limit: int = 8) -> str:
    if isinstance(value, list):
        head = ", ".join(repr(v) for v in value[:limit])
        if len(value) > limit:
            head += f", …(共 {len(value)})"
        return f"[{head}]"
    return repr(value)


def watch(job_dir: Path, job_id: str, nonce: str, *,
          seconds: float = 180.0, poll: float = 0.5,
          expect_bones: int | None = None) -> int:
    """轮询并打印 telemetry，直到超时或出现模型信息。"""
    print(f"等待插件上线（最多 {seconds:.0f}s）…")
    try:
        hb = proto.wait_for_plugin(job_dir, job_id=job_id, nonce=nonce,
                                   timeout=seconds)
    except proto.PluginNotReady as exc:
        print(f"\n!! 插件未上线\n{exc.message}")
        return 2

    print(f"\n✅ 插件已上线：pid={hb.pid} version={hb.plugin_version} "
          f"state={hb.scene_state} marker={hb.marker_position}\n")

    deadline = time.time() + seconds
    last_seen: dict[str, str] = {}
    saw_model = False

    while time.time() < deadline:
        state = proto.read_state(job_dir)
        model = proto.read_snapshot(job_dir, "model")
        frames = proto.read_snapshot(job_dir, "display_frame")
        motion = proto.read_snapshot(job_dir, "motion")
        bake = proto.read_snapshot(job_dir, "bake")

        bone_count = model.get("bone_count", 0)
        if bone_count and not saw_model:
            saw_model = True
            print("=" * 72)
            print("模型已加载")
            print("=" * 72)

        for label, payload in (("state", state), ("model", model),
                               ("display_frame", frames), ("motion", motion),
                               ("bake", bake)):
            if not payload:
                continue
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if last_seen.get(label) == text:
                continue
            last_seen[label] = text
            print(f"\n--- {label} ---")
            for key, value in payload.items():
                if key in ("updated",):
                    continue
                print(f"    {key:26s} = {_fmt(value)}")

        hb = proto.read_heartbeat(job_dir)
        if hb is not None:
            print(f"\r  [{hb.scene_state:15s}] marker={hb.marker_position:<7d} "
                  f"age={hb.age:4.1f}s bones={bone_count}   ", end="")

        if expect_bones and bone_count == expect_bones:
            print(f"\n\n✅ 已读到 {expect_bones} 根骨骼 —— Stage 2.2 验收通过")
            return 0

        time.sleep(poll)

    print("\n\n（观察窗口结束）")
    return 0 if saw_model else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MMM 插件 Stage 2.2 验证工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="只安装 DLL 到 MMM")
    p_install.add_argument("--mmm", required=True, help="MMM 安装目录")
    p_install.add_argument("--dll", help="指定插件 DLL 路径（默认用 Release 产物）")
    p_install.add_argument("--isolate", action="store_true",
                           help="先复制 MMM 到隔离 Runtime 再安装，不碰原目录")
    p_install.add_argument("--force-copy", action="store_true",
                           help="隔离目录已存在时强制重新复制")

    p_run = sub.add_parser("run", help="安装 + 建任务 + 启动 MMM + 观察")
    p_run.add_argument("--mmm", required=True, help="MMM 安装目录")
    p_run.add_argument("--model", default="", help="模型路径（仅记录进任务）")
    p_run.add_argument("--vmd", default="", help="动作路径（仅记录进任务）")
    p_run.add_argument("--seconds", type=float, default=180.0)
    p_run.add_argument("--expect-bones", type=int, default=None,
                       help="读到该骨骼数即判定通过（final.pmx 是 689）")
    p_run.add_argument("--no-launch", action="store_true", help="不自动启动 MMM")
    p_run.add_argument("--isolate", action="store_true",
                       help="先复制 MMM 到隔离 Runtime 再安装并启动，不碰原目录")
    p_run.add_argument("--force-copy", action="store_true",
                       help="隔离目录已存在时强制重新复制")

    p_watch = sub.add_parser("watch", help="只观察已有任务")
    p_watch.add_argument("--job", required=True, help="任务目录（含 heartbeat.json 的目录）")
    p_watch.add_argument("--job-id", default="")
    p_watch.add_argument("--nonce", default="")
    p_watch.add_argument("--seconds", type=float, default=180.0)

    args = parser.parse_args(argv)

    if args.command == "install":
        mmm_dir = Path(args.mmm)
        if args.isolate:
            mmm_dir = prepare_runtime(mmm_dir, force=args.force_copy)
        install_dll(mmm_dir, source=Path(args.dll) if args.dll else None)
        print("\n启动 MMM 后，本工具会用 active_job.json 让插件开始上报。")
        return 0

    if args.command == "run":
        mmm_dir = Path(args.mmm)
        if args.isolate:
            mmm_dir = prepare_runtime(mmm_dir, force=args.force_copy)
        install_dll(mmm_dir)
        request = make_job(args.model, args.vmd)
        print(f"\n任务目录: {request.job_dir}")
        print(f"job_id  : {request.job_id}")
        print(f"nonce   : {request.nonce}")

        exe = mmm_dir / "MikuMikuMoving64.exe"
        if not args.no_launch:
            if not exe.is_file():
                print(f"\n!! 找不到 {exe}，请手动启动 MMM")
            else:
                print(f"\n启动 MMM: {exe}")
                subprocess.Popen([str(exe)], cwd=str(mmm_dir))
                time.sleep(3.0)
        else:
            print("\n（未自动启动 MMM，请手动启动）")

        print("\n请在 MMM 里打开 final.pmx（或任意模型）。")
        return watch(Path(request.job_dir), request.job_id, request.nonce,
                     seconds=args.seconds, expect_bones=args.expect_bones)

    if args.command == "watch":
        job_dir = Path(args.job)
        active = proto.read_active_job()
        job_id = args.job_id or (active.job_id if active else "")
        nonce = args.nonce or (active.nonce if active else "")
        if not job_id or not nonce:
            print("!! 无法确定 job_id / nonce，请用 --job-id 与 --nonce 指定")
            return 2
        return watch(job_dir, job_id, nonce, seconds=args.seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
