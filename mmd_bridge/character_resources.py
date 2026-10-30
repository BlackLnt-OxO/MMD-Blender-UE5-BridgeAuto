"""Install registered shader templates without overwriting project assets."""
import shutil
import re
from pathlib import Path

from .core import read_json, file_hash, atomic_json
from .character_profiles import load_profiles, resolve_profile


def copy_runtime_file(source, target):
    target=Path(target)
    if target.is_file() and file_hash(source)==file_hash(target):
        return str(target)
    return shutil.copy2(source,target)


def install(settings, root, project, unreal):
    profiles = load_profiles(root,settings.get('character_material_profiles_dir',''))
    profile = resolve_profile(profiles,settings.get('character_master_material',''),
                               settings.get('character_material_profile','auto'))
    if settings.get('character_material_profile') == 'builtin_cel':
        from .character_cel import PREFIX
        profile = {'id': 'builtin_cel', 'template': 'BuiltinCel',
                   'template_content_prefix': PREFIX.removeprefix('/Game/'),
                   'outline': {'parent': PREFIX+'Character/Materials/Master/M_Outline'}}
    if not profile:
        return False
    project = Path(project).resolve()
    content = project.parent/'Content'
    template = profile.get('template')
    receipt = {'profile':profile['id'],'copied':[],'reused':[]}
    copies = []
    if template:
        if not isinstance(template,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',template):
            raise ValueError('母材质模板目录无效')
        source = Path(root)/'resources/character_materials'/template/'Content'
        if not source.is_dir():
            raise ValueError('缺少角色母材质模板: '+template)
        for asset in sorted(source.rglob('*')):
            if not asset.is_file():
                continue
            active_prefix = profile.get('template_content_prefix')
            if active_prefix and not asset.relative_to(source).as_posix().startswith(active_prefix):
                continue
            target = content/asset.relative_to(source)
            if not target.resolve().is_relative_to(content.resolve()):
                raise ValueError('母材质模板路径越界')
            if target.is_file():
                if file_hash(target)!=file_hash(asset):
                    raise ValueError('保留工程中不同版本的母材质资产，拒绝覆盖: '+str(target))
                receipt['reused'].append(str(target))
            else:
                copies.append((asset,target))
    needs_runtime = bool(profile.get('outline')) or profile.get('sdf',{}).get('format','none') != 'none'
    if needs_runtime:
        source = Path(root)/'plugins/unreal/MMDBridgeCharacter'
        modules = source/'Binaries/Win64/UnrealEditor.modules'
        engine_modules = Path(unreal).with_name('UnrealEditor.modules') if unreal else None
        if (not modules.is_file() or engine_modules is None or not engine_modules.is_file()
                or read_json(modules).get('BuildId')!=read_json(engine_modules).get('BuildId')):
            raise ValueError('角色 Runtime 插件未针对所选 UE 编译，请按角色材质文档构建')
        target = project.parent/'Plugins/MMDBridgeCharacter'
        if target.exists() and not (target/'.mmd_bridge_managed').is_file():
            raise ValueError('角色 Runtime 插件目录不属于本工具，拒绝覆盖')
    # Finish all ownership and dependency checks before writing project assets.
    for asset,destination in copies:
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(asset,destination)
        receipt['copied'].append(str(destination))
    if needs_runtime:
        shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('Intermediate'),
                        copy_function=copy_runtime_file)
        (target/'.mmd_bridge_managed').write_text('MMDBridge character 0.1.0\n',encoding='utf-8')
        data = read_json(project)
        plugins = data.setdefault('Plugins',[])
        current = next((p for p in plugins if p['Name']=='MMDBridgeCharacter'),None)
        changed = False
        if current:
            if current.get('Enabled') is not True:
                current['Enabled']=True
                changed=True
        else:
            plugins.append({'Name':'MMDBridgeCharacter','Enabled':True})
            changed=True
        if changed:
            atomic_json(project,data)
        receipt['runtime_build_id']=read_json(modules)['BuildId']
    atomic_json(project.parent/'Plugins/MMDBridge/character_material_install.json',receipt)
    return True
