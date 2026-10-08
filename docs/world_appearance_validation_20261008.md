# 世界环境、灯光与混合世界雾验证

本轮只扩展世界场景还原。`legacy_main_v1` 表面策略、`legacy_diffusion` 树叶默认、水材质、实验性占位材质补修均未改动。USD 默认显示材质增加持久化，防止重载网格后出现悬空材质引用；不修改其材质图。

## 原生验证

- Blender 5.2.2：嵌套节点组实例上下文、Blackbody 和静态计算、Value 输出变化的缓存失效、不支持坐标拒绝、IES Strength 只乘一次、角度表达式及除零拒绝均通过。
- 环境：相机红色与照明蓝色分支分别产生对应 HDR；注入诊断异常后保留有效 HDR；源文件哈希不变。
- 混合世界雾：两个白色散射分量密度各 0.01、HG 各为 0 与 0.75，得到总消光 0.02、散射反照率 1、加权 HG 0.375；8³ VDB 消光网格为 0.005/voxel。Density 的 Value 和 Color 的 RGB 输出链路均纳入回归。
- UE 5.8.2：原生 Light Function 编译及 TextureLightProfile 导入通过。独立 640×360 场景的真实 MRQ 图，相比常量基线，Light Function / IES 的平均像素绝对差为 60.44 / 10.92，确认实际改变光照；不据此宣称任意源方向性表达式视觉等价。
- 9 项便携测试通过；新增世界常量修复后的报告、资源保存测试也通过。

## 港口干净导入

源工程：`F:/MMD_Scene/Blender/SableFiSh/港口/Harbor.blend`。

最终任务：`F:/MMD_Scene/BlenderToUe5/Harbor_World_Clean_20261008_02/Transfer/MMDBridge_Jobs/Scene_Harbor_01`。初次导出没有借用旧任务的几何或贴图；后续修正续跑同一任务自己的检查点。

关卡：`/Game/Harbor_World_Clean_20261008_02/A_Scene_Harbor_01_0286230a/Maps/MMD_Preview`。

- 82 个表面材质的策略、通道、表面图及状态与旧港口一致；260 原型、473 实例、4,748,711 唯一三角形、7,204,055 实例三角形一致。
- 局部 Sphere 的七个体积网格逐体素数值与旧版本一致。
- 64 个灯光无着色器表达式未解警告；世界照明与独立背景 HDR 接入，天空光实际强度为 683。
- 港口 TrueSky 的 Enable Haze 与 Enable Fog 在原始和驱动求值后的节点组中均为 0。世界雾的所有闭包均被源开关关闭，空消光网格符合源配置；不能将本场景当作活动世界雾的视觉等价验证。源分量仍保留在导出记录中。图中的其他雾差异继续复核。
- 前景 `立方体.001` 标牌关闭可视并保存，重载后核对隐藏标记；材质及代码哈希未变化。
- 完整原生 MRQ 输出 0–300 共 301 帧、1920×1080，无缺帧、无渲染 GPU 崩溃。最终 GPU CSV 有效帧平均 41.33 ms；外部专用内存峰值 5065 MiB、共享 1171 MiB。该测量是此任务的 GPU 帧耗时，不代表视口性能或跨运行的严格性能对比。
- 初次干净导入发生一次 EXCEPTION_ACCESS_VIOLATION，程序从落盘检查点恢复；记录在验证目录的 `import_crash.json`。不能仅凭此前显存暂停断定该崩溃是显存不足。
- 旧目标工程关卡及源 Blender 哈希不变；既有 MyGO/Railway 的 630 个材质及关卡文件哈希不变，未重导入它们。

## 仍未通过的部分

渲染进程成功且所有帧产生，但最终回执 `complete=false`：水材质 `MI_M_003_MMDBridge_01` 缺少 Nanite 使用标记，运行时回退默认材质。本轮遵照范围保留此问题。`Material.061` 独立 SSS 仍为原有占位失败项；缺失 ABC 不在本轮范围。

对照图显示远景雾光、水面反射和部分灯罩亮度仍有差距。当前 UE Lumen 异质体积使用各向同性散射，云节点双 HG 不能证明此路径支持多相函数；保留原始分量与加权 HG 数据，也不宣称完整角分布还原。相机相关字段冻结在参考帧，有限 Generated 域、其他射线与视角均需复核。场景保持 `needs_review`，等待人工验收。

本机完整证据位于 `work/world_appearance/harbor_world_clean_20261008_02/`：`delivery_summary.json`、`world_grid_validation.json`、`legacy_surface_regression.json`、`volume_data_regression.json`、`preset_test_result.json` 和 `matched_comparison.png`。
