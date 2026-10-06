"""MMM 的 UI Automation 驱动 —— 方案 §26 / Stage 2.7 / 2.8。

为什么必须有它
--------------
MMM 的 SDK 里没有任何触发物理烘焙的 API；扫 `MikuMikuMoving64.exe` 的
37,301 条字符串，**"bake" 一次都没出现**。所以"执行物理烘焙"只能靠点界面按钮，
这是硬约束（方案 §34）。

设计要点
--------
**1. 控件名用中文按钮名，不用 exe 里的内部名。**
exe 里有 `recordPhysicsButtonElement` 这类内部标识，但实测 UIA 树暴露的是
**按钮的显示文本**（`加载模型` / `导入动作` / `选择所有骨骼`）。
因此本模块按**显示名**定位，并保留多语言候选（用户可能切换 MMM 的界面语言）。

**2. 布局会变，定位必须容错。**
实测两次 dump 的树结构不同：一次有 `mainRibbon` + `Ribbon Tabs`，
另一次只有若干 `ToolBar`。MMM 的界面随页签与窗口宽度动态重建。
因此**不做固定路径匹配**，而是在整棵树里按名字搜。

**3. 定位逻辑与真实 UIA 解耦。**
`find_element()` 只依赖 :class:`ElementAdapter` 协议，
于是可以用假元素树完整测试 —— 不需要真的开着 MMM。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, Sequence

__all__ = [
    "BUTTON_EXPORT_VMD",
    "BUTTON_SAVE",
    "BUTTON_RECORD_ONLY_SELECTED",
    "BUTTON_PHYSICS_SETTINGS",
    "BUTTON_PHYSICS_ENABLE",
    "BUTTON_INIT_PHYSICS",
    "BUTTON_LOAD_MODEL",
    "BUTTON_LOAD_MOTION",
    "BUTTON_PHYSICS_ALWAYS",
    "BUTTON_RECORD_PHYSICS",
    "BUTTON_SELECT_ALL_BONES",
    "DIALOG_CONFIRM",
    "MMM_UI_ACTION_FAILED",
    "MMM_UI_ELEMENT_NOT_FOUND",
    "MMM_PROCESS_NAME",
    "RESIDENT_TOOLBAR_NAMES",
    "MMM_UI_WINDOW_NOT_FOUND",
    "ElementAdapter",
    "MmmUiDriver",
    "MmmUiError",
    "find_element",
    "is_mmm_window",
    "match_name",
    "normalize_label",
    "walk_elements",
]

MMM_UI_WINDOW_NOT_FOUND = "MMM_UI_WINDOW_NOT_FOUND"
MMM_UI_ELEMENT_NOT_FOUND = "MMM_UI_ELEMENT_NOT_FOUND"
MMM_UI_ACTION_FAILED = "MMM_UI_ACTION_FAILED"

#: 默认窗口标题关键词。
WINDOW_TITLE_HINT = "MikuMikuMoving"

#: MMM 的进程名 —— 按它连接比全量枚举窗口快一个数量级。
MMM_PROCESS_NAME = "MikuMikuMoving64.exe"

# --------------------------------------------------------------------------
# 控件名候选（方案 §26 的人工流程逐步对应）
# --------------------------------------------------------------------------
#
# 顺序即优先级：先中文（本机语言），再日文，最后英文。
# 加候选而不是写死一个名字，是因为 MMM 的界面语言可切换
# （Settings.xml 的 <Language>zh-CN</Language>），而且不同版本译名可能不同。

BUTTON_LOAD_MODEL: tuple[str, ...] = ("加载模型", "モデル読込", "Load Model")
BUTTON_LOAD_MOTION: tuple[str, ...] = ("导入动作", "モーション読込", "Load Motion")
BUTTON_LOAD_ACCESSORY: tuple[str, ...] = ("加载附件", "アクセサリ読込")

#: 「总是计算」——物理面板的三个模式之一。
#: **注意**：实测 UIA 报的文本是 `总是\r\n计算`（按钮文字换行），
#: 所以比对前必须归一化空白，见 :func:`match_name`。
BUTTON_PHYSICS_ALWAYS: tuple[str, ...] = ("总是计算", "常に計算", "Physics Always")
BUTTON_PHYSICS_PLAYING: tuple[str, ...] = ("仅播放时计算", "再生時計算", "Physics Playing")
BUTTON_PHYSICS_NONE: tuple[str, ...] = ("不计算", "計算しない", "Physics None")

#: 物理总开关（启用/禁用）。
BUTTON_PHYSICS_ENABLE: tuple[str, ...] = ("启用/禁用", "有効/無効")

#: 插件按钮 —— **点一下才会启用插件**。
#:
#: ⚠ **必须点「常驻（Resident）」组里的那个，不是「命令」组里的。**
#:
#: MMM 的插件页签下有两个工具条，各自都有一个同名按钮：
#:
#: .. code-block:: text
#:
#:     name='命令'      type=ToolBar
#:       Angle Zero / LipSynchloid / AutoBake / Random Blink      ← 命令按钮
#:     name='Resident'  type=ToolBar
#:       AutoBake / PoseDictionary                                 ← ★ 启用开关
#:
#: 点「命令」组那个只会触发 ``ICommandPlugin.Run()`` —— 而 ``Run()`` 被调用
#: **不等于**插件被启用。点「常驻」组那个才会让 MMM 调 ``IResidentPlugin.Enabled()``，
#: 之后 ``Update()`` 才开始跑、telemetry 才有数据。
#:
#: 实测踩过：点了「命令」组那个，日志里 ``Run()`` 出现 3 次，
#: 而 ``Enabled()`` 一次都没有，流程卡在「等待插件上线」直到超时。
BUTTON_PLUGIN_ENABLE: tuple[str, ...] = ("AutoBake", "MMDBridge")

#: 「常驻」工具条的名字 —— 启用开关所在的组。
RESIDENT_TOOLBAR_NAMES: tuple[str, ...] = ("Resident", "常駐", "常驻")

#: 「记录」——真正触发物理烘焙的那个按钮。
BUTTON_RECORD_PHYSICS: tuple[str, ...] = ("记录", "記録", "Record")

#: 「仅选定的骨骼」——在**记录**工具条下。
#:
#: ⚠ 这是个危险开关：开着时烘焙只记录选中的骨骼，
#: 结果会**不完整但不报错**。自动化流程必须确认它处于**关闭**状态。
BUTTON_RECORD_ONLY_SELECTED: tuple[str, ...] = (
    "仅选定的骨骼", "選択したボーンのみ", "Only Selected Bones")

#: 「初始化刚体」——必要时在烘焙前重置物理状态。
BUTTON_INIT_PHYSICS: tuple[str, ...] = (
    "初始化刚体", "剛体初期化", "Initialize Rigidbodies")

#: 物理设置对话框。
BUTTON_PHYSICS_SETTINGS: tuple[str, ...] = ("物理设置", "物理設定")

#: 「导出动作」——导出 VMD。
#:
#: 实测：在「文件」页签的工具条里**没有**这个按钮，
#: 它可能藏在菜单或需要先打开某个面板。若改用"插件直接写 VMD"
#: （见方案讨论），这个按钮就完全不需要了。
BUTTON_EXPORT_VMD: tuple[str, ...] = (
    "导出动作", "VMD出力", "Export Motion", "Export VMD")

#: 导出对话框里的「选择所有骨骼」。
BUTTON_SELECT_ALL_BONES: tuple[str, ...] = (
    "选择所有骨骼", "全てのボーンを選択", "Select All Bones")

#: 保存（导出对话框确认后可能用得上）。
BUTTON_SAVE: tuple[str, ...] = ("保存", "上書き保存", "Save")

#: 各种确认按钮。PMX 载入后可能弹「使用许可」，必须点掉，
#: 否则模态窗口会阻塞后续所有操作。
DIALOG_CONFIRM: tuple[str, ...] = ("确定", "OK", "はい", "是", "确认")


class MmmUiError(RuntimeError):
    """UIA 操作失败。携带稳定 ``code``。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# 元素抽象 —— 让定位逻辑可以脱离真实 UIA 测试
