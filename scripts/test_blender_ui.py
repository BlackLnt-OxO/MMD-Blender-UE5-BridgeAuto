"""Exercise the real file browser, modal and exporter in an isolated Blender session."""
import sys,time,traceback,os,ctypes
from pathlib import Path
import bpy
root=Path(__file__).resolve().parents[1]
for path in (root,root/'vendor/blender_mmd_tools-4.5.13',root/'vendor/python'):
    sys.path.insert(0,str(path))
from mmd_bridge.core import Job,Settings,atomic_json
from mmd_bridge import blender_host as host,blender_ui as ui,win_dialog
source=Job((root/'work/current_job.txt').read_text().strip())
cfg=Settings(**source.data['settings'])
cfg.end=2
cfg.unattended=False
cfg.camera=''
cfg.output_override=str(root/'work/ui-export')
job=Job.create(root/'work/jobs',cfg)
(root/'work/ui_job.txt').write_text(str(job.folder))
try:
    host.ACTIVE=host.Character(job)
    host.ACTIVE.prepare()
    win_dialog.idle_seconds=lambda:100
    manual=os.environ.get('MMDBRIDGE_TEST_MANUAL')=='1'
    def cancel(hwnd):
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd),0x400+102,101,0)
    ui.export_choice=lambda:win_dialog.export_choice(seconds=2,_on_created=cancel if manual else None)
    ui.register()
    bpy.app.timers.register(ui.open_export,first_interval=.5)
    started=time.monotonic()
    exported=set()
    def complete():
        if manual and host.ACTIVE.manual and ui.active_browser() and host.ACTIVE.index not in exported:
            exported.add(host.ACTIVE.index)
            window,area=ui.active_browser()
            with bpy.context.temp_override(window=window,area=area):
                bpy.ops.file.execute()
        if host.ACTIVE.finished or time.monotonic()-started>40:
            atomic_json(root/('work/ui-manual-result.json' if manual else 'work/ui-export-result.json'),{'finished':host.ACTIVE.finished,'files':host.ACTIVE.manifest['files']})
            bpy.context.preferences.view.use_save_prompt=False
            bpy.ops.wm.quit_blender()
            return None
        return .5
    bpy.app.timers.register(complete,first_interval=1)
except Exception:
    atomic_json(root/'work/ui-export-result.json',{'error':traceback.format_exc()})
    bpy.context.preferences.view.use_save_prompt=False
    bpy.ops.wm.quit_blender()
