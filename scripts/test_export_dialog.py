import sys,time,json,ctypes
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
import mmd_bridge.win_dialog as dialog
dialog.idle_seconds=lambda:100
results={}
start=time.monotonic()
result=dialog.export_choice(seconds=2)
results['timeout']={'choice':result,'elapsed':time.monotonic()-start}
for name,button in [('default_button',100),('cancel_button',101)]:
    def click(hwnd):
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd),0x400+102,button,0)
    results[name]=dialog.export_choice(_on_created=click)
start=time.monotonic()
dialog.idle_seconds=lambda:max(0,time.monotonic()-start-1.2)
result=dialog.export_choice(seconds=2)
results['input_reset']={'choice':result,'elapsed':time.monotonic()-start}
(root/'work/dialog_test.json').write_text(json.dumps(results),encoding='utf-8')
