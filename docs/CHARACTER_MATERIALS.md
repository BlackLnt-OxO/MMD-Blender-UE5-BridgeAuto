# 角色材质

“工作区 → 角色设置 → 角色母材质适配配置”控制 PMX 材质的创建与绑定，独立于 Blender 场景的材质工作流。

![角色和眼睛母材质及适配配置](images/gui-v0.3.0/character-settings.png)

图为 v0.3.0 的独立角色设置页；内置 PBR 模式下母材质路径由程序管理。

## 配置选择

| 配置 | 何时使用 | 转换依据 |
| --- | --- | --- |
| UE 官方卡通 | 使用 UE 官方 Substrate Toon | 工具创建 Toon 母材质与实例，不使用自选母材质路径 |
| 内置PBR角色（默认，第二项） | 原生PBR、皮肤散射和各向异性头发 | MatCap跟随原PMX，提供逐材质修正及应用／撤销，见[详细说明](CHARACTER_PBR.md) |
| 内置卡通渲染 | Cel 原工程三层角色蓝图 | 保留曲线及部位实例参数；笔刷、边缘光默认关闭，提供 A／B 明暗配色，见[详细说明](BUILTIN_CEL.md) |
| ZZZ_Render_PMX | 使用配套 ZZZ 材质与描边 | 已登记父材质、参数契约、PMX 数据和部位规则 |
| 自动按母材质匹配 | 已有自己的 UE 母材质 | 用完整资产路径匹配登记配置；没有专用配置时走通用映射 |
| 自选母材质（手动贴图映射） | 使用自己的 Material 或 Material Instance Constant | 读取真实 Texture Parameter，由用户指定来源类型；不自动猜贴图参数 |

新任务默认内置PBR角色。缺少该字段的旧任务／旧预设按 `auto` 读取，避免改变既有任务；自定义配置 id 支持载入和保存，菜单仅允许选择已有项。

导入后在 UE 的材质实例、散射配置和贴图编辑器中调整参数，见 [UE 参数含义与调节指南](UE_PARAMETERS.md)。该指南区分内置 PBR 与专用／自选母材质，不能只凭相似参数名套用其它母图的含义。

## 贴图颜色编码

颜色贴图按导出的 sRGB 用途设置 `Source Color Settings → Encoding Override = sRGB`，同时启用纹理 `sRGB`。法线、遮罩及其它线性数据明确使用 `Linear` 源编码并关闭纹理 `sRGB`；HDR 保持线性。源编码用于把输入解码为光照计算所需的线性颜色，纹理 `sRGB` 则控制 GPU 纹理的存储与采样，两者不能互相替代。

UE 对 16 位 RGBA 源像素默认按线性值解释，仅勾选纹理 `sRGB` 会使本来已按 sRGB 编码的 16 位 PNG 变亮。工具现在明确指定源编码，保留原图位深、RGB 与 Alpha，不通过压暗原图补偿。原有资产不会因更新源码而在后台批量改变；再次执行对应导入时，仍先检查归属、来源和后续手工编辑，再更新工具管理的贴图。

## 缺图补选

从桌面开始新的 MMDBridge 角色或组合转换时，工具检查原 PMX 的基础色和启用中的 Sphere 引用，包括透明度为零的隐藏槽。发现缺图会打开现有的角色材质分析窗口，并定位到第一个缺图槽；窗口列出原槽位、原引用和透明度。

在“角色材质与贴图 → 分析与配置材质…”中也可提前配置。选择文件后保存映射再转换；映射按原 PMX 身份和原槽位隔离，随角色设置保存。内置 PBR 保留完整参数配置；其它预设在此窗口补选基础色和 Sphere，保持原着色预设及 PMX 透明度。

尚无对应文件时，可明确选择“保留缺图诊断并继续”。取消则不创建转换任务。直接调用无人值守入口不会伪造用户选择：新任务默认复核策略下，可导出的缺图结果保留诊断并进入复核；旧任务的严格策略仍按原设置处理。手选文件只绑定到 Blender 导入副本，不修改原始 PMX 或贴图；手选文件或映射变化后须新建任务，不能复用旧导出。

## UE 官方卡通

工具通过 `SubstrateToonBSDF` 和 `ToonProfile` 建立母材质，再绑定 PMX 基础图、颜色等输入。角色和眼睛母材质路径值会保留，但输入框禁用。

目标项目必须有可用的 Toon API，并满足：

```text
r.Substrate=1
r.Substrate.ProjectGBufferFormat=0
```

不满足时明确报错，不自动改成 PBR，也不自动修改项目 Substrate 配置。项目配置需手动修改并重启 UE 生效，调整前确认对其它材质与渲染设置的影响。

缺少新版映射标记的旧任务仍按原 `auto`／`generic` 规则恢复；新版自选母材质任务需先读取参数并保存映射，母材质不存在时明确报错。显式选择官方卡通时，不兼容则停止。

