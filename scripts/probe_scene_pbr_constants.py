"""Read-only effective shader channel probe; no scene rendering or saving."""
import argparse
import json
from pathlib import Path
import sys
import time

import bpy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_pbr_bake import native_pbr_channels
from mmd_bridge.material_graph import constant_socket
from mmd_bridge.core import atomic_json,file_hash

parser=argparse.ArgumentParser()
parser.add_argument('--source',required=True)
parser.add_argument('--material',action='append',required=True)
parser.add_argument('--out',required=True)
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
before=file_hash(args.source)
bpy.ops.wm.open_mainfile(filepath=args.source)
rows=[]
for name in args.material:
    mat=bpy.data.materials.get(name)
    if mat is None:raise ValueError('Material not found: '+name)
    output=mat.node_tree.get_output_node('CYCLES' if bpy.context.scene.render.engine=='CYCLES' else 'EEVEE')
    started=time.perf_counter()
    with native_pbr_channels(mat,output.inputs['Surface']) as (channels,limits,normal_required):
        values={}
        for channel,socket in channels.items():
            probe=mat.node_tree.nodes.new('ShaderNodeMath' if channel in ('roughness','metallic','opacity') else 'ShaderNodeEmission')
            mat.node_tree.links.new(socket,probe.inputs[0])
            try:values[channel]=constant_socket(probe.inputs[0])
            finally:mat.node_tree.nodes.remove(probe)
        rows.append({'material':name,'constants':values,'normal_required':normal_required,
                     'omitted_effects':limits,'seconds':time.perf_counter()-started})
after=file_hash(args.source)
assert before==after
atomic_json(args.out,{'complete':True,'source':args.source,'source_sha256':after,'materials':rows})
print(json.dumps(rows,ensure_ascii=True))
