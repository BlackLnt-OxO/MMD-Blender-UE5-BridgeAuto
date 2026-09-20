import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "vendor/blender_mmd_tools-4.5.13"))
sys.path.insert(0, str(root / "vendor/python"))
from mmd_bridge.blender_host import main

main(sys.argv[sys.argv.index("--") + 1:])
