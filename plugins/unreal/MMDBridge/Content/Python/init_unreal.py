"""Only this project-local, explicitly installed bridge is started."""
from pathlib import Path
import os
import json
import unreal

if os.environ.get('MMDBRIDGE_STANDALONE_REQUEST'):
    # Register the runtime Python executor before the command-line MRQ starts.
    # Render processes do not consume the editor import mailbox.
    import mmd_bridge.ue_standalone_render

config = Path(__file__).parent / "bridge_config.json"
if (config.exists() and not os.environ.get('MMDBRIDGE_STANDALONE_REQUEST')
        and not os.environ.get('MMDBRIDGE_DISABLE_AUTOSTART')):
    from mmd_bridge.ue_host import start_bridge
    data = json.loads(config.read_text(encoding="utf-8"))
    start_bridge(data["mailbox"], data["project"])
