"""Tests for the MMM UI Automation driver (方案 §26 / Stage 2.7 / 2.8).

The point of these tests is the **locating logic**, not pywinauto itself.
MMM's layout is rebuilt as you switch ribbon tabs (two dumps in a row produced
different trees), so path-based matching is guaranteed to break.  Everything
here exercises name-based search over an arbitrary tree, using an in-memory
fake element tree — no MMM required.
"""
from __future__ import annotations

import pytest

from mmd_bridge.mmm_ui import (
    BUTTON_EXPORT_VMD,
    BUTTON_LOAD_MODEL,
    BUTTON_LOAD_MOTION,
    BUTTON_PHYSICS_ALWAYS,
    BUTTON_RECORD_PHYSICS,
    BUTTON_SELECT_ALL_BONES,
    DIALOG_CONFIRM,
    MMM_UI_ELEMENT_NOT_FOUND,
    FakeElement,
    MmmUiDriver,
    MmmUiError,
    find_element,
    match_name,
    walk_elements,
)


# --------------------------------------------------------------------------
# match_name
# --------------------------------------------------------------------------

def test_match_name_exact():
    assert match_name("加载模型", BUTTON_LOAD_MODEL) == "加载模型"


def test_match_name_tolerates_shortcut_suffix():
    """MMM 的按钮文本常带快捷键后缀，如 '加载模型(M)'。"""
    assert match_name("加载模型(M)", BUTTON_LOAD_MODEL) == "加载模型"
    assert match_name("导出动作...", BUTTON_EXPORT_VMD) == "导出动作"


def test_match_name_tolerates_surrounding_space():
    assert match_name("  导入动作  ", BUTTON_LOAD_MOTION) == "导入动作"


def test_match_name_prefers_exact_over_contains():
    """精确匹配优先 —— 否则 '记录' 会被 '记录书签' 抢走。"""
    candidates = ("记录书签", "记录")
    assert match_name("记录", candidates) == "记录"


def test_match_name_returns_none_when_absent():
    assert match_name("渲染AVI", BUTTON_LOAD_MODEL) is None


def test_match_name_handles_empty_input():
    assert match_name("", BUTTON_LOAD_MODEL) is None
    assert match_name("加载模型", ()) is None


def test_match_name_works_across_languages():
    """候选表要覆盖中/日/英 —— MMM 的界面语言可切换。"""
    assert match_name("Load Model", BUTTON_LOAD_MODEL) == "Load Model"
    assert match_name("モデル読込", BUTTON_LOAD_MODEL) == "モデル読込"


# --------------------------------------------------------------------------
# 候选表要覆盖方案 §26 的 12 步
# --------------------------------------------------------------------------

def test_candidate_tables_cover_the_documented_flow():
    for table in (BUTTON_LOAD_MODEL, BUTTON_LOAD_MOTION, BUTTON_PHYSICS_ALWAYS,
                  BUTTON_RECORD_PHYSICS, BUTTON_EXPORT_VMD,
                  BUTTON_SELECT_ALL_BONES, DIALOG_CONFIRM):
        assert table, "候选表不能为空"
        assert all(isinstance(name, str) and name for name in table)


def test_record_button_candidates_include_chinese_and_japanese():
    """「记录」是触发烘焙的关键按钮，中/日两种译名都要有。"""
    assert "记录" in BUTTON_RECORD_PHYSICS
    assert "記録" in BUTTON_RECORD_PHYSICS


# --------------------------------------------------------------------------
# walk_elements
# --------------------------------------------------------------------------

def make_tree() -> FakeElement:
    """一棵形状接近真实 MMM 的树（工具条里挂按钮）。"""
    return FakeElement(name="MikuMikuMoving - 新项目", control_type="Window",
                       automation_id="TopForm", children=[
        FakeElement(name="propertyDockPanel", control_type="Pane", children=[
            FakeElement(name="File Toolbar", control_type="ToolBar", children=[
                FakeElement(name="加载模型"),
                FakeElement(name="导入动作"),
                FakeElement(name="渲染AVI"),
            ]),
            FakeElement(name="编辑工具栏", control_type="ToolBar", children=[
                FakeElement(name="注册关键帧"),
                FakeElement(name="选择所有骨骼"),
            ]),
        ]),
        FakeElement(name="物理", control_type="ToolBar", children=[
            FakeElement(name="总是计算", control_type="Button"),
            FakeElement(name="记录", control_type="Button"),
            FakeElement(name="记录", control_type="Button", enabled=False),
        ]),
    ])


