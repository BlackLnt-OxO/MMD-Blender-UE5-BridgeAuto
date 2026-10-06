"""导出 MMM 的 UI Automation 控件树 —— 写 UIA 驱动的前置调查。

为什么需要它
------------
MMM 的 SDK 里没有触发物理烘焙的 API，主程序里也**完全没有 "bake" 字符串**，
所以烘焙只能靠点界面按钮。要写自动化，就必须知道每个按钮**在 UIA 里长什么样**。

从 `MikuMikuMoving64.exe` 的字符串里已经挖到一批控件名：

.. code-block:: text

    recordPhysicsButtonElement          ← 点「记录」触发物理烘焙
    recordPhysicsBookmarkButtonElement  ← 烘焙 bookmark 区间
    physicsAlwaysToggleButtonElement    ← 「总是计算」
    physicsPlayingToggleButtonElement   ← 「播放时计算」
    physicsNoneToggleButtonElement      ← 「不计算」
    physicsSettingsButtonElement        ← 物理设置
    exportVmdButtonElement              ← 「导出 VMD」
    exportPoseButtonElement             ← 导出姿势

但**名字存在 ≠ UIA 能访问到**。DevExpress 的 Ribbon 控件未必完整暴露
UIA 树，而且真实路径（父链）与可用的调用方式（Invoke / Toggle / SelectionItem）
只能实测。本工具就是去把这一层确认下来。

用法
----
    # MMM 开着的时候跑
    ./.venv/Scripts/python.exe scripts/mmm_uia_dump.py

    # 只看我们关心的控件
    ./.venv/Scripts/python.exe scripts/mmm_uia_dump.py --focus

    # 调整深度
    ./.venv/Scripts/python.exe scripts/mmm_uia_dump.py --depth 12
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 导出时重点标注的控件。
#:
#: **用"显示名"而不是 exe 里的内部名。**
#: 实测踩过：exe 里有 `recordPhysicsButtonElement` 这类标识，但 UIA 树暴露的是
#: **按钮的显示文本**（`记录` / `总是\r\n计算`）。早期版本按内部名标注，
#: 结果一个都没标上，看起来像"导出失败"。
INTERESTING = (
    # 加载类
    "加载模型", "导入动作", "加载附件", "导入姿势",
    # 物理面板（实测确认）
    "物理设置", "初始化刚体", "启用/禁用",
    "总是", "仅播放", "记录", "仅选定的骨骼",
    # 导出/保存
    "导出", "保存", "选择所有骨骼",
    # 插件
    "重新加载插件",
)

#: 与流程相关的关键词，用于 --focus 过滤。
FOCUS_KEYWORDS = (
    "record", "physics", "export", "model", "motion", "vmd", "play",
    "load", "import", "file", "menu", "ribbon", "bar", "button",
)


def say(text: str = "") -> None:
    print(text, flush=True)


def is_mmm_window(window) -> bool:
    """这个窗口是不是真正的 MMM 主窗口。

    **只看标题会误伤**：实测浏览器里开着标题含 "MikuMikuMoving" 的标签页时，
    `Desktop().windows()` 会把它也返回 —— 于是工具去 dump 了浏览器。
    必须再加一道 WinForms 类名判定（MMM 是 WinForms 程序，
    类名形如 ``WindowsForms10.Window.8.app.0.xxx``）。
    """
    try:
        title = window.window_text()
    except Exception:                              # noqa: BLE001
        return False
    if "MikuMikuMoving" not in title:
        return False
    try:
        class_name = window.element_info.class_name or ""
    except Exception:                              # noqa: BLE001
        class_name = ""
    return "WindowsForms" in class_name


def find_mmm_windows(backend: str = "uia"):
    """找出 MMM 主窗口（已排除同名的浏览器标签页）。

    ``backend`` 可以是 ``uia`` 或 ``win32``。
    **两个后端暴露的控件不一样** —— 实测某些 DevExpress 按钮在 UIA 树里
    看不到（例如「导出动作」），但在 Win32 树里存在。
    所以排查"某个按钮找不到"时，两个后端都要试。
    """
    from pywinauto import Desktop
    return [w for w in Desktop(backend=backend).windows()
            if is_mmm_window(w)]


def describe(element, backend: str = "uia") -> str:
    info = element.element_info
    parts = []
    try:
        if info.name:
            parts.append(f"name={info.name!r}")
    except Exception:                              # noqa: BLE001
        pass
    try:
        if info.control_type:
            parts.append(f"type={info.control_type}")
    except Exception:                              # noqa: BLE001
        pass
    try:
        if info.automation_id:
            parts.append(f"auto_id={info.automation_id!r}")
    except Exception:                              # noqa: BLE001
        pass
    try:
        if info.class_name:
            parts.append(f"class={info.class_name!r}")
    except Exception:                              # noqa: BLE001
        pass
    if backend == "win32":
        # Win32 后端没有 automation_id，但矩形位置很有用 ——
        # 找不到控件时，可以用坐标点它。
        try:
            rect = element.rectangle()
            parts.append(f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
        except Exception:                          # noqa: BLE001
            pass
    return "  ".join(parts) or "<无信息>"


def walk(element, *, depth: int, max_depth: int, lines: list[str],
         focus_only: bool, backend: str = "uia", prefix: str = "") -> None:
    if depth > max_depth:
        return
    try:
        text = describe(element)
    except Exception as exc:                       # noqa: BLE001
        text = f"<读取失败 {type(exc).__name__}>"

    marker = ""
    flat = text.replace("\r", "").replace("\n", "")
    for name in INTERESTING:
        if name in flat:
            marker = "  ★ 流程相关"
            break

    if not focus_only or marker or depth <= 2:
        lines.append(f"{prefix}{text}{marker}")

    if depth == max_depth:
        return
    try:
        children = element.children()
    except Exception:                              # noqa: BLE001
        return
    for index, child in enumerate(children):
        last = index == len(children) - 1
        walk(child, depth=depth + 1, max_depth=max_depth, lines=lines,
             focus_only=focus_only, backend=backend,
             prefix=prefix + ("    " if last else "│   "))


def dump_supported_patterns(element, lines: list[str]) -> None:
    """列出这个控件支持哪些 UIA 调用方式 —— 决定怎么点它。"""
    lines.append("")
    lines.append("支持的模式（决定用 Invoke / Toggle / SelectionItem 哪种去点）：")
    for pattern in ("invoke", "toggle", "selection_item", "expand_collapse",
                    "value", "legacy_iaccessible"):
        try:
            if element.is_enabled() and getattr(element, f"is_{pattern}_pattern_available")():
                lines.append(f"  ✓ {pattern}")
        except Exception:                          # noqa: BLE001
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 MMM 的 UIA 控件树")
    parser.add_argument("--depth", type=int, default=8, help="最大深度")
    parser.add_argument("--focus", action="store_true",
                        help="只输出与流程相关的控件（以及浅层结构）")
    parser.add_argument("--backend", default="uia", choices=("uia", "win32"),
                        help="uia（默认）或 win32 —— 两者暴露的控件不同，"
                             "找不到某个按钮时换另一个试试")
    parser.add_argument("--out", default=str(REPO_ROOT / "work" / "mmm_uia_tree.txt"))
    args = parser.parse_args(argv)

    try:
        import pywinauto  # noqa: F401
    except ImportError:
        say("!! 未安装 pywinauto。请先运行：")
        say("   .venv\\Scripts\\python.exe -m pip install pywinauto")
        return 2

    say("查找 MMM 窗口…")
    windows = find_mmm_windows(args.backend)
    if not windows:
        say("!! 没有找到 MMM 窗口。请先启动 MMM 再跑本工具。")
        return 2

    lines: list[str] = []
    for window in windows:
        try:
            title = window.window_text()
        except Exception:                          # noqa: BLE001
            title = "<标题读取失败>"
        lines.append("=" * 78)
        lines.append(f"顶层窗口：{title}")
        lines.append("=" * 78)
        lines.append(f"自身：{describe(window, args.backend)}")
        dump_supported_patterns(window, lines)
        lines.append("")
        lines.append("控件树：")
        walk(window, depth=0, max_depth=args.depth, lines=lines,
             focus_only=args.focus, backend=args.backend)
        lines.append("")

    text = "\n".join(lines)
    say(text)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    say("")
    say(f"已写入：{out}")

    # 单独列出找到的流程相关控件，方便直接抄
    hits = [ln for ln in lines if "★ 流程相关" in ln]
    say("")
    say("=" * 78)
    say(f"流程相关控件：{len(hits)} 个")
    say("=" * 78)
    for line in hits:
        say(line)
    if not hits:
        say("（没有找到 —— 可能 MMM 的 Ribbon 未暴露 UIA 树，")
        say("  或者当前不在「物理」/「文件」页签上。请切到对应页签后重跑。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