## ZZZ_Render_PMX

内置配置见 [ZZZ_Render_PMX.json](../profiles/character_materials/ZZZ_Render_PMX.json)，登记父材质为 `/Game/Character/MasterMaterial/M_RenderMaster`。工具安装原模板及依赖，在任务管理的复制品上适配，不改写原材质，也不覆盖目标工程里同路径但内容不同的资产。

选择 ZZZ 时，仍是通用默认值的角色路径会换成模板路径，默认眼睛路径清空以使用配置里的眼部母材质。已经明确自选的路径保留。

### 贴图映射与部位识别

- 基础图映射到 `BaseTex`，没有引用基础图时使用白图乘 PMX 材质颜色。
- 部位由 PMX 中英文材质名和配置正则规则识别，包括面部、皮肤、头发、眼睛及三组衣服。
- 附带贴图只在已登记用途且唯一匹配时绑定，多候选或用途不明只报告。
- 缺少 Lightmap 时按部位和 sphere 模式生成常量控制图；缺少 MN 使用 `[0,0,1,0]`。
- 源文件明确引用的贴图丢失会报告缺失，中性图不能恢复原花纹或透明裁切。

ZZZ 母材质的 Lightmap.R 使用 Skin=0、Cloth1=98/255、Cloth2=18/255、Cloth3=181/255。G 是第一组 Matcap 权重，B×A 是第二组，A 不是透明度；AO 位于 MN.B。PMX sphere 模式按槽保留，附加 UV 的 subtexture 不能直接当普通 Matcap。默认明暗使用母材质的三色／Ramp，PMX Toon 数据继续保留供其它配置使用。

工具自动生成 Lightmap 的衣服槽，会归一化 Cloth Ramp 的亮面强度，避免源专用深灰再次乘到 PMX 基础图上。`Ramp_Cloth_1/2/3_LitGain` 可调整，设为 `[1,1,1]` 恢复原曲线强度；用户自带 Lightmap 不自动归一化。面部 ShadowColor 默认 HSV V=0.2。

### 眼睛、透明和描边

眼睛路径留空时，ZZZ 的 `default_eye_parent` 用于眼睛、眼白和瞳孔，眉毛与睫毛不归入眼球。默认 ShadowColor=0.65、TransitionColor=0.85，保留自发光；源图的黑瞳不会被洗白。用户明确自选的眼睛材质保持原样。

Alpha 按材质实际使用的 UV 采样，包括重复寻址与零面积 UV 三角形。裁切、渐变透明和 PMX 材质 Alpha 分别保留；半透明自发光也乘 Alpha。生成实例启用骨骼网格与 MorphTargets usage，不改源母材质。

启用 outline 的配置生成 `/Game/Character/MMDBridge/<任务>/BP_Character`：两层组件引用同一个 SkeletalMesh，分别绑定主材质与描边材质。MainAnimation／OutlineAnimation 默认相同，在 Sequencer 中各有同起止帧的动画轨道，不使用 LeaderPose。

描边使用 `M_OutLine` 副本，参考 PMX 的描边开关、宽度与颜色。默认 scale/face_scale 为 0.1/0.04 厘米；描边层不投影、不碰撞。全部 PMX 材质关闭描边时，会使用按部位排除眼、眉、睫毛、口腔等面片的默认黑描边，并报告回退。当前按材质槽控制宽度，尚未把每顶点 edge scale 写入遮罩，薄表面与特殊法线需要复核。

默认 `sdf.format=none`，不生成面部 SDF、不启用分支，也不创建面部方向组件。实验说明放在 [SDF 技术附录](advanced/CHARACTER_SDF.md)。

## 自定义母材质

UE 中已经手调好的角色材质可保存为独立配置文件，包含实际参数、父材质和槽位对应，再应用到另一个选中角色或新导入角色。此功能位于“角色设置 → UE 材质配置文件”，不依赖下面的 PMX 来源映射，见 [材质配置文件](CHARACTER_MATERIAL_PRESETS.md)。

新任务选择“自选母材质（手动贴图映射）”，在现有“分析与配置材质…”窗口操作：

1. 填写本 PMX 的默认角色／眼睛父材质 `/Game/...` 路径，也可逐槽选择不同父材质或把当前父材质批量用于选中槽。
2. 连接目标 UE 工程，点击“读取全部使用的 UE 母材质参数”。表格显示实际 Texture Parameter、有效默认贴图及支持状态，包含普通材质函数和父实例继承的参数。
3. 为目标参数选择 PMX 来源类型。例如 `FancyBase` ← 基础色，工具将每个槽自己引用的基础图分别连接到它。统一规则按母材质保存；“当前原槽的独立覆盖”可改用其它类型，或明确保留 UE 默认。切换父材质不串用逐槽规则。
4. 在“PMX 来源与补选”核对真实引用、Sphere 启用状态和缺图；需要时手选法线、控制图等补充图片并声明用途。目录里存在图片不代表 PMX 使用它。可选择 RGB 或单独 R/G/B/A，支持反转；数据图使用 Linear，法线使用完整 RGB，需与母材质采样类型一致。本轮不支持多图通道打包。
5. “预览全部槽”检查后保存。未指定参数、未引用或禁用的来源均不写贴图覆盖，继承父材质在 UE 中的有效默认；明确映射到丢失文件则需补选或改为保留默认。

