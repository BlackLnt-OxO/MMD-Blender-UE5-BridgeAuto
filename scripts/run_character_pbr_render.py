"""Render a single approved PBR fixture with production MRQ and receipts."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json, atomic_json
from mmd_bridge.ue_render_policy import standalone_command

parser=argparse.ArgumentParser()
parser.add_argument('--fixture',required=True,type=Path)
parser.add_argument('--project',required=True,type=Path)
parser.add_argument('--unreal',default='C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor-Cmd.exe')
parser.add_argument('--variant',choices=('source','matcap_off'),default='source')
parser.add_argument('--width',type=int,default=1920)
parser.add_argument('--height',type=int,default=1080)
parser.add_argument('--start',type=int,default=0)
parser.add_argument('--end',type=int,default=61)
parser.add_argument('--portrait',action='store_true')
parser.add_argument('--material-overrides',type=Path)
parser.add_argument('--preset')
parser.add_argument('--view')
parser.add_argument('--probe-bones',type=Path)
args=parser.parse_args()
args.fixture=args.fixture.resolve()
args.project=args.project.resolve()
receipt=read_json(args.fixture/'ue_receipt.json')
assert receipt.get('status')=='ue_assets_created',receipt.get('error',receipt)
label=args.material_overrides.stem if args.material_overrides else args.variant
if args.view:
    assert args.view in receipt.get('review_views',{}), 'Unknown fixture view: '+args.view
    label += '_'+args.view
run=args.fixture/('render_'+label+'_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
run.mkdir(parents=True,exist_ok=False)
request=dict(receipt['render_assets'],run=str(run),project=str(args.project),output=str(run/'images'),
             pipeline='lumen',frame=None,id='pbr_'+run.name,ranges=[[args.start,args.end]],
             expected_resolution=[args.width,args.height],capture_gpu_csv=True,
             render_vram_guard_enabled=True,memory_profile='balanced',gpu_sample_interval=.25,
             frame_ledger=True,frame_contract={'start':args.start,'end':args.end,'step':1,
                 'resolution':[args.width,args.height],'passes':['FinalImage']})
if args.view:
    request['sequence']=receipt['review_views'][args.view]
    request['level']=receipt.get('review_levels',{}).get(args.view,request['level'])
if args.portrait:
    request['preset']=receipt['portrait_preset']
if args.preset:
    request['preset']=args.preset
if args.variant=='matcap_off':
    request['material_overrides']=receipt['matcap_off_overrides']
if args.material_overrides:
    request['material_overrides']=read_json(args.material_overrides.resolve())
manifest=read_json(args.fixture/'manifest.json')
if manifest.get('fixture_kind','character')=='character':
    request['character_pbr_probe']={
        'mesh':receipt['mesh'],
        'head_bone':next(b['exported'] for b in manifest['bones'] if b['original'] in ('頭','头','head','Head')),
        'morphs':[m['exported'] for m in manifest.get('morph_probe',[])],
        'eye_bones':{b['original']:b['exported'] for b in manifest['bones'] if b['original'] in ('左目','右目')}}
    slot_mapping={row['pmx_index']:row['ue_slot'] for row in receipt.get('checks',{}).get('material_slots',[])}
    if args.probe_bones:
        request['character_pbr_probe']['bone_probes']=read_json(args.probe_bones.resolve())
    request['character_pbr_probe']['hidden_slots']=[slot_mapping.get(r['pmx_index'],r['pmx_index'])
        for r in manifest['materials'] if r['character_material']['pbr']['params'].get('Opacity',1)==0]
atomic_json(run/'request.json',request)
cfg={'project':str(args.project),'unreal':args.unreal}
command=standalone_command(cfg,{'assets':receipt['render_assets']},request['preset'],request=request,allow_custom_preset=True)
command += ['-DisablePlugins=OSC,OpenXR,OpenXREditor','-abslog='+str(run/'ue_process.log')]
env=dict(os.environ,MMDBRIDGE_STANDALONE_REQUEST=str(run/'request.json'),MMDBRIDGE_DISABLE_AUTOSTART='1')
with (run/'launcher.log').open('w',encoding='utf-8') as log:
    code=subprocess.call(command,env=env,stdout=log,stderr=subprocess.STDOUT)
runtime=read_json(run/'runtime.json') if (run/'runtime.json').is_file() else {}
images=sorted((run/'images').rglob('*.png'))
dimensions=[]
for path in images:
    with path.open('rb') as stream:
        header=stream.read(24)
    dimensions.append(list(struct.unpack('>II',header[16:24])) if header[:8]==b'\x89PNG\r\n\x1a\n' else [])
text=(run/'ue_process.log').read_text(encoding='utf-8',errors='replace') if (run/'ue_process.log').is_file() else ''
errors=[line for line in text.splitlines() if 'Failed to compile Material' in line or 'Default Material will be used' in line]
result={'exit_code':code,'runtime':runtime,'image_count':len(images),'images':[str(p) for p in images],
        'shader_errors':errors,'expected_resolution':[args.width,args.height],
        'image_dimensions':dimensions,
        'material_override_count':len(request.get('material_overrides',{})),
        'matcap_comparison':('no_active_matcap' if args.variant=='matcap_off' and not receipt['matcap_off_overrides']
                             else 'disabled_for_comparison' if args.variant=='matcap_off' else None),
        'complete':code==0 and runtime.get('success') is True and not runtime.get('errors')
                   and len(images)==args.end-args.start and not errors
                   and all(size==[args.width,args.height] for size in dimensions)
                   and (not request.get('material_overrides') or runtime.get('material_variant_components',0)>0)}
csv_path=Path(runtime.get('gpu_csv_path',''))
if csv_path.is_file():
    shutil.copy2(csv_path,run/'gpu.csv')
    from mmd_bridge.material_calibration import gpu_csv
    try:
        result['gpu_measurement']=gpu_csv(run/'gpu.csv',scene_only=True)
    except ValueError as exc:
        result['gpu_measurement_error']=str(exc)
atomic_json(run/'acceptance.json',result)
print(str(run),flush=True)
assert result['complete'],'Render verification failed; see '+str(run/'acceptance.json')
