# MMD Bridge

Windows 桌面调度与 Unreal 编辑器桥接。目标 **UE 5.8.x**；Blender 场景导出适配 4.3.x。

## 三部分职责

| 部分 | 当前流程 |
|---|---|
| 主项目一阶段 | 桌面预检、任务恢复与 UE 连接；角色 PMX、动作/表情 VMD、相机 VMD 交给 MMD2Unreal 官方窗口导入 |
| 主项目二阶段 | Blender 最终求值场景 → 分层 USD → UE；自建场景材质、实例和精确碰撞 |
| CameraCutAuto | 独立镜头处理入口，沿用 MMDBridge 安装结构，不依赖角色导出任务 |

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

1. 选择 UE 5.8、项目及 PMX、VMD 或 Blender 场景。仅相机也可创建任务；仅角色/相机不需要 Blender。
2. 默认使用“MMD2Unreal 官方窗口”。模型缩放、导入帧率和相机构图由上游窗口控制；桌面帧率用于组合序列，MMD 缩放与相机 XYZ 参数仅作用于历史 FBX 通路。
3. 安装 MMDBridge 桥接，保存并重启 UE 后连接。缺少上游插件时可使用下载安装提示。
4. 开始流程。场景由独立 Blender 进程导出；UE 将 GUI 已选文件的完整路径直接传给上游 Factory，依次弹出模型、动作、口型、相机的官方设置窗口，不需要再次选择文件。当前文件名和路径显示在任务状态与 UE 日志。动作窗口选择刚导入的角色。
5. 工具以实际生成资产确认成功，并保存逐文件回执。取消后可恢复；缺失插件、错误骨架或无法确认的中断资产会明确阻塞，不自动覆盖。
6. 查看任务目录 `ue_report.json`。独立表情若不是同骨架叠加动画，将保留资产并标记需在 UE 配置混合，不会静默覆盖身体动作。

上游结果位于 `<内容目录>/Upstream_<任务ID>`；自建场景、关卡与组合序列位于 `<内容目录>/A_<稳定ID>`。相机组合使用副本，保留原序列。源 PMX、VMD、`.blend` 不覆盖保存。

任务状态区分等待用户导入、导入完成、取消、上游阻塞、失败和待复核。旧任务没有 `character_backend` 时继续按历史 FBX 恢复。源输入哈希变化必须新建任务。

## 场景材质

- 简单材质优先：基础 PBR 常量、静态贴图及受支持的简单计算直接在 UE 还原，保留原贴图和 UV，不因物件小而烘焙。
- 复杂小物件：独立 UV 烘焙 PBR 通道，保留透明和法线。
- 大面积平铺表面：保持基础 UV 平铺，在 UE 还原可支持的计算节点；无法翻译的遮罩或通道局部烘焙。
- 平面遮罩使用同源材质共用的世界投影范围，基础贴图与遮罩各用自己的 UV；非平面采用非重叠图集，接缝需视觉验收。
- 默认使用工具材质。启用用户母材质选项后，仅参数完整匹配时创建实例，不修改 `M_00` / `MF_PSR` 原资产。

分类规则、覆盖和限制见 [USD 教程](docs/USD_SCENE_TUTORIAL.md)。测试场景为 MyGO 停车场；复杂闭包近似和大表面局部烘焙不会仅因导入成功就标为视觉通过。

## CameraCutAuto

同名入口执行 Probe → Detect → 人工复核 → Bake → Verify。MMDBridge 插件名不变，旧 `CAMCUT_*` 配置、生成标签和备份目录保留兼容。

已有上游多段 Camera Cuts 会被保留并跳过重新拆分，报告结构检查结果；这不等于 MRQ 子帧验收通过。混合正交投影采用普通 Camera 的 ProjectionMode、OrthoWidth、FieldOfView 轨道；若采样中才发现预检未捕获的正交切换，会停止而非用透视近似代替。

## 开发与验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build.py
```

打包后启动 `dist/MMDBridge/MMDBridge.exe`，整个目录一起移动。当前检查结果和待完成项目见[验收记录](docs/VALIDATION.md)。源码采用 GPL-3.0-or-later，历史依赖见 [THIRD_PARTY.md](THIRD_PARTY.md)；外部插件遵循各自上游授权。
