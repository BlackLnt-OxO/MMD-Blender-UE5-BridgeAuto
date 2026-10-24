# 安装与启动

普通使用下载 Windows 发行包；需要修改程序或为不同 UE 构建编译插件，再准备源码环境。

## Windows 发行包

从 [v0.3.0 发布页](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/tag/v0.3.0) 下载 `MMDBridgeForUE-Windows-x64.zip`，完整解压，运行 `MMDBridgeForUE.exe`。

v0.3.0 包含当前内置 PBR、补充动作多选、Alpha 接线修复和独立“角色设置”页。此前公开 v0.2.0 及本地 `20261024-705a681` 构建均不代表此版本。升级请更换完整目录，并重新部署 UE 桥接；已有任务、工程及手工材质仍按各自版本处理。版本关系见 [源码与发行包](PROJECT_STATUS.md#源码与发行包)。

保留 `_internal`、文档和其它配套文件。移动程序时保留整个解压目录，不要从压缩包内直接运行。包内已含桌面 Python／Qt 依赖。程序尚未做代码签名，Windows 可能显示未知发布者提示。

首次操作看 [快速上手](QUICK_START.md)。

## 按用途准备外部软件

| 用途 | 需要安装 |
| --- | --- |
| 默认角色导入、全流程 | [Blender 5.2.x](https://www.blender.org/download/)、[UE 5.8.x](https://www.unrealengine.com/download)、上游 [rintrint/mmdbridge Release 配套程序](https://github.com/rintrint/mmdbridge/releases) |
| Blender 场景、镜头导入 | Blender 5.2.x、UE 5.8.x；单独场景转换不需要外部 MMDBridge |
| PhysicsBakeFix 文件检查与修复 | 本工具即可 |
| MMD2Unreal 旧通路 | 另需匹配引擎的 MMD2Unreal、KawaiiPhysics，见 [旧流程](LEGACY_FBX.md) |
| 已弃用 MMM 工具 | 自行安装 MMM 64 位版，见 [旧流程](LEGACY_FBX.md#mmm-物理烘焙) |

v0.2.0 使用同一套 Blender 5.2 处理角色与场景，角色适配器兼容 4.3。从 v0.1.0 升级时请更换完整发行包。

模型、动作、场景、外部贴图与模拟缓存自行准备。Blender 使用它自己的 Python、USD、NumPy 和 OCIO，不要往桌面 `.venv` 里安装 `bpy` 代替它。

## 上游 MMDBridge 目录

选择 **rintrint/mmdbridge Release 配套程序**的安装目录，包含以下主程序、导出脚本与依赖。此处与本工具的 UE 桥接插件目录不同。

```text
<MMDBridge 安装目录>/
  MikuMikuDance_CHS.exe
  d3d9.dll
  python312.dll
  mmdbridge_scripts/mmdbridge_vmd_mode1.py
  mmdbridge_settings/MikuMikuDance_CHS.exe.ini
  ...其余上游配套文件
```

在“素材与软件”手动选择目录，或点击 **自动检测**。检测优先检查当前与记忆路径，再搜索常见目录；多份安装可供选择，未检出时手动指定。检测只检查所需文件，不启动上游程序或验证版本兼容性。

烘焙使用独立 Runtime 副本。运行期间保持 Windows 登录、桌面未锁定。

## UE 插件与 BuildId

首次连接目标工程：保存并关闭 UE → 选择 `.uproject` → **安装 UE 桥接插件** → 重新打开工程 → **连接所选项目**。

USD、OpenColorIO、MovieRenderPipeline 是 UE 官方插件；MMDBridge 是本工具的 Python 桥接。MMDBridgeViewport 提供视口与几何读取，ZZZ 双网格角色还会用到 MMDBridgeCharacter。

发行包带有 **UE 5.8.2 / BuildId 55116800** 的原生插件。BuildId 匹配时无需安装 C++ 构建环境。不匹配时需为实际引擎编译插件，Python 桥接仍可单独安装。

### 编译视口插件

准备源码（或解压发行包内的 `Source.zip`）及源码 Python 环境，再安装目标 UE 要求的 Visual Studio／Build Tools、C++ 游戏开发组件和 Windows SDK。不需要重新编译 UE 引擎。

```powershell
.\.venv\Scripts\python.exe scripts\build_viewport_helper.py --unreal "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
```

脚本在 `%TEMP%/MMDBridgeNative` 新建独立构建目录，检查输出 BuildId 后才把 Binaries 复制回仓库。构建失败不替换旧二进制，也不会直接安装到用户工程。完成后再通过 GUI 安装桥接。

MMDBridgeCharacter 的构建及材质要求见 [角色材质](CHARACTER_MATERIALS.md#插件与重新生成)。不要在不同 BuildId 之间直接复制 DLL。

## 从源码启动

安装 Git for Windows 和 **Windows x64 Python 3.12**（支持 3.11；当前固定 Qt 不支持以 3.13/3.14 作为桌面环境）。

```powershell
git clone https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto.git
cd MMD-Blender-UE5-BridgeAuto
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

也可以双击 `start.cmd`，首次安装需要联网访问 PyPI。便携 Python 未被识别时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -PythonExe "C:\Tools\Python312\python.exe"
```

安装脚本不删除已有 `.venv`。环境不完整时先退出工具，把旧 `.venv` 改名备份，再重新建立；不要跨电脑复制虚拟环境。

## 开发与打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Dev
.\.venv\Scripts\python.exe scripts\check_installation.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build.py
```

日常修改先运行相关测试；完整回归按需要执行。Windows测试使用本次独立的临时目录，例如先创建 `work/agent_checks`，再给pytest传入 `-p no:cacheprovider --basetemp work/agent_checks/<本次唯一目录>`；不要将已有任务/证据作为basetemp。可用 `MMDBRIDGE_HOME` 隔离配置。开发约定见 [AGENTS.md](../AGENTS.md)。

默认产物为 `dist/MMDBridgeForUE/`。目录内含 EXE、运行资源、文档、许可、Blender 插件压缩包及 `Source.zip`。`MMDBridge.spec` 也使用仓库相对路径。

运行包保留匹配引擎的原生 DLL 与模块信息，排除 UE `Intermediate`、对象缓存及 PDB 调试文件，避免把本机编译缓存带到另一台电脑。

`Source.zip` 包含开发约定、接手说明及固定依赖源码，排除本地环境、原生编译产物和缓存；源码克隆与任务资产的恢复范围见 [克隆与开发接手](DEVELOPMENT_HANDOFF.md)。需要隔离构建输出时，可先设置 `MMDBRIDGE_DIST_ROOT` 为新的输出目录，再运行 `scripts/build.py`。

`desktop_entry.py --smoke-test` 会打开 GUI、检测软件，保存 `smoke.json` 和截图后退出；默认位于 `%LOCALAPPDATA%/MMDBridge`。开发测试可设置 `MMDBRIDGE_HOME` 隔离配置。桌面启动检查只验证启动与资源，不替代真实素材的导入、播放或渲染。

更新后重新部署 UE 桥接；原生插件源码变动时重新编译对应插件。更新前保留自己的代码和工程修改。
