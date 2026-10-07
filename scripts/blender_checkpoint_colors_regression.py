"""Native USD regression for absent, authored and incomplete checkpoint colors."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/python'))
from pxr import Usd, UsdGeom, Sdf, Vt, Gf
from mmd_bridge.blender_usd import _restore_mesh_colors
from mmd_bridge.core import atomic_json

stage = Usd.Stage.CreateInMemory()
empty = UsdGeom.Mesh.Define(stage, '/Absent')
assert _restore_mesh_colors(empty) is None
authored = UsdGeom.Mesh.Define(stage, '/Authored')
primvars = UsdGeom.PrimvarsAPI(authored)
rgb = Vt.Vec3fArray([Gf.Vec3f(.1, .2, .3), Gf.Vec3f(.4, .5, .6)])
alpha = Vt.FloatArray([.25, .75])
primvars.CreatePrimvar('displayColor', Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.faceVarying).Set(rgb)
primvars.CreatePrimvar('displayOpacity', Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.faceVarying).Set(alpha)
restored = _restore_mesh_colors(authored)
assert restored['rgb'] == rgb and restored['alpha'] == alpha
broken = UsdGeom.Mesh.Define(stage, '/Incomplete')
UsdGeom.PrimvarsAPI(broken).CreatePrimvar('displayColor', Sdf.ValueTypeNames.Color3fArray).Set(rgb)
try:
    _restore_mesh_colors(broken)
except ValueError:
    pass
else:
    raise AssertionError('incomplete vertex colors must fail closed')
atomic_json(ROOT / 'work/material_restoration/checkpoint_colors_regression.json',
    {'complete': True, 'absent_colors_omitted': True, 'authored_colors_exact': True,
     'incomplete_colors_rejected': True})
print('CHECKPOINT_COLORS_PASS', flush=True)
