# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

analysis_script = ('D:/Codex_Projects/MMD_Blender_UE5_Autowork/scripts/blender_analyze_materials.py', 'bridge_resources/scripts')
hiddenimports = []
hiddenimports += collect_submodules('mmd_bridge')

# mmm_ui.py 里 pywinauto 是**延迟导入**的（放在函数内），
# PyInstaller 的静态分析发现不了 —— 不显式列出，打包后就缺依赖。
hiddenimports += [
    'pywinauto', 'pywinauto.controls', 'pywinauto.application',
    'comtypes', 'comtypes.client', 'comtypes.stream',
    'win32com', 'win32com.client', 'pythoncom', 'pywintypes',
]


a = Analysis(
    ['D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\scripts\\desktop_entry.py'],
    pathex=['D:\\Codex_Projects\\MMD_Blender_UE5_Autowork'],
    binaries=[],
    datas=[analysis_script, ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\mmd_bridge', 'bridge_resources/mmd_bridge'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\scripts\\blender_entry.py', 'bridge_resources/scripts'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\scripts\\validate_blender_fbx.py', 'bridge_resources/scripts'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\scripts\\install_upstream_plugins.ps1', 'bridge_resources/scripts'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\plugins', 'bridge_resources/plugins'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\vendor\\blender_mmd_tools-4.5.13', 'bridge_resources/vendor/blender_mmd_tools-4.5.13'), ('D:\\Codex_Projects\\MMD_Blender_UE5_Autowork\\vendor\\python', 'bridge_resources/vendor/python')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MMDBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MMDBridge',
)