# --------------------------------------------------------------------------

class ElementAdapter(Protocol):
    """UIA 元素的最小接口。真实实现包 pywinauto，测试用假树。"""

    def element_name(self) -> str: ...
    def element_control_type(self) -> str: ...
    def element_automation_id(self) -> str: ...
    def element_children(self) -> list["ElementAdapter"]: ...
    def element_enabled(self) -> bool: ...
    def element_click(self) -> None: ...


def normalize_label(text: str) -> str:
    """把控件文本归一化，便于比对。

    实测 MMM 的按钮文本会带这些噪声：

    * **换行** —— 「总是计算」实际是 ``'总是\\r\\n计算'``（按钮文字折行）
    * **快捷键后缀** —— ``'加载模型(M)'``
    * **省略号** —— ``'导出动作...'``
    * 首尾空白

    不做归一化就会**匹配不上**，而 UIA 匹配失败的表现是"点了别的按钮"
    或"找不到控件" —— 后者至少会报错，前者是静默错。
    """
    if not text:
        return ""
    result = re.sub(r"\s+", "", text)          # 去掉所有空白（含 \r \n）
    result = result.rstrip(".．。…")           # 去掉省略号
    result = re.sub(r"[（(][A-Za-z0-9][）)]$", "", result)   # 去掉快捷键后缀
    return result


