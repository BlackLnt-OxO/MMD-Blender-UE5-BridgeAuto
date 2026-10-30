# CelShading_Lesson 头发材质分析与 1017 场景移植

分析日期：2026-10-27。参考引擎为本机 UE 5.8.2。本记录服务于当前角色的场景移植和之后的内置母版设计，不改变通用转换工具。**制作母版前必须重新打开下述原工程，复读实际材质、实例、曲线及关卡绑定并比较哈希；本文件不能代替原工程。**

2026-10-28 后续：已重新读取原工程包并核对依赖身份，在转换工具中接入独立的[内置卡通渲染](../BUILTIN_CEL.md)。下文保留 10-27 的分析及移植状态；新母版的参数、测试和限制以新文档及本轮回执为准。

## 来源、证据与当前状态

原工程：`F:/Ue5_Project/CelShading_Lesson 5.7 5.8/CelShading_Lesson.uproject`；参考关卡 `/Game/Render/Map`；角色蓝图 `/Game/Character/BP_Render`。实际头发材质来自 `Character/Materials/PBR` 与 `Character/Materials/NPR`，不是另一个 `Miku_2partSkirt/Material` 目录里的自制材质。

原生读取证据在本地 `work/cel_material_readonly_20261027/`，包括母材质、函数、蓝图及曲线的 T3D 文本快照、有效参数回读和原贴图导出。当前迁移的依赖、原文件 SHA256/mtime、备份和回执在 `work/night_sky_20261026_01/cel_hair_10/`。这些本机证据不作为通用工具运行资源分发。

状态：分析已完成；1017 中双网格应用及视口验收正在进行，最终结果见文末“本次移植结果”。尚未制作内置母版。

## 原工程的叠层结构

`BP_Render` 包含“基础光照模型”“滤镜模型”“描边模型”和隐藏的 `VertexModel` 调试模型。它们引用同一个 `/Game/Character/Miku_era`。滤镜层是基础层的子组件，相对变换为单位变换；描边层使用自己的反向缩放和 WPO。原参考关卡并未配置 Leader Pose 动画跟随，不能把这个静态示例当作动画同步已经验证。

基础层的 `M_PBR` 为 Opaque、Default Lit、双面。`BaseTex.RGB` 接 Base Color；`MSRE` 的 R/G/B 分别控制 Metallic/Specular/Roughness，A 乘基础图接 Emissive，默认 `(0,0,1,0)`。BaseTex Alpha 虽接 Opacity，但 Opaque 模式不会因此获得裁切效果。

滤镜层的 `M_NPR` 为 Translucent、Default Lit、双面，使用 Volumetric Non Directional 透明光照模式。它不是全屏后处理，而是由重合角色表面执行的局部屏幕颜色重着色。最终颜色同时写入 Base Color 和 Emissive，后者乘“全局卡渲自发光”；Opacity 来自 BaseTex Alpha。Anisotropy、Tangent、Normal 和 WPO 均未连接，不能把这里的绘制亮带称为已经完成的物理各向异性高光。

## 实际着色链路

### 屏幕受光与明暗重映射

`ScreenPosition → SceneColor → RGBToHSV → V` 读取底层已经受光后的亮度。第一组明暗曲线以 `saturate(V / ShadowSmooth)` 采样 `CB_Shadow`，使用 **Alpha 通道**作为 ShadowMask。第二组明暗曲线先减去 `ShadowOffest - Offest-- + VertexColor.B`，再除以 ShadowSmooth、饱和并采样同一曲线的 Alpha。它们组合基础色、第一暗部及第二暗部。

`MF_RGBcurve` 分开取基础图 R/G/B，以各通道为横坐标采样 `CA_RGB` 的 `CB_RGB02` 对应颜色通道，再合成 RGB。随后通过颜色参数的 Alpha 控制去饱和或颜色混合，RGB 用作颜色／乘色。`ShadowColor_2nd` 与 `ShadowColor2nd` 是两个不同参数，不能合并。

### 边缘光、灯光高光与视角高光

边缘光遮罩为 `1-saturate((dot(View,N)-RimScale)/RimSmooth)`，由“启用边缘光”静态开关控制。颜色参数 Alpha 决定乘色与替换颜色之间的插值。

`DirectionHighLight` 用 `dot(AtmosphereLightDirection[0], VertexNormalWS)` 采样曲线并取 R；`CameraHighLight` 用 `dot(VertexNormalWS, CameraVectorWS)` 采样并取 G。两者均乘 ShadowMask 后参与混合。**这两个标量的 0 表示图集第 0 行，不表示高光强度为 0。**不过当前三份头发实例的两种 HighLightColor 都是 `(1,1,1,0)`，其颜色混合是中性的，不能只凭这些节点存在就断言它们贡献了明显亮带。

