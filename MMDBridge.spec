# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path
import sys

root = Path(SPECPATH)
sys.path.insert(0, str(root))
from scripts.check_installation import RUNTIME_SCRIPTS, check
check(root)
resources = [(str(root/'scripts'/name), 'bridge_resources/scripts') for name in RUNTIME_SCRIPTS]
resources += [(str(root/name), 'bridge_resources/'+name) for name in
              ('mmd_bridge', 'plugins', 'vendor/blender_mmd_tools-4.5.13', 'vendor/python')]
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
    [str(root/'scripts/desktop_entry.py')],
    pathex=[str(root)],
    binaries=[],
    datas=resources,
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