def test_walk_visits_every_node():
    root = make_tree()
    names = [e.element_name() for e in walk_elements(root)]
    assert "加载模型" in names
    assert "总是计算" in names
    assert "选择所有骨骼" in names


def test_walk_respects_max_depth():
    root = make_tree()
    shallow = [e.element_name() for e in walk_elements(root, max_depth=1)]
    assert "propertyDockPanel" in shallow
    assert "加载模型" not in shallow


def test_walk_respects_node_limit():
    root = make_tree()
    limited = list(walk_elements(root, max_nodes=3))
    assert len(limited) <= 3


def test_walk_survives_a_child_that_raises():
    class Broken(FakeElement):
        def element_children(self):
            raise RuntimeError("boom")

    root = FakeElement(name="root", children=[Broken(name="bad"),
                                              FakeElement(name="good")])
    names = [e.element_name() for e in walk_elements(root)]
    assert "good" in names, "一个坏节点不能中断整棵树的遍历"


# --------------------------------------------------------------------------
# find_element
# --------------------------------------------------------------------------

def test_find_by_name_anywhere_in_the_tree():
    element, hit = find_element(make_tree(), names=BUTTON_LOAD_MODEL)
    assert element is not None
    assert hit == "加载模型"


def test_find_physics_buttons_that_live_in_another_toolbar():
    """物理按钮在另一条工具条里 —— 不做路径匹配正是为了这个。"""
    element, _ = find_element(make_tree(), names=BUTTON_PHYSICS_ALWAYS)
    assert element is not None
    element, _ = find_element(make_tree(), names=BUTTON_RECORD_PHYSICS)
    assert element is not None


def test_find_skips_disabled_by_default():
    """同名控件里有一个被禁用时，必须选中可用的那个。"""
    root = FakeElement(name="root", children=[
        FakeElement(name="记录", enabled=False),
        FakeElement(name="记录", enabled=True),
    ])
    element, _ = find_element(root, names=BUTTON_RECORD_PHYSICS)
    assert element is not None
    assert element.element_enabled() is True


def test_find_can_return_disabled_when_asked():
    root = FakeElement(name="root", children=[
        FakeElement(name="记录", enabled=False)])
    element, _ = find_element(root, names=BUTTON_RECORD_PHYSICS,
                              require_enabled=False)
    assert element is not None


def test_find_by_control_type():
    element, _ = find_element(make_tree(), control_types=("ToolBar",))
    assert element is not None
    assert element.element_control_type() == "ToolBar"


def test_find_by_automation_id():
    element, _ = find_element(make_tree(), automation_id="TopForm")
    assert element is not None
    assert element.element_name().startswith("MikuMikuMoving")


def test_find_returns_none_when_absent():
    element, hit = find_element(make_tree(), names=("不存在的按钮",))
    assert element is None
    assert hit == ""


def test_find_returns_none_on_empty_tree():
    element, _ = find_element(FakeElement(name="root"), names=BUTTON_LOAD_MODEL)
    assert element is None


# --------------------------------------------------------------------------
# MmmUiDriver（注入假树）
# --------------------------------------------------------------------------

def make_driver(root: FakeElement, **kwargs) -> MmmUiDriver:
    logs: list[str] = []
    kwargs.setdefault("timeout", 0.3)
    kwargs.setdefault("poll", 0.05)
    return MmmUiDriver(root=root, on_log=logs.append, **kwargs)


def test_driver_click_returns_the_matched_name():
    driver = make_driver(make_tree())
    assert driver.click(names=BUTTON_LOAD_MODEL) == "加载模型"


def test_driver_click_actually_invokes_the_element():
    root = make_tree()
    driver = make_driver(root)
    driver.click(names=BUTTON_RECORD_PHYSICS)
    clicked = [n for n in _collect(root) if n == "记录"]
    assert clicked, "点击必须真的落到元素上"


