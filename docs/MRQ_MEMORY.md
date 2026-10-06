# Lumen MRQ 显存策略

均衡、低显存、画质优先保留电影级总开关、1×1 TSR、硬件光追阴影和命中光照。场景还原页的显存策略会随配置与任务保存，旧任务没有这个字段时默认均衡。参考帧渲染在加载大场景前先应用所选预算，MRQ 开始后继续覆盖。切换策略会移除上一档留下的预算，路径追踪不使用这些 Lumen 预算。另有明确标注的可选软件兼容档；当前测试不采用此档。

| 参数 | 原电影级缓存 | 均衡（默认） | 低显存 |
| --- | --- | --- | --- |
| 纹理池 MB | UE 5.8 Cine 3000 | 2048 | 1536 |
| TSR 历史比例 | 200 | 100 | 100 |
| Lumen Surface Cache 图集 | 4096 | 2048 | 1024 |
| Surface Cache 单卡上限 | 512 | 256 | 128 |
| 渲染目标缓存最低保留 MB | 1000 | 400 | 300 |
| 虚拟阴影物理页 | 8192 | 4096 | 4096 |
| 反射降采样因子 | 原设置 | 原设置 | 2 |
| 光追几何驻留软预算 MB | 原设置 | 1024 | 768 |
| 每帧动态光追几何更新图元预算 | Cine -1（不限） | 100000 | 30000 |

原电影级数值来自本机 UE 5.8 BaseScalability.ini。均衡策略不减输出分辨率、不减植被密度、不关阴影、也不强制贴图统一降 mip。TSR 的细节稳定性、间接光缓存精度及阴影页预算仍需视觉复核。低显存会进一步降低间接光、反射与景深的内部计算分辨率。纹理池不是总显存硬上限，非流送资源、光追结构和其他进程仍占显存；缩小缓存可能增加更新或流送开销。

脚本 `scripts/ue_prepare_memory_presets.py` 可以在 UE Python 命令脚本中生成自有预设；通过环境变量 `MMDBRIDGE_MRQ_JOB` 指定任务目录，`MMDBRIDGE_MRQ_PROFILES` 指定档位，`MMDBRIDGE_MRQ_PRESET_SUFFIX` 指定名称后缀。`MMDBRIDGE_MRQ_UPDATE_DEFAULTS=0` 保留已有源配置和参考配置。它不会启动渲染或加载场景。可用 NullRHI 准备预设，但真实渲染不能使用 NullRHI。

2026-10-05 已在 TESTON_1004 中保存并原生核对三档预设：

- `MRQ_Lumen_Balanced`：先尝试此档。
- `MRQ_Lumen_Low`：均衡仍超出显存时手动选择。
- `MRQ_Lumen_Quality`：保留电影级缓存配置。

它们位于当前任务的 `Materials` 目录。记录在 `work/test1005_optimization/ue_cinematic/memory_presets.json`。预设准备验证不代表已经成功出图或已经测得节省的总显存。

## 独立进程渲染

保存并关闭当前 UE 工程后，可以运行 `work/test1005_optimization/render_low_vram.ps1`。默认均衡，可传 `-Profile low`。入口检查该工程没有其他 UE 进程后才启动；不会自动关闭编辑器。当前测试预设沿用已有参考任务的单帧范围，输出到该任务 `renders/<profile>`。独立运行使用已保存的场景和序列，不执行工具 PIE 回调中的临时相机调整或玻璃隐藏操作。

通用入口是 `scripts/render_ue_standalone.py JOB --profile balanced --run`，不带 `--run` 只生成命令计划。它用 `UnrealEditor-Cmd -game -RenderOffscreen` 调用已保存的 MRQ 预设，保留纹理流送，不重复保留编辑器场景。它仍然使用显卡，没有将 Lumen 改为 CPU 渲染，也没有承诺总显存不超预算。

MRQ 高分辨率分块可降低部分图像缓冲占用，但不支持时间抗锯齿及部分屏幕空间效果，因此没有自动用于当前 TSR/Lumen 流程；也不能解决全部几何与光追结构占用。增加页面文件不等于增加独立显存。

