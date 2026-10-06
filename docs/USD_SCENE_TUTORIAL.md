# 通用 USD 场景流程教程

本流程是主项目第二阶段的场景通路。角色、动作、表情和 VMD 相机由 MMD2Unreal 官方窗口导入，角色材质不在这里处理。新任务目标 UE 5.8，Blender 4.3.x。默认采用工具场景母材质；显式启用后才尝试只读匹配 `M_00` / `MF_PSR`。

## 1. 导出与刷新

1. 在“素材与软件”选择 `.blend` 场景、Blender 4.3.x 和目标 `.uproject`。
2. 在“参数”把“场景导入通路”设为“USD Stage”，指定“场景参考帧”。该帧决定 Shape Key、骨骼变形、修改器、Geometry Nodes 和已缓存模拟的求值结果。
3. 材质策略通常保持“自动判断”（简单材质直接还原，复杂小物件烘焙，大面积平铺表面还原）；烘焙分辨率选“自动”时，工具按源图最大尺寸选择 1K、2K 或 4K。显式设置可到 8K。
4. 开始任务。工具从 `depsgraph.object_instances` 读取最终显示几何，按原型和三角面预算写成多个 Payload。
5. 首次连接项目时安装桥接。安装器先备份 `.uproject`，再启用 `USDImporter`，不会修改其他插件开关。重启 UE 后连接项目。
6. UE 关卡中只有一个 `MMD USD Stage` 场景 Actor；重复刷新会复用持久化 `UsdAssetCache3`，不会生成数百个 FBX 导入任务。

项目外的导出批次包含一个完整可搬运包。UE 项目内的运行副本位于：

```text
<Project>/Content/<内容目录>/A_<稳定ID>/Source/Scene/
  Scene.usda
  Geometry.usda
  ToolMaterials.usda
  Geometry/Payload_*.usdc
  Textures/
  package.json
```

`Scene.usda` 是用户覆盖根层。第一次部署后，工具刷新只替换 `Geometry.usda`、`ToolMaterials.usda`、`Geometry/`、`Textures/` 和 `package.json`，不会覆盖项目内已有的根层。

## 2. Geometry Nodes、实例和 Payload

工具不读取对象的原始代理 Mesh，也不使用控制立方体的 Bounds。它遍历最终依赖图：

- 普通 Mesh、修改器、Array、Curve、Text、Metaball 和可求值 Hair Curves 都先转为当前帧的最终 Mesh。
- Geometry Nodes、Collection Instance、粒子实例和链接副本保留各自世界变换。
- 拓扑、材质槽、UV/属性和碰撞设置相同的结果只写一份原型。大量停车挡块共享同一 Mesh 和碰撞烹制数据。
- 具有自身几何又输出实例的对象会同时保留两部分。
- 隐藏原型可以作为实例来源，但不会再以可见场景对象重复导出；空的控制对象也不会进入 USD。
- 单个对象无法得到最终求值几何时跳过该对象，并在最终报告中列出；不会悄悄退回可能导致错误碰撞盒的原始网格。

Payload 以原型为不可拆分单元装箱。这样同一原型的实例不会为了满足预算而复制碰撞数据。`manifest.json` 的 `scene.stats`、`scene.prototypes` 和 `scene.usd.payloads` 给出原型数、实例数、唯一/实例化三角面数、复用率及每块大小。

## 3. 材质自动策略

### 对象自动分类与覆盖

实际求值尺寸中最长边至少 4 米或最大两维包围面积至少 8 平方米，并且满足平面特征（最小维小于 2 厘米），或薄表面（最小维不超过最长维的 20%）且 UV/Mapping 重复大于 1.25，默认归为平铺表面。最长边至少 8 米、第二长边至少 3 米且最长边至少为最短边 3 倍的大型立体网格，如长柱、梁，若材质需要烘焙，则按空间分块并为每块独立烘焙；其他归为独立物件。阈值是可解释的启发式，不是仅按名称分类。

对象自定义属性 `mmd_bridge_surface_class=auto/small/tiled/chunked` 可以覆盖分类。`mmd_bridge_material_strategy=auto/nodes/hybrid/bake` 可设在对象或材质上，优先级为对象 → 材质 → 任务设置；显式策略优先于自动分类。auto 首先检查有效表面：基础 PBR 常量、静态贴图、切线法线和受支持的简单计算直接 nodes，不新增烘焙 UV 或贴图；未连接的复杂节点不影响判断。复杂小物件自动 bake，平铺表面自动 hybrid。显式 bake 仍优先执行。

基础纹理保留原 UV，遮罩使用 `MMD_BakeUV`。共面的平面采用同源材质的统一世界投影范围，因此相邻边界使用相同采样基准；非平面采用非重叠图集。烘焙保留 16 像素扩边，UE 遮罩使用边缘钳制。独立 UV 法线通过 UE DeriveTangentBasis 校正切线基。跨网格、远距离 mip 和非平面接缝仍须视觉核验；报告不会自动把它们标为通过。

