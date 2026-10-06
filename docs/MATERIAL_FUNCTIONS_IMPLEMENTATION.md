# 通用材质转换与 GPU 稳定性实现记录

本次代码在原 Qt 程序中实现，转换时不调用 AI。测试目标为用户确认的 `F:\Ue5_Project\TESTON1_1005_02\TESTON1_1005_02.uproject`。原 `TESTON_1005_Clean` 的 `MMD_Preview` 与新导入结果分别验收。

## 已实现的转换

- 按有效输出、稳定插槽标识符、静音旁路、嵌套组和数据依赖分析材质。组名及预设名不作为转换条件。
- 数值、颜色和 Shader 组保留函数定义／调用关系；按版本化语义签名共享函数，资源从调用方传入。兼容的闭包图共享母材质，差异放在材质实例中。
- 混合及相加闭包使用 Substrate；不能表达时报告失败，不按连接数量选一个 Principled 代替。
- 复杂色带／RGB Curves 由 Blender 求值为共享的小型查找表。原生对照包含 Cardinal、组合曲线、独立通道及边界。
- 源对象随机值、岛随机值和顶点色分别保留。普通网格使用 Custom Primitive Data；实例使用 PerInstanceCustomData，经顶点插值进入普通网格的像素着色器。岛值使用独立或打包的 UV 数据。
- Generated 使用原对象的 texture space 和原始坐标，打包为两个 UV 通道。仅启用原生对照通过的 `DISPLACE`／`SIMPLE_DEFORM` 形变适配；要求原始拓扑及顶点索引一致。未知拓扑修改器和 Shape Keys 仍明确拒绝。
- 不兼容的静态空间字段局部缓存。含视角／动画依赖的字段不静态烘焙。世界或实例相关字段把变换／身份计入缓存键；不因出现 Noise 就隔离所有实例。
- 场景 `auto/nodes/hybrid` 不因局部转换失败而整材质烘焙。完整烘焙需要显式 `bake`。失败项保留源预设描述并标记 `needs_review`。

UE 5.8 原生 USD 导入器只保留 **4 个 UV 通道**。分析及局部缓存阶段保留 Blender 的源通道，最终按有效材质图（包括可达函数及调用参数）打包为 UE 通道，并重新生成受影响函数的语义签名。原始 UV 留在源文件和冻结断点；未使用通道不写入最终 USD。确实需要超过四个有效通道时明确报告。

## 导入、渲染及恢复

同一冻结 depsgraph 内复用材质依赖分析，避免数万实例反复扫描相同图。缓存不跨帧或后续节点编辑。分析、转换、独立性能采样、后台渲染及恢复在原 GUI 中分别控制。

纹理逐项导入，材质分批保存及释放临时引用，着色器工作进程和异步资产构建并发受限。材质、函数、纹理及外观资源记录磁盘哈希，检测手工修改和未保存修改后拒绝覆盖。树叶覆盖率设置引用材质实际使用的内容版本贴图，保存后更新登记哈希。清单和大场景检查点可使用紧凑 JSON；旧清单／检查点仍可读取。

UE 材质后端版本为 8。复用资产时不再重复写相同元数据，以免 UE 将共享母材质或贴图标为未保存而误报用户修改。旧后端生成的材质实例会重建；所有权及修改保护仍执行。Mapping 使用 XYZ 变换，支持可编辑的旋转输入。

修正版薄表面 Translucent BSDF 使用 Substrate Two-Sided Wrap，并通过 Transmittance-To-MFP 将透光颜色转换为散射距离；辅助节点与 Slab 共用 UE 的默认厚度，采用原生 TwoSidedFoliage 的单位转换约定。遮罩、混合权重和法线独立保留，不增加字段烘焙。`thin_surface_scattering=legacy_diffusion` 保留原映射用于明确回退；模式参与函数及母材质缓存标识，复用时核对模式。薄表面母材质同时声明 Nanite 和实例网格用途，避免独立渲染使用默认替代材质。外观仍需视觉复核。

薄叶 A/B 对照脚本在同一已保存场景中，以独立进程的组件材质覆盖运行 current/corrected/corrected/current，每轮 80 帧、2560×1080、硬件 Lumen 和光追阴影。两组使用相同用途标记；CSV 仅选择存在场景 GPU 绘制通道的记录，再剔除 16 个预热样本，排除 MRQ 等待写图时的空闲帧。GPU 中位耗时增加超过 10% 时保留原方案。该固定候选对照不表示逐复杂图签名自动校准已经完成。

