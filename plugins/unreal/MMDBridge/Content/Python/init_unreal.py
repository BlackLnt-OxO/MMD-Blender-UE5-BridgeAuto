"""Only this project-local, explicitly installed bridge is started."""
from pathlib import Path
import json
import unreal

config = Path(__file__).parent / "bridge_config.json"
if config.exists():
    from mmd_bridge.ue_host import start_bridge
    data = json.loads(config.read_text(encoding="utf-8"))
    start_bridge(data["mailbox"], data["project"])