数值只同步可唯一识别且类型一致的 PMX 原始颜色、透明度、镜面颜色、镜面指数和环境色。不会生成 PBR 粗糙度、金属度等推断值；任意命名数值仍使用父材质默认。父材质决定透明模式和着色逻辑，工具不会为自选父材质改图；不透明父材质不能表达 PMX 半透明或隐藏状态时会保留复核诊断。

映射与补选按原 PMX 哈希、工程、原槽及父材质保存，和内置 PBR 参数／几何设置独立。可“导出映射…”为 JSON，再在另一 PMX 手动“载入映射…”；模板仅携带父材质路径与统一类型规则，不携带源图路径、原槽绑定、UE 参数快照或 PBR 参数，载入后需重新读取目标工程参数。

已有角色可在 UE 选中带可靠来源记录的 SkeletalMeshActor／组件，再在分析窗口选择原槽并“应用选中槽到 UE”。工具创建独立材质实例、验证绑定并保存当前关卡；不改网格。独立“撤销上次自选材质应用”仅恢复该批绑定，后续人工编辑冲突会拒绝覆盖，和 PBR 撤销记录分开。应用前保存其它关卡编辑。

普通二维 Texture Parameter 支持手动映射；Cube、Array、Virtual Texture、不能唯一定位的材质层／函数实例等显示诊断。本机读取的参数或父材质依赖改变后需重新读取并准备，旧检查点不会静默复用。此功能包含在 v0.3.0；旧 v0.2.0 EXE 需升级或改用源码启动。

### 已登记专用适配与旧任务

`auto` 仍以完整父材质路径查找已登记 JSON：唯一匹配使用原专用适配，多重匹配报错；无匹配的新任务进入上述手动映射。内置 PBR、官方卡通、ZZZ、部件识别、几何及场景材质的匹配规则不受此改动影响。

“角色设置 → 角色通路与兼容 → 母材质配置目录”存放用户映射 JSON，内置配置自动读取。默认目录为 `%LOCALAPPDATA%/MMDBridge/material_profiles`。

配置示例：

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
  "packed_textures": {"control": {"channels": {
    "r": {"value": 0}, "g": {"value": 0},
    "b": {"source": "ao", "channel": "g", "invert": true},
    "a": {"value": 1}
  }}},
  "source_rules": [{"semantic": "ao", "pattern": "{base}_AO\\.png"}],
  "sdf": {"format": "none"}
}
```

`textures/scalars/vectors/switches` 声明语义与参数名；`required` 是必须存在的参数。`packed_textures` 逐通道声明来源、反转和比例，示例把 AO 的 G 反转后写入控制图 B。`{base}` 是基础图文件名主体。

`role_rules` 识别部位，`role_presets` 直接声明 default 和各部位的参数值，部位覆盖 default，例如 `{"skin":{"scalars":{"Roughness":0.6}}}`。控制图使用线性 RGBA8，距离图使用 R16F；不要把数据贴图按 sRGB 解读。

## 插件与重新生成

ZZZ 双网格蓝图依赖 **MMDBridgeCharacter Runtime 插件**，即便关闭 SDF 也需要。无 outline 的自选配置保留单网格路线。

重复连接已加载该插件的工程时，相同哈希的 Runtime 文件直接复用；实际二进制变化仍需保存并重启 UE，不会通过忽略文件占用错误冒充更新成功。

发行包原生插件匹配 UE 5.8.2 / BuildId 55116800。其它引擎构建使用 `scripts/build_native_plugins.py --unreal "目标引擎的 UnrealEditor.exe"`，默认一起编译两个原生插件；仅构建角色插件时加 `--plugin MMDBridgeCharacter`。该入口检查专用工作目录的写权限、隔离 UBA 工作目录并限制异常日志，全部输出核对后才更新仓库 Binaries，再经 GUI 安装。其它 UE 5 版本需要显式允许未验证版本，并单独完成加载和功能验收；详见 [编译原生插件](INSTALL_WINDOWS.md#编译原生插件)。不能跨 BuildId 复用 DLL。

缓存记录输入、配置、生成设置和母材质依赖。改配置后重新生成角色，避免把旧 Blender 数据绑定到新通道契约。导入后重点检查眼睛、透明面片、衣服明暗与描边。