2026-10-06 railway 实测：RTX 4060 Laptop / UE 5.8.2，原方案两轮 GPU 中位耗时分别 238.3645、238.7032 ms，修正方案分别 323.8772、326.8360 ms；合并比较增加 86.8228 ms（36.40%）。专用显存峰值原方案 6990 MB／修正 6974 MB，共享显存 1609 MB／1632 MB。全部四轮各 80 帧成功，外观仍为 `needs_review`。按用户要求暂不激活修正材质，程序／历史设置缺省继续采用 `legacy_diffusion`；Qt 场景参数增加“叶片透光方案”选择，允许显式使用修正版。记录见 `work/material_functions_implementation/leaf_transmission/comparison.json`，第 20 帧原图分别为 `railway_current_0020.png` 和 `railway_corrected_0020.png`。

距离保护只作用于本任务拥有的遮罩网格、组件和贴图：关闭组件距离／密度缩减，设置 Nanite Preserve Area，并对直接 Alpha 遮罩保留 mip 覆盖率。USD 重载和独立渲染时重新应用组件设置。HISM 的屏幕面积剔除和复杂 Shader 遮罩仍需单独验证，不能宣称所有远景覆盖率均保持不变。

薄表面判断增加材质所在连通表面的拓扑证据。完整草丛虽然有体积，各草叶仍可为开放薄片；检查完整网格再按材质选取连通表面，避免把闭合实体的单个材质面误判为薄片。非流形和混合闭合表面不据此启用薄表面散射；显式 Glass／Water 通路保留。分析修订号为 11，旧冻结几何可复用，材质分析会失效重做。railway 两种草原生分析分别通过 32／18 个开放表面，使用 Masked 加双面散射，保留 Opacity Mask 与透光输入，没有新增烘焙。实际 UE 阴影出图尚待验证，用户已修改的 UE 材质未覆盖。证据：`grass_shadow/blender_result.json`。

后台默认独立 UE 进程，复制 MRQ 预设到临时作业，不修改原预设。显式设置 Game Overrides、纹理流送与资源预算，并记录实际 CVar。专用显存、共享 GPU 内存及当前 UE 的 DXGI 本地预算分别判断；90% 停止，严格低于 80% 才恢复。系统 RAM 只记录，不作为中止依据。纹理池上限不代表整个 GPU 的硬上限，原生单次分配仍可能先于采样超限。

生成的 SourceLook 预设使用任务分辨率，运行时契约及实际 PNG 均校验尺寸；用户选择的自定义预设保留其分辨率。

程序生成的 MRQ 预设统一放在任务的 `Sequences` 文件夹，材质和 OCIO 资源仍放在 `Materials`。旧目录引用及恢复记录继续兼容。保存的预设自带唯一的有效 Deferred／PathTracer 渲染通道和 PNG 写出；切换渲染管线时移除相反的通道，避免重复渲染。旧 SourceLook 曾只保存输出、颜色与控制设置，缺少实际渲染通道及图像写出；独立运行时临时补齐掩盖了手动 MRQ 不出图的问题。原生检查已确认该遗漏。

已为完整 railway 的 `Sequences` 保存 `MRQ_SourceLook`、`MRQ_Lumen`、`MRQ_PathTracing`、`MRQ_Cypress` 四个预设，各有独立的 `Saved/MovieRenders/railway/<预设名>` 输出目录。它们均为 2560×1080、PNG 和 MoviePipelineGameMode；原有预设、关卡及材质没有覆盖。Lumen 与 SourceLook 保留源色彩配置，Cypress 保留已能出图的基础配置，PathTracing 使用对应渲染通道。此次只验证 UE 配置及资产保存，未重渲染完整场景。证据：`railway_presets/original_configs.json`、`railway_presets/result.json`。

默认生成的 SourceLook、Lumen／PathTracing 和 Cypress 测试预设均明确设置 Game Overrides 为 `MoviePipelineGameMode`，在运行时隐藏玩家 Pawn 和 HUD。Cypress 旧预设此前没有 Game Overrides，手动 MRQ 渲染落到 GameModeBase 时会拍到默认球形 Pawn；既有预设需在 MRQ 中补上并保存这个设置。UE 5.8 原生 Python 命令行已验证两条渲染管线的 GameMode 类配置，验证只使用临时对象，不保存用户资产，也未重渲染用户图片。证据：`mrq_game_mode/result.json`；相关 Python 回归 22 项通过。

恢复核对工程、源资源／材质依赖、序列、关卡、配置及运行时哈希。PNG 验证尺寸、CRC、解压完成和扫描行。有效帧不覆盖，半写文件隔离保存，仅渲染缺失范围。

## 原生证据

证据目录：`work/material_functions_implementation/`。

当前 Python／Qt 回归结果为 **603 passed、3 skipped**（20.42 秒），包含请求分辨率不符时拒绝验收、共享资源复用不误标为用户修改、实际版本贴图的编辑保护、开放薄片材质的遮罩／散射通路，以及保存预设的渲染通道切换／PNG 输出。原生 Blender 另验证闭合实体的材质面、混合开放／闭合网格及非流形拒绝。这些测试与以下 UE 原生证据分别记录。