### 弧形头发 MatCap

`V=normalize(CameraPosition-WorldPosition)`，`R=normalize(cross(WorldUp,V))`，`U=normalize(cross(R,V))`。取归一化顶点法线与 R/U 的点积构成二维坐标，乘 0.5 后加 `MatCapUV偏离`（当前 0.5）。材质不依赖 UV 发丝流向图。

`HairSpa1_psd` 本身画有上方弧形亮带：通过上述视角／法线坐标将这条亮纹映射到头发。采样值乘 ShadowMask，再按 MatCapColor.RGB/Alpha 与先前颜色组合并相加。该贴图当前启用 sRGB、Wrap 寻址；迁移先保留原设置。视线接近 WorldUp 时基向量可能退化，应留给后续母版做专项验证，当前不改原算法。

### Brush 分支与顶点色

`BrushTex` 虽名为 `T_SDF`，在这里实际作为重复采样的灰阶笔刷／阈值图使用，不是面部朝向 SDF。原图是一张径向灰阶渐变，坐标来自 `WorldSpaceAlignedScreenCoordinates × BrushTile`。保留其原 sRGB 与 Wrap 设置。

`Brush?` 控制该分支。关闭 `LambertBrush?` 时，阈值由 `VertexColor.R` 经 BrushOffest、BrushSmooth、BrushMax 处理得到；开启时则结合 `dot(AtmosphereLightDirection[1],N)` 与顶点色 R。参考关卡读取到一盏启用 Atmosphere Sun Light、索引为 0 的方向光；索引 1 的实际 GPU 回退行为尚未单独验收，不自动增加或替换场景灯光。

## 当前三槽参数与差异

当前 1017 原槽与参考实例对应为：

| 1017 槽位 | 当前槽名 | Cel PBR/NPR 实例 | 基础图 | MatCap | LambertBrush? |
|---|---|---|---|---|---|
| 14 | M_hair01_MMDBridge_01 | MI_14_hair03 | hair03 | HairSpa1_psd | True |
| 15 | M_Hair02_MMDBridge_01 | MI_15_hair01 | Hair01 | HairSpa1_psd | True |
| 16 | M_Hair03_MMDBridge_01 | MI_16_hair02 | Hair02 | 引擎黑图 | False |

三槽共同有效参数：RGB 图集行 2；ShadowSmooth 0.3；Shadow、DirectionHighLight、CameraHighLight 图集行 0；RimScale 0.07；RimSmooth 0.1；MatCapUV偏离 0.5；ShadowOffest 0；Offest-- 1；BrushTile 461.184326；BrushOffest 0.618770；BrushSmooth 0.243593；BrushMax 0；全局卡渲自发光 1；Brush? 与启用边缘光均为 True。下表保留全部有效回读，不把继承值当作未配置。

### 槽位 14 的完整有效参数

| 类型 | 名称 | 有效值 |
|---|---|---|
| scalars | `RGB` | `2.0` |
| scalars | `ShadowSmooth` | `0.30000001192092896` |
| scalars | `Shadow` | `0.0` |
| scalars | `RimScale` | `0.07000000029802322` |
| scalars | `RimSmooth` | `0.10000000149011612` |
| scalars | `DirectionHighLight` | `0.0` |
| scalars | `CameraHighLight` | `0.0` |
| scalars | `MatCapUV偏离` | `0.5` |
| scalars | `ShadowOffest` | `0.0` |
| scalars | `Offest--` | `1.0` |
| scalars | `BrushTile` | `461.184326171875` |
| scalars | `BrushOffest` | `0.6187700033187866` |
| scalars | `BrushSmooth` | `0.2435930073261261` |
| scalars | `BrushMax` | `0.0` |
| scalars | `全局卡渲自发光` | `1.0` |
| vectors | `BaseColor` | `(R=1.350000,G=1.350000,B=1.350000,A=0.000000)` |
| vectors | `ShadowColor` | `(R=0.164569,G=0.309198,B=0.451389,A=1.000000)` |
| vectors | `RimColor` | `(R=2.169924,G=4.966842,B=5.000000,A=1.000000)` |
| vectors | `DirectionHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `CameraHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `MatCapColor` | `(R=1.970136,G=2.000000,B=0.000000,A=1.000000)` |
| vectors | `ShadowColor_2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `ShadowColor2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `BrushColor` | `(R=1.524470,G=1.771660,B=2.000000,A=1.000000)` |
| textures | `BaseTex` | `/Game/Character/Tex/hair03.hair03` |
| textures | `MatCapMap` | `/Game/Character/MatCap/HairSpa1_psd.HairSpa1_psd` |
| textures | `BrushTex` | `/Game/Character/Tex/T_SDF.T_SDF` |
| switches | `Brush?` | `True` |
| switches | `启用边缘光` | `True` |
| switches | `LambertBrush?` | `True` |

