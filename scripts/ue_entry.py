import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mmd_bridge.ue_host import import_job
import_job(os.environ["MMD_BRIDGE_JOB"])
