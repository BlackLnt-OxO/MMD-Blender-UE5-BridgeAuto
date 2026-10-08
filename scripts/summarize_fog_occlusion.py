"""Collect native receipts and protection hashes; never promote a renderer."""
import argparse,ast,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mmd_bridge.core import read_json,atomic_json,file_hash

parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);args=parser.parse_args()
out=Path(args.out);root=Path(__file__).resolve().parents[1];baseline=read_json(out/'preflight.json')
changed=[name for name,digest in baseline['files'].items() if not Path(name).is_file() or file_hash(name)!=digest]
protected_modules=('blender_materials.py','ue_materials.py','material_policy.py','material_plan.py',
                   'material_workflow.py','ue_foliage.py','ue_render_policy.py')
code_changed=[name for name in protected_modules if file_hash(root/'mmd_bridge'/name)!=baseline['code']['mmd_bridge/'+name]]
old=ast.parse(subprocess.check_output(['git','show','e4945b5:mmd_bridge/ue_appearance.py'],cwd=root).decode('utf-8-sig'))
new=ast.parse((root/'mmd_bridge/ue_appearance.py').read_text(encoding='utf-8-sig'))
def constants(tree):
    return {n.targets[0].id:ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
            and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('VOLUME_RENDER_CVARS','LUMEN_RENDER_CVARS')}
rows=[]
for kind,variant in [('grid','baseline'),('grid','vsm_cache_medium'),('grid','vsm_finer'),
                     ('height','baseline'),('height','rt_inject'),('grid','froxel_box_rt'),('grid','froxel_svt_rt')]:
    path=out/f'{kind}_{variant}_result.json'
    if not path.exists():continue
    r=read_json(path);metric=out/f'{kind}_{variant}_w5_metrics.json'
    rows.append(dict(kind=kind,variant=variant,complete=r['complete'],run=r['run'],
                     gpu=r.get('gpu_measurement'),metrics=read_json(metric) if metric.exists() else None))
record=dict(version=1,status='experimental_validation_only',production_certificate='not_issued',
    default='legacy',protected_files=len(baseline['files']),protected_files_changed=changed,
    protected_material_code_changed=code_changed,legacy_console_values_unchanged=constants(old)==constants(new),
    main_material_workflow_unchanged=not code_changed,harbor_reimported=False,harbor_detail_validation='not_run',
    results=rows,metrics=[read_json(p) for p in sorted(out.glob('*_metrics.json'))],
    limitations=['No candidate has passed the entire required fixture matrix and budgets',
                  'Masked measurements evaluate the central band, not every aperture',
                  'Uniform boxed prototype is not proof of arbitrary SVT boundaries',
                  'Source coefficients and physical light parameters are not tuned to improve scores'])
atomic_json(out/'summary.json',record)
assert not changed and not code_changed and record['legacy_console_values_unchanged'],record
print('PROTECTED',record['protected_files'],'UNCHANGED; legacy constants unchanged; no production certificate')
