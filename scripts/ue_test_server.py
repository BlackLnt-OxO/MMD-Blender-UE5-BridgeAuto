"""Development-only persistent editor test runner; never shipped in bridge plugin."""
import json
import sys
import time
import traceback
from pathlib import Path
import importlib
import unreal as u
import faulthandler
root = Path(__file__).resolve().parents[1]
fault_log=(root/"work/ue_python_fault.log").open("w")
faulthandler.enable(file=fault_log,all_threads=True)
sys.path.insert(0,str(root))
# Commands predate this editor session are never replayed, especially quit.
command_path = root/"work/ue_command.json"
try:
    last = json.loads(command_path.read_text(encoding="utf-8"))["id"]
except (OSError, ValueError, KeyError):
    last = None
last_time = 0
busy = False
def tick(delta):
    global last,last_time,busy
    if busy:
        return
    if time.monotonic()-last_time<1:
        return
    last_time=time.monotonic()
    path = root/"work/ue_command.json"
    if not path.exists():
        return
    cmd=json.loads(path.read_text(encoding="utf-8"))
    if cmd["id"]==last:
        return
    last=cmd["id"]
    busy=True
    try:
        if cmd["action"]=="import":
            import mmd_bridge.ue_materials as mats
            import mmd_bridge.ue_host as host
            importlib.reload(mats)
            importlib.reload(host)
            host.import_job(cmd["job"])
        elif cmd["action"]=="script":
            exec(compile(Path(cmd["script"]).read_text(encoding="utf-8"),cmd["script"],"exec"),{"__name__":"__main__"})
        elif cmd["action"]=="quit":
            u.SystemLibrary.quit_editor()
        (root/"work/ue_command_result.json").write_text(json.dumps({"id":last,"ok":True}),encoding="utf-8")
    except Exception:
        error=traceback.format_exc()
        u.log_error(error)
        (root/"work/ue_command_result.json").write_text(json.dumps({"id":last,"ok":False,"error":error}),encoding="utf-8")
    finally:
        busy=False
handle=u.register_slate_post_tick_callback(tick)
(root/"work/ue_ready.txt").write_text(u.SystemLibrary.get_engine_version(),encoding="utf-8")
