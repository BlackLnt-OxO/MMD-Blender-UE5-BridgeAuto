# 角色母材质配置与自动贴图

ZZZ 配置登记 `default_eye_parent`：眼睛母材质留空时，眼睛、眼白、瞳孔走该配置，默认 `ShadowColor=0.65`、`TransitionColor=0.85`，沿用母材质的自发光以保留暗处贴图颜色。黑瞳等源图黑色不会被洗白。明确自选的眼睛母材质保持原样；`generic` 或未登记配置仍走原通用分支。已有 GUI 保存的眼睛路径不会被清空，使用自动眼部适配时需手动留空。眉毛和睫毛不归入眼球分类。

程序自动补齐 Lightmap 的衣服槽，会按各 Cloth Ramp 的亮面 RGB 归一化，避免将源角色专用的深灰亮面再次乘到 PMX 基础图上；保留各曲线的相对阴影颜色和过渡。生成实例的 `Ramp_Cloth_1/2/3_LitGain` 可分别调整，设为 `[1,1,1]` 恢复原曲线强度。自带或规则绑定的 Lightmap 不自动归一化，部位预设可覆盖这些参数。面部 `ShadowColor` 默认 HSV V=0.2。

Alpha 检查按材质实际使用的 UV 取样；PMX 的零面积 UV 三角形按其纹素取样，避免错误回退到整张贴图。真实渐变透明、材质透明度和裁切仍保留；SDF 的严格 UV 检查不受影响。

现有角色、眼睛母材质选择器继续使用 `/Game/...` 路径。`角色母材质适配配置` 默认自动按完整路径匹配；选择 `generic` 使用原通用参数匹配。其它值为配置 id。新增 JSON 配置放在界面指定目录，转换不依赖 AI 或外部制图程序。

内置 `ZZZ_Render_PMX` 使用 `/Game/Character/MasterMaterial/M_RenderMaster`。程序安装原母材质及依赖，适配工作发生在当前任务管理的复制品。原资产不改写；目标工程中同路径、不同内容的资产也不覆盖。默认用母材质三色/Ramp，保留 PMX Toon 数据供其它配置使用。SDF 按用户要求暂缓，默认 `sdf.format=none`，不生成面部贴图、不启用 SDF 分支、不创建面部方向组件。

`outline` 配置启用双网格角色蓝图，放在 `/Game/Character/MMDBridge/<任务目录>/BP_Character`。描边复用 `M_OutLine` 的复制品，基础图 Alpha 和 PMX 透明度参与可见性；PMX 的 `enabled_toon_edge/edge_weight/edge_color` 决定描边开关、相对宽度和颜色，`scale/face_scale` 默认 0.1/0.04 厘米。两层组件引用同一 SkeletalMesh 资产，分别使用主材质和描边材质，不复制大型网格文件。每层各自播放动画：蓝图 `MainAnimation/OutlineAnimation` 默认引用同一 AnimSequence，Sequencer 创建两条同起止帧的动画轨道，没有 LeaderPose。用户可以在蓝图实例中分别修改动画资产。

此蓝图依赖 `MMDBridgeCharacter` Runtime 插件，即使 SDF 已关闭也需安装插件；没有 `outline` 的自选配置保留原单网格流程。描边副本保留源材质的反壳算法，并对透明材质使用匹配的透明分支，描边组件不投影、不碰撞。若一个模型的 PMX 全部关闭描边，则按部位使用默认黑色描边，排除眼、眉、睫毛、口腔等面片并报告该回退。当前宽度按材质槽控制，PMX 每顶点的 edge scale 尚未写入描边遮罩，极薄表面和异形法线仍需视觉复核。

配置模板见 `profiles/character_materials/ZZZ_Render_PMX.json`。`parents` 是完整父材质路径，`textures/scalars/vectors/switches` 按语义指定参数名。`required` 为必须存在的参数；`packed_textures` 按 rgba 明确声明源通道、反转和比例。`source_rules` 通过正则匹配附带贴图，`{base}` 替换成 PMX 基础图的文件名主体。只有唯一匹配会绑定；未登记用途和多候选只报告，不猜通道。

`role_rules` 可以明确识别 face、skin、hair、eye、cloth_1/2/3。`role_presets` 使用这些部位及 default 分组，在 scalars/vectors/switches 中直接登记参数名和数值；部位值覆盖 default 值。例如 `{"skin":{"scalars":{"Roughness":0.6}}}`。透明度分支优先采样材质实际使用的 UV，避免将图集中未使用的渐变 Alpha 当作角色半透明。控制图使用线性 RGBA8 保留通道，距离图使用 R16F。

