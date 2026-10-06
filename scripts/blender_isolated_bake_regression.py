"""Check original and isolated bake contexts produce the same native fields."""
import sys
from pathlib import Path
import bpy, numpy as np, OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/isolated_bake_regression';OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.mesh.primitive_plane_add(size=2)
obj=bpy.context.object;mat=bpy.data.materials.new('Anonymous field');mat.use_nodes=True
obj.data.materials.append(mat);tree=mat.node_tree
noise=tree.nodes.new('ShaderNodeTexNoise');noise.inputs['Scale'].default_value=3
random=tree.nodes.new('ShaderNodeObjectInfo');color=tree.nodes.new('ShaderNodeRGB')
color.outputs[0].default_value=(.2,.6,.1,1)
source_scene=bpy.context.scene;original_scene_count=len(bpy.data.scenes);checks=[]
for name,output in (('noise',noise.outputs['Fac']),('random',random.outputs['Random']),('color',color.outputs[0])):
    pixels=[]
    for isolated in (False,True):
        converter=Materials(OUT/(name+str(isolated)),'anonymous-isolated-bake',resolution=32)
        converter.surface_policy={'category':'small'};converter.isolated_baking=isolated
        texture=converter.bake(obj,mat,None,'field_'+name,name+str(isolated),from_output=output)
        inp=oiio.ImageInput.open(texture['image']['path']);a=inp.read_image(format=oiio.FLOAT);inp.close();pixels.append(a)
        assert bpy.context.scene==source_scene and len(bpy.data.scenes)==original_scene_count
    error=float(np.max(np.abs(pixels[0]-pixels[1])))
    assert error<1e-5,(name,error)
    checks.append({'field':name,'max_error':error})
# Production export starts from unlinked, newly created mesh helpers, whereas
# a mesh created by an operator already has an evaluated context.
mesh=obj.data.copy();mesh.uv_layers.remove(mesh.uv_layers['MMD_BakeUV'])
helper=bpy.data.objects.new('Unlinked export helper',mesh)
converter=Materials(OUT/'unlinked','anonymous-isolated-bake',resolution=32)
converter.surface_policy={'category':'small'};converter.isolated_baking=True
texture=converter.bake(helper,mat,None,'field_unlinked','unlinked',from_output=noise.outputs['Fac'])
assert helper.data.uv_layers.get('MMD_BakeUV') and Path(texture['image']['path']).is_file()
assert bpy.context.scene==source_scene and len(bpy.data.scenes)==original_scene_count
checks.append({'field':'unlinked_export_helper','uv_created':True})
atomic_json(OUT/'result.json',{'complete':True,'version':bpy.app.version_string,'checks':checks})
print('ISOLATED_BAKE_PARITY_PASS',checks,flush=True)