def match_name(actual: str, candidates: Sequence[str]) -> str | None:
    """把实际控件名与候选表比对，返回命中的候选。

    先比归一化后的**精确**匹配，再退化为**包含**匹配 ——
    因为 MMM 的按钮文本常带换行、快捷键后缀或省略号。

    精确优先很重要：否则「记录」会被「记录书签」抢走。
    """
    normalized = normalize_label(actual)
    if not normalized:
        return None
    for candidate in candidates:
        if normalized == normalize_label(candidate):
            return candidate
    for candidate in candidates:
        if normalize_label(candidate) in normalized:
            return candidate
    return None


def walk_elements(root: ElementAdapter, *, max_depth: int = 24,
                  max_nodes: int = 20000) -> Iterable[ElementAdapter]:
    """深度优先遍历。带深度与节点数上限，避免异常 UI 把遍历拖死。

    ``max_nodes`` 是**硬上限**：在产出之前检查，而不是产出之后 ——
    否则已经压栈的兄弟节点仍会被产出，实际数量会超。
    """
    stack: list[tuple[ElementAdapter, int]] = [(root, 0)]
    seen = 0
    while stack:
        if seen >= max_nodes:
            return
        element, depth = stack.pop()
        yield element
        seen += 1
        if depth >= max_depth:
            continue
        try:
            children = element.element_children()
        except Exception:                          # noqa: BLE001
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))


def find_element(root: ElementAdapter, *,
                 names: Sequence[str] = (),
                 control_types: Sequence[str] = (),
                 automation_id: str = "",
                 require_enabled: bool = True,
                 max_depth: int = 24) -> tuple[ElementAdapter | None, str]:
    """在整棵树里找元素。

    **不做路径匹配** —— MMM 的布局会随页签与窗口宽度重建
    （实测两次 dump 结构就不同），固定路径必然失效。

    Returns:
        ``(元素, 命中的名字)``；找不到时元素为 ``None``。
    """
    for element in walk_elements(root, max_depth=max_depth):
        try:
            if automation_id and element.element_automation_id() != automation_id:
                continue
            if control_types:
                ctype = element.element_control_type() or ""
                if ctype not in control_types:
                    continue
            if names:
                hit = match_name(element.element_name(), names)
                if hit is None:
                    continue
            else:
                hit = ""
            if require_enabled and not element.element_enabled():
                continue
            return element, hit
        except Exception:                          # noqa: BLE001
            continue
    return None, ""