PMX 基础图 Alpha 按重复 UV 寻址采样，材质透明度为零时使用遮罩裁剪；半透明实例保留材质 Alpha，当前适配版的自发光同时乘以 Alpha，避免隐藏覆盖层仍输出颜色。材质实例启用 SkeletalMesh 和 MorphTargets usage，源母材质保持原文件。

例如把已有 AO 的 G 通道反转后装到目标图 B：

```json
{
  "version": 1,
  "id": "MyShader",
  "parents": ["/Game/MyShader/M_Character"],
  "required": ["ColorMap", "ControlMap"],
  "textures": {
    "base_color": {"parameter": "ColorMap", "color_space": "srgb"},
    "control": {"parameter": "ControlMap", "color_space": "data"}
  },
  "vectors": {"diffuse_color": {"parameter": "ColorTint"}},
  "scalars": {"opacity": {"parameter": "Alpha"}},
  "packed_textures": {
    "control": {"channels": {
      "r": {"value": 0}, "g": {"value": 0},
      "b": {"source": "ao", "channel": "g", "invert": true}, "a": {"value": 1}
    }}
  },
  "source_rules": [{"semantic": "ao", "pattern": "{base}_AO\\.png"}],
  "sdf": {"format": "none"}
}
```

本材质的 Lightmap.R 编码来自 CB_LightMap_R 实测：Skin=0、Cloth1=98/255、Cloth2=18/255、Cloth3=181/255。G 为第一组 Matcap 权重，B×A 为第二组；A 不是透明度。AO 属于 MN.B。PMX sphere 引用与模式按材质槽保留；附加 UV 的 subtexture 模式不能当普通 Matcap 使用。

缺少 Lightmap 时按 PMX 材质部位和 sphere 模式生成常量控制图；缺少 MN 时使用 `[0,0,1,0]`（中性法线、无 AO 衰减、无额外 Matcap 权重）。未引用基础图的材质使用白图乘 PMX 材质颜色。PMX 引用了文件但文件丢失是另一种情况：提取阶段报告缺失，不能靠中性图恢复原来的花纹、瞳孔或透明裁剪。缺少 sphere 不启用其权重；PMX Toon 不作为当前默认明暗依据。用途不明的附带控制图仅列为候选，不从基础色亮暗猜法线或金属遮罩。

以下为暂缓的 SDF 实验实现记录，不在当前默认流程中执行。显式启用 `threshold_lr` 时，原始面部几何生成水平每 22.5° 的 16 张遮罩，分别沿左右的正面至背光半圈计算距离场和进入阴影的角度，合成为一张 1024×1024 渐变 EXR。R 为正 yaw 阈值，G 为负 yaw 阈值，B 为面部覆盖，A 固定 1；0、0.5、1 分别对应 0°、90°、180°。两个方向独立计算，不强制镜像 UV。`face_sdf_R.png` 是 R 通道的灰度预览，材质实际绑定的是 EXR。

以参考灰度图的黑白分布为目标，默认 `smooth_sigma=2` 对阈值图做覆盖遮罩归一化的高斯平滑，避免眼部 UV 空洞拉黑周边。`contrast=8` 使用单调 sigmoid 编码增强暗侧和亮侧：原角度值 0.25、0.75 约编码为 0.018、0.982；材质中的 `SDF_Contrast` 对光照角度应用相同编码，保持比较的意义。中间灰度不再等于角度除以 180；0/0.5/1 的标定仍保持。编码增强贴图对比，平滑会小幅调整局部边界。修改这些配置需重新生成，不应单独修改实例中的 `SDF_Contrast`。

