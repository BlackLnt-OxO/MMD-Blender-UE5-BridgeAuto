# MMD Bridge

Windows 桌面调度与 Unreal 编辑器桥接。目标 **UE 5.8.x**；基础场景导出适配 Blender 4.3.x，完整雾与光影转换使用 **Blender 5.2**。

## 三部分职责

| 部分 | 当前流程 |
|---|---|
| 角色通路 | 顶部“导入角色”：GUI 默认 MMDBridge Mode 1 自动烘焙并导出角色双 FBX；旧 Blender / FBX 和 MMD2Unreal 通路保留 |
| 场景通路 | 顶部“导入场景”：仅处理 `.blend` 的最终求值场景 → 分层 USD → UE |
| 镜头通路 | 顶部“导入镜头”：仅处理镜头 VMD → 逐帧采样 → UE Level Sequence，不依赖角色或场景 |
| CameraCutAuto | 独立镜头处理入口，沿用 MMDBridge 安装结构，不依赖角色导出任务 |
| PhysicsBakeFix | 独立 PMX 预处理入口，检测并补齐物理骨显示枠，不依赖角色导出任务 |

角色材质、骨架、IK、Grant 与角色物理由上游负责，本项目不重新生成或替换角色材质。旧 Blender/FBX 任务可恢复，见[历史说明](docs/LEGACY_FBX.md)。历史 UE 5.7.4 结果不代表 UE 5.8 新流程验收通过。

## 安装上游依赖