### 槽位 15 的完整有效参数

| 类型 | 名称 | 有效值 |
|---|---|---|
| scalars | `RGB` | `2.0` |
| scalars | `ShadowSmooth` | `0.30000001192092896` |
| scalars | `Shadow` | `0.0` |
| scalars | `RimScale` | `0.07000000029802322` |
| scalars | `RimSmooth` | `0.10000000149011612` |
| scalars | `DirectionHighLight` | `0.0` |
| scalars | `CameraHighLight` | `0.0` |
| scalars | `MatCapUV偏离` | `0.5` |
| scalars | `ShadowOffest` | `0.0` |
| scalars | `Offest--` | `1.0` |
| scalars | `BrushTile` | `461.184326171875` |
| scalars | `BrushOffest` | `0.6187700033187866` |
| scalars | `BrushSmooth` | `0.2435930073261261` |
| scalars | `BrushMax` | `0.0` |
| scalars | `全局卡渲自发光` | `1.0` |
| vectors | `BaseColor` | `(R=1.350000,G=1.350000,B=1.350000,A=0.000000)` |
| vectors | `ShadowColor` | `(R=0.164569,G=0.309198,B=0.451389,A=1.000000)` |
| vectors | `RimColor` | `(R=2.169924,G=4.966842,B=5.000000,A=1.000000)` |
| vectors | `DirectionHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `CameraHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `MatCapColor` | `(R=1.970136,G=2.000000,B=0.000000,A=1.000000)` |
| vectors | `ShadowColor_2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `ShadowColor2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `BrushColor` | `(R=1.524470,G=1.771660,B=2.000000,A=1.000000)` |
| textures | `BaseTex` | `/Game/Character/Tex/Hair01.Hair01` |
| textures | `MatCapMap` | `/Game/Character/MatCap/HairSpa1_psd.HairSpa1_psd` |
| textures | `BrushTex` | `/Game/Character/Tex/T_SDF.T_SDF` |
| switches | `Brush?` | `True` |
| switches | `启用边缘光` | `True` |
| switches | `LambertBrush?` | `True` |

### 槽位 16 的完整有效参数

