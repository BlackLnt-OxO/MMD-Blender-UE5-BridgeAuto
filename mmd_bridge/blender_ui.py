import traceback
import bpy
from . import blender_host as host
from .core import read_json, Cancelled
from .win_dialog import export_choice

original_save = None
opening = False


def active_browser():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "FILE_BROWSER":
                return window, area
    return None


def choose_mode():
    active = host.ACTIVE
    if not active or active.finished:
        return None
    if not active_browser():
        return 0.2
    active.job.status("awaiting_export", "等待导出选择；连续 60 秒空闲使用默认设置")
    choice = export_choice()
    if choice == "default":
        browser = active_browser()
        if browser:
            window, area = browser
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.file.cancel()
        def run():
            try:
                active.export_all()
            except Cancelled as exc:
                active.job.status('stopped',str(exc))
            except Exception:
                active.job.status("failed", traceback.format_exc())
            return None
        bpy.app.timers.register(run, first_interval=0.3)
    else:
        active.manual = True
        active.job.status("manual_export", "手动导出：倒计时已关闭", kind=active.kinds[active.index])
    return None


def open_export():
    global opening
    active = host.ACTIVE
    if not active or active.finished or active.index >= len(active.kinds):
        return None
    kind = active.kinds[active.index]
    active.set_kind(kind)
    opening = True
    bpy.ops.export_scene.fbx("INVOKE_DEFAULT", filepath=str(active.path_for(kind)), **host.export_options(kind))
    if not active.manual:
        bpy.app.timers.register(choose_mode, first_interval=0.3)
    return None


def poll():
    global opening
    active = host.ACTIVE
    if not active or active.finished:
        return None
    if (active.job.folder/'stop').exists():
        active.job.status('stopped','任务已停止；Blender 窗口保留')
        return None
    if opening and active_browser():
        opening = False
    if active.manual and not opening and not active_browser():
        active.job.status("export_paused", "导出窗口已关闭；可在桌面工具重开或关联已导出 FBX", kind=active.kinds[active.index])
        opening = True
    command = active.job.folder / "command.json"
    if command.is_file():
        cmd = read_json(command)
        if cmd["nonce"] != active.last_command:
            active.last_command = cmd["nonce"]
            if cmd["name"] == "reopen_export" and not active_browser():
                active.manual = True
                bpy.app.timers.register(open_export, first_interval=0.1)
            elif cmd["name"] == "attach_export":
                try:
                    kind = active.kinds[active.index]
                    active.record(kind, cmd["path"], {**host.export_options(kind), "external": True})
                    advance()
                except Exception:
                    active.job.status("export_paused", traceback.format_exc())
    return 0.5


def advance():
    global opening
    active = host.ACTIVE
    active.index += 1
    opening = True
    if active.index == len(active.kinds):
        active.complete()
    else:
        bpy.app.timers.register(open_export, first_interval=0.6)


def register():
    global original_save
    from io_scene_fbx import export_fbx_bin
    if original_save is None:
        original_save = export_fbx_bin.save
        def tracked_save(operator, context, **kwargs):
            active = host.ACTIVE
            result = original_save(operator, context, **kwargs)
            if active and active.manual and not active.finished and "FINISHED" in result:
                kind = active.kinds[active.index]
                # User may change settings, but invalid outputs must not advance into UE.
                expected = host.export_options(kind)
                for key in ("axis_forward", "axis_up", "global_scale", "apply_unit_scale", "apply_scale_options", "add_leaf_bones", "use_mesh_modifiers", "bake_anim"):
                    if key in kwargs and kwargs[key] != expected[key]:
                        active.job.status("export_paused", f"导出完成，但 {key} 与当前 UE 配置不一致；请重开并修正或新建配置")
                        return result
                active.record(kind, kwargs["filepath"], kwargs)
                advance()
            return result
        export_fbx_bin.save = tracked_save
    bpy.app.timers.register(poll, first_interval=1)
