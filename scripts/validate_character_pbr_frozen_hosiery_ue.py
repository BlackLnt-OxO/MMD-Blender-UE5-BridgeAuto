"""Recheck v8 stockings in a new level without writing frozen GenZ assets."""
from pathlib import Path
import os,sys,copy
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.ue_character_pbr import build
from mmd_bridge.ue_character_pbr_edit import apply,undo,_ledger
folder=Path(os.environ['MMDBRIDGE_PBR_FIXTURE']);source=folder.parent/'Hosiery_v8'
manifest=read_json(source/'manifest.json');receipt=read_json(source/'ue_receipt.json')
root='/Game/MMDBridgePBR/CrossCharacter_1018_v9/GenZ_StockingRegression_v8'
editor=u.get_editor_subsystem(u.LevelEditorSubsystem);actors=u.get_editor_subsystem(u.EditorActorSubsystem)
level=root+'/L_StockingRegression'
report={'warnings':[],'failures':[],'checks':{}};m=Materials(root,'cross_stocking_v8',report,manifest['settings'])
record=copy.deepcopy(next(r for r in manifest['materials'] if r['character_material']['pbr']['type']=='hosiery'))
candidate=m.asset(record['id']) if m.reuse(record) else build(m,record)
mesh=u.load_asset(receipt['mesh']);source_bindings=[r.material_interface.get_path_name() for r in mesh.materials]
protected={str(m.asset_file(u.load_asset(path))):file_hash(m.asset_file(u.load_asset(path))) for path in source_bindings}
assert (editor.load_level(level) if u.EditorAssetLibrary.does_asset_exist(level) else editor.new_level(level))
actor=next((a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor)),None)
if actor is None:actor=actors.spawn_actor_from_class(u.SkeletalMeshActor,u.Vector())
actor.set_actor_label('PBR_FrozenGenZ_Stocking_Regression');actor.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)
slot=source_bindings.index(receipt['assets']['materials'][record['id']])
actor.skeletal_mesh_component.set_material(slot,candidate);assert editor.save_current_level()

def comp(reload=False):
    if reload:assert editor.load_level(level)
    actor=next(a for a in actors.get_all_level_actors() if isinstance(a,u.SkeletalMeshActor))
    actors.set_selected_level_actors([actor]);return actor.skeletal_mesh_component

def paths(c):return [x.get_path_name() for x in c.get_materials()]

lib=u.MaterialEditingLibrary;component=comp();before=paths(component)
parent=candidate.get_editor_property('parent')
node=next(n for n in u.ObjectIterator(u.MaterialExpressionScalarParameter) if n.get_outer()==parent
          and str(n.get_editor_property('parameter_name'))=='HosieryThickness')
ui={k:str(node.get_editor_property(k)) if k=='group' else node.get_editor_property(k)
    for k in ('group','slider_min','slider_max','default_value')}
assert ui['group']=='Hosiery' and ui['slider_min']==0 and ui['slider_max']==3
dynamic=u.MaterialLibrary.create_dynamic_material_instance(component,candidate);assert dynamic
runtime=[]
for value in (0.,.35,.73125,1.,1.23456,1.85,2.5,3.):
    dynamic.set_scalar_parameter_value('HosieryThickness',value)
    actual=dynamic.get_scalar_parameter_value('HosieryThickness');assert abs(actual-value)<1e-5
    runtime.append({'requested':value,'actual':actual})
ledger=_ledger();previous=ledger.read_bytes() if ledger.is_file() else None
operations=[]
try:
    for value in (.35,1.,2.5,0.,.65,1.23456,1.85,3.):
        cfg=copy.deepcopy(manifest['settings'])
        cfg.setdefault('character_material_overrides',{}).setdefault(manifest['source_sha256'],{}).setdefault(
            str(record['pmx_index']),{}).setdefault('params',{})['HosieryThickness']=value
        applied=apply({'model':manifest['model'],'model_sha256':manifest['source_sha256'],
                       'pmx_index':record['pmx_index'],'settings':cfg})
        assert applied['count']==1 and not applied['report']['failures']
        changed=[i for i,(a,b) in enumerate(zip(before,paths(component))) if a!=b];assert changed==[slot]
        component=comp(True);native=component.get_material(slot)
        actual=lib.get_material_instance_scalar_parameter_value(native,'HosieryThickness');assert abs(actual-value)<1e-5
        undone=undo();assert undone['count']==1
        component=comp(True);assert paths(component)==before
        operations.append({'requested':value,'actual':actual,'changed_slots':changed,'save_reload':True,
                           'undo_reload':True,'other_48_slots_unchanged':True})
    assert all(file_hash(path)==digest for path,digest in protected.items())
    assert [r.material_interface.get_path_name() for r in mesh.materials]==source_bindings
    assert file_hash(manifest['model'])==manifest['source_sha256']
finally:
    if previous is not None:ledger.write_bytes(previous)
    elif ledger.is_file():ledger.unlink()
atomic_json(folder/'hosiery_regression_receipt.json',{'status':'complete','root':root,'frozen_v8_root':receipt['root'],
    'source_preserved':True,'frozen_mesh_bindings_unchanged':True,'frozen_material_files_unchanged':True,
    'parameter_ui':ui,'operations':operations,'runtime_values':runtime,'app_presets':{'thin':.35,'medium':1.,'thick':2.5},
    'prior_undo_ledger_restored':True,'native_build':report})
print('FROZEN_HOSIERY_REGRESSION_COMPLETE',len(operations),len(runtime))
