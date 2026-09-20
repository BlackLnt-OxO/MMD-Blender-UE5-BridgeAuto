"""Run via UE Tools > Execute Python Script to open the acceptance result."""
import unreal as u

ROOT = '/Game/MMDBridgeFinal/A_e87a49048496c3b5'


def main():
    level = ROOT + '/Maps/MMD_Preview'
    sequence_path = ROOT + '/Sequences/LS_MMD'
    if not all(u.EditorAssetLibrary.does_asset_exist(path) for path in (level, sequence_path)):
        raise RuntimeError('当前项目没有最终验收资源，请打开 MMDBridgeTest 工程')
    saving = u.EditorLoadingAndSavingUtils
    if saving.get_dirty_map_packages() or saving.get_dirty_content_packages():
        # Keep user's choice to save/discard/cancel in the editor's native dialog.
        if not saving.save_dirty_packages_with_dialog(True, True):
            return
    u.LevelSequenceEditorBlueprintLibrary.close_level_sequence()
    if not u.get_editor_subsystem(u.LevelEditorSubsystem).load_level(level):
        raise RuntimeError('无法打开最终关卡')
    sequence = u.load_asset(sequence_path)
    u.LevelSequenceEditorBlueprintLibrary.open_level_sequence(sequence)
    world = u.get_editor_subsystem(u.UnrealEditorSubsystem).get_editor_world()
    missing = [binding.get_name() for binding in sequence.get_bindings()
               if not sequence.locate_bound_objects(binding, world)]
    if missing:
        raise RuntimeError('未解析绑定: ' + ', '.join(missing))
    u.EditorAssetLibrary.sync_browser_to_objects([sequence_path])
    u.log('已打开最终关卡与序列，全部对象绑定有效: ' + sequence_path)


main()
