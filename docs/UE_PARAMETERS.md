# UE 参数含义与调节指南

本页介绍本工具导入后在 UE 中常见的材质、贴图、灯光和渲染参数，重点是**内置 PBR 角色**。依据 v0.3.0：分类 v9、着色 v11、默认 v6、Alpha 接线 v1。旧 v0.2.0 EXE、旧任务和自选母材质可能没有相同参数；以当前材质的 Parent 和实际节点连接为准。版本区别见 [源码与发行包](PROJECT_STATUS.md#源码与发行包)。

## 参数调整入口

在关卡中选中角色，在 Skeletal Mesh 组件的 Materials 中打开目标槽当前绑定的 `MI_` 材质实例，以组件实际绑定关系确认材质。多角色的材质分目录存放；建议每次仅修改一个角色的一个材质槽。

`MI_` 是实例，`M_` 是母材质，`T_` 是贴图，`SSP_` 是皮肤散射配置。参数左侧勾选框表示“覆盖父级值”，未勾选时继承父级；右侧复位箭头恢复父级值。需要保留导入时的逐槽设置时，先记录原值或复制实例，并将复制品绑定到目标槽。基本操作见 [UE 材质实例说明](https://dev.epicgames.com/documentation/unreal-engine/creating-and-using-material-instances-in-unreal-engine?application_version=5.8)。

下表列出常见调整目标及对应参数，后续章节说明全部内置 PBR 配置参数。数值范围指程序允许的配置范围；UE 中手工输入不一定受同样限制。默认值指新配置，实际实例还受材质类型、PMX 透明度、贴图和显式覆盖影响。

例图按参数放在对应段落中。基础几何图使用独立 UE 工程和当前母材质，每组固定机位、灯光与曝光，只改变注明的设置；皮肤双层高光采用已有角色实测图。图片均保留原生输出，点击可看原尺寸。各组版本、固定条件及验证状态见 [例图说明](images/ue-parameters/README.md)。

| 调整目标 | 对应参数与位置 |
|---|---|
| 基础颜色与色偏 | `base_color_Tex`、`BaseColorTint`、MatCap 与灯光 |
| 表面粗糙度与镜面反射 | `Roughness`、`Specular`、`DetailStrength` |
| 皮肤粉嫩度 | `SkinRosiness`，默认 0.35 |
| 皮肤散射、鼻部细节 | `SSSStrength`、面部遮罩和关联的 `SSP_` |
| 皮肤高光范围与柔和度 | `SSP_` 的 Dual Specular，见 [皮肤高光](#皮肤高光) |
| 丝袜覆盖厚度 | 半透明丝袜母材质中的 `HosieryThickness` |
| Alpha 裁切与镂空 | 基础图 A、`Opacity`、Masked 和最终 Opacity Mask 连接 |
| 半透明光照 | 母材质的 Translucency Lighting Mode 与实际着色节点 |
| 贴图亮度与色彩差异 | Source Color Settings、纹理 `sRGB`、预览通道 |

## 基础色、反射和细节

| UE 参数 | 含义与调节效果 | 范围与适用条件 |
|---|---|---|
| `BaseColorTint` | 乘到基础图 RGB 上的颜色。白色保持图中颜色；有基础图时通常为白色，无图时可来自 PMX 固有色 | 使用 RGB；表面透明覆盖读取基础图 A |
| `Roughness` | 表面粗糙度。降低通常让反射更集中，提高让反射更分散 | 0.02—1；`Use_roughness=1` 时由贴图代替基础标量，仍可叠加细节与水渍 |
| `Specular` | 控制非金属表面的镜面反射贡献 | 0—1；身体／面部默认 0.42／0.35 |
| `Metallic` | 金属响应，改变反射与漫反射的分配 | 0—1；金属类型默认 1，其它通常 0；`Use_metallic=1` 时读取贴图；适用于普通 PBR 金属分支 |
| `AO` | 没有 AO 图时的环境遮蔽输入；1 为中性值，降低可增加遮蔽 | 图中默认 1；`Use_ao=1` 时读取 AO 图 |
| `DetailStrength` | 内置微法线叠加强度；0 关闭这层细节，保留原基础法线 | 0—1；身体／面部默认 0.065／0.04 |
| `DetailTiling` | 微法线和细节粗糙度的 UV 重复次数；越大纹理越密、单个纹理越小 | 1—300；丝袜默认 135，也参与丝袜编织遮罩采样；准备时可能按表面校准 |

基础法线 `normal_Tex` 与微法线 `detail_normal_Tex` 分层叠加，`DetailStrength=0` 时仅保留基础法线。最终逐像素粗糙度由标量或贴图、细节和水渍共同计算。UE 的通用物理输入含义见 [Material Inputs](https://dev.epicgames.com/documentation/unreal-engine/material-inputs-in-unreal-engine?application_version=5.8)。

### 粗糙度例图

同一通用 PBR 球体，仅改 `Roughness`；粗糙度贴图和微法线均关闭。观察反射亮带从集中到分散的变化。

| 0.08 | 0.40 | 0.85 |
|---|---|---|
| ![粗糙度 0.08](images/ue-parameters/roughness_0.png) | ![粗糙度 0.40](images/ue-parameters/roughness_1.png) | ![粗糙度 0.85](images/ue-parameters/roughness_2.png) |

## 皮肤颜色与散射

| UE 参数 | 含义与调节效果 | 范围与适用条件 |
|---|---|---|
| `SkinRosiness` | 在原基础色与暖粉色乘算结果之间混合。0 保留原基础色，增大时暖粉色影响更强 | 0—1；面部与所有皮肤默认均为 0.35 |
| `SkinRosyTint` | 暖粉色乘算的 RGB 颜色 | 当前母材质默认 `(1.04, 0.25, 0.5)`；配合 `SkinRosiness` 使用，旧母材质默认不同 |
| `SSSStrength` | 控制皮下散射贡献；降低可减少散射造成的柔化 | 0—1；中间散射的身体／面部默认 0.35／0.27；用于非半透明皮肤／面部 |
| `SSSDetailPreservation` | 面部散射遮罩的参与强度。0 忽略遮罩，1 完整使用遮罩；遮罩较暗区域会减少散射 | 0—1，默认 1；仅面部且 `Use_sss_mask=1` 有效 |
| `SSSMaskFloor` | 程序生成面部散射遮罩的下限；提高可避免鼻部等区域的散射被压得过低 | 0—1，默认 0.9；只对 `GeneratedSSSMask=1` 的自动遮罩生效 |
| `GeneratedSSSMask` | 标记当前遮罩是否由程序生成，用于启用上述下限 | 自动设置为 0 或 1；手工遮罩通常为 0，避免改变手绘黑白含义 |
| `SubsurfaceTint` | 母材质的 SubSurfaceColor 兼容输入 | 散射距离与颜色由关联 `SSP_` 的 Mean Free Path 参数设置 |

面部遮罩控制散射强度分布，`SSSMaskFloor` 为自动遮罩设置下限。皮肤节点的 Opacity 控制散射贡献，最终输出的 Opacity Mask 控制表面裁切。[UE Subsurface Profile](https://dev.epicgames.com/documentation/en-us/unreal-engine/subsurface-profile-shading-model-in-unreal-engine) 介绍了 Profile 的散射遮罩与实例覆盖方式。

### 粉嫩度与散射例图

第一组只改 `SkinRosiness`。0 为原基础色，0.35 为当前默认，0.8 展示较强的暖粉色影响。

| 0 | 0.35（当前默认） | 0.8 |
|---|---|---|
| ![粉嫩度 0](images/ue-parameters/rosiness_0.png) | ![粉嫩度 0.35](images/ue-parameters/rosiness_1.png) | ![粉嫩度 0.8](images/ue-parameters/rosiness_2.png) |

第二组只改 `SSSStrength`，固定中间散射 Profile 和粉嫩度 0.35。观察受光、阴影过渡及 Profile 高光响应的变化。

| 0 | 0.35（身体默认） | 1 |
|---|---|---|
| ![散射强度 0](images/ue-parameters/scattering_0.png) | ![散射强度 0.35](images/ue-parameters/scattering_1.png) | ![散射强度 1](images/ue-parameters/scattering_2.png) |

### 皮肤高光

程序中的三个高光参数保存在每槽专属的 `SSP_` 配置中。在皮肤实例 Details 找到 **Override Subsurface Profile**，打开已关联的配置，在 **Dual Specular** 中调整。着色 v11 的非半透明皮肤／面部使用这套控制。

| 程序参数 | 程序范围；身体／面部默认 | UE 配置中的对应含义 |
|---|---|---|
| `SkinHighlightWidth` | 0.5—2；0.6／0.85 | Roughness 0，主高光粗糙度倍率；增大通常扩大主高光 |
| `SkinHighlightSoftness` | 0—1；0.5／0.4 | 决定 Roughness 1：`Width + (2 - Width) × Softness`；增大扩展外围高光 |
| `SkinHighlightBlend` | 0.1—0.9；0.45／0.65 | Lobe Mix，第二层高光的混合占比 |

UE 将 Profile 的粗糙度倍率与表面粗糙度结合，再根据 `Specular`、灯光尺寸和观察角度计算高光形状。散射贡献很低时，双层高光可能减弱。

| `SSP_` 中的字段 | 含义 | 当前新配置 |
|---|---|---|
| Enable Burley／Enable Mean Free Path | 选择当前使用的散射表示 | 当前新配置均开启 |
| Surface Albedo | 散射模型的表面反照率颜色 | 中间散射 `(0.8, 0.62, 0.56)` |
| Mean Free Path Color | 各颜色分量的散射距离比例 | 中间散射 `(1, 0.55, 0.435)` |
| Mean Free Path Distance | 散射距离尺度 | 面部 0.18 cm、身体 0.32 cm；增大需检查鼻翼、唇线等细节 |
| World Unit Scale | 散射计算的世界单位尺度 | 程序写入 1；模型单位通过导入设置校正 |

原暖色配置与当前中间散射的区别见 [柔和阴影与皮肤散射](CHARACTER_PBR_SOFT_SHADOW.md)。编辑共享 `SSP_` 会影响所有引用它的材质；仅调整单个槽时，应确认该槽使用独立的高光配置。

下面是 UE 5.8.2 单参数实测图，点击图片可看 1440×1440 原图。四张均为相同机位、姿态、条形光与曝光；腿部 `Roughness=0.34`、`Specular=0.42`，只改变表中一个高光控制。观察腿部纵向亮带的范围和外围过渡。

| 基准：Width 0.6／Softness 0.5／Blend 0.45 | 仅 Width → 1.5 |
|---|---|
| ![基准皮肤高光](images/ue-parameters/highlight_production.png) | ![仅增大主高光范围](images/ue-parameters/highlight_wide.png) |

| 仅 Softness → 1.0 | 仅 Blend → 0.85 |
|---|---|
| ![仅增大外围高光柔和度](images/ue-parameters/highlight_softedge.png) | ![仅增大外围高光占比](images/ue-parameters/highlight_feathered.png) |

这组图采用 2026-10-23 着色 v11／默认 v5：身体粉嫩度 0.55、面部 0.45；当前默认 v6 的粉嫩度统一为 0.35，高光控制公式沿用。角色模型提供：miHoYo；模型改造：观海。原图来源与参数见 [例图说明](images/ue-parameters/README.md)。

## 布料、缎面、头发与丝袜

| UE 参数 | 含义与调节效果 | 范围与适用条件 |
|---|---|---|
| `Fuzz` | 布料纤维绒光贡献，增大通常加强边缘纤维感 | 0—1；Cloth 类型默认 0.7；接到节点的 Fuzz Amount |
| `FuzzBrightness` | 将纤维颜色从基础色向白色提亮，帮助深色布料保留纤维光泽 | 0—1，默认 0.08 |
| `FabricRoughnessVariation` | 细节图对粗糙度的局部扰动幅度；增大使纹理区域的光滑差别更明显 | 0—0.5；布料默认 0.22，缎面 0.16 |
| `Anisotropy` | 控制方向性反射；0 为各向同性，正负值改变响应方向 | -0.95—0.95；用于头发、缎面和丝袜，仍需切线与灯光支持 |
| `HairDirection` | 旋转头发反射切线方向；有流向图时在图中方向基础上旋转 | -360—360°，默认 90°；流向图的默认零偏置对应 90° |
| `FabricDirection` | 旋转缎面／丝袜的切线方向 | -360—360°，默认 90° |
| `HosieryCoverage` | 标准厚度下的编织覆盖密度；越大通常越不透 | 0—1，默认 0.5；仅半透明丝袜母材质 |
| `HosieryEdgeDensity` | 斜视角时的覆盖增强；0 关闭角度增强，1 使用完整角度效果 | 0—1，默认 1 |
| `HosieryThickness` | 对标准厚度的覆盖结果作线性缩放，最后限制到 0—1 | 0—3，默认 1；0 隐藏丝袜层，薄／中／厚预设为 0.35／1／2.5 |

丝袜厚度是**无单位的视觉覆盖倍率**。程序先计算基础图 A、PMX 透明度、独立透明图和编织覆盖，再乘以厚度，并将最终结果限制到 0—1。厚度节点在创建半透明丝袜母材质时生成；已有 Opaque 母材质需要补建对应分支后才能使用。

调整丝袜颜色时，在角色所在关卡中核对基础图 RGB、`BaseColorTint`、MatCap、反射环境、光照和透明模式。材质预览球使用独立的灯光、背景和模型。

### 丝袜厚度例图

**异常复现：** 本组 SceneCapture2D 例图中，白色丝袜层随厚度增加而偏黑。内层为固定皮肤球体，外层为白色半透明丝袜球壳；固定 `Opacity=0.8`、`HosieryCoverage=0.5`、`HosieryEdgeDensity=1`，关闭 MatCap，仅改变 `HosieryThickness`。

| 0.35（薄） | 1（中） | 2.5（厚） |
|---|---|---|
| ![丝袜厚度 0.35](images/ue-parameters/hosiery_0.png) | ![丝袜厚度 1](images/ue-parameters/hosiery_1.png) | ![丝袜厚度 2.5](images/ue-parameters/hosiery_2.png) |

复核状态见 [半透明异常记录](KNOWN_ISSUES.md#玻璃与半透明)。

### 布料绒光例图

同一深色布料球体，仅改变 `Fuzz`；`Roughness=0.65`，`FuzzBrightness=0.08`。观察边缘和斜视角处的纤维光泽。

| 0 | 0.7（布料默认） | 1 |
|---|---|---|
| ![布料绒光 0](images/ue-parameters/fuzz_0.png) | ![布料绒光 0.7](images/ue-parameters/fuzz_1.png) | ![布料绒光 1](images/ue-parameters/fuzz_2.png) |

## 眼睛、涂层、发光和玻璃

| UE 参数 | 含义与调节效果 | 范围与适用条件 |
|---|---|---|
| `ClearCoat` | 透明涂层的贡献，用于角膜式高光、釉质或塑料表层 | 0—1；仅母材质实际创建 Clear Coat 分支时有效 |
| `CoatRoughness` | 涂层本身的粗糙度，与底层 `Roughness` 分开 | 0.02—1；降低通常让涂层高光更集中 |
| `IrisDepth` | 由虹膜遮罩限制的视差 UV 偏移幅度 | 0—0.03，默认 0；按 UV 偏移量设置，需明确的 `iris_mask_Tex` |
| `EmissiveStrength` | 自发光强度，提高会减弱视觉上对外部光照的依赖 | 0—100；虹膜／眼白／眼部高光默认 0.35／0.20／0.30，普通类型通常 0 |
| `EmissiveColor` | 发光 RGB 乘色，与发光来源、遮罩和强度相乘 | 母材质默认白色；最终发光也乘表面 Alpha |
| `IOR` | 玻璃折射率，1 表示与空气接近的无折射差异，增大通常加强偏折 | 在半透明玻璃母材质中设置，默认 1.45；需启用对应折射路径 |

未指定发光图时，眼部三类用基础图及其乘色作为轻微补色来源；指定 `emission_Tex` 后由 `Use_emission` 切换来源。高自发光会让眼睛显得“平”，先小幅调整。普通表面只增大 `EmissiveStrength` 也可能产生整片发光，因为默认发光来源为白图。

程序按材质类型和透明模式选择涂层及玻璃节点。玻璃光追折射的已知问题见 [异常记录](KNOWN_ISSUES.md#玻璃与半透明)。

## MatCap 与水渍

MatCap 按视空间法线采样 Sphere 贴图，并按 PMX 混合方式参与基础颜色计算。

| UE 参数 | 含义与调节效果 | 范围／默认 |
|---|---|---|
| `UseMatCap` | 是否使用 `sphere_Tex` | 0 关闭、1 开启；默认跟随 PMX 和逐槽选择 |
| `MatCapAdd` | Sphere 的混合方式 | 0 乘算、1 加算；来自 PMX 模式 |
| `MatCapStrength` | Sphere 参与强度 | 0—2，默认 1；乘算图较暗时会压暗基础色 |
| `WetEnabled` | 水膜、水滴与流痕总控制 | 0—1，默认 0；通常按 0／1 使用，仅皮肤和面部 |
| `WetAmount` | 水渍效果参与量，与总控制相乘 | 0—1，默认 1；总控制为 0 时无效 |
| `WetTiling` | 水渍图案的 UV 重复次数，增大使图案更密、更小 | 0.1—100，默认 8；准备记录可能保存了自动校准值 |
| `WetRotation` | 水渍 UV 图案和法线方向的旋转角度 | -360—360°，默认 0° |
| `WetNormalStrength` | 水滴／流痕法线的凹凸强度 | 0—2，默认 1 |
| `WetRoughness` | 湿区趋近的粗糙度 | 0.02—0.5，默认 0.04；降低通常产生更集中的湿润反射 |

水渍层通过贴图叠加表现水膜、水滴与流痕，同时影响法线、粗糙度、镜面反射和颜色。`WetRotation` 调整这层贴图的 UV 与法线方向。

| WetEnabled = 0 | WetEnabled = 1 |
|---|---|
| ![关闭水渍](images/ue-parameters/wet_0.png) | ![开启水渍](images/ue-parameters/wet_1.png) |

本组仅切换 `WetEnabled`，固定 `WetAmount=1`、`WetRoughness=0.04`、`WetNormalStrength=1`。

## 贴图、通道和开关

### Texture Parameter

| UE 参数 | 用途 | 数据解释 |
|---|---|---|
| `base_color_Tex` | 原基础色 RGB 与原 Alpha | RGB 按 sRGB 颜色；A 用于表面覆盖 |
| `normal_Tex` | 原基础法线 | Normal，完整 RGB |
| `roughness_Tex`、`metallic_Tex`、`ao_Tex` | 粗糙度、金属度、环境遮蔽 | Linear，选择实际数据通道 |
| `opacity_Tex` | 独立透明遮罩 | Linear；由 `Use_opacity` 控制是否相乘 |
| `emission_Tex`、`emission_mask_Tex` | 发光颜色、发光强度遮罩 | 前者 sRGB，后者 Linear |
| `sphere_Tex` | 原 PMX Sphere／MatCap | sRGB 颜色，由 `UseMatCap` 控制 |
| `direction_Tex` | 头发的切线流向图 | Linear RGB，按向量解码 |
| `iris_mask_Tex`、`sss_mask_Tex` | 虹膜视差范围、面部散射范围 | Linear，分别对应虹膜与面部遮罩 |
| `detail_normal_Tex`、`detail_roughness_Tex` | 程序内置微结构 | 法线／Linear；只在对应材质分支出现 |
| `wet_normal_Tex`、`wet_mask_Tex` | 水渍法线、图案遮罩 | 法线／Linear；皮肤分支使用 |
| `hosiery_mask_Tex` | 丝袜编织覆盖图 | Linear R；半透明丝袜分支使用 |

### Use、Channel、UseAlpha 与 Invert

`Use_roughness`、`Use_metallic`、`Use_ao` 为 0 时采用对应标量，为 1 时采用图中数据。`Use_opacity` 为 1 时将独立遮罩乘入 Alpha；`Use_direction` 为 1 时使用头发流向图。`Use_emission`、`Use_emission_mask`、`Use_sss_mask` 分别控制相应来源或遮罩。这些标量决定节点采用哪个输入来源；左侧勾选框决定实例是否覆盖父级值。基础图与基础法线直接接入。

对 `roughness`、`metallic`、`ao`、`opacity`、`emission_mask`、`iris_mask`、`sss_mask` 这些单值数据，通道读取遵循相同规则：

| 参数形式 | 含义 | 例子 |
|---|---|---|
| `<用途>_Channel` | RGB 的权重；通常用单一通道 | `ao_Channel=(0,1,0,0)` 读取 G |
| `<用途>_UseAlpha` | 1 时读取贴图 A，0 时采用上述 RGB 结果 | `opacity_UseAlpha=1` 读取独立透明图的 A |
| `<用途>_Invert` | 1 时将读取结果变为 `1-值` | 白色代表透明的来源可按需要反转 |

**读取独立数据贴图的 A 时，将对应的 `UseAlpha` 设为 1。** `Channel` 的 RGB 分量用于点积，A 分量在该计算中未使用；基础色图的 A 直接参与表面覆盖。

替换贴图后，按实际通道内容设置开关与权重。ORM 等打包图需确认各通道用途；法线图使用完整 RGB。

### sRGB 与源编码

纹理 `sRGB` 控制颜色采样解释；**Source Color Settings → Encoding Override** 声明原始文件的编码。当前导入规则将颜色源设置为 sRGB、法线和数据源设置为 Linear，保留原始图片。16 位颜色 PNG 的具体处理见 [贴图颜色编码](CHARACTER_MATERIALS.md#贴图颜色编码)。

纹理编辑器的 RGB／A 开关选择预览通道，棋盘背景用于观察透明区域，Mip 级别决定预览分辨率。比较原图与 UE 时，统一通道、Mip 和背景设置。字段说明见 [UE Texture Asset Editor](https://dev.epicgames.com/documentation/en-us/unreal-engine/texture-asset-editor-in-unreal-engine)。

## 透明模式与 Substrate 接线

普通标量 `Opacity` 的范围为 0—1，默认取该槽 PMX 透明度。在新 Alpha 图中，基础表面覆盖为：

```text
基础色图 A × Opacity ×（Use_opacity 开启时的独立透明遮罩）
```

半透明丝袜还会乘入编织、视角和厚度计算，再限制到 0—1。

| UE 设置 | 含义 | 检查项目 |
|---|---|---|
| Blend Mode：Opaque | 将表面作为完整不透明面渲染 | 需要 Alpha 镂空时使用 Masked |
| Blend Mode：Masked | 按阈值保留或剔除像素 | 需要最终输出的 Opacity Mask 连接；适合孔洞、蕾丝、发片边缘 |
| Opacity Mask Clip Value | Masked 的裁切阈值；提高会剔除更多较低 Alpha 像素 | 仅 Masked 模式生效 |
| Blend Mode：Translucent | 使用连续透明覆盖 | 检查母图的透明分支、受光方式和排序 |
| Two Sided | 是否渲染背面 | 几何厚度与法线由模型设置 |
| Shading Model／节点 Single Shading Model | 表面的着色模型，如 Cloth、Clear Coat、Subsurface Profile | Substrate 使用实际节点及其 Front Material 连接；着色分支在母图中配置 |
| Translucency Lighting Mode | 半透明表面的受光路径 | 当前新建半透明 PBR 母材质使用 Surface ForwardShading 对应模式；切换已有实例时需同时核对该设置 |

`_AM1` 母材质将表面覆盖值接到最终输出的 **Opacity Mask**，Opaque 父材质保留这条线，供实例切换为 Masked 使用。真正半透明图的覆盖值连接 Substrate Opacity，皮肤 Profile 的 Opacity 则控制散射。

下面使用同一张含 Alpha 0、约 0.39、1 和圆孔的程序生成贴图，分别创建三种混合模式的母材质，`Opacity=1`。后方放置棋盘，用于观察透明与裁切区域。

| Opaque | Masked | Translucent（受光异常，待复核） |
|---|---|---|
| ![不透明模式](images/ue-parameters/alpha_0.png) | ![遮罩模式](images/ue-parameters/alpha_1.png) | ![半透明模式](images/ue-parameters/alpha_2.png) |

左、中图展示完整不透明面与阈值裁切的区别；Masked 中超过阈值的区域完整显示。右图可见连续透明覆盖，同时出现表面偏黑，复核状态见 [半透明异常记录](KNOWN_ISSUES.md#玻璃与半透明)。

Masked 预览仍有实心背景时，检查基础图 A 和最终 Opacity Mask 的连接；旧母材质需要补建裁切连接，或重新生成 `_AM1` 母材质。半透明受光差异依次检查 Translucency Lighting Mode、发光和场景灯光。引擎规则见 [Blend Modes](https://dev.epicgames.com/documentation/en-us/unreal-engine/material-blend-modes-in-unreal-engine)、[Lit Translucency](https://dev.epicgames.com/documentation/unreal-engine/lit-translucency-in-unreal-engine) 和 [Substrate](https://dev.epicgames.com/documentation/unreal-engine/overview-of-substrate-materials-in-unreal-engine?application_version=5.8)。

## 灯光、场景材质与输出

| UE 中的位置／参数 | 含义与调节方向 |
|---|---|
| 灯光 Intensity 与 Intensity Units | 实际灯光强度及其单位；同一个数字在 lux、lm、cd 下含义不同，先确认光源类型与单位 |
| Directional Light 的 Source Angle | 光源角尺寸；增大通常让投影更柔和，当前角色柔光预览为 7° |
| Point Light 的 Source Radius | 光源半径，影响反射与阴影形状；本工具按厘米写入 |
| Rect Light 的 Source Width／Height | 矩形光源尺寸，影响反射与投影的形状 |
| SkyLight Intensity | 环境照明强度；当前角色柔光预览为 0.65，场景导入保留自己的来源设置 |
| Post Process／相机的 Exposure Compensation | 画面曝光补偿；在其它条件相同的情况下，+1 EV 对应曝光翻倍，影响整张画面 |
| 相机 Focus Method／Focus Distance | 选择对焦方式并设置对焦平面的距离 |
| MRQ 的画幅、帧范围、采样与色彩配置 | 由实际加载的完整 MRQ 预设决定最终输出 |
| `r.VolumetricFog.GridPixelSize`／`r.VolumetricFog.GridSizeZ` | 体积雾栅格精度；减小前者、增大后者通常提高细节与开销 |

有 Blender 场景时，角色柔和阴影调整工具导入灯的源尺寸／角度，保留其强度、位置及原本更大的源尺寸。单角色预览关卡使用独立的柔光配置。详细操作见 [场景光影](SCENE_APPEARANCE.md)、[显存与画质](MRQ_MEMORY.md) 与 [后台渲染](background_render.md)。

场景材质按 Blender 的有效节点转换，保留对应输入和源参数名。旧通用材质中的 `BaseColor`／`Normal` 为纹理参数；水面可能暴露 `WaveScale1/2`、`WaveSpeedX1/2`、`WaveSpeedY1/2`、`WaveStrength` 和 `SequenceTime`，分别控制两层波纹贴图的密度、UV 速度、法线强度和时间来源。

ZZZ 使用专用映射，其中 `Lightmap.A` 参与 MatCap 权重计算，详见 [角色材质](CHARACTER_MATERIALS.md#zzz_render_pmx)。自选父材质按实际读取到的参数和节点配置。

## 保存、重新应用与排查

在 UE 修改后保存实例及相关 `SSP_`；改变组件材质绑定时，同时保存关卡。母材质可能被多个实例共用，修改前确认引用关系。UE 手工设置与程序逐槽配置分别保存。

程序的“应用材质到 UE”与撤销会检查来源、资产和当前绑定。遇到“保留尚未保存的用户编辑”时，先保存手调结果；后续应用仍会核对修改记录和当前绑定，发现人工编辑时保留用户改动并报告冲突。需要生成新的转换版本时，使用新任务或新输出位置；继续手调时可复制实例并重新绑定。

| 现象 | 排查顺序 |
|---|---|
| 参数修改未生效 | 是否覆盖父级 → 是否是角色当前绑定的实例 → 母图是否有该参数与有效分支 → 贴图开关是否替代了标量 |
| 贴图对比度降低 | 纹理源编码与 sRGB → 原图 RGB/A → Tint/MatCap → 灯光、曝光与色彩管理 |
| 透明区域未裁切 | 原图 A → `Opacity` 与独立遮罩 → Masked → 最终 Opacity Mask → 是否仍为旧母图 |
| 高光过于集中或过渡生硬 | 固定机位和曝光 → 光源尺寸 → Roughness／Specular → 皮肤 Dual Specular |
| 丝袜厚度增加后出现色偏 | 半透明丝袜母图 → 原 RGB/Tint/MatCap → 实际关卡受光 → [异常记录](KNOWN_ISSUES.md#玻璃与半透明) |
| 视口与 MRQ 不同 | 相机、帧、关卡保存状态 → 完整 MRQ 预设 → 曝光／OCIO／渲染管线 |

例图的来源、参数和逐图状态见 [例图说明](images/ue-parameters/README.md)；项目验证记录见 [当前状态](PROJECT_STATUS.md)。
