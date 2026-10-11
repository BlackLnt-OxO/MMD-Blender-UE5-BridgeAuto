import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.blender_surface_layer import run
run(sys.argv[-1])
