import json
from pathlib import Path
import unreal as u
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
path='/Game/MMDBridgeFinal/A_e87a49048496c3b5'
u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(path+'/Maps/MMD_Preview')
world=u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
sequence=u.load_asset(path+'/Sequences/LS_MMD')
result={'world':world.get_path_name(),'actors':[], 'bindings':[]}
for actor in u.get_editor_subsystem(u.EditorActorSubsystem).get_all_level_actors():
    if actor.get_actor_label() in ('MMD Character','MMD_Camera','MMD Sequence'):
        result['actors'].append({'label':actor.get_actor_label(),'path':actor.get_path_name()})
for binding in sequence.get_bindings():
    objects=sequence.locate_bound_objects(binding,world)
    result['bindings'].append({'name':binding.get_name(),'id':str(binding.get_id()),'objects':[o.get_path_name() for o in objects], 'parent':str(binding.get_parent().get_id())})
result['sequence_text']=u.SystemLibrary.get_object_name(sequence)
(root/'work/binding-diagnosis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
u.SystemLibrary.quit_editor()
