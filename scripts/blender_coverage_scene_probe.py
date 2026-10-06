"""Probe active transparent shader masks on a UV card without changing the blend."""
import argparse, sys
from pathlib import Path
import bpy
import numpy as np
import OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_appearance import expose_material_output
from mmd_bridge.core import atomic_json

args=argparse.ArgumentParser()
args.add_argument('--blend',required=True)
args.add_argument('--out',required=True)
cfg=args.parse_args(sys.argv[sys.argv.index('--')+1:])
out=Path(cfg.out); out.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=cfg.blend,load_ui=False,use_scripts=False)
original=list(bpy.data.materials)
# This is a shader probe. Keep source node trees and packed images, but exclude
# the full scatter dependency graph from the tiny diagnostic bake.
for source_object in list(bpy.data.objects):
    bpy.data.objects.remove(source_object,do_unlink=True)
bpy.ops.mesh.primitive_plane_add(size=2)
proxy=bpy.context.object
converter=Materials(out,cfg.blend,resolution=64)
converter.restore_scene_appearance=True
records=[]; errors=[]
def contains_transparent(tree,seen):
    if not tree or tree.as_pointer() in seen: return False
    seen.add(tree.as_pointer())
    return any(n.type=='BSDF_TRANSPARENT' or n.type=='GROUP' and
        contains_transparent(n.node_tree,seen) for n in tree.nodes)
for mat in original:
    if not mat.use_nodes or not contains_transparent(mat.node_tree,set()): continue
    name=mat.name
    expose_material_output(mat,'CYCLES')
    proxy.data.materials.clear(); proxy.data.materials.append(mat)
    proxy.pop('_mmd_uv_pinned',None)
    try:
        record=converter.analyze(mat,obj=proxy,source_name=name)
        if record.get('coverage_source')!='shader_mix':
            errors.append({'name':name,'error':record.get('coverage_error') or record.get('material_error')})
            continue
        def textures(expr):
            if isinstance(expr,dict):
                if expr.get('op')=='texture': yield expr
                for value in expr.values(): yield from textures(value)
            elif isinstance(expr,list):
                for value in expr: yield from textures(value)
        mask_textures=list(textures(record['channels']['opacity']))
        ranges=[]
        for expression in mask_textures:
            image=expression['image']
            inp=oiio.ImageInput.open(image['path']); pixels=inp.read_image(format=oiio.FLOAT); inp.close()
            channel=pixels[:,:,3] if expression.get('output')=='Alpha' else pixels[:,:,0]
            ranges.append({'image':image['path'],'channel':expression.get('output'),
                'min':float(channel.min()),'max':float(channel.max())})
        record['probe_mask_ranges']=ranges
        # Seasonal source graphs can intentionally cap leaf coverage below 1.
        assert mask_textures and any(r['min']<.1 and r['max']>.5 for r in ranges),ranges
        records.append(record)
        print('SOURCE_COVERAGE_OK',name,flush=True)
    except Exception as exc:
        errors.append({'name':name,'error':str(exc)})
atomic_json(out/'source_result.json',{'complete':bool(records) and not errors,'records':records,'errors':errors,
    'method':'Original active shaders on a small UV proxy, not full scene geometry or UE render'})
print('SOURCE_COVERAGE_RESULT',len(records),'errors',errors,flush=True)
