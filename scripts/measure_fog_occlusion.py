"""Compare matched-frame fog-only profiles. Requires Pillow + NumPy runtime."""
import argparse,json,sys
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.fog_occlusion_metrics import shadow_profile,band_metrics

parser=argparse.ArgumentParser();parser.add_argument('--out',required=True)
parser.add_argument('--kind',required=True);parser.add_argument('--variant',required=True)
parser.add_argument('--reference',default='path_tracing')
parser.add_argument('--width',default='5');args=parser.parse_args();out=Path(args.out)

def result(variant):
    file=out/f'{args.kind}_{variant}_w{args.width}_result.json'
    if not file.exists() and args.width=='5':file=out/f'{args.kind}_{variant}_result.json'
    r=read_json(file)
    if not r['complete'] or r.get('fog_test_fixture_version',0)<3:
        raise ValueError('必须使用有效的正向机位测试，不能把其他图当作验收图')
    path=next(x['path'] for x in r['frames']['files'] if x['frame']==16)
    pixels=np.asarray(Image.open(path).convert('RGB'),dtype=np.float64)/255
    # Output-referred contrast: identical tone curve per matched case.
    pixels=np.where(pixels<=.04045,pixels/12.92,((pixels+.055)/1.055)**2.4)
    lum=pixels@np.array([.2126,.7152,.0722])
    # Above every primary cube silhouette (top is >=152px); no floor pixels.
    # The camera sits on the chosen geometric shadow cone for all widths.
    profile=lum[40:120,540:740].mean(axis=0).tolist()
    return r,profile

reference,rp=result(args.reference);reference_open,ro=result(args.reference+'_unoccluded')
candidate,cp=result(args.variant);candidate_open,co=result(args.variant+'_unoccluded')
records=[reference,reference_open,candidate,candidate_open]
if len({r['level'] for r in records})!=1:raise ValueError('对照关卡不一致')
if any(r.get('fog_test_width_cm',5)!= (int(args.width) if args.width!='masked' else 'masked') for r in records):
    raise ValueError('对照机位不一致')
metrics=band_metrics(shadow_profile(rp,ro),shadow_profile(cp,co))
metrics.update(fog=args.kind.split('_')[0],light='spot' if args.kind.endswith('_spot') else 'point',
    width_cm=int(args.width) if args.width!='masked' else 'masked',variant=args.variant,
    roi=[540,40,740,120],frames=[r['run'] for r in records],
    source='matched_output_png_linear_luminance',output_frame=16)
atomic_json(out/f'{args.kind}_{args.variant}_w{args.width}_metrics.json',metrics)
print(json.dumps(metrics,ensure_ascii=False,indent=2))
