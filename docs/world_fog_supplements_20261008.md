# 世界雾主方案与实验性补充

2026-10-08 最新决定：新转换任务的 Lumen 主方案改为原生 Volumetric Fog＋光追灯注入，默认 `native_balanced`（网格 8、深度 128）。另外两个可选档为 `native_quality`（4、256）和 `native_detail`（2、256）。三档只改变 UE 网格采样，不需要重复导出／导入；本轮港口验证使用一张独立地图。

均匀世界雾继续通过 Exponential Height Fog 的 Volumetric Fog 表达。局部和非均匀字段保留原 SVT 密度／形状遮罩，由原生 Volume 材质注入雾网格。RGB 消光取均值；多个相函数或未证明恒定的相函数使用各向同性近似，报告保留 `needs_review`。不宣称完整还原任意 Blender 体积。PT 保留原 HVC 载体。

`vsm_finer`／`vsm_nocache` 保留为旧异质体积任务的选项，对新原生雾不适用。以下测量和限制属于此前的旧路线记录；不能用其小夹具耗时替代本轮港口实测。

## 可见雾与测试修正

用户指出前次截图不能清楚区分雾与普通灯光。复核发现旧测试脚本按对象迭代顺序选中了隐藏的生成 SVT 辅助组件，未证明细步进作用在实际显示的主雾上。旧 `work/fog_performance_20261008_213503` 的方案比较已标为 `invalid_component_target`，原始 CSV 与图像保留。

测试改为按夹具记录的明确体积身份定位，记录实际材质、组件可见性和 Actor 隐藏状态；独立重测后分别关闭雾组件生成同帧对照。关雾后背景变黑，受光体积与雾中暗带消失。三种方案都有真实雾贡献；普通地面阴影仍保留，因此不能把地面阴影当成雾阴影。

## 新性能记录

UE 5.8.2、RTX 4060 Laptop 8 GiB、1280×720、Lumen；四轮异质体积与四轮普通雾测试，均每轮输出 48 帧。采集实际输出期间 GPU CSV，去掉前 16 个样本，各方案两轮共 70 有效样本。下列数值是 GPUTime 中位数。

| 方案 | 所用雾路径 | GPU 中位数 |
|---|---|---:|
| Cinematic 无缓存 `vsm_nocache` | 可见的均匀异质体积 | 84.57 ms |
| VSM＋精细缓存／步进 `vsm_finer` | 同一可见异质体积 | 33.84 ms |
| 普通雾原行为 | 高度雾／普通体积雾 | 10.04 ms |
| 光追灯注入 `rt_inject` | 同一普通体积雾 | 11.18 ms |

精细缓存相对无缓存约少 60.0% 整帧耗时。普通雾灯注入相对其自身基线约增加 11.4%。普通雾与异质体积的完整光输运和边界表达不同，不能仅凭耗时把任意 VDB 自动替换成普通雾。盒状 froxel 和通用 SVT 适配候选单轮分别 10.49／11.67 ms；只有单轮，且完整画质矩阵未通过，不能成为通用发布依据。

## 低开销方案的通用性

`rt_inject` 对已转换为普通体积雾的源雾有意义；它不直接修复异质体积，也不提供任意 VDB 形状与程序化密度转换。盒状局部 froxel 只证明了静态均匀盒子；旧通用 SVT 边界候选的 5 cm 暗带对比仅 36.57%，未达 60% 门槛。混合相函数、相机／时间相关字段、动画、彩色消光和自发光尚无完整验证。因此没有证据表明其源雾覆盖范围高于两个异质体积补充，主方案保持原样。

普通体积雾的分辨率、相函数及动态时间滤波限制参见 [UE 文档](https://dev.epicgames.com/documentation/en-us/unreal-engine/volumetric-fog-in-unreal-engine)。异质体积的缓存／步进与内存权衡参见 [UE 文档](https://dev.epicgames.com/documentation/en-us/unreal-engine/heterogeneous-volumes-in-unreal-engine)。

## 实验补充入口

旧任务载入时保留旧路线选项，不自动迁移；新任务下拉框提供三个原生雾档位。`detail` 的旧生产验收注册表仍为空。旧补充仅接受 UE 5.8.2、Lumen 和已转换且有源 prim 路径的异质体积；已标记原生雾的字段不接受这两个补充。

- `vsm_finer`：LightingCache=2、Scalability=3、VSM；原生组件 StepFactor=0.25、LightingDownsampleFactor=0.5。
- `vsm_nocache`：LightingCache=0、Scalability=3、VSM；保留／恢复源组件步进和缓存倍率。
- 仅工具管理的源灯光切换至 VSM 阴影，其他灯不改；不重写源密度、曝光或材质。
- 视口、参考帧和后台渲染通过源 USD prim 定位体积，拒绝缺失或不唯一的目标。组件参数使用比较后恢复，用户手工修改会撤销该字段的自动控制。
- 实验结果始终为 `needs_review`，小测试不是整场景视觉验收。已有任务不自动迁移。

原生 UE NullRHI 验证了参数切换、回到原值和手工覆盖保留；它是原生属性验证，不是新增画质／性能证据。真实 MRQ 的可见雾及性能证据另存。

## 证据

证据目录：`work/fog_visible_validation_20261008_215148/`，包含 `summary.json`、`visible_fog_metrics.json`、`fog_on_off_comparison.png`、`native_supplement.json` 和各轮原始回执／GPU CSV。测量期间 154 个原文件哈希不变。实现完成后再次核对外部工程和受保护材质模块。

旧阶段仅更新源码。最新原生雾验证部署了工具管理的运行模块，原港口地图、源渲染预设、材质及序列受哈希保护；验证资源位于独立 `/Game/NativeFogValidation_20261008_224113`。恢复点和实际三档回执存于 `work/native_fog_20261008_224113/`。