# --------------------------------------------------------------------------
# 假元素树（测试用，也可用于 dry-run）
# --------------------------------------------------------------------------

@dataclass
class FakeElement:
    """一个纯内存的 UIA 元素。用于测试定位逻辑，不需要真的开 MMM。"""

    name: str = ""
    control_type: str = "Button"
    automation_id: str = ""
    enabled: bool = True
    children: list["FakeElement"] = field(default_factory=list)
    clicked: list[str] = field(default_factory=list, repr=False)

    def element_name(self) -> str:
        return self.name

    def element_control_type(self) -> str:
        return self.control_type

    def element_automation_id(self) -> str:
        return self.automation_id

    def element_children(self) -> list["FakeElement"]:
        return list(self.children)

    def element_enabled(self) -> bool:
        return self.enabled

    def element_click(self) -> None:
        self.clicked.append(self.name)


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------

def is_mmm_window(window, title_hint: str = WINDOW_TITLE_HINT) -> bool:
    """这个窗口是不是真正的 MMM 主窗口。

    **只看标题会误伤。** 实测浏览器里开着标题含 "MikuMikuMoving" 的标签页时，
    ``Desktop().windows()`` 会把它也返回 —— 驱动可能连上浏览器去点按钮。

    因此再加一道 **WinForms 类名**判定：MMM 是 WinForms 程序，
    类名形如 ``WindowsForms10.Window.8.app.0.xxx``；浏览器不是。
    """
    try:
        if title_hint not in (window.window_text() or ""):
            return False
    except Exception:                              # noqa: BLE001
        return False
    try:
        class_name = window.element_info.class_name or ""
    except Exception:                              # noqa: BLE001
        class_name = ""
    return "WindowsForms" in class_name