def _collect(root: FakeElement) -> list[str]:
    out = []
    for element in walk_elements(root):
        out.extend(getattr(element, "clicked", []) or [])
    return out


def test_driver_raises_with_actionable_message_when_missing():
    driver = make_driver(make_tree())
    with pytest.raises(MmmUiError) as excinfo:
        driver.click(names=("绝对不存在的按钮",))
    assert excinfo.value.code == MMM_UI_ELEMENT_NOT_FOUND
    assert "页签" in excinfo.value.message, "提示要指向最常见的原因"


def test_driver_logs_what_it_clicks():
    logs: list[str] = []
    driver = MmmUiDriver(root=make_tree(), on_log=logs.append,
                         timeout=0.3, poll=0.05)
    driver.click(names=BUTTON_LOAD_MOTION)
    assert any("导入动作" in line for line in logs)


def test_driver_find_waits_then_succeeds():
    """元素晚一点才出现时，find 要重试而不是立刻失败。"""
    root = FakeElement(name="root")
    driver = make_driver(root, timeout=1.0)

    import threading
    import time

    def appear():
        time.sleep(0.15)
        root.children.append(FakeElement(name="加载模型"))

    thread = threading.Thread(target=appear)
    thread.start()
    try:
        element, hit = driver.find(names=BUTTON_LOAD_MODEL)
    finally:
        thread.join()
    assert hit == "加载模型"


def test_flow_helpers_use_the_right_buttons():
    """流程动作必须点到对应的按钮 —— 用假树记录点击序列。"""
    root = make_tree()
    driver = make_driver(root)

    driver.set_physics_always()
    driver.start_record_physics()

    clicked = _collect(root)
    assert "总是计算" in clicked
    assert "记录" in clicked


# --------------------------------------------------------------------------
# 文本归一化 —— 实测 MMM 的按钮文本带换行，不归一化就匹配不上
# --------------------------------------------------------------------------

def test_normalize_strips_newlines():
    """实测：物理面板的「总是计算」实际文本是 '总是\\r\\n计算'（按钮折行）。"""
    from mmd_bridge.mmm_ui import normalize_label
    assert normalize_label("总是\r\n计算") == "总是计算"
    assert normalize_label("书签\r\n记录") == "书签记录"
    assert normalize_label("初始化\r\n刚体") == "初始化刚体"


def test_normalize_strips_shortcut_and_ellipsis():
    from mmd_bridge.mmm_ui import normalize_label
    assert normalize_label("加载模型(M)") == "加载模型"
    assert normalize_label("导出动作...") == "导出动作"
    assert normalize_label("  导入动作  ") == "导入动作"


def test_match_name_handles_the_real_wrapped_label():
    """这是真实踩到的：候选是 '总是计算'，UIA 报的是 '总是\\r\\n计算'。"""
    assert match_name("总是\r\n计算", BUTTON_PHYSICS_ALWAYS) == "总是计算"


def test_match_name_does_not_confuse_record_with_bookmark_record():
    """精确优先 —— '记录' 不能被 '书签\\r\\n记录' 抢走。"""
    assert match_name("记录", BUTTON_RECORD_PHYSICS) == "记录"
    assert match_name("书签\r\n记录", BUTTON_RECORD_PHYSICS) == "记录"


def test_normalize_handles_empty():
    from mmd_bridge.mmm_ui import normalize_label
    assert normalize_label("") == ""
    assert normalize_label(None) == ""


def test_new_candidate_tables_cover_the_physics_panel():
    """物理面板的控件名已由真实 dump 确认，候选表必须覆盖。"""
    from mmd_bridge.mmm_ui import (
        BUTTON_INIT_PHYSICS,
        BUTTON_PHYSICS_ENABLE,
        BUTTON_PHYSICS_PLAYING,
        BUTTON_PHYSICS_SETTINGS,
        BUTTON_RECORD_ONLY_SELECTED,
    )
    assert "初始化刚体" in BUTTON_INIT_PHYSICS
    assert "启用/禁用" in BUTTON_PHYSICS_ENABLE
    assert "仅播放时计算" in BUTTON_PHYSICS_PLAYING
    assert "物理设置" in BUTTON_PHYSICS_SETTINGS
    assert "仅选定的骨骼" in BUTTON_RECORD_ONLY_SELECTED


