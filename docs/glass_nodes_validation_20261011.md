# 玻璃、半透与遮罩节点修复

上一轮植物识别与 UE 当前选择分析已经提交到 main：`aa17e05`。本轮更新默认场景材质转换；已有地图、原材质和手工植物覆盖不自动迁移。

## 分支依据

先展开有效节点组、组输入和重路由，剔除确定未参与输出的 Mix 分支，再分别判断覆盖率、漫透射与折射透射。植物名称不能单独决定分支。

| 源表面 | 转换行为 | 限制 |
| --- | --- | --- |
| 白色 Transparent 与其他 BSDF 的 Mix | 独立覆盖率，移除空洞闭包，归一化剩余 BSDF 权重 | 常量部分覆盖率采用半透；旧薄片散射策略保留遮罩 |
| Principled Alpha，包括贴图与混合树的局部 Alpha | 覆盖率单独传递，避免混合权重和 Alpha 重复衰减 | 贴图遮罩的裁切阈值需要视觉复核 |
| Translucent BSDF | 薄片漫透射，与孔洞遮罩分开 | legacy_diffusion 是近似，不是玻璃折射 |
| Glass BSDF / Principled Transmission | 保留透射、颜色、粗糙度和源 IOR | Blender 与 UE 的微表面/介质响应需要复核 |
| Refraction BSDF，包括节点组 | 保留 IOR 与透射 | UE Slab 同时具有 Fresnel 反射，明确标记近似 |
| 有色或纹理驱动的 Transparent BSDF | 零 Fresnel 的直通透射，不作为玻璃 IOR 判据 | UE 介质吸收的厚度/角度响应与 Blender 不完全等价 |
| Glass 与 Diffuse 的 Add | 保留闭包相加 | 渲染器能量响应需复核 |
| Transparent 空洞或局部 Alpha 的 Add | 显式报告不能等价为单一覆盖率 | 不将其静默当成普通玻璃或已完成还原 |

## 薄玻璃与 PT

按材质覆盖的顶点拟合玻璃自身方向，避免斜放薄板被世界轴包围盒误判为厚玻璃。泳池原始文件中检查的 8 个玻璃网格符合薄板条件，测得厚度约 1.4–2 厘米。

不能直接把材质的 Refraction Method 改成 PNO：UE 5.8 的 PathTracingMaterialHitShader 会据此关闭 PT 折射。最终方案保留 Index Of Refraction 方法，仅切换折射输入：薄板 Raster 使用 1，Ray Traced 与 PT 使用源 IOR。反射 BSDF、F0/F90、粗糙度及颜色保留。曲面、折角和厚玻璃继续使用源 IOR。

薄板 Raster 的屏幕位移被取消，法线贴图的折射位移细节也是近似；其反射法线与 PT 光学传播保留。Raster 仍不能等价实现多层介质传播，不能承诺任意立体玻璃的交界伪影全部消失。

## 验证证据

- `scripts/blender_glass_nodes_regression.py` 在实际 Blender 5.2.2 中验证 13 个组合，包含组输入 IOR、重路由、贴图 Alpha、局部 Alpha 混合及无效玻璃分支。
- 几何用例在 Blender 内置 NumPy 中运行，检查旋转薄板、其他材质槽干扰与折角；桌面 Python 没有 NumPy 时只跳过该模块。
- 原始泳池植物回归仍为镂空遮罩/薄片透光，未进入折射分支；原 blend 文件未写入。
- 实际 UE 编译、保存、缓存复用与源玻璃构建结果：`work/glass_nodes_20261011/ue_result.json`。
- 保存后的资产重载检查：`work/glass_nodes_20261011/ue_reload.json`。桌面编译测试曾在退出后的状态栏清理中触发 `ModeManagerInteractiveToolsContext` 错误，未出现 D3D 设备移除；最终编译与重载改用无界面 UE 命令进程验证。
- 400×225、PT 8 次采样的同机位对照：`work/glass_nodes_20261011/visual_low.json`。PNO 对照曾改变 PT，已弃用；最终 `pt_ior.png` 与 `pt_corrected.png` SHA256 完全相同：`961ebf9c6ec6d1d605d513fcdbf648c0326d5903e67d189dbb9bf46acc49e8dd`。
- 对照只复制原母材质、改变折射输入；原地图、原材质和 Source USD 的哈希保持一致。监测到专用显存峰值约 6.5 GB，80% 预算停止条件未触发。

当前工程实际启用 Substrate、硬件光追与 Lumen 硬件光追，但 `r.Substrate.ProjectGBufferFormat=0`。Ray Traced 半透的黑玻璃问题仍需单独验证；本轮没有修改全工程 GBuffer、曝光或雾密度。未进行完整泳池高分辨率 MRQ、Adaptive GBuffer 或高采样 Ray Traced 压力测试。

受视角、光线类型、时间影响的节点和 OSL 不属于“任意节点都等价还原”；保持显式限制或复核状态，不自动整材质烘焙后宣称完整还原。

## 手动切换入口补接

旧任务的手动“透射折射”原先沿用导入时的轴向尺寸，缺少新拟合证据，且会把源 Glass BSDF 改为 Principled。这次改为读取当前 UE 网格 LOD0 中所选材质槽的顶点及组件变换，按同一薄板阈值重新判断。薄板与立体网格即使共用源材质，也生成不同候选；Glass/Refraction 闭包保留。读取失败时保留原绑定并报错，不靠场景名、植物名或固定材质 ID 推断。

分析弹窗按当前 UE 的混合模式和薄片散射元数据预选，结果不一致时预选“不透明”；展示实际折射处理，便于确认新修正是否应用。

`work/glass_surface_hook_20261011/ue_geometry.json` 验证了现有泳池玻璃、旋转薄板、立方体和球体。`ue_switch.json` 在独立测试地图验证同一源玻璃的两个几何分支生成、编译、应用和撤销；保护的原地图、材质、USD、转换记录哈希均未改变。编辑器正常退出，103 项相关回归通过。此次未重新渲染完整泳池，也未进一步调整立体交界或 Ray Traced 黑玻璃。

## 已有 USD 强覆盖与撤销

已有集合绑定或祖先强绑定会压住弱层中的新直接绑定。手动切换现在在有效绑定所属位置建立只包含选中网格／材质子集的集合，明确控制绑定优先级；原集合、原材质及未选对象保留。各批次使用独立的 USD 材质命名空间，防止后续批次改写之前批次的材质引用。

Blender 只生成候选覆盖层和候选根层，不写入当前根层；UE 核对源绑定、文件哈希和保存状态后才提交。撤销恢复切换前根层的完整字节，连同原集合、优先级和实例标记一起恢复；出现后续编辑或还原点损坏时拒绝覆盖。旧批次仍可使用既有撤销流程。

- 105 项相关回归通过，新增候选根层篡改保护与精确还原用例。
- `tests/test_surface_binding_usd.py` 在实际 Blender USD 中通过 5 项用例：共享强覆盖、直接强覆盖、材质子集、实例代理、连续切换及撤销。
- `work/strong_surface_override_20261011/ue_result.json` 在独立 UE 地图完成 3 次切换与 3 次逐次撤销；每步核对实际组件材质，最终根层哈希恢复原值，原泳池文件哈希不变。无 GPU 渲染的命令进程正常退出。
- `pool_probe.json` 对当前泳池 USD 副本中的 6 个原失败目标完成切换和撤销，未选控制对象保持原绑定，原源文件只读。
- 未直接替换用户当前泳池绑定，也未重新渲染完整场景；图像效果仍由用户重新应用后检查。