| 验证 | 结果 | 回执 |
|---|---|---|
| 任意名称、重复插槽名、嵌套函数及 railway 树木 | 7 个材质、26 个函数变体、6 个母材质通过 UE 检查 | `ue_result.json` |
| 色带／曲线 Cycles 与 UE GPU 对照 | 12 个对照通过 | `curves/ue_result.json` |
| 对象随机值、实例值、岛载体、函数内 UV6→UV1 打包及保存重载后实际 GPU 像素 | 通过；普通对象 .1、实例 .8、独立载体 .2 | `shader_data/ue_gpu_result.json` |
| Generated、负缩放和 USD 保存重载 | 通过 | `generated/result.json`、`generated/ue_reload_result.json` |
| 位移、简单形变及组合的 Cycles ORCO 对照 | 最大误差 < 2e-7 | `generated/deform/result.json` |
| Cypress 独立渲染，2560×1080，硬件 Lumen、光追阴影，两次镜头切换 | 300 帧全部有效，无 GPU 崩溃 | `tree_300_result.json` |
| 更新后的三次独立冷启动单帧渲染 | 三次通过；单帧不作为稳定 GPU 成本测量 | `tree_cold_result.json` |
| 真正中断后恢复 | 41 帧保留，补齐至 300 帧，原帧哈希未变，半写文件保留 | `tree_resume_result.json` |
| 完整 railway 重新导出与 UE 导入 | 59 个材质、42,730 个源实例；导入失败 0，保留视觉复核状态 | `full_scene/export_summary.json`、`full_scene/import_result.json` |
| 完整 railway 独立进程参考帧 | 实际 PNG 为 2560×1080，硬件 Lumen、光追阴影生效，无 GPU 崩溃 | `full_scene/render_result.json` |

上述 300 帧测试为单棵 Cypress 小场景，不能替代完整 railway 或原崩溃的验收。GPU CSV 记录来自 UE 的 RHI GPU 时间，不使用 CPU FrameTime；不足 16 个有效输出帧不作为稳定场景测量。已有 300 帧回执的中位数约 25.16 ms 包含引擎采样窗口的预热／收尾，不能当成逐材质成本，也没有公平的旧方案性能对照。

完整 railway 记录为 369 个原型、32,736,249 个原型三角面、156,845,255 个实例化三角面，29 个 USD payload；本次未形成 scatter 合批。132 个字段引用复用 10 个字段图像，导出无 `material_error`，33 个材质完成 UV 重排。导出恢复用时 993.151 秒，导入恢复用时 430.037 秒，材质阶段 61.118 秒；均为带缓存的恢复运行，不是公平的新旧性能对照。

完整参考帧采样峰值为专用显存 6,597 MB、共享 GPU 内存 2,049 MB，当前进程 DXGI 本地使用 5,341.711 MB。单帧不能证明完整动画稳定，也不提供稳定 GPU 耗时。运行时恢复 2,253 个遮罩组件；35 个复杂遮罩材质保持 `needs_review`。源相机景深和场景作者文字几何保留，不能用该参考帧代替近／远距叶片覆盖率测试。首次输出为 1920×1080，未计入目标分辨率验收；修正输出设置后重跑的结果才是本表中的完整参考帧。

原工程最新日志显示 Movie Render Graph、AnyHit GPU 页错误，本地使用报告约 13,486 MB／预算 7,188 MB。这证明预算压力明显，尚不能证明具体材质或单一内存不足就是根因。证据：`original_crash_readonly.json`。目前只读诊断，不据此宣布修复。

## 尚未完成的计划项

1. 关闭分支目前常量折叠，并保留完整源预设描述；**重新启用需重新导出**。可直接在 UE 材质实例中重新启用的完整静态开关预设尚未实现。
2. 已有确定性候选筛选／校准缓存及独立场景 GPU 采样，但**逐复杂签名自动生成并测量实时／缓存候选**尚未接入。Blender Noise 实时兼容版本未获语义验证，当前使用局部缓存并标记估算，不声称 UE 同名 Noise 等价。
3. 完整场景已重新导出、导入并渲染参考帧；同条件旧新成本比较、复杂遮罩的近／远距覆盖率、完整场景三次冷启动及 300 帧片段、原崩溃帧分项隔离、完整动画和跨渲染器人工视觉验收仍待完成。所有视觉结果保持 `needs_review`。
4. 材质及外观资源已有编辑保护；关卡、序列和 USD 自动生成几何的全部用户编辑保护仍需补齐，不能把当前哈希登记描述为覆盖全部 UE 资产。

这些限制意味着本计划仍在实施，不能将局部函数生成成功或小场景出图称作全部验收通过。
