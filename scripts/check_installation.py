"""Check a source checkout without launching DCCs or reading user configuration."""
import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME_SCRIPTS = (
    'blender_entry.py', 'blender_analyze_materials.py', 'blender_surface_layer.py',
    'validate_blender_fbx.py', 'prepare_custom_materials.py', 'install_upstream_plugins.ps1',
)
RESOURCE_FILES = (
    'mmd_bridge/character_material_preset.py',
    'mmd_bridge/character_material_preset_dialog.py',
    'mmd_bridge/ue_character_material_preset.py',
    'mmd_bridge/motion_inputs.py',
    'mmd_bridge/motion_files_dialog.py',
    'mmd_bridge/character_custom_materials.py',
    'mmd_bridge/character_custom_dialog.py',
    'mmd_bridge/blender_custom_materials.py',
    'mmd_bridge/ue_custom_materials.py',
    'mmd_bridge/ue_custom_material_edit.py',
    'mmd_bridge/character_pbr.py',
    'mmd_bridge/blender_character_pbr.py',
    'mmd_bridge/ue_character_pbr.py',
    'mmd_bridge/ue_character_pbr_edit.py',
    'mmd_bridge/character_pbr_dialog.py',
    'resources/character_materials/Character_PBR/neutral_data.png',
    'resources/character_materials/Character_PBR/flat_normal.png',
    'resources/character_materials/Character_PBR/skin_normal.png',
    'resources/character_materials/Character_PBR/skin_roughness.png',
    'resources/character_materials/Character_PBR/cloth_normal.png',
    'resources/character_materials/Character_PBR/cloth_roughness.png',
    'resources/character_materials/Character_PBR/satin_normal.png',
    'resources/character_materials/Character_PBR/satin_roughness.png',
    'resources/character_materials/Character_PBR/hosiery_normal.png',
    'resources/character_materials/Character_PBR/hosiery_roughness.png',
    'resources/character_materials/Character_PBR/hosiery_mask.png',
    'resources/character_materials/Character_PBR/leather_normal.png',
    'resources/character_materials/Character_PBR/leather_roughness.png',
    'resources/character_materials/Character_PBR/hair_normal.png',
    'resources/character_materials/Character_PBR/micro_normal.png',
    'resources/character_materials/Character_PBR/wet_normal.png',
    'resources/character_materials/Character_PBR/wet_mask.png',
    'profiles/character_materials/ZZZ_Render_PMX.json',
    'resources/character_materials/ZZZ_Render_PMX/Content/Character/MasterMaterial/M_RenderMaster.uasset',
    'plugins/unreal/MMDBridgeCharacter/MMDBridgeCharacter.uplugin',
    'assets/app-icon.png', 'assets/app-icon.ico',
    'assets/windows-version.txt', 'assets/check.svg', 'assets/chevron-down.svg', 'assets/chevron-up.svg',
    'vendor/blender_mmd_tools-4.5.13/mmd_tools/__init__.py',
    'vendor/python/opencc/__init__.py',
    'plugins/unreal/MMDBridge/MMDBridge.uplugin',
    'plugins/unreal/MMDBridge/Content/Python/init_unreal.py',
    'plugins/unreal/MMDBridgeViewport/MMDBridgeViewport.uplugin',
    'plugins/mmm/MMDBridgeMMMPlugin/MMDBridgeMMMPlugin.csproj',
)


def plugin_bundle_files(root=ROOT):
    """Ship native runtime binaries and sources, without compiler caches."""
    return [path for path in (root/'plugins').rglob('*')
            if path.is_file() and not set(path.relative_to(root).parts) & {'Intermediate', '__pycache__', 'obj'}
            and path.suffix.lower() not in ('.pyc', '.pdb')]


def check(root=ROOT):
    missing = [name for name in RESOURCE_FILES if not (root/name).is_file()]
    missing += ['scripts/'+name for name in RUNTIME_SCRIPTS if not (root/'scripts'/name).is_file()]
    if missing:
        raise RuntimeError('Missing checkout resources: '+', '.join(missing))
    for name in ('PySide6.QtWidgets', 'pywinauto', 'mmd_bridge.app'):
        importlib.import_module(name)
    print('Source resources and desktop Python imports: OK')


if __name__ == '__main__':
    check()
