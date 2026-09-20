import unreal as u
from pathlib import Path
import json
root=Path(__file__).resolve().parents[1] if '__file__' in globals() else Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
result={}
for kind in ('MovieSceneComponentMaterialTrack','MovieSceneParameterSection','LevelSequenceFactoryNew','LevelSequence','AnimPoseExtensions','AnimPoseEvaluationOptions'):
    cls=getattr(u,kind,None)
    result[kind]=[name for name in dir(cls) if not name.startswith('_')]
for path in u.EditorAssetLibrary.list_assets('/Game/MMDBridge/A_e87a49048496c3b5',True,False):
    asset=u.load_asset(path)
    if asset:
        u.EditorAssetLibrary.set_metadata_tag(asset,'MMDBridge.Owner','e87a49048496c3b5')
        u.EditorAssetLibrary.save_loaded_asset(asset)
(root/'work/ue_api.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('PROBE creating sequence')
seq=u.AssetToolsHelpers.get_asset_tools().create_asset('ProbeSequence','/Game/Probes',u.LevelSequence,u.LevelSequenceFactoryNew())
print('PROBE sequence created',seq)
