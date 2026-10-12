"""Build a relocatable, offline Windows distribution with source and dependencies."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from check_installation import RUNTIME_SCRIPTS, check

root=Path(__file__).resolve().parents[1]
check(root)
distribution = Path(os.environ.get('MMDBRIDGE_DIST_ROOT', str(root/'dist'))).resolve()
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed','--name','MMDBridge',
    '--icon',str(root/'assets/app-icon.ico'),
    '--distpath',str(distribution),'--workpath',str(distribution.parent/'build'),
    '--specpath',str(distribution.parent/'build'),
    '--paths',str(root),'--collect-submodules','mmd_bridge',
    '--collect-submodules','pywinauto',
    '--hidden-import','comtypes.client','--hidden-import','win32com.client',
    '--hidden-import','pythoncom','--hidden-import','pywintypes',
    '--add-data',str(root/'mmd_bridge')+';bridge_resources/mmd_bridge',
    '--add-data',str(root/'plugins')+';bridge_resources/plugins',
    '--add-data',str(root/'assets')+';bridge_resources/assets',
    '--add-data',str(root/'vendor/blender_mmd_tools-4.5.13')+';bridge_resources/vendor/blender_mmd_tools-4.5.13',
    '--add-data',str(root/'vendor/python')+';bridge_resources/vendor/python',
    str(root/'scripts/desktop_entry.py')]
for name in RUNTIME_SCRIPTS:
    command[-1:-1] = ['--add-data', str(root/'scripts'/name)+';bridge_resources/scripts']
subprocess.run(command,cwd=root,check=True)
out=distribution/'MMDBridge'
for name in ('README.md','LICENSE','THIRD_PARTY.md'):
    if (root/name).exists():
        shutil.copy2(root/name,out/name)
shutil.copy2(root/'scripts/init_upstream.ps1',out/'init_upstream.ps1')
shutil.copy2(root/'scripts/install_upstream_plugins.ps1',out/'install_upstream_plugins.ps1')
for name in ('docs','licenses'):
    if (root/name).exists():
        shutil.copytree(root/name,out/name,dirs_exist_ok=True)
with zipfile.ZipFile(out/'MMD_Blender_Addon.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for file in (root/'plugins/blender/mmd_bridge_addon').glob('*.py'):
        archive.write(file,'mmd_bridge_addon/'+file.name)
shutil.copytree(root/'plugins/unreal/MMDBridge',out/'UE_Plugin_Source',dirs_exist_ok=True)
with zipfile.ZipFile(out/'Source.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for directory in ('mmd_bridge','plugins','scripts','tests','docs','assets','licenses','vendor/blender_mmd_tools-4.5.13','vendor/python'):
        for file in (root/directory).rglob('*'):
            if (file.is_file() and not set(file.parts) & {'__pycache__','Intermediate','Binaries','bin','obj'}
                    and file.suffix not in ('.pyc','.pdb')):
                archive.write(file,file.relative_to(root))
    for name in ('pyproject.toml','README.md','LICENSE','THIRD_PARTY.md','start.cmd','MMDBridge.spec'):
        archive.write(root/name,name)
print(out/'MMDBridge.exe')