UE 侧需要安装 [MMD2Unreal](https://github.com/BlackLnt-OxO/MMD2Unreal) 和匹配 UE 5.8 的 [KawaiiPhysics](https://github.com/pafuhana1213/KawaiiPhysics)。检测缺失或未启用时，GUI 提供“下载安装”。请先保存并关闭目标 UE 工程，完成后重新打开并连接。

安装器只从官方 GitHub 下载插件，安装到所选工程的 `Plugins` 目录并启用；不执行 git clone 或编译。MMD2Unreal 使用已验证提交中的预编译插件，KawaiiPhysics 使用 UE 5.8 发行包。检查 DLL 和引擎 BuildId；已有插件不覆盖，不兼容则明确报错。修改工程描述前保留备份，下载与回执保存在 `%LOCALAPPDATA%/MMDBridge/plugin-downloads`。

也可单独执行（发行包中的脚本位于程序旁）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_upstream_plugins.ps1 -Project "D:/YourProject/YourProject.uproject" -Unreal "C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
```

来源、提交与发行包见[依赖登记](docs/upstream-lock.json)。程序发行包不捆绑上游插件。`init_upstream.ps1` 为旧的开发者源码获取入口，不用于用户插件安装。

## 启动与导入

```powershell
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

顶部三个导入按钮分别创建任务。已填写的其他类型素材不会加入当前任务；错误与待复核项统一显示在“任务与日志”，并写入任务清单和 UE 报告。
场景灯光只导出 Blender 渲染可见集合中的灯；“场景灯光亮度倍率”默认 0.15，用于校准 Blender 与 UE 的曝光差异。

下面是需要 UE 的旧 MMD2Unreal 角色通路；独立 FBX 通路见下一节。

1. 选择 UE 5.8、项目及 PMX、VMD 或 Blender 场景。仅相机也可创建任务；MMD2Unreal 角色/相机通路不需要 Blender。
2. 选择“MMD2Unreal（Legacy）”。模型缩放、导入帧率和相机构图由上游窗口控制；桌面帧率用于组合序列，MMD 缩放与相机 XYZ 参数仅作用于历史 FBX 通路。
3. 安装 MMDBridge 桥接，保存并重启 UE 后连接。缺少上游插件时可使用下载安装提示。
4. 点击“导入角色”。UE 将 GUI 已选文件的完整路径传给上游 Factory，依次弹出模型、动作、口型的官方设置窗口，不需要再次选择文件。动作窗口选择刚导入的角色。
5. 工具以实际生成资产确认成功，并保存逐文件回执。取消后可恢复；缺失插件、错误骨架或无法确认的中断资产会明确阻塞，不自动覆盖。
6. 查看任务目录 `ue_report.json`。独立表情若不是同骨架叠加动画，将保留资产并标记需在 UE 配置混合，不会静默覆盖身体动作。

上游结果位于 `<内容目录>/Upstream_<任务ID>`；自建场景、关卡与组合序列位于 `<内容目录>/A_<英文任务名_序号>`。源 PMX、VMD、`.blend` 不覆盖保存。

任务状态区分等待用户导入、导入完成、取消、上游阻塞、失败和待复核。旧任务没有 `character_backend` 时继续按历史 FBX 恢复。源输入哈希变化必须新建任务。

## MMDBridge Mode 1 → FBX

GUI 默认选择 **MMDBridge Mode 1 → FBX**。填写原始 PMX、动作 VMD、可选口型 VMD、MMDBridge 安装目录，选择 Blender 4.3.x 后点击「导入角色」。「临时中转目录」可留空：所有导入流程默认在桌面创建 `Middle_角色名.场景名_01`，名称取文件名去掉扩展名，只选场景或镜头时取其名称，重名编号递增；选择目录时直接使用该目录。job、断点、日志和中间导出都存放在中转目录内。安装目录须包含 `MikuMikuDance_CHS.exe`、`d3d9.dll`、`python312.dll`、`mmdbridge_scripts/mmdbridge_vmd_mode1.py` 和对应配置。运行时 Windows 必须保持登录且桌面未锁定；其他电脑使用发行包或安装 `pyproject.toml` 中的依赖，无需 AI 逐步查看窗口。

程序在所选目录的 `MMDBridge_Jobs/<英文任务名_序号>` 中生成骨名及显示枠修复副本、AVI、烘焙 VMD、逐阶段日志和来源回执。独立 MMD 进程先烘焙 0–60 帧短段，再烘焙全段；校验通过后，Blender 才能读取该任务新生成的 VMD。在所选目录直接导出 `<任务前缀>_Model.fbx`、`<任务前缀>_Animated.fbx`。任务目录保存独立恢复快照，源文件和安装目录不被修改。异常对话框、超时、旧输出和不完整文件均报告失败。

底部绿色按钮支持角色、场景、镜头独立导入；最左侧“走全流程”使用同一个任务自动串联三部分，并自动导出 FBX。角色阶段使用已检测到的 Blender 4.3，完整场景光影阶段使用 Blender 5.2，实际路径写入任务日志。停止会立即中断当前任务进程并保留已落盘还原点，恢复仅重做未完成步骤。USD 场景的网格恢复和小物体碰撞筛选见 [USD 场景教程](docs/USD_SCENE_TUTORIAL.md#导出进度与停止后恢复)。

## 场景材质

本项目负责 Blender → UE 的材质管线自动化：为常规资产生成可编辑的 UE PBR 材质和贴图，并为复杂材质选择直接映射、局部烘焙或人工复核策略。不承诺跨渲染器逐像素还原；最终视觉效果与运行性能由 UE 材质、光照和项目性能预算验收。

- 简单材质优先：基础 PBR 常量、静态贴图及受支持的简单计算直接在 UE 还原，保留原贴图和 UV，不因物件小而烘焙。
- 复杂小物件：独立 UV 烘焙 PBR 通道，保留透明和法线。
- 大面积平铺表面：保持基础 UV 平铺，在 UE 还原可支持的计算节点；无法翻译的遮罩或通道局部烘焙。
- 平面遮罩使用同源材质共用的世界投影范围，基础贴图与遮罩各用自己的 UV；非平面采用非重叠图集，接缝需视觉验收。
- 默认使用工具材质。启用用户母材质选项后，仅参数完整匹配时创建实例，不修改 `M_00` / `MF_PSR` 原资产。
- 玻璃、半透与镂空按有效闭包和独立 Alpha 分流；薄玻璃的实时预览与 PT 保留不同折射输入。节点覆盖、近似和验证见[玻璃节点说明](docs/glass_nodes_validation_20261011.md)。
- 颜色贴图保留 sRGB；粗糙度、金属度、遮罩等标量贴图按线性数据导入 UE，关闭 sRGB、使用 BC7，并读取对应的单通道。法线贴图沿用 UE 的法线压缩设置。

分类规则、覆盖和限制见 [USD 教程](docs/USD_SCENE_TUTORIAL.md)。测试场景为 MyGO 停车场；复杂闭包近似和大表面局部烘焙不会仅因导入成功就标为视觉通过。

## CameraCutAuto

同名入口执行 Probe → Detect → 人工复核 → Bake → Verify。MMDBridge 插件名不变，旧 `CAMCUT_*` 配置、生成标签和备份目录保留兼容。

已有上游多段 Camera Cuts 会被保留并跳过重新拆分，报告结构检查结果；这不等于 MRQ 子帧验收通过。混合正交投影采用普通 Camera 的 ProjectionMode、OrthoWidth、FieldOfView 轨道；若采样中才发现预检未捕获的正交切换，会停止而非用透视近似代替。

## PhysicsBakeFix

部分 PMX 模型的裙子、头发、饰品等骨骼由 Bullet 刚体驱动（Physics Mode 1/2），却没有被登记进任何**表示枠 / Display Frame**。这类模型在 MMD 与 MMM 中实时物理正常，但 MMM 的 Timeline 看不到这些骨骼，导致物理焼き込み无处写入关键帧，物理动画在导出 VMD 后丢失。

该入口独立于角色导出任务，不加入“开始流程”：

1. 选择 `.pmx` → **检测**（只读，不写任何文件）
2. 人工复核检出的缺失骨骼
3. **修复** → 输出 `<原名>_PhysicsBakeFixed.pmx`

判定完全基于 `RigidBody → Related Bone → Physics Mode`，不使用骨骼名称；已存在于任意显示枠的骨骼不会被重复添加，原有显示枠条目不被移动、删除或重排。输出始终为新文件，原始模型不被覆盖。

修复后的 PMX 交给 MMM 完成物理焼き込み，再进入既有的 Blender / mmd_tools → FBX → UE 流程。

## Plan V2 文件级入口

`scripts/plan_v2.py prepare --pmx <模型.pmx> --motion <动作.vmd> --lipsync <表情.vmd> --job <任务目录>` 在独立任务目录生成处理后的 PMX/VMD、骨骼映射和 `prepared.json`。旧的 Mode 2 文件级手动入口仍可用；GUI FBX 通路使用上方的 MMDBridge Mode 1 自动烘焙。

## 开发与验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build.py
```

打包后启动 `dist/MMDBridge/MMDBridge.exe`，整个目录一起移动。当前检查结果和待完成项目见[验收记录](docs/VALIDATION.md)。源码采用 GPL-3.0-or-later，历史依赖见 [THIRD_PARTY.md](THIRD_PARTY.md)；外部插件遵循各自上游授权。

## 场景雾与光影

新场景任务默认启用程序化雾与光影转换，运行时无需 AI。部署要求、参数、限制和真实验证方法见 [SCENE_APPEARANCE.md](docs/SCENE_APPEARANCE.md)。
