"""Native, Blender-owned two-button modal dialog with global idle timeout."""
import ctypes
import os
import time
from ctypes import wintypes as w
from .core import idle_seconds


def export_choice(seconds=60, _on_created=None):
    if os.name != "nt":
        return "manual"
    user = ctypes.windll.user32
    user.GetActiveWindow.restype = w.HWND
    user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    class Button(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("id", ctypes.c_int), ("text", w.LPCWSTR)]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_long, w.HWND, w.UINT, w.WPARAM, w.LPARAM, ctypes.c_ssize_t)
    class Config(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("size", w.UINT), ("parent", w.HWND), ("instance", w.HINSTANCE),
            ("flags", w.UINT), ("common", w.UINT), ("title", w.LPCWSTR), ("icon", ctypes.c_void_p),
            ("instruction", w.LPCWSTR), ("content", w.LPCWSTR), ("count", w.UINT),
            ("buttons", ctypes.POINTER(Button)), ("default", ctypes.c_int), ("radio_count", w.UINT),
            ("radio_buttons", ctypes.POINTER(Button)), ("radio_default", ctypes.c_int),
            ("verification", w.LPCWSTR), ("expanded", w.LPCWSTR), ("expanded_control", w.LPCWSTR),
            ("collapsed_control", w.LPCWSTR), ("footer_icon", ctypes.c_void_p), ("footer", w.LPCWSTR),
            ("callback", callback_type), ("callback_data", ctypes.c_ssize_t), ("width", w.UINT)]
    start = time.monotonic()
    last = [-1]
    @callback_type
    def callback(hwnd, notification, wp, lp, data):
        if notification == 0 and _on_created:
            _on_created(hwnd)
        if notification == 4:  # TDN_TIMER
            remaining = max(0, seconds - int(min(idle_seconds(), time.monotonic()-start)))
            if remaining != last[0]:
                last[0] = remaining
                text = ctypes.create_unicode_buffer(f"连续 {seconds} 秒无鼠标或键盘操作将自动导出。\n当前剩余 {remaining} 秒。\n取消后本批次交由你手动导出，不再倒计时。")
                user.SendMessageW(hwnd, 0x400+108, 0, ctypes.cast(text, ctypes.c_void_p).value)  # TDE_CONTENT
            if remaining == 0:
                user.SendMessageW(hwnd, 0x400+102, 100, 0)
        return 0
    buttons = (Button * 2)(Button(100, "使用默认导出设置"), Button(101, "取消"))
    cfg = Config()
    cfg.size = ctypes.sizeof(cfg)
    cfg.parent = user.GetActiveWindow()
    cfg.flags = 0x0800  # TDF_CALLBACK_TIMER; no escape/close bypass
    cfg.title = "MMD Bridge · 导出选择"
    cfg.instruction = "使用推荐设置，或接管 Blender 导出"
    cfg.content = "连续 60 秒无输入将使用默认设置导出。"
    cfg.count, cfg.buttons, cfg.default = 2, buttons, 100
    cfg.callback = callback
    button = ctypes.c_int()
    try:
        hr = ctypes.windll.comctl32.TaskDialogIndirect(ctypes.byref(cfg), ctypes.byref(button), None, None)
        return "default" if hr == 0 and button.value == 100 else "manual"
    except (OSError, AttributeError):
        return "manual"
