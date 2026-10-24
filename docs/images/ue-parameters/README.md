# UE 参数例图来源

本目录图片用于 [UE 参数指南](../../UE_PARAMETERS.md)。PNG 保留原生渲染输出，未做调色、裁切或拼图；文档表格仅将原图并排显示。

## 当前基础几何例图

20 张 720×540 图片生成于 2026-10-24 独立 UE 5.8.2 工程，使用仓库功能基线 `b2b5398` 的内置 PBR 图：分类 v9、着色 v11、默认 v6、Alpha 接线 v1。只使用引擎自带 Sphere／Plane 和程序生成的棋盘、Alpha 输入，没有使用用户模型或贴图。

渲染为 DX12／SM6、Substrate Blendable GBuffer、SceneCapture2D Final Color LDR；光追、Lumen GI 和 Lumen 反射均关闭。示例机位 `(235,28,90)` cm，目标 `(0,0,52)` cm，FOV 35°；矩形主光 1800 lm、35×130 cm，平行光 1.5 lux，SkyLight 0.2，固定手动曝光补偿 -5 EV。SSS 图保留实时捕捉的采样噪点。

| 文件组 | 唯一变化 | 判定范围 |
|---|---|---|
| `roughness_0/1/2.png` | Roughness 0.08／0.4／0.85 | 反射集中与分散 |
| `rosiness_0/1/2.png` | SkinRosiness 0／0.35／0.8 | 皮肤乘色趋势 |
| `scattering_0/1/2.png` | SSSStrength 0／0.35／1 | 当前 Profile 的散射与高光变化 |
| `fuzz_0/1/2.png` | Fuzz 0／0.7／1 | 此照明下的布料响应，差异较细 |
| `wet_0/1.png` | WetEnabled 0／1 | 内置水渍层参与效果 |
| `alpha_0/1.png` | Opaque／Masked | 同一 RGBA 输入的实心与裁切 |
| `alpha_2.png` | Translucent | **颜色偏黑，异常复现，待复核** |
| `hosiery_0/1/2.png` | HosieryThickness 0.35／1／2.5 | **白色半透明层增厚后偏黑，异常复现，待复核** |

每个目标标量均由 UE 原生 API 回读，与图注数值对应。半透明组改用 Movable 灯光后仍偏黑，当前状态为 `needs_review_translucent_lighting`；根因待查，正式 MRQ 对照尚未执行。复核记录见 [已知问题](../../KNOWN_ISSUES.md#玻璃与半透明)。本批验证包含这些固定条件下的基础几何捕捉，整角色及其它渲染路径沿用 [项目验证记录](../../PROJECT_STATUS.md)。

图片 SHA256、版本和逐图状态保存在 [例图清单](figure_manifest.json)。本目录只发布文档所用图片及其来源摘要，完整 UE 工程、失败启动日志和原始回执保留在开发机。

## 皮肤双层高光

`highlight_production.png`、`highlight_wide.png`、`highlight_softedge.png`、`highlight_feathered.png` 是 2026-10-23 的 UE 5.8.2 原生 MRQ 图，1440×1440、同一帧 8。来源为固定腿部相机的条形光实验，每张只改变一个控制。

| 文件 | Width | Softness | Blend |
|---|---|---|---|
| `highlight_production.png` | 0.6 | 0.5 | 0.45 |
| `highlight_wide.png` | 1.5 | 0.5 | 0.45 |
| `highlight_softedge.png` | 0.6 | 1 | 0.45 |
| `highlight_feathered.png` | 0.6 | 0.5 | 0.85 |

身体皮肤固定为 Roughness 0.34、Specular 0.42、SSSStrength 0.35、SkinRosiness 0.55、WetEnabled 0；面部粉嫩度为 0.45。该组使用着色 v11／默认 v5，当前默认 v6 的粉嫩度统一为 0.35。主条形光 400 lm、12×170 cm，补光 12 lm、轮廓光 15 lm、SkyLight 0.005、曝光补偿 -2.2。源参数、机位及原生成功回执在开发机保留。

角色为《原神》芙宁娜；模型提供 miHoYo，模型改造观海（观海子）。图片展示材质参数效果，仓库不提供原模型和贴图；模型及角色权利属于原权利人，不因本仓库源码许可而改变，原模型使用规则继续适用。