## 导入时减负

材质页的“场景导入策略”提供完整还原和渲染减负。旧配置默认为完整还原；渲染减负保留几何、UV、材质节点、树叶透明轮廓及普通灯光/高度雾，省去精细物理碰撞和稀疏网格体积。相同源父物体下、相同几何/材质引用、具有静态正尺度 TRS 变换的重复散件（至少 16 个）改用 USD PointInstancer，UE 使用原生实例组件。单独物体、带剪切/反射变换或单独可见性/材质绑定的物体保留原路径。原散件名字和世界矩阵保留在清单中，以实例索引代替每散件独立 Actor。

该策略还将生成的浮点烘焙贴图在 GPU 上限制为最大 1024，源 EXR 文件保持不变，带符号线性值和 HDR 压缩设置保持不变。原始 PNG 树叶颜色/透明贴图、法线和世界环境图不应用该限制。程序纹理细节有变化，需复核；切回完整还原会恢复工具记录的原尺寸限制。普通纹理池不足以控制所有浮点数据贴图的驻留占用。

该策略不关闭硬件光追。物理碰撞和光追加速结构独立：省去碰撞能减少 CPU 数据和物理查询负担，但不会直接移除光追几何。批次合并保持实例数量和三角形数量；是否降低总 GPU 成本必须以真实渲染验证。

2026-10-05 已在 `MMD_Preview_RenderOptimized` 完成一次 1280×540 单帧独立渲染，使用 `LS_MMD_RenderOptimized_Reference01` 和 `MRQ_Compact_Hardware`：1×1 TSR、电影级总开关，硬件 Lumen、光追阴影及命中光照均从 MRQ 日志核对为 1。326 个批次容纳 42,295 个实例，保留其余独立对象；复核包含原型变换的实例矩阵，最大误差为 2.32e-5 源单位。按任务要求在诊断 USD 覆盖层禁用玻璃物体，未修改源 Blender 文件。

每约 4 秒采样的适配器总使用量最高为独立显存 7,604 MiB、共享 1,175 MiB、同一采样时刻合计 8,716 MiB；三个数值的峰值可能不同时发生，并且包含其他进程。UE 原生进程计数器峰值工作集 8.49 GiB、提交量 17.93 GiB；提交量不是全系统页面文件实际占用。未发生 D3D 设备移除，但只验证了该参考帧，仍标记 `needs_review`。本次复用的旧资产仍保留 BodySetup 数据，仅关闭组件碰撞；新减负导出才会跳过精细碰撞数据生成。原始对照场景和手工 MRQ 队列保留。

证据位于 `work/test1005_optimization/ue_compact_hardware/standalone_visual/ue_render.json`、`render_memory_samples.json` 和 `scatter_shape_check.json`。本帧仍存在与 Blender 的光照/叶片观感差异，应继续视觉复核，不能把成功出图视为完整外观还原。独立渲染曾触发仅用于编辑器的标签恢复回调；现已在无编辑器子系统的进程中禁用编辑器邮箱启动。

另用原生 UE NullRHI 只读核对全部 42,729 个可见实例（排除玻璃），包含 434 个未合批的独立物体。独立物体的最大相对缩放误差为 1.13e-7，合批实例最大为约 0.1015%；未发现放大叶片的实例缩放错误。证据在 `ue_instance_scale_check.json`，这项尺寸验证不能替代材质透明度、光照与景深的视觉核验。

来源：[Epic MRQ 设置](https://dev.epicgames.com/documentation/en-us/unreal-engine/cinematic-rendering-image-quality-settings-in-unreal-engine)、[TSR](https://dev.epicgames.com/documentation/en-us/unreal-engine/temporal-super-resolution-in-unreal-engine)、[纹理流送配置](https://dev.epicgames.com/documentation/en-us/unreal-engine/texture-streaming-configuration-in-unreal-engine)、[MRQ 命令行接口](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Plugins/MovieRenderPipelineCore/FMovieRenderPipelineCoreModule/ParseMovieRender-)