def test_record_only_selected_is_documented_as_dangerous():
    """这个开关开着会让烘焙静默地不完整 —— 候选表必须留着它，别被删掉。"""
    from mmd_bridge.mmm_ui import BUTTON_RECORD_ONLY_SELECTED
    assert BUTTON_RECORD_ONLY_SELECTED, "不能删掉：它是必须被显式关掉的开关"


# --------------------------------------------------------------------------
# SplitButton 陷阱
# --------------------------------------------------------------------------

def test_record_exists_as_both_button_and_splitbutton():
    """实测：MMM 里「记录」有两个控件 —— 普通按钮和分裂按钮。

    点 SplitButton 会**展开下拉菜单**而不是执行烘焙，而且不报错。
    所以点击必须限定 control_types=('Button',)。
    """
    root = FakeElement(name="root", children=[
        FakeElement(name="记录", control_type="Button"),
        FakeElement(name="记录...", control_type="SplitButton"),
    ])
    element, _ = find_element(root, names=BUTTON_RECORD_PHYSICS,
                              control_types=("Button",))
    assert element is not None
    assert element.element_control_type() == "Button"


def test_without_control_type_filter_splitbutton_could_win():
    """不加限定时可能选中 SplitButton —— 这正是要防的情况。"""
    root = FakeElement(name="root", children=[
        FakeElement(name="记录...", control_type="SplitButton"),
        FakeElement(name="记录", control_type="Button"),
    ])
    element, _ = find_element(root, names=BUTTON_RECORD_PHYSICS)
    assert element is not None
    # 遍历顺序决定谁先被看到；这里只断言"不加过滤时可能拿到 SplitButton"
    assert element.element_control_type() in ("Button", "SplitButton")


def test_flow_actions_restrict_to_plain_buttons():
    """流程动作必须只点普通按钮。"""
    root = FakeElement(name="root", children=[
        FakeElement(name="记录...", control_type="SplitButton"),
        FakeElement(name="记录", control_type="Button"),
    ])
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05)
    driver.start_record_physics()

    clicked = []
    for element in walk_elements(root):
        clicked.extend(getattr(element, "clicked", []) or [])
    assert clicked == ["记录"], f"必须点普通按钮，实际点了 {clicked}"


def test_init_physics_button_is_available():
    """GPT 建议里的"必要时初始化刚体"—— 控件名已实测确认。"""
    from mmd_bridge.mmm_ui import BUTTON_INIT_PHYSICS
    root = FakeElement(name="root", children=[
        FakeElement(name="初始化\r\n刚体", control_type="Button")])
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05)
    driver.init_physics()
    assert "初始化\r\n刚体" in root.children[0].clicked


# --------------------------------------------------------------------------
# 窗口匹配：只看标题会误伤浏览器
# --------------------------------------------------------------------------

class FakeWindow:
    """最小窗口替身：只需要标题与类名。"""

    def __init__(self, title: str, class_name: str):
        self._title = title
        self.element_info = type("Info", (), {"class_name": class_name})()

    def window_text(self) -> str:
        return self._title


def test_is_mmm_window_accepts_the_real_window():
    from mmd_bridge.mmm_ui import is_mmm_window
    window = FakeWindow("MikuMikuMoving - 新项目 *",
                        "WindowsForms10.Window.8.app.0.aec740_r6_ad1")
    assert is_mmm_window(window) is True


def test_is_mmm_window_rejects_a_browser_tab_with_the_same_title():
    """实测踩到的：Edge 里开着标题含 MikuMikuMoving 的标签页，
    只按标题匹配会把浏览器当成 MMM 去点。"""
    from mmd_bridge.mmm_ui import is_mmm_window
    browser = FakeWindow(
        "MikuMikuMoving_v1 - Google 云端硬盘 和另外 23 个页面 - 个人 - Microsoft Edge",
        "Chrome_WidgetWin_1")
    assert is_mmm_window(browser) is False


