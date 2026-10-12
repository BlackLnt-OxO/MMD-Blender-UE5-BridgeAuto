# Windows 安装与启动

MMDBridgeForUE 提供 Windows 64 位发行包，也可以从源码运行。发行包自带桌面运行依赖；Blender、UE、上游 MMDBridge 和 MMM 由用户按所用流程安装。无需 AI 服务、API Key 或 Codex。

## 1. 下载发行包并启动

1. 从 [Releases](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/latest) 下载 `MMDBridgeForUE-Windows-x64.zip`。
2. 解压整个压缩包，双击 `MMDBridgeForUE.exe`。保留 `_internal` 和其它配套文件，不要从压缩包内直接运行或只复制 EXE。
3. 在 GUI 中选择外部软件及素材，按下面的流程清单准备所需依赖。

普通使用无需安装 Python、Qt 或 Git。可以整体移动解压目录，任务与个人配置保存在独立位置。没有代码签名；首次运行可能显示 Windows 发布者提示。

发行包包含 **UE 5.8.2 / BuildId 55116800** 的 `MMDBridgeViewport`，安装桥接时会检查与目标引擎是否匹配。匹配时不用安装 C++ 编译环境；不匹配时仍可安装 Python 桥接，但完整视口／几何读取功能需要按第 3 节重新编译。已弃用 MMM 工具的遥测 DLL 也包含在发行包中。

### 可选：从源码启动

1. 安装 [Git for Windows](https://git-scm.com/downloads/win) 和 [Python](https://www.python.org/downloads/windows/) **64 位 3.12**；3.11 也支持。安装 Python 时保留 Python Launcher，或加入 PATH。当前固定的 Qt 版本不以 Python 3.13/3.14 为支持目标。
2. 克隆 [本项目仓库](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto)，然后进入仓库根目录：`git clone https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto.git`。
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
| 桌面 GUI、PhysicsBakeFix 文件检查 | Windows 发行包即可；源码方式使用上述 Python 环境 |
| Blender 场景转换和完整雾／光影还原 | [Blender](https://www.blender.org/download/)；使用其内置 Python、USD、NumPy 和 OCIO，不将这些模块装进桌面 `.venv` |
| 默认 MMDBridge Mode 1 角色导出 | 第三方 [MMDBridge（rintrint/mmdbridge）](https://github.com/rintrint/mmdbridge) 的 [Release 配套程序](https://github.com/rintrint/mmdbridge/releases) 负责烘焙 VMD，需要匹配下面的目录结构；[Blender](https://www.blender.org/download/) 负责后续角色采样与 FBX 导出 |
| UE 导入、材质切换和渲染 | 用户安装的 UE 5.8.x、自己的 `.uproject`；本工具通过 GUI 安装桥接和启用所需 UE 插件 |
| 旧 MMD2Unreal 角色通路（可选） | 仅选择 GUI 的“MMD2Unreal（Legacy）”时需要匹配 UE 5.8 的 [MMD2Unreal](https://github.com/BlackLnt-OxO/MMD2Unreal)、[KawaiiPhysics](https://github.com/pafuhana1213/KawaiiPhysics)；缺失时 GUI 提供下载安装提示，也可使用 `scripts/install_upstream_plugins.ps1` |
| MMM 工具（已弃用，可选） | 暂停维护，现有功能保留。从 [MMM 官网](https://sites.google.com/site/mikumikumovingeng/) 的 **MikuMikuMoving version 1.2.9.2 — 03 Jun 2018 — 32bit/64bit** 条目下载并使用 **64 位版**；发行包已含本项目 C# 遥测 DLL；自行构建时需 .NET SDK 及 .NET Framework 4.8 Targeting Pack，见 `plugins/mmm/MMDBridgeMMMPlugin/README.md`。GUI 入口负责环境准备与遥测，不自动烘焙 |

默认角色流程不依赖 MMD2Unreal、KawaiiPhysics 或 MMM；单独导入 Blender 场景不需要外部 MMDBridge。上游 MMDBridge 是第三方烘焙程序，与本项目的 UE 桥接插件不同，不由 UE 插件安装器下载。

### 当前 Blender 版本兼容说明

主流程以 **Blender 5.2.x** 为主配置：上游 MMDBridge 先烘焙物理 VMD，Blender 再读取 PMX 与烘焙 VMD，导出模型和动画双 FBX；场景及完整雾／光影转换也使用 5.2.x。上游 VMD 烘焙本身不依赖 Blender。

**旧 Blender / FBX（不烘焙物理）已弃用。**该旧入口曾在 4.3 测试，直接处理 PMX／VMD，不执行上游 MMDBridge 物理烘焙。入口保留，详情见 [旧角色流程](LEGACY_FBX.md)。

**当前 v0.1.0 发行包及本次文档对应源码仍有遗留版本检查：**旧入口和默认 MMDBridge 入口共用的角色适配器要求 4.3.x，GUI 的一键全流程也会查找 4.3.x 处理角色、5.2.x 处理场景。使用该版本完整运行时，两个版本都需能被工具发现，可在“素材与软件”添加路径；角色推荐使用 4.3.2。只导入场景无需 4.3.x。

这项程序限制尚未在本次文档更新中解除；不能仅因 Blender 5.2 能导出 FBX，就认为当前发行包已开放单一 5.2.x 的全流程。后续程序兼容更新以发行说明为准。

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

仅源码方式运行或发行包插件与引擎 BuildId 不匹配时需要编译。Git 不保存 `MMDBridgeViewport/Binaries`；发行包包含指定引擎版本的产物。升级 UE 后先检查匹配情况。插件负责读取实际视口模式及当前材质槽顶点。

先克隆源码仓库或解压发行包内的 `Source.zip`，按第 1 节创建源码环境，再执行以下命令。

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

## 4. 源码安装检查

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

打包产物在 `dist/MMDBridgeForUE`，入口为 `MMDBridgeForUE.exe`，整个目录一起移动。`MMDBridge.spec` 使用仓库相对位置，也可直接调用 PyInstaller。普通使用下载发行包即可。

桌面 Python 未安装 Blender 的 NumPy／USD 时，相应专用测试会跳过；真实 Blender 和 UE 用例按文档另行运行。测试临时目录权限异常时，可指定一个新目录：`-p no:cacheprovider --basetemp work/pytest_new_run`；不要指定有重要文件的目录，pytest 会清理其临时目录。

具体操作见 [使用指南](USAGE.md)，转换模块见 [源码结构](ARCHITECTURE.md)。
