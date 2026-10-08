# 世界雾主方案与实验性补充

2026-10-09 回退决定：正式转换恢复源高度雾／SVT 异质体积流程，Lumen 默认选择 `vsm_finer`，`vsm_nocache` 保留可选。原生 Volumetric Fog＋光追灯注入及三个档位已撤出正式界面和转换入口，留作独立小场景实验；雾中受光和遮挡验证通过后再考虑上线。

均匀世界雾仍按源数据使用 Exponential Height Fog；局部和非均匀字段恢复原 SVT 与异质体积载体。原生雾候选的 RGB 消光取均值、相函数近似及静态 SVT 限制仅属于离线实验，不进入正式转换。

渲染显存保护仍默认关闭，导入／导出保护保留。港口原生雾批次在用户重启后中断，三档比较未完成，不能作为上线依据。源码恢复点为提交 `84d4850`；图片、日志、独立实验地图和 `outputs` 保留。

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

正式下拉框提供 `vsm_finer`、`vsm_nocache`、`legacy` 和待验证的 `detail`，旧任务缺失设置时保留 `legacy`。载入试验期间保存的 `native_*` GUI 配置时回到 `vsm_finer`。原生三个档位仅由独立验证脚本设置，不属于正式质量注册表。`detail` 的生产验收注册表仍为空。两个 VSM 补充接受 UE 5.8.2、Lumen 和已转换且有源 prim 路径的异质体积。

- `vsm_finer`：LightingCache=2、Scalability=3、VSM；原生组件 StepFactor=0.25、LightingDownsampleFactor=0.5。
- `vsm_nocache`：LightingCache=0、Scalability=3、VSM；保留／恢复源组件步进和缓存倍率。
- 仅工具管理的源灯光切换至 VSM 阴影，其他灯不改；不重写源密度、曝光或材质。
- 视口、参考帧和后台渲染通过源 USD prim 定位体积，拒绝缺失或不唯一的目标。组件参数使用比较后恢复，用户手工修改会撤销该字段的自动控制。
- 实验结果始终为 `needs_review`，小测试不是整场景视觉验收。已有任务不自动迁移。

原生 UE NullRHI 验证了参数切换、回到原值和手工覆盖保留；它是原生属性验证，不是新增画质／性能证据。真实 MRQ 的可见雾及性能证据另存。

## 证据

证据目录：`work/fog_visible_validation_20261008_215148/`，包含 `summary.json`、`visible_fog_metrics.json`、`fog_on_off_comparison.png`、`native_supplement.json` 和各轮原始回执／GPU CSV。测量期间 154 个原文件哈希不变。实现完成后再次核对外部工程和受保护材质模块。

回退同时更新工具管理的 UE 运行模块。原港口地图、源渲染预设、材质及序列受哈希保护；原生实验资源保留在独立 `/Game/NativeFogValidation_20261008_224113` 和 `/Game/NativeFogOcclusionOffline_20261009`。切换前快照和中断的港口回执存于 `work/native_fog_20261008_224113/`，回退回执存于 `work/fog_vsm_restore_20261008_235622/rollback_receipt.json`。