class MmmUiDriver:
    """驱动 MMM 的界面。

    **pywinauto 是延迟导入的** —— 让本模块在没装它的环境里也能被导入与测试。
    """

    def __init__(self, *, window_title: str = WINDOW_TITLE_HINT,
                 timeout: float = 30.0, poll: float = 0.3,
                 on_log: Callable[[str], None] | None = None,
                 root: ElementAdapter | None = None):
        self.window_title = window_title
        self.timeout = timeout
        self.poll = poll
        self._on_log = on_log
        self._root = root              # 注入假树用于测试
        self._window: Any = None

    # -- 基础设施 ------------------------------------------------------

    def log(self, text: str) -> None:
        if self._on_log:
            self._on_log(text)

    def root(self) -> ElementAdapter:
        """当前 MMM 主窗口（根元素）。

        **必须包装成 :class:`_PywinautoAdapter`。** ``_attach()`` 返回的是
        pywinauto 的原始对象，它没有 ``element_name()`` 等方法 ——
        直接返回会让 :func:`walk_elements` 在每个节点上抛
        ``AttributeError``，而那个异常会被逐节点吞掉，
        表现是"整棵树一个控件都找不到"，极难定位。
        """
        if self._root is not None:
            return self._root
        if self._window is None:
            self._window = _PywinautoAdapter(self._attach())
        return self._window

    def _attach(self):
        """找到并附加到 MMM 主窗口。

        **分两阶段，慢路径绝不能进重试循环。**

        阶段 1（占超时的前 80%）：只走**快路径** ``connect(path=...)``。
        它按进程名连接，单次约 0.5 秒。

        阶段 2（最后 20%）：才退回全量枚举窗口。

        实测教训：早期版本每次重试都跑全量枚举，
        而枚举要遍历**所有**顶层窗口、每个 ``window_text()`` 都是一次 COM 往返 ——
        机器上窗口一多，**单次就要约 44 秒**。
        表现是"MMM 开着但一分钟都不去点插件"，
        而超时明明设的是 1 秒 —— 耗时全在枚举本身。
        """
        try:
            from pywinauto import Application, Desktop
        except ImportError as exc:                 # pragma: no cover
            raise MmmUiError(
                MMM_UI_WINDOW_NOT_FOUND,
                "未安装 pywinauto，无法驱动 MMM 界面。\n"
                "请运行：.venv\\Scripts\\python.exe -m pip install pywinauto") from exc

        started = time.time()
        fast_deadline = started + self.timeout * 0.8
        hard_deadline = started + self.timeout
        scanned = False

        while time.time() < hard_deadline:
            window = self._connect_by_process(Application)
            if window is not None:
                self._log_attached(window)
                return window
            # 只在快路径耗完配额后才扫一次全量 —— 且**只扫一次**。
            if time.time() >= fast_deadline and not scanned:
                scanned = True
                window = self._scan_desktop(Desktop)
                if window is not None:
                    self._log_attached(window)
                    return window
            time.sleep(self.poll)

        raise MmmUiError(
            MMM_UI_WINDOW_NOT_FOUND,
            f"在 {self.timeout:.0f}s 内没找到标题含 {self.window_title!r} 的窗口。\n"
            "请确认 MMM 已启动。")

    def _log_attached(self, window) -> None:
        try:
            self.log(f"已连接 MMM 窗口：{window.window_text()}")
        except Exception:                          # noqa: BLE001
            pass

    def _connect_by_process(self, application_cls):
        """按进程名连接 —— 快路径。"""
        try:
            app = application_cls(backend="uia").connect(
                path=MMM_PROCESS_NAME, timeout=0.5)
        except Exception:                          # noqa: BLE001
            return None
        try:
            window = app.window(title_re=f".*{self.window_title}.*")
            if window.exists(timeout=0.3) and is_mmm_window(window,
                                                            self.window_title):
                return window
        except Exception:                          # noqa: BLE001
            pass
        return None

    def _scan_desktop(self, desktop_cls):
        """全量枚举 —— 慢路径，只在快路径失败时用。"""
        try:
            windows = desktop_cls(backend="uia").windows()
        except Exception:                          # noqa: BLE001
            return None
        for window in windows:
            if is_mmm_window(window, self.window_title):
                return window
        return None

    # -- 定位与点击 ----------------------------------------------------

    def find(self, *, names: Sequence[str] = (),
             control_types: Sequence[str] = (),
             automation_id: str = "",
             require_enabled: bool = True,
             timeout: float | None = None) -> tuple[ElementAdapter, str]:
        """等待并返回元素。超时抛 :class:`MmmUiError`。"""
        budget = self.timeout if timeout is None else timeout
        deadline = time.time() + budget
        while True:
            element, hit = find_element(
                self.root(), names=names, control_types=control_types,
                automation_id=automation_id, require_enabled=require_enabled)
            if element is not None:
                return element, hit
            if time.time() >= deadline:
                raise MmmUiError(
                    MMM_UI_ELEMENT_NOT_FOUND,
                    f"找不到控件（候选名：{list(names) or control_types or automation_id}）。\n"
                    "  可能原因：\n"
                    "    1) 当前不在对应的页签上（MMM 的界面随页签重建）\n"
                    "    2) MMM 的界面语言与候选名都不匹配\n"
                    "    3) 该按钮被禁用（灰色）")
            time.sleep(self.poll)

    def click(self, *, names: Sequence[str] = (),
              control_types: Sequence[str] = (),
              automation_id: str = "",
              timeout: float | None = None) -> str:
        """点一个控件。返回命中的名字。"""
        element, hit = self.find(names=names, control_types=control_types,
                                 automation_id=automation_id, timeout=timeout)
        try:
            element.element_click()
        except Exception as exc:                   # noqa: BLE001
            raise MmmUiError(
                MMM_UI_ACTION_FAILED,
                f"点击 {hit or names!r} 失败：{type(exc).__name__}: {exc}") from exc
        self.log(f"已点击：{hit or names}")
        return hit

    # -- 弹窗处理 ------------------------------------------------------

    def dismiss_dialogs(self, *, rounds: int = 4) -> int:
        """点掉挡路的确认对话框（例如 PMX 的「使用许可」）。

        **必须在"载入模型"之后调用。** 模态窗口会阻塞后续所有 UIA 操作 ——
        实测 MMM 载入 PMX 后常弹许可框。

        Returns:
            点掉的对话框数量。
        """
        dismissed = 0
        for _ in range(rounds):
            try:
                from pywinauto import Desktop
            except ImportError:                    # pragma: no cover
                break
            found = False
            try:
                windows = Desktop(backend="uia").windows()
            except Exception:                      # noqa: BLE001
                break
            for window in windows:
                try:
                    title = window.window_text()
                    if self.window_title in title:
                        continue
                    button = window.child_window(
                        title_re="|".join(DIALOG_CONFIRM), control_type="Button")
                    if button.exists(timeout=0.5):
                        button.click_input()
                        dismissed += 1
                        found = True
                        self.log(f"已关闭对话框：{title!r}")
                except Exception:                  # noqa: BLE001
                    continue
            if not found:
                break
        return dismissed

    # -- 流程动作（方案 §26 的 12 步）---------------------------------

    def _click_button(self, names: Sequence[str]) -> str:
        """点一个**普通按钮**。

        必须限定 ``control_types=("Button",)``：
        实测 MMM 里「记录」同时存在两个控件 ——
        ``name='记录'  type=Button`` 与 ``name='记录...'  type=SplitButton``。
        SplitButton 点主体会**展开下拉菜单**而不是执行动作，
        点错就变成"什么都没发生"，而且不报错。
        """
        return self.click(names=names, control_types=("Button",))

    def load_model(self, path: str) -> None:
        """① 加载模型 + ② 处理许可弹窗。"""
        self._click_button(BUTTON_LOAD_MODEL)
        self._type_file_path(path)
        self.dismiss_dialogs()

    def load_motion(self, path: str) -> None:
        """③ 导入主动作。"""
        self._click_button(BUTTON_LOAD_MOTION)
        self._type_file_path(path)
        self.dismiss_dialogs()

    def set_physics_always(self) -> None:
        """⑥ 物理模式选「总是计算」。"""
        self._click_button(BUTTON_PHYSICS_ALWAYS)

    def enable_plugin(self) -> str:
        """点「常驻」组里的插件按钮启用它。

        **必须限定在 ``Resident`` 工具条内查找。** 插件页签下「命令」组里
        有一个同名按钮，点它只触发 ``ICommandPlugin.Run()`` ——
        而 ``Run()`` 被调用不等于插件被启用（``Enabled()`` 不会来）。
        见 :data:`BUTTON_PLUGIN_ENABLE` 的说明。

        它是个开关 —— 调用方必须先确认插件尚未上线，否则会把已启用的关掉。
        """
        # 先定位「常驻」工具条，再在它的子树里找按钮。
        try:
            toolbar, _ = self.find(names=RESIDENT_TOOLBAR_NAMES,
                                   control_types=("ToolBar",))
        except MmmUiError:
            # 找不到分组时退回全局查找 —— 总比完全不点好，
            # 但要在日志里说清楚，否则又会掉进"点了但没启用"的坑。
            self.log("⚠ 找不到「常驻」工具条，退回全局查找（可能点到「命令」组）")
            return self._click_button(BUTTON_PLUGIN_ENABLE)

        for element in walk_elements(toolbar):
            hit = match_name(element.element_name(), BUTTON_PLUGIN_ENABLE)
            if hit is None or not element.element_enabled():
                continue
            element.element_click()
            self.log(f"已点击「常驻」组的插件按钮：{hit}")
            return hit

        raise MmmUiError(
            MMM_UI_ELEMENT_NOT_FOUND,
            f"「常驻」工具条里找不到插件按钮（候选：{list(BUTTON_PLUGIN_ENABLE)}）。\n"
            "  可能原因：插件未被 MMM 识别，或按钮文本与候选名都不匹配。")

    def start_record_physics(self) -> None:
        """⑦ 点「记录」—— 真正触发物理烘焙。"""
        self._click_button(BUTTON_RECORD_PHYSICS)

    def init_physics(self) -> None:
        """必要时在烘焙前初始化刚体。"""
        self._click_button(BUTTON_INIT_PHYSICS)

    def export_vmd(self, path: str) -> None:
        """⑩⑪⑫ 导出动作：选所有关键帧 + 勾选所有骨骼 + 保存。"""
        self._click_button(BUTTON_EXPORT_VMD)
        self._click_button(BUTTON_SELECT_ALL_BONES)
        self._type_file_path(path)

    # -- 文件对话框 ----------------------------------------------------

    def _type_file_path(self, path: str) -> None:
        """在 Windows 通用文件对话框里填路径并确认。

        用"直接输入完整路径"而不是逐级浏览 —— 浏览依赖目录结构，
        路径一长就容易点错，而且慢。
        """
        try:
            from pywinauto import Desktop
        except ImportError:                        # pragma: no cover
            raise MmmUiError(MMM_UI_ACTION_FAILED, "未安装 pywinauto")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            for window in Desktop(backend="uia").windows():
                try:
                    if window.window_text() not in (
                            "打开", "另存为", "Open", "Save As", "名前を付けて保存"):
                        continue
                    edit = window.child_window(control_type="Edit")
                    edit.set_edit_text(path)
                    window.child_window(
                        title_re="打开|保存|Open|Save|開く|保存",
                        control_type="Button").click_input()
                    return
                except Exception:                  # noqa: BLE001
                    continue
            time.sleep(self.poll)

        raise MmmUiError(
            MMM_UI_ACTION_FAILED,
            f"没等到文件对话框（要填的路径：{path}）。")


