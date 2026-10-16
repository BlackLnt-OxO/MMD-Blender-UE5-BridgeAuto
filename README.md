<p align="center"><img src="assets/app-icon.png" width="88" alt="MMDBridgeForUE"></p>

# MMDBridgeForUE

将 MMD 角色、动作、镜头和 Blender 场景导入 Unreal Engine，继续编辑、打光与渲染。

Windows 桌面工具，支持角色物理烘焙、USD 场景转换、镜头导入和后台渲染。角色、场景与镜头可分别导入，也可一次完成全流程。转换在本地执行，无需在线服务。

**[下载 v0.2.0](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/releases/tag/v0.2.0) · [快速上手](docs/QUICK_START.md) · [完整文档](docs/README.md) · [反馈问题](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto/issues)**

![素材与软件页面](docs/images/workspace.png)

*软件界面截图，素材路径为示例。*

## 快速开始

1. 下载 `MMDBridgeForUE-Windows-x64.zip`，**完整解压**，打开 `MMDBridgeForUE.exe`。普通使用不用另装 Python；不要只复制 EXE。
2. 准备 **Blender 5.2.x、UE 5.8.x**。导入角色还需要 [rintrint/mmdbridge 的 Release 配套程序](https://github.com/rintrint/mmdbridge/releases)，它与本工具的 UE 桥接插件不同。
3. 在 **素材与软件** 中选择软件和 `.uproject`。首次使用工程，先保存并关闭 UE，点击 **安装 UE 桥接插件**，重新打开工程，再点 **连接所选项目**。
4. 填入 **PMX 模型、动作 VMD、Blender 场景**。口型、镜头 VMD 可选。MMDBridge 安装目录支持 **自动检测**；临时中转目录可留空。
5. 在 **参数** 中确认帧范围、画幅和角色材质。默认使用 **UE 官方卡通**，需要项目支持 Substrate Toon；具体要求见 [角色材质](docs/CHARACTER_MATERIALS.md)。
6. 点击 **走全流程**，在 **任务与日志** 查看进度。完成后到 UE 检查关卡、序列和画面。

首次建议用短动作、小场景。只导入一项时，直接用底部的 **导入角色／导入场景／导入镜头**，输入要求见 [快速上手](docs/QUICK_START.md)。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 角色与物理动画 | PMX、VMD 经 MMDBridge 烘焙，再由 Blender 导出模型和动画双 FBX。可叠加口型。 |
| Blender 场景 | 导出参考帧的最终几何、修改器与实例，生成 USD 场景，并转换材质、灯光和雾。 |
| VMD 镜头 | 导入 VMD 镜头，生成可在 UE Sequencer 中编辑的镜头轨道。 |
| 参考图与渲染 | 渲染 Blender／UE 参考帧，生成对照与差分；也能关闭编辑器后使用已保存的 MRQ 预设后台输出。 |
| 任务恢复 | 保存任务、日志和检查点，支持停止后继续处理。 |

## 功能特色

### 常用参数与高级分组

文件路径各占一行；帧率、范围、画幅及角色母材质集中在常用区，高级参数按用途折叠。角色材质支持 **UE 官方卡通**、**ZZZ_Render_PMX** 和自定义母材质配置。

![常用参数与角色材质配置](docs/images/parameters.png)

[角色材质如何选择 →](docs/CHARACTER_MATERIALS.md)

### 按物体与材质槽局部调整

在 UE 单选或多选物体，分析其材质槽，按需切换为不透明、镂空遮罩、薄片透光或透射折射。仅更新勾选的槽位，支持撤销，无需重新导入整个场景。该功能适用于能匹配到本工具转换记录的物体。

![场景还原中的材质和参考图操作](docs/images/scene-tools.png)

[操作步骤与写入范围 →](docs/USAGE.md#局部材质切换)

### 物理骨自动修复与镜头切分

默认角色流程自动检查物理骨的显示枠登记，并按需修复任务中的 PMX 副本。**PhysicsBakeFix** 提供独立检测和修复入口。

**CameraCutAuto** 检测跳切并列出候选帧，经确认后生成 Camera Cuts；候选帧支持手动增删。

![CameraCutAuto 候选帧与确认入口](docs/images/camera-cuts.png)

[自动切镜与物理骨修复 →](docs/USAGE.md#镜头自动切分)

## 环境与兼容

- **Windows x64**；发行包自带桌面 Python／Qt 依赖。
- 主流程使用 **Blender 5.2.x + UE 5.8.x**。v0.2.0 可用同一套 Blender 5.2 处理角色与场景，角色适配器也保留 4.3 兼容。
- 发行包包含 **UE 5.8.2 / BuildId 55116800** 的视口与角色插件。引擎 BuildId 不同时，需要为自己的引擎编译相应插件，不用重编 UE。
- 默认角色流程需要外部 MMDBridge；单独转换 Blender 场景不需要。MMD2Unreal、KawaiiPhysics 和 MMM 只用于各自的旧入口。

跨渲染器差异、模拟迁移和显存限制见 [已知问题](docs/KNOWN_ISSUES.md)。

## 文档

| 内容 | 文档 |
| --- | --- |
| 首次导入 | [快速上手](docs/QUICK_START.md) |
| 安装软件、连接 UE、从源码启动 | [安装与启动](docs/INSTALL_WINDOWS.md) |
| 单项导入、配置、修复、切镜和恢复 | [日常操作](docs/USAGE.md) |
| 选择官方 Toon、ZZZ 或自定义母材质 | [角色材质](docs/CHARACTER_MATERIALS.md) |
| 处理几何、材质和碰撞 | [USD 场景](docs/USD_SCENE_TUTORIAL.md) |
| 对照灯光、雾和源色彩 | [场景光影](docs/SCENE_APPEARANCE.md) |
| 设置 MRQ 和后台输出 | [显存与画质](docs/MRQ_MEMORY.md) · [后台渲染](docs/background_render.md) |
| 修改程序或排查数据流 | [源码结构](docs/ARCHITECTURE.md) |

## 从源码运行

安装 Git 和 Windows 64 位 Python 3.12（也支持 3.11），然后执行：

```powershell
git clone https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto.git
cd MMD-Blender-UE5-BridgeAuto
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

也可以双击仓库里的 `start.cmd`。开发依赖、检查与打包方法见 [安装说明](docs/INSTALL_WINDOWS.md#开发与打包)。

反馈问题时，附上版本、操作步骤、预期与实际结果，以及相关任务日志；画面问题最好同时附 Blender 和 UE 的同帧截图。请勿上传无权公开的素材。

源码采用 **GPL-3.0-or-later**，见 [LICENSE](LICENSE)。第三方组件与配套资产的来源、许可见 [THIRD_PARTY.md](THIRD_PARTY.md)，模型、动作和场景由使用者自行准备。
