"""Native saved-level volume restoration and idempotence check."""
import sys,os
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_appearance import restore_volume_materials
base=Path(os.environ['MMDBRIDGE_VOLUME_VALIDATION'])
job=Path(read_json(base/'current.json')['job']);report=read_json(job/'ue_report.json')
assert u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(report['assets']['level'])
actors=u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors()
stages=[a for a in actors if isinstance(a,u.UsdStageActor)];assert len(stages)==1
stage=stages[0];bindings=report['checks']['usd_stage']['volumes']
root_layer=Path(stage.get_editor_property('root_layer').file_path)
if not root_layer.is_absolute():root_layer=Path(u.Paths.project_dir())/root_layer
package=root_layer.parent/'package.json';data=read_json(package)
data['ue_volume_material_bindings']=bindings;atomic_json(package,data)
components=[stage.get_generated_component(x['prim_path']) for x in bindings]
before=[c.get_material(0).get_path_name() if c.get_material(0) else None for c in components]
restored=restore_volume_materials(stage)
assert all(c.get_material(0) for c in components)
assert not restore_volume_materials(stage)
# Regeneration of one empty slot must restore it, while preserving the other.
kept=[c.get_material(0) for c in components];components[0].set_material(0,None)
again=restore_volume_materials(stage);assert len(again)==1
assert all(c.get_material(0)==m for c,m in zip(components,kept))
atomic_json(base/'volume_reload_native.json',dict(complete=True,before=before,restored=restored,
    after=[c.get_material(0).get_path_name() for c in components],idempotent=True,empty_slot_restored=True))
print('VOLUME_RELOAD_NATIVE_PASS')