相邻方向使用距离场零交叉合成阈值，`sdf.interpolation` 可为 `linear` 或默认 `pchip`。PCHIP 使用相邻区间的单调三次 Hermite 插值，使距离场随角度变化的一阶导数连续，并在每个进入阴影的区间内求零点；不做逐图 min/max 拉伸，避免改变角度标定。算法参考 [Naga 的相邻距离比例方法](https://nagakagachi.hatenablog.com/entry/2024/03/02/140704) 和 [PCHIP 公式](https://docs.scipy.org/doc/scipy-1.15.2/reference/generated/scipy.interpolate.PchipInterpolator.html)，实现仅依赖 Blender 内置 NumPy，不需要 SciPy 或外部 EXE。

单图分支接入复制母材质原来的 Step、If 和明暗配色分支，仅适配采样通道、裁剪 UV 和头部方向输入。贴图为线性 FloatRGBA 半浮点资源、无 mipmap，1024 图约 8 MiB。暗部颜色由 `role_presets.face.vectors.ShadowColor` 控制，默认中性灰 RGB 为 `[0.2,0.2,0.2]`（HSV 的 V=0.2），A 保持 0；普通光照分支也使用此颜色。贴图灰度控制切换角度，不能用拉黑贴图代替降低暗部颜色。

原母材质 `Shadow_Smooth` 作用于普通 N·L 分支；原 SDF 的 Step 比较会硬截断，采样前的 `BlurIntensity` 九点模糊不等于最终阴影柔边。当前单图采样不再使用该模糊参数。适配副本增加 `SDF_EdgePixels`，默认 1.5；0 使用原硬截断，1–2 仅在边缘做窄过渡，4 更柔和。它通过阈值图的屏幕导数调整过渡宽度，保留明确的亮暗块。该参数可在生成的材质实例中直接修改，不需重新烘焙。

`sdf.nose_cutout` 默认启用几何鼻尖候选定位，分别在左右扫掠的鼻尖侧面生成独立亮区；`end_angle=135` 表示亮区至少保留至该角度附近，再进入阴影。亮区在主阈值图平滑后合成，避免小形状被整图模糊抹掉；`nose_cutout.png` 保存两个方向的区域权重。表达方式参考 [Anime SDF Gen 的闭合亮区和单向传播规则](https://github.com/xht8723/Anime-SDF-Gen/blob/main/output-format.md)，没有引入该项目的代码。这里的几何鼻尖识别是程序自有启发式，在鼻部不突出、有效纹素不足时跳过并记录 `needs_review`；识别成功仍需确认位置与形状。

保留黑白遮罩与收据，可修改相同缓存目录的方向遮罩，下一次转换核对哈希后重新合成。一张单轴阈值图不能完整保存“亮→暗→亮”，生成收据记录 `non_monotonic_fraction`，并输出 `non_monotonic.png` 标记受影响的 UV。自动鼻影和脸颊形状仍需视觉复核。

旧 `direction_atlas` 格式继续支持：自定义配置设为此格式，登记 `sdf_atlas→SDF_Atlas`（half_float）、`sdf_coverage→SDF_Coverage`（data）及 `sdf_atlas→UseSDFAtlas` 开关。它生成 114 个水平及垂直方向，512 像素格组成 16×9 R16F 图集，约 72 MiB。默认单图只控制水平转光，完整上下方向需使用此格式。无头骨、UV 重叠或不能可靠选择面部时，保留普通光照并标记复核；`none` 可关闭 SDF 生成。

`sdf.cheek_triangle` 独立控制鼻旁的面颊三角亮区，默认启用，`end_angle=98`。三条有向边的平滑最小距离生成圆角三角形，左右分别锚定已识别的鼻部，面颊深度限制排除背面；区域在主图平滑后合成，鼻侧小亮点仍独立保留。`cheek_triangle.png` 保存两个方向的区域权重。三角中心编码约 0.671，比鼻尖亮点更灰，并随转光进入阴影。固定面部比例是程序的风格预设，不是几何烘焙必然产生的形状；异常脸型、缺少可靠鼻尖或区域纹素时标记 `needs_review`。可在配置中关闭此区域或修改保留角度，修改后重新转换。

[Anya 的知乎文章](https://zhuanlan.zhihu.com/p/655758761) 已通过浏览器核验正文和示例图。它演示已有 SDF 的阈值比较、幂次调整、左右采样翻转和明暗颜色混合，示例图包含三角亮区与鼻侧小亮点；正文没有提供三角区自动生成算法。本程序在生成阶段实现独立亮区，不将文章的着色代码误认为生成算法。

动态面部方向依赖 `MMDBridgeCharacter` Runtime 插件。每个骨骼组件的自定义数据索引 20–31 用于头部三轴，原索引 0 的对象数据不受影响。组件在骨骼变换完成后及编辑器更新时刷新，能放入以后生成的组合蓝图。

在当前 FBX 导出/导入约定下，Blender −Y 前方、+X 横轴、+Z 上方对应 UE +X、−Y、+Z。图集文件从上至下保存 +90° 至 −90° 俯仰方向；UV 变换保留 Blender UV 空间，采样端换算 UE 的 V 翻转。其它图集 Shader 必须遵守这些约定，并在 `sdf.runtime.data_indices` 声明 `[20,24,28]`。当前主材质使用 Atmosphere Sun Light Index 0 的方向光。

源码版构建插件：用当前 UE 的 `RunUAT.bat BuildPlugin` 构建 `plugins/unreal/MMDBridgeCharacter/MMDBridgeCharacter.uplugin`，将生成的 Binaries 放回插件源码目录。安装时会核对 UnrealEditor.modules 的 BuildId；其它 UE 构建不能复用 DLL。

缓存记录输入、配置、生成设置与母材质依赖。修改配置后，应重新生成角色；程序会拒绝将旧 Blender 数据绑定到新通道契约。
