"""Check a source checkout without launching DCCs or reading user configuration."""
import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME_SCRIPTS = (
    'blender_entry.py', 'blender_analyze_materials.py', 'blender_surface_layer.py',
    'validate_blender_fbx.py', 'install_upstream_plugins.ps1',
)
RESOURCE_FILES = (
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
