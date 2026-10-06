"""Render the source active camera at the converter's static frame and native aspect."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import bpy
from mmd_bridge.core import Job,read_json,atomic_json
from mmd_bridge.scene_reference import render_blender

render_blender(Path(sys.argv[sys.argv.index('--')+1]).resolve())