测试场景为 MyGO 停车场。报告新增 `surface_policy`、`bake_layout`、`bake_margin_pixels`、`requires_visual_review`。每份烘焙区分几何、UV、策略、分辨率与位置相关坐标，防止大小物件共用材质时错用贴图。

每个材质记录都有 `strategy`、`strategy_reasons`、`channels`、`baked_channels`、`bake_resolution` 和最终 UE 资产路径。

- `nodes`：贴图/常量、UV、常见二维 Mapping（含 Texture 逆变换与 Z 轴旋转）、Math/Mix、ColorRamp、Normal Map、Bump 等能稳定对应 UE 时，生成独立的 `_MMDBridge` 材质图和实例。
- `hybrid`：可翻译部分仍是 UE 节点；Noise、Voronoi、复杂三维坐标和 Shader Mix 等只烘焙所需通道或遮罩。
- `baked`：用户强制 bake，或所有有效通道都必须烘焙。烘焙副本创建 `MMD_BakeUV`，源 `.blend` 不保存。
- 不支持的 OSL、第三方闭包、纯 Volume 或缺数据表面会明确失败；不会用紫色诊断材质冒充成功。

材质自定义属性 `mmd_bridge_material_strategy` 可设为 `auto`、`nodes`、`hybrid` 或 `bake`。强制 `nodes` 而图中存在不能翻译的通道时，工具会报错，便于发现错误而不是降低画面质量。

打包在 `.blend` 内的图片直接从 `packed_file` 提取，因此原来的 `textures` 文件夹已不存在也能工作。

对于只有 Albedo/BaseColor、Normal、Roughness 等贴图，或贴图后仅有简单乘加/色调参数再连接 BSDF 的材质，显式启用用户母材质选项后，UE 端会先读取 `/Game/SelfMakeMaterial/Material/M_00` 的真实纹理、数值、颜色和静态开关参数。只有所有必需通道及计算参数都能匹配时，才创建 `M_00` 的材质实例并打开对应贴图开关；否则自动回到工具生成材质。参数名不是硬编码猜测，`MF_PSR` 也不会被工具修改。

## 4. MyGO 地面积水和停车线

`Patterned_Concrete_ugfnfifo` 会被识别为混合表面：

- 混凝土 Albedo/AO/Roughness/Normal 与 UV 关系保持为可重新打光的 PBR 输入；分析器会在命名遮罩进入最终颜色混合的边界处分离基础混凝土与覆盖层，避免把停车线连同整张 Base Color 一起烘焙。
- 从 `unwater` 到下游 Noise/ColorRamp/Mix 的最终结果烘焙为 `water_mask`。
- 从 `parkline v1.png` 到磨损节点的最终结果烘焙为 `parking_line_mask`。
- UE 材质用积水遮罩降低粗糙度、压平局部法线、乘湿润暗色，并按 IOR 1.45 设置约 0.42 的 UE Specular。
- 停车线遮罩在湿润混凝土之上恢复线色和独立粗糙度。相关颜色与粗糙度是材质实例可调参数。
- 2.5 mm 位移进入 Normal/Height 细节，不改变物理地面高度，因而不会使角色、车辆或雨滴碰撞表面上下抖动。

检查材质时，在 `manifest.json` 搜索源材质名，确认 `strategy=hybrid`、两个 `special_masks`、烘焙分辨率和 `_MMDBridge` 资产名。随后在 `ue_report.json` 检查 `assets.materials` 和 `checks.unsupported_materials`。

## 5. 精确碰撞和雨滴

除非对象自定义属性 `mmd_bridge_collision` 明确设为 `none`，每个最终 Mesh Prim 都写入：

```text
UsdPhysicsCollisionAPI
physics:collisionEnabled = true
UsdPhysicsMeshCollisionAPI approximation = "none"
```

UE Stage Actor 的 `FallbackCollisionType` 为 `None`，因此未声明碰撞的控制物体不会被自动包成 Box/Convex Hull。生成的 Static Mesh 使用 `UseComplexAsSimple`；组件为 `QueryAndPhysics` 和 `BlockAll`。

验收方法：

1. 在 UE 视口打开 **Show → Collision**（或控制台 `show collision`）。不应看到控制立方体或明显大于显示网格的凸包。
2. 从模型六个轴向各做一次 Line Trace。报告中命中点与显示三角面误差应不超过 1 cm。
3. 对 Niagara 雨滴使用 Collision 模块，CPU 粒子选择 Scene Query；GPU 粒子按项目渲染器选择 Global Distance Field 或 Hardware Ray Tracing。若必须精确命中薄停车线/挡块，优先 CPU Scene Query。
4. 高面数雨伞仍是逐三角形碰撞。工具报告风险但不自动简化；运行时性能由项目负责人决定是否对指定对象关闭碰撞。

## 6. 不被重导出覆盖的根层修改

在 USD Composer 中把项目内的 `Scene.usda` 设为编辑目标，再建立 Override，例如覆盖 `/World/Materials/M_<id>` 的绑定或添加项目专用灯光。不要直接修改 `ToolMaterials.usda` 或 `Geometry.usda`，它们是工具所有层。