class _PywinautoAdapter:
    """把 pywinauto 元素包成 :class:`ElementAdapter`。"""

    def __init__(self, element):
        self._element = element
        self._info = element.element_info

    def element_name(self) -> str:
        try:
            return self._info.name or ""
        except Exception:                          # noqa: BLE001
            return ""

    def element_control_type(self) -> str:
        try:
            return self._info.control_type or ""
        except Exception:                          # noqa: BLE001
            return ""

    def element_automation_id(self) -> str:
        try:
            return self._info.automation_id or ""
        except Exception:                          # noqa: BLE001
            return ""

    def element_children(self) -> list["_PywinautoAdapter"]:
        try:
            return [_PywinautoAdapter(child) for child in self._element.children()]
        except Exception:                          # noqa: BLE001
            return []

    def element_enabled(self) -> bool:
        try:
            return bool(self._element.is_enabled())
        except Exception:                          # noqa: BLE001
            return True

    def element_click(self) -> None:
        """点这个元素。

        **先把窗口拉到前台再点。** ``click_input`` 模拟的是真实鼠标，
        窗口不在前台时点击可能落空或被目标程序忽略 ——
        实测表现是"控件找到了、点了、但目标没有任何反应"，
        极难定位（看起来像控件找错了，其实是焦点问题）。
        """
        try:
            top = self._element.top_level_parent()
            if top is not None and not top.is_active():
                top.set_focus()
        except Exception:                          # noqa: BLE001
            pass
        self._element.click_input()