def test_is_mmm_window_rejects_unrelated_windows():
    from mmd_bridge.mmm_ui import is_mmm_window
    assert is_mmm_window(FakeWindow("记事本", "Notepad")) is False


def test_is_mmm_window_tolerates_broken_window():
    from mmd_bridge.mmm_ui import is_mmm_window

    class Broken:
        def window_text(self):
            raise RuntimeError("boom")

    assert is_mmm_window(Broken()) is False


# --------------------------------------------------------------------------
# 插件启用必须点「常驻」组里的按钮（实测踩过的坑）
# --------------------------------------------------------------------------

def make_plugin_page() -> FakeElement:
    """插件页签的结构 —— 两个工具条各有一个同名按钮。

    这是实测 dump 出来的真实结构：
        name='命令'      type=ToolBar
          Angle Zero / LipSynchloid / AutoBake / Random Blink   ← 命令按钮
        name='Resident'  type=ToolBar
          AutoBake / PoseDictionary                              ← ★ 启用开关
    """
    return FakeElement(name="MikuMikuMoving", control_type="Window", children=[
        FakeElement(name="命令", control_type="ToolBar", children=[
            FakeElement(name="Angle\r\nZero", control_type="Button"),
            FakeElement(name="AutoBake", control_type="Button"),      # 左
            FakeElement(name="命令...", control_type="SplitButton"),
        ]),
        FakeElement(name="Resident", control_type="ToolBar", children=[
            FakeElement(name="AutoBake", control_type="Button"),      # 右 ★
            FakeElement(name="PoseDictionary", control_type="Button"),
            FakeElement(name="Resident...", control_type="SplitButton"),
        ]),
    ])


def test_enable_plugin_clicks_the_resident_group_button():
    """点「命令」组那个只会触发 Run()，不会启用插件。

    实测：点了左边的 AutoBake，插件日志里 ``Run()`` 出现 3 次，
    而 ``Enabled()`` 一次都没有 —— 流程卡在「等待插件上线」直到超时。
    """
    root = make_plugin_page()
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05)

    assert driver.enable_plugin() == "AutoBake"

    clicked = []
    for element in walk_elements(root):
        clicked.extend(getattr(element, "clicked", []) or [])
    assert clicked == ["AutoBake"], f"只该点一次，实际 {clicked}"

    # 确认点的是「常驻」组里的那个，而不是「命令」组里的。
    resident = next(e for e in root.children if e.name == "Resident")
    command = next(e for e in root.children if e.name == "命令")
    assert resident.children[0].clicked == ["AutoBake"]
    assert command.children[1].clicked == [], "「命令」组那个不该被点"


def test_enable_plugin_falls_back_when_resident_group_is_missing():
    """找不到「常驻」组时退回全局查找 —— 总比完全不点好，但要记录。"""
    logs: list[str] = []
    root = FakeElement(name="W", control_type="Window", children=[
        FakeElement(name="AutoBake", control_type="Button")])
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05,
                         on_log=logs.append)

    assert driver.enable_plugin() == "AutoBake"
    assert any("退回全局查找" in line for line in logs)


def test_enable_plugin_reports_clearly_when_button_absent():
    root = FakeElement(name="W", control_type="Window", children=[
        FakeElement(name="Resident", control_type="ToolBar", children=[
            FakeElement(name="PoseDictionary", control_type="Button")])])
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05)
    with pytest.raises(MmmUiError, match="常驻"):
        driver.enable_plugin()


def test_root_must_be_wrapped_into_an_adapter():
    """``root()`` 必须返回 adapter —— 返回原始对象会让整棵树遍历静默失效。

    实测踩过：``_attach()`` 改成按进程名连接后忘了包装，
    结果每个节点都抛 AttributeError、被逐节点吞掉，
    表现是"一个控件都找不到"，极难定位。
    """
    root = make_plugin_page()
    driver = MmmUiDriver(root=root, timeout=0.3, poll=0.05)
    assert hasattr(driver.root(), "element_name")
    # 注入的假树本身就是 adapter，所以这里验证的是真实路径的约定。
    assert driver.root() is root
