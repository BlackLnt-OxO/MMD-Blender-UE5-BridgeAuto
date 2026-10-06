"""Object/material restoration receipts for GUI and audit exports."""
import copy
from .material_contract import finalize_restoration


def restoration_rows(manifest, report):
    scene=manifest.get('scene',{})
    native={item['id']:item for item in report.get('material_source_records',[])}
    affected={}
    prototypes={p['id']:p for p in scene.get('prototypes',[])}
    for obj in scene.get('objects',[])+scene.get('instances',[]):
        prototype=prototypes.get(obj.get('prototype_id'),{})
        materials=obj.get('materials',prototype.get('materials',prototype.get('material_slots',[])))
        for identity in materials:
            affected.setdefault(identity,set()).add(obj['name'])
    rows=[]
    for source in manifest.get('materials',[])+scene.get('materials',[]):
        record=copy.deepcopy(source)
        finalize_restoration(record)
        record.update(copy.deepcopy(native.get(record['id'],{})))
        finding=report.get('checks',{}).get('material_visual_findings',{}).get(record['id'])
        if finding:
            record['visual_status']='needs_review'
        # Legacy UE reports can claim zero failures while importing placeholders.
        if source.get('material_error') or source.get('basic_pbr_bake',{}).get('status')=='failed':
            finalize_restoration(record)
        objects=set(record.get('source_objects',[]))|affected.get(record['id'],set())
        reasons=record.get('strategy_reasons',[])+[item['reason'] for item in record.get('issues',[])]+record.get('warnings',[])
        if finding:reasons+=finding.get('reasons',[])
        rows.append({'id':record['id'],'material':record['name'],'objects':sorted(objects),
            'strategy':record.get('strategy','unknown'),'conversion_status':record['conversion_status'],
            'automatic_route':record.get('automatic_route'),'material_usage_count':record.get('material_usage_count'),
            'restoration_plan':record.get('restoration_plan'),
            'visual_status':record.get('visual_status','not_run'),
            'visual_result':finding.get('result','not_assessed') if finding else 'not_assessed',
            'ue_verified':record['id'] in report.get('assets',{}).get('materials',{}),
            'reasons':list(dict.fromkeys(reasons)),'fallback_attempts':record.get('fallback_attempts',[])})
    return rows
