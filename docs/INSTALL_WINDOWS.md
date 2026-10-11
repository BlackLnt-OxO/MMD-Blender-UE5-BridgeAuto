# Windows 新电脑安装

本项目是源码工具，不捆绑 Blender、UE、MMD、MMDBridge 或 MMM 软件。克隆后先安装桌面 Python 依赖，再根据使用的流程配置外部软件。无需 AI 服务、API Key 或 Codex。

## 1. 基础安装和启动

1. 安装 [Git for Windows](https://git-scm.com/downloads/win) 和 [Python](https://www.python.org/downloads/windows/) **64 位 3.12**；3.11 也支持。安装 Python 时保留 Python Launcher，或加入 PATH。当前固定的 Qt 版本不以 Python 3.13/3.14 为支持目标。
2. 使用将来实际发布的 GitHub 仓库地址克隆本项目，然后进入仓库根目录。仓库地址发布前尚未确定，不要使用占位地址。
3. 双击 `start.cmd`。首次安装需要联网访问 PyPI；以后启动使用仓库内 `.venv`。没有系统 Python 时会提示安装，不会下载外部应用。

也可以手动执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

便携 Python 或 Launcher 检测不到时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -PythonExe "C:\Tools\Python312\python.exe"
```

脚本不会删除已有 `.venv`。若环境不完整或 Python 版本不匹配，先退出工具，将 `.venv` 改名留作备份后重跑。克隆目录可以包含空格或中文，避免无写入权限的系统目录。不要从别的电脑复制 `.venv`。

## 2. 外部软件：按流程安装

| 流程 | 用户安装／准备 |
| --- | --- |
| 桌面 GUI、PhysicsBakeFix 文件检查 | 上述 Python 环境即可 |
| Blender 场景转换和完整雾／光影还原 | [Blender 5.2.x](https://www.blender.org/download/)；使用其内置 Python、USD、NumPy 和 OCIO，不将这些模块装进桌面 `.venv` |
| 默认 MMDBridge Mode 1 角色导出 | 用户自己的 [上游 MMDBridge](https://github.com/uimac/mmdbridge) 安装，以及 Blender 4.3.2；需要匹配下面的目录结构 |
| UE 导入、材质切换和渲染 | 用户安装的 UE 5.8.x、自己的 `.uproject`；本工具通过 GUI 安装桥接和启用所需 UE 插件 |
| 旧 MMD2Unreal 角色通路 | 匹配 UE 5.8 的 MMD2Unreal、KawaiiPhysics；见 README 的上游依赖安装或 `scripts/install_upstream_plugins.ps1` |
| 可选 MMM 烘焙和遥测 | 用户安装的 MMM 1.2.9.2；构建本项目 C# 插件还需 .NET SDK 及 .NET Framework 4.8 Targeting Pack，见 `plugins/mmm/MMDBridgeMMMPlugin/README.md` |

MMDBridge Mode 1 目前检查这些文件，不能保证任意上游发行版均兼容：

```text
<MMDBridge安装目录>/
  MikuMikuDance_CHS.exe
  d3d9.dll
  python312.dll
  mmdbridge_scripts/mmdbridge_vmd_mode1.py
  ...上游配套配置和依赖
```

安装目录在 GUI 中选取，本项目不会捆绑或自动替代上述软件。MMD 自动烘焙需要 Windows 保持登录、桌面未锁定。模型、动作、贴图、场景以及模型自身引用的外部文件也由用户准备。

## 3. 编译本项目的 UE 视口小插件

Git 不保存 `MMDBridgeViewport/Binaries`。完整功能需要在新电脑上针对所用引擎构建一次；升级 UE 后重新构建。插件负责读取实际视口模式及当前材质槽顶点，不是本轮放弃的 Raster／Ray Traced 引擎改造。

先安装符合目标 UE 要求的 **Visual Studio 2022／Build Tools、C++ 游戏开发工具和 Windows SDK**，参考 [Epic 开发环境说明](https://dev.epicgames.com/documentation/en-us/unreal-engine/setting-up-visual-studio-development-environment-for-cplusplus-projects-in-unreal-engine)。不用下载完整 UE 引擎源码。

```powershell
.\.venv\Scripts\python.exe scripts\build_viewport_helper.py --unreal "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
```

也可以首次安装时一起执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Unreal "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
```

编译脚本使用 `%TEMP%\MMDBridgeNative` 下的新建独立目录，避免仓库路径较深时触发 UBT 的 260 字符限制；通过检查后只把产物复制到仓库的插件 `Binaries`。不打开工程、不修改引擎、不安装到 UE 工程。输出 BuildId 必须与所选引擎一致，失败时不替换已有二进制文件。

然后在 GUI 中选择实际 UE 和工程，保存并关闭工程，点击“安装 UE 桥接插件”，再打开工程并连接。没有匹配的原生插件时，Python 桥接仍可安装，但视口雾的 PT 自动切换和手动玻璃几何分析不能视为完整可用。

## 4. 新电脑验收

```powershell
.\.venv\Scripts\python.exe scripts\check_installation.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts\desktop_entry.py --smoke-test
```

最后一条会打开 GUI、检测软件、保存结果并退出。默认结果在 `%LOCALAPPDATA%\MMDBridge\smoke.json` 和 `smoke.png`。设置 `MMDBRIDGE_HOME` 可以隔离测试配置；正式使用不用设置。

安装检查只能证明源码资源和桌面依赖齐全，不代表 UE 导入或渲染成功。打开自己的工程后，先用小场景完成导入、重新加载、材质切换与撤销，再检查 Lumen/PT 效果；完整工程性能取决于显存和场景规模。GPU 不支持所需硬件光追时，不能保证本项目相关效果。

## 5. 更新、开发和可选打包

更新代码后重新运行 `scripts/setup.ps1`，重新部署 UE 桥接；UE 原生源码变化时重新编译小插件。执行 Git 更新前保存自己的源码修改。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build.py
```

打包产物在 `dist/MMDBridge`，整个目录一起移动。`MMDBridge.spec` 使用仓库相对位置，也可供开发者直接调用 PyInstaller。普通使用推荐 `start.cmd` 源码入口。

桌面 Python 未安装 Blender 的 NumPy／USD 时，相应专用测试会跳过；真实 Blender 和 UE 用例按文档另行运行。测试临时目录权限异常时，可指定一个新目录：`-p no:cacheprovider --basetemp work/pytest_new_run`；不要指定有重要文件的目录，pytest 会清理其临时目录。

本次独立源码副本和全新虚拟环境的实际检查见 [迁移验证记录](SOURCE_READY_VALIDATION_20261012.md)。
