<p align="center"><img src="assets/app-icon.png" width="112" alt="MMDBridgeForUE 图标"></p>

# MMDBridgeForUE

将 MMD 角色、VMD 镜头和 Blender 场景带入 Unreal Engine 5 的 Windows 桌面工具。支持物理烘焙、场景材质与光影转换、镜头切分，以及在 UE 中局部调整材质。转换由程序执行，使用时不需要 AI 服务。

## 快速使用：一键全流程

1. 从 [Releases](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/latest) 下载 **`MMDBridgeForUE-Windows-x64.zip`**，或 [直接下载](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/latest/download/MMDBridgeForUE-Windows-x64.zip)。
2. **完整解压**压缩包，双击目录中的 **`MMDBridgeForUE.exe`**。不要只复制 EXE；`_internal` 等配套文件必须保留。发行包自带桌面 Python 依赖，无需另装 Python。
3. 主流程的软件配置以 **Blender 5.2.x、UE 5.8.x 和上游 MMDBridge** 为主。当前 v0.1.0 仍有角色版本检查，使用发行包前请查看下方 [版本兼容说明](#blender-版本与旧流程)。在 **素材与软件** 中选择 Blender、UE、目标 `.uproject`，填写 **PMX 模型、动作 VMD、Blender 场景**；口型 VMD 和镜头 VMD 可选。指定上游 **MMDBridge 安装目录**。
4. 首次连接 UE 工程：保存并关闭工程，点击 **安装 UE 桥接插件**，重新打开工程后点击 **连接所选项目**。发行包带有 UE 5.8.2 的视口插件；不同引擎 BuildId 的处理见 [安装说明](docs/INSTALL_WINDOWS.md)。
5. 保持默认的 **MMDBridge Mode 1 → 角色双 FBX**、**USD Stage** 和 **Lumen**。点击绿色的 **走全流程**：工具用 Blender 处理角色和场景，导入可选镜头，并在 UE 中生成关卡与序列。
6. 在 **任务与日志** 查看进度和结果，完成后在 UE 检查材质、镜头、灯光和雾。首次使用建议用短动作和小场景检查效果。

**临时中转目录**保存任务、断点、贴图与导出文件。留空时在桌面创建独立的 `Middle_*` 目录；原始 PMX、VMD 和 `.blend` 不覆盖保存。MMD 烘焙期间保持 Windows 登录，桌面不要锁定。

只需要其中一部分时，用 **导入角色／导入场景／导入镜头**。这些按钮只处理对应素材，不会把其它已填写的素材加入任务。全流程不自动执行下方的 PhysicsBakeFix、CameraCutAuto 或 MMM 独立工具。

## 功能

| 功能 | 可以做什么 |
| --- | --- |
| 角色与物理动画 | 调用第三方 MMDBridge 烘焙 VMD，经过 Blender 输出模型与动画双 FBX，再导入 UE。支持可选口型与角色母材质。 |
| Blender 场景转换 | 转换当前参考帧的最终几何、修改器和实例，生成 USD 场景、可编辑材质、贴图及碰撞，并转换世界光、灯光和雾。 |
| 局部材质切换 | 读取 UE 当前单选或多选物体，勾选材质槽，再切换不透明、镂空遮罩、薄表面散射或透射折射；支持撤销上次切换。 |
| 物理骨修复 · PhysicsBakeFix | 查找受物理刚体驱动但未加入显示枠的骨骼，输出修复后的新 PMX，避免部分烘焙工具遗漏裙子、头发等物理骨。 |
| 镜头自动切分 · CameraCutAuto | 检测镜头跳切，人工确认切点后生成 Camera Cuts 并检查结果，减少连续镜头轨道在跳切处产生的插值与渲染混合问题。 |
| 参考图与后台渲染 | 生成 Blender／UE 参考帧和差分；使用已保存的 MRQ 预设进行后台渲染，并查看输出和日志。 |
| 停止与恢复 | 停止当前任务，保留已完成文件；通过任务记录恢复。源文件变化时创建新任务。 |
| MMM 物理烘焙（已弃用） | 暂时停止维护，现有环境准备、隔离 Runtime 和遥测插件功能仍可使用；烘焙操作在 MMM 中完成。默认角色流程使用 MMDBridge。 |

操作步骤见 [使用指南](docs/USAGE.md)。

## 场景转换与材质

场景默认采用 **USD Stage**。常规贴图与可对应的节点直接生成 UE 材质；复杂输入按支持程度局部烘焙或转为贴图。不支持的效果会记录限制，不把导入完成等同于视觉完全一致。场景保持可重新打光、可在 UE 中编辑。

- 支持最终求值的网格、修改器、Geometry Nodes 和实例；参考帧决定静态导出的形状。
- 区分不透明、Alpha 镂空、薄表面散射与透射折射。识别依据是有效材质输出与节点输入，不只看物体名字。
- 默认 Lumen 使用原生体积雾，并计算雾中受光与遮挡；路径追踪作为效果对照。实时雾不自动模拟玻璃透镜聚光。
- 碰撞与散件优化可以配置。大场景先用默认设置检查，再按显存与实际画面调整。

导入后若某些叶片或玻璃被分到不合适的分支，无需重转整个场景：在 UE 选中物体，使用 **场景还原 → 分析／切换 UE 选中物体的材质…**。操作前保存相关关卡和资产；仅能处理可匹配到本工具转换记录的材质。详细流程及撤销见 [局部材质切换](docs/USAGE.md#局部材质切换)。

## 安装上游依赖

按所用功能安装即可。外部应用不随本仓库分发。

| 软件／插件 | 何时需要 | 来源 |
| --- | --- | --- |
| Blender | 主流程以 5.2.x 为主；处理 PMX／烘焙 VMD、FBX 导出及场景雾／光影转换，当前版本兼容限制见下文 | [Blender](https://www.blender.org/download/) |
| Unreal Engine 5.8.x | UE 导入、编辑与渲染 | [Epic Games](https://www.unrealengine.com/download) |
| 上游 MMDBridge | 默认角色流程的 MMD 物理／VMD 烘焙；与本项目 UE 桥接插件不同 | [仓库](https://github.com/rintrint/mmdbridge) · [Releases](https://github.com/rintrint/mmdbridge/releases) |
| MMD2Unreal + KawaiiPhysics | 仅可选的 **MMD2Unreal（Legacy）** 角色流程；GUI 缺失提示可下载安装 | [MMD2Unreal](https://github.com/BlackLnt-OxO/MMD2Unreal) · [KawaiiPhysics](https://github.com/pafuhana1213/KawaiiPhysics) |
| MikuMikuMoving（MMM） | 已弃用的可选 MMM 工具 | [官网](https://sites.google.com/site/mikumikumovingeng/)：**version 1.2.9.2，03 Jun 2018，32bit/64bit** 条目，使用 64 位版 |

### Blender 版本与旧流程

主流程以 **Blender 5.2.x** 为主介绍：**PMX／动作 VMD → 上游 MMDBridge 烘焙物理 VMD → Blender 读取 PMX 与烘焙 VMD → 模型／动画双 FBX → UE**。物理由上游 MMDBridge 烘焙，Blender 负责后续采样与导出。

**旧 Blender / FBX（不烘焙物理）已弃用**，保留入口供既有任务使用。该旧流程曾在 Blender 4.3 测试，不支持自动物理烘焙；它与默认 MMDBridge 烘焙流程不同。

**v0.1.0 兼容限制：**当前发行包及本次文档对应源码仍保留共用角色适配器的 4.3.x 检查，默认 MMDBridge 角色流程也受影响；一键全流程仍会调用 4.3.x 处理角色、5.2.x 处理场景。只导入场景无需 4.3.x。本次仅更新文档，尚未解除这项检查或发布支持单一 5.2.x 全流程的新程序，详见 [安装说明](docs/INSTALL_WINDOWS.md#当前-blender-版本兼容说明)。

默认角色流程不依赖 MMD2Unreal、KawaiiPhysics 或 MMM；单独转换 Blender 场景不需要外部 MMDBridge。版本要求、目录结构和安装命令见 [安装说明](docs/INSTALL_WINDOWS.md)。许可与来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 教程与参考

- [安装与源码启动](docs/INSTALL_WINDOWS.md)
- [使用指南：独立导入、物理骨修复、自动切镜、材质切换与撤销](docs/USAGE.md)
- [USD 场景：几何、实例、材质与碰撞](docs/USD_SCENE_TUTORIAL.md)
- [灯光、雾与参考图](docs/SCENE_APPEARANCE.md)
- [MRQ 显存与渲染参数](docs/MRQ_MEMORY.md) · [后台渲染](docs/background_render.md)
- [已弃用的旧角色流程与兼容入口](docs/LEGACY_FBX.md) · [已知问题](docs/KNOWN_ISSUES.md)
- [转换流程与源码结构](docs/ARCHITECTURE.md)

## 从源码启动

安装 **Windows 64 位 Python 3.12**（也支持 3.11）及 Git：

```powershell
git clone https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto.git
cd MMD-Blender-UE5-BridgeAuto
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

也可双击 `start.cmd`，首次运行自动创建 `.venv` 并安装桌面 Python 依赖。软件与素材路径在 GUI 中选择，不要求固定盘符。发行包已包含匹配 UE 5.8.2 的视口小插件。源码启动或引擎 BuildId 不同的情况下，按安装说明重新编译该插件，**不用重编 UE 引擎**。

普通使用直接下载 [Windows 发行包](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/latest)；源码启动用于修改程序或重新编译插件。

## 源码与反馈

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
```

Python 负责 GUI 和转换调度，Blender／UE 适配器独立，C++ 小插件提供视口与几何读取。入口、数据文件及扩展位置见 [转换流程与源码结构](docs/ARCHITECTURE.md)。

反馈问题请提交 [Issue](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/issues)，附上软件版本、操作步骤、预期与实际结果，以及相关任务日志；涉及画面时附截图。不要上传无权公开的模型或工程。欢迎修复与改进的 Pull Request。

源码采用 **GPL-3.0-or-later**，详见 [LICENSE](LICENSE)。第三方软件、素材与角色形象遵循各自许可，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。