| 类型 | 名称 | 有效值 |
|---|---|---|
| scalars | `RGB` | `2.0` |
| scalars | `ShadowSmooth` | `0.30000001192092896` |
| scalars | `Shadow` | `0.0` |
| scalars | `RimScale` | `0.07000000029802322` |
| scalars | `RimSmooth` | `0.10000000149011612` |
| scalars | `DirectionHighLight` | `0.0` |
| scalars | `CameraHighLight` | `0.0` |
| scalars | `MatCapUV偏离` | `0.5` |
| scalars | `ShadowOffest` | `0.0` |
| scalars | `Offest--` | `1.0` |
| scalars | `BrushTile` | `461.184326171875` |
| scalars | `BrushOffest` | `0.6187700033187866` |
| scalars | `BrushSmooth` | `0.2435930073261261` |
| scalars | `BrushMax` | `0.0` |
| scalars | `全局卡渲自发光` | `1.0` |
| vectors | `BaseColor` | `(R=1.350000,G=1.350000,B=1.350000,A=0.000000)` |
| vectors | `ShadowColor` | `(R=0.164569,G=0.309198,B=0.451389,A=1.000000)` |
| vectors | `RimColor` | `(R=2.169924,G=4.966841,B=5.000000,A=1.000000)` |
| vectors | `DirectionHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `CameraHighLightColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `MatCapColor` | `(R=1.000000,G=1.000000,B=1.000000,A=0.000000)` |
| vectors | `ShadowColor_2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `ShadowColor2nd` | `(R=0.500000,G=0.500000,B=0.500000,A=0.000000)` |
| vectors | `BrushColor` | `(R=1.524470,G=1.771660,B=2.000000,A=1.000000)` |
| textures | `BaseTex` | `/Game/Character/Tex/Hair02.Hair02` |
| textures | `MatCapMap` | `/Engine/EngineResources/Black.Black` |
| textures | `BrushTex` | `/Game/Character/Tex/T_SDF.T_SDF` |
| switches | `Brush?` | `True` |
| switches | `启用边缘光` | `True` |
| switches | `LambertBrush?` | `False` |

## 本次接入规则与后续母版边界

本次只给当前角色头发套用原参数，不重做通用转换逻辑。目标资源隔离到 `/Game/Scenes/A_Composite_final_01/CelHair_01`。当前原角色的三个头发槽采用 Cel PBR 实例；独立滤镜网格仅显示这三个槽，使用对应 NPR 实例，两层完全重合，不用 Overlay。它们共享当前 WeightedNormal-only/noWeld 网格，保留原动画、Morph、UV、法线和蒙皮。滤镜蓝图保存构造逻辑，恢复仅头发的 Section 可见性与 Leader Pose 绑定。

目标顶点色是否与参考网格相同必须实测记录：R 影响 Brush，B 影响第二明暗遮罩。法线版本不同也会改变 MatCap 亮纹位置。不能通过悄悄重焊、重算法线或补画控制图掩盖这些差异。

之后制作内置母版需要区分“母材质着色逻辑”和“叠层组件装配”。仅携带一个母材质并不能自动复现整个方案。还需验证 Substrate 工程兼容、透明排序／景深、灯光索引、顶点色约定、曲线数据、动画同步及性能。当前不修改全局渲染设置、不增加通用参数或默认值。

## 关键源文件身份

| 源包 | SHA256 |
|---|---|
| `/Game/Character/MatCap/HairSpa1_psd` | `390137c93760255cbf3d2998976595674176a9d5588bda1b634e548644f2aded` |
| `/Game/Character/Materials/Master/M_NPR` | `4261beb777a9d80cfb0dcecb1c5a3f610519261302a44e95d56049ad51f1ece8` |
| `/Game/Character/Materials/Master/M_PBR` | `a4daf8170b864b36e4a47b08d070d1eb7dd0c4276dc95540de8025e4696e956e` |
| `/Game/Character/Materials/MaterialFunction/CA_GlobalRender` | `ff44ac42d3472077e9e3fcd581564d6647f8ee69dccde41c2ac5c76fb6812850` |
| `/Game/Character/Materials/MaterialFunction/CA_RGB` | `26cb700df20cb0c8d609907d071911678f8ebf1ab323932744387651b7d29a61` |
| `/Game/Character/Materials/MaterialFunction/CB_RGB01` | `3bc3a8ff3cd86426ee2b8ea1541b24029005a984b47be096a0c824d3234e23d8` |
| `/Game/Character/Materials/MaterialFunction/CB_RGB02` | `61b9a82e4b35de9e06c01f9ee70a0151705207ec843183e57bbd44cdea1f59be` |
| `/Game/Character/Materials/MaterialFunction/CB_Shadow` | `0923fa89f5ab0374a3c023fc490bb1a6ab3fa95a36d6807bbe1ad1009df34fb3` |
| `/Game/Character/Materials/MaterialFunction/CB_SkinRGB` | `963b38b8eac215a1c0d905d10bb1cc36e7d4052b28cf5441b73b0acd53077bd4` |
| `/Game/Character/Materials/MaterialFunction/MF_RGBcurve` | `21fb54e0f442c08c06d695e3379d1b088fa7ca99c238313f20a809cbb96f5b67` |
| `/Game/Character/Materials/NPR/MI_14_hair03` | `fa07083b0af3d85ac0d4f4a625e9561e9a27c1f451e9f1bcf60e9582d2d87588` |
| `/Game/Character/Materials/NPR/MI_15_hair01` | `958ab715ec09d1257650a8132e63dd8f86f65e420efe2b807082070c7e330888` |
| `/Game/Character/Materials/NPR/MI_16_hair02` | `fbcbd9c8139641abad94d5eaf11d010f2ff16e55f2d3094e596f1c9d3f672e57` |
| `/Game/Character/Materials/PBR/MI_14_hair03` | `3d5031e7d0d035cbdc7aa3f2d26a6ed4c5c001ac8e8cd2a2e450d4cc414450e2` |
| `/Game/Character/Materials/PBR/MI_15_hair01` | `8d502621584bdeb4d434e1b6914e0efb04e13ad817808d06fb127879616727e6` |
| `/Game/Character/Materials/PBR/MI_16_hair02` | `951677b8d700a5146cd3bab4c25d1789e15fba77045883cea80611c883d19e8b` |
| `/Game/Character/Tex/Hair01` | `2a88e36e8f0079a704b9691678cfb059a1ec99aa47488f00254fc1de4b1960f2` |
| `/Game/Character/Tex/Hair02` | `d60cd43e2dd42276b53efe8d51a36931b462541e7ca3b11dfacfe2e9ecd20f3c` |
| `/Game/Character/Tex/T_SDF` | `2118e8ea41e3d29e541d02bd4dfa15d5fe32a4881c4f2cdb05f03e40616ae764` |
| `/Game/Character/Tex/hair03` | `95d2349f83e7ae564b0fce7eac10e81d8bf7043e2b0558f2300c12ad540f73c5` |

## 本次移植结果

待原生应用和视口验收完成后更新。本段未标为完成前，资源复制、编译成功和静态分析均不等于画面验收通过。源工程文件与当前 USD 层的保护结果以最终回执为准。