如果需要恢复纯工具结果，可先把项目内 `Scene.usda` 另存备份，然后删除它并刷新；工具会重新创建只包含两个工具子层的新根层。这个删除是显式的用户操作，自动化不会替你删除覆盖层。

## 7. 旧任务与故障定位

- schema v1 任务没有 `scene_format`，恢复时自动走 `fbx_legacy`。
- schema v2 新任务默认 `usd_stage`，开发期间仍可在界面显式选旧 FBX 通路做对照。
- 先看任务 `status.json`，再看 Blender 日志、`manifest.json` 和 `ue_report.json`。
- 若 UE 报找不到 `UsdStageActor` 或 `UsdAssetCache3`，确认项目插件中 `USDImporter` 已启用并完整重启编辑器。
- 材质失败会包含具体节点类型与通道；先尝试 `hybrid`，只有无法拆分的闭包才改为 `bake`。


## 导出进度与停止后恢复

USD 写入显示分块、逐原型进度、三角形数和实例数，并显示耗时及进程内存。冻结求值结束后的“大型立体物体已分块”是准备材质分块烘焙，随后分析材质、烘焙贴图和写入 USD；精确碰撞在 USD/UE 阶段处理。

“停止任务”立即终止本任务的 Blender/MMD 进程；正在执行原生 UE 导入时也会终止目标工程进程，未保存的当前操作会丢弃。已落盘的还原点保留。job.json 是配置和输入指纹，不包含内存中的网格；恢复时必须同时保留整个任务目录及其中的断点文件。

冻结阶段每完成 500 项或经过约 5 秒，在下一项边界提交一批新增几何和实例数据；几何使用压缩 `.blend`，元数据使用压缩 JSON，共享网格只保存一次，材质和打包贴图从源文件重新关联。恢复会校验源文件、导出参数、版本、已提交批次文件及实例顺序，跳过已冻结前缀的几何复制。场景仍需打开并初始化依赖图；当前尚未提交的一批以及单个物体正在执行的求值会重做。尺寸统计也按批次保存已经完成的网格和当前网格的实例位置，恢复从保存位置继续。

冻结和材质分块完成后提交完整压缩几何快照，再清理已被替代的冻结批次，避免长期保存两份相同几何。材质分块本身中断时会重做分块，但已冻结几何和尺寸统计复用。材质分析按完整网格建立还原点，完整 USD 分块也会复用。旧任务已经丢失的内存进度无法补回，第一次用新代码恢复后才会建立分批断点。几何快照的实际占用取决于求值后的网格大小，不能只保存一个很小的进度文件就恢复复杂几何。

所有新 GUI 导入任务都使用所选中转目录。未选择时在桌面创建 `Middle_角色名.场景名_01`，重名递增为 `02`、`03`；只选一种素材时使用该素材名。任务位于其中的 `MMDBridge_Jobs/<任务名>/job.json`，场景 USD、贴图和快照也都在该中转目录内。源文件和用户 Scene.usda 覆盖根层保留；源素材或导出参数变化需新建任务。

USD 场景默认排除自身最长边小于场景最长边 0.5% 的物体碰撞；实例逐个按自身大小判断，分块地形使用分块前的大小。参数“碰撞最小尺寸 / 场景最长边”可调整，0 表示关闭筛选。Blender 自定义属性 `mmd_bridge_collision=exact` 强制保留，`none` 强制关闭；关闭“精确碰撞”会关闭所有物体碰撞。

尺寸统计批量读取冻结网格的顶点，用 NumPy 计算实际变换后的顶点边界；进度按秒更新，同时显示网格数、实例数和耗时。没有用旋转后的包围盒八个角替代实际顶点，因此不会为了提速扩大物体边界或改变空间材质使用的边界。

最终网格分析每完成一个网格，只写新增的压缩元数据日志；共享几何的 USD 还原点只写一次。整份 `scene_checkpoint.json` 在阶段结束时合并保存，避免数万个物体导致每分析一个网格都重写全场景。旧版完整断点仍可恢复。普通节点材质分析使用未接入场景的临时物体，需要原生烘焙时才接入，完成后释放临时网格与材质。进度会显示材质、几何和断点保存的累计耗时，帮助区分材质烘焙与文件保存瓶颈。

正在运行的 Blender 使用启动时加载的代码。更新开发版后，先停止任务，再恢复同一个 `job.json`，新进程才会使用优化并复用已完成的网格。首次恢复仍需读取和校验还原点；复杂材质烘焙的耗时不会因断点写入优化而消失。

相对尺寸只是一种兜底规则：100 米场景的 0.5% 是 0.5 米，1 公里场景则是 5 米。场景越大，越容易排除小物体，也可能误排椅子和栏杆。已经合并成一个大网格的落叶按整个网格判断，无法仅凭最长边识别叶片。需要确定结果时，给叶片物体或散布发射器设置 `mmd_bridge_collision=none`，给地面、墙和树干设置 `exact`；树叶与树干合并时，应分离碰撞用途或提供独立碰撞模型。当前没有自动识别叶片，也没有按连通块拆分碰撞。
