"""Read-only native reload and transient renderer-contract validation.

MMDBRIDGE_WORLD_WORKFLOW_JOB points to a converted job; this script saves no
assets and does not retune its materials, lighting, fog or project settings.
"""
import os
import sys
from pathlib import Path

import unreal as u

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_appearance import restore_volume_materials,runtime_world_snapshot
from mmd_bridge.ue_render_policy import ensure_mrq_image_output,mrq_beauty_passes

job=Path(os.environ['MMDBRIDGE_WORLD_WORKFLOW_JOB'])
out=Path(os.environ['MMDBRIDGE_WORLD_WORKFLOW_OUT'])
report=read_json(job/'ue_report.json')
cfg=read_json(job/'job.json')['settings']
assert u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level'])
world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
stages=u.GameplayStatics.get_all_actors_of_class(world,u.UsdStageActor)
restored=[]
for stage in stages:restored.extend(restore_volume_materials(stage))
bindings=[dict(actor_name=item['actor'].rsplit('.',1)[-1],source=item['snapshot']['source'])
    for item in report['checks']['scene_lights_native']['lights'] if item.get('snapshot')]
snapshot=runtime_world_snapshot(world,stages,'lumen',bindings,cfg.get('scene_light_scale',1.))
atomic_json(out/'native_world_snapshot.json',snapshot)
assert snapshot['resource_match'],snapshot['errors']
assert len(snapshot['lights'])==report['checks']['scene_lights_native']['expected']
assert not snapshot['parameter_mismatches'],snapshot['parameter_mismatches']
assert all(item['binding_valid'] for item in snapshot['volumes'])
source=u.load_asset(report['assets']['render_preset'])
before=mrq_beauty_passes(source,u)
config=u.new_object(u.MoviePipelinePrimaryConfig)
config.copy_from(source)
observed=[]
for pipeline,expected in [('path_tracing',['PathTracer']),('lumen',['FinalImage'])]:
    ensure_mrq_image_output(config,pipeline,u)
    passes=mrq_beauty_passes(config,u)
    assert passes==expected
    observed.append(dict(pipeline=pipeline,passes=passes))
assert mrq_beauty_passes(source,u)==before
atomic_json(out/'native_workflow_result.json',dict(complete=True,saved_assets_modified=False,
    material_conversion_changed=False,volume_restored=restored,world=snapshot,
    transient_renderers=observed,original_preset_passes=before))
print('WORLD_WORKFLOW_NATIVE_PASS')
