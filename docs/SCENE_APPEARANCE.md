# Blender → UE 静态场景雾与光影

场景任务的「还原雾与光影」默认开启。运行过程是 Blender、OpenVDB、OpenColorIO 和 UE 原生程序转换，不调用 AI 或网络服务。

新任务采用灯光物理基准倍率 1、雾密度倍率 1、曝光补偿 0 EV。没有这些字段的历史任务继续使用原流程及原倍率。转换参考帧只使用最终求值、渲染可见对象和有效输出上游节点。

## 本机部署

完整路径需要 Blender 5.2 自带的 `openvdb`、`PyOpenColorIO`、`OpenImageIO`、NumPy，以及 UE 的 USDImporter、OpenColorIO 和桥接插件。导出前检查 Blender 模块，UE 导入前检查原生类；缺失会显示部署提示。GUI 的桥接部署复制所需 Python 模块并启用插件；正在使用旧桥接的 UE 工程需要保存、关闭并重开。

本轮验证使用 Blender 5.2.2 LTS、UE 5.8.2 和 TEST_ON_926。无需另外安装在线工具或下载模型。

## 转换结果

- 均匀世界雾使用可编辑的 Exponential Height Fog。RGB 消光无法由标量表示时改用 VDB。
- 局部网格雾、已有 VDB 和常见程序体积生成 OpenVDB，通过 USD Volume 导入原生 SVT/Heterogeneous Volume；纯体积容器不生成实体壳和碰撞。闭合网格内部裁剪使用原生 OpenVDB SDF。
- 节点组、Noise、Voronoi、Gradient、ColorRamp、Math、Mix、常用坐标变换和黑体发光由 Blender 原生节点求值。分块采样，默认最长轴 256。缓存包含源文件、参考帧、变换、分辨率和 Blender 版本；命中后核对文件哈希。
- UE 的 SVT 在体素索引空间积分。网格将对象缩放与剪切烘焙进等距体素，消光和发光系数按体素步长换算，并保留原始变换；改变分辨率不会改变积分密度。
- 世界 Surface 原生烘焙为线性 HDR。单个可分离程序天空生成环境和太阳，避免重复太阳光。
- 灯光保留功率、HDR 色彩、尺寸、阴影、散射、色温和聚光边缘。Blender 5.2 中灯光数据功率与 Cycles Emission 节点相乘。材质发光使用线性浮点 EXR。
- AgX/Filmic/Look、曝光、Gamma 和可迁移全局合成调色生成本地 OCIO 配置及 HDR LUT。UE 视口及 MRQ 使用同一配置，关闭额外色调曲线。单位校准与用户曝光补偿分别记录。
- 活动相机迁移画幅、传感器、焦距、光圈和沿相机轴计算的焦点距离；景深仍受两个渲染器各自的散景实现影响。
- 新光影任务在 Substrate 工程中用原生玻璃 BSDF 保留透射颜色和 IOR，避免把玻璃颜色当成漫反射或套用固定透明度。相同光学属性的两种 Glass 混合保留粗糙度图片遮罩；多闭包折射分布仍需复核。

生成包包含可选 `environment`、`volumes`、`render_look`；报告保存实际资源路径、原生体积组件和不支持项。重导入仅更新受管文件；用户 USD 根层和已有 `.uasset`/`.umap` 保留。

支持的 Mix/Add Shader 保留闭包树，用原生 Substrate 混合 Principled、Glossy、Diffuse、Metallic 和 Emission；每层保留法线、粗糙度、颜色和权重。程序字段由 Blender 原生烘焙为浮点 EXR，源 UV 与原始贴图保留；Fresnel 在 UE 随视角求值。节点组、旁路和源渲染器有效输出参与解析，未保留的闭包与高级分量具体列出。微表面分布、Schlick Fresnel 和粗糙漫反射仍有跨渲染器近似，需要视觉复核。

HDR/程序字段使用数据目标保存线性求值数值；原生烘焙会重设图像格式，因此烘焙结束后重新选择 EXR，避免用 EXR 后缀保存实际 PNG、额外 sRGB 编码与高亮截断。缓存版本及 EXR 文件头检查拒绝复用旧结果；原生检查验证 `0.18` 和 `4.0` 原值回读。

带独立覆盖遮罩与透光权重的薄网格保留 Masked 覆盖，透光使用原生 Two Sided Foliage 近似，并标记视觉复核。判断依据是通道结构及求值网格厚度，不依赖草、叶片或场景命名。没有薄网格证据时不套用植被散射；透明分支仍保留源 Alpha。历史任务保持原路径。

逻辑父级保留位置、旋转与大纲关系，网格缩放放在散件层，避免非均匀父级缩放与实例旋转组合后在 UE 中丢失剪切。最终求值实例本身含剪切时，将残差写进共享网格，并使用逆转置变换法线，保留逐角 UV 和原始变换记录。半透明表面采用 Surface ForwardShading，缓存复用也检查实际模式。镜头被半透明表面挡住的特殊折射差异暂不纳入本轮验收。

平铺表面优先保留原贴图、原 UV 和映射；不把整面地面默认合成大图。非周期的程序遮罩独立烘焙并保持覆盖范围。整面独有烘焙、可重复的小图与 UDIM 分块具有不同的细节密度和内存开销；大平面分类本身不证明节点图能够无缝循环。

后续修改必须进入共用转换路径，不能按测试场景或材质命名决定功能。验收覆盖其他节点组合、UV、实例、单位和缺失数据；MyGO 水渍只是一个样例。当前优先世界/自然光、体积雾及普通点光/面光；BREAK ME DOWN 的透明板投影灯光继续暂停。运行时不调用 AI。不支持的效果保持 `needs_review`，不以导入成功或占位结果判定还原通过。

透明覆盖率根据活动材质输出的实际 Shader 混合树解析，支持白色 Transparent BSDF、Mix Shader 的两个分支顺序、多层混合、节点组及 Shader 组输入、转接与静音旁路，以及 Principled Alpha。能够映射的遮罩保留贴图和 UV；空间程序字段通过原生 Cycles 单独烘焙，保留 RGB 转标量规则。UE 将覆盖率接入 Opacity Mask，不把矩形背景当作实体。Translucent BSDF 的薄表面散射与覆盖率分开处理，整棵树的三维包围盒不会使叶片被当成玻璃。不存在按草、树或材质名字判断的规则。

有色 Transparent BSDF 和含透明的 Add Shader 不能简单等价为裁切遮罩，明确记录 coverage_error 并保留 needs_review；透光的双面散射及连续 Alpha 的遮罩近似仍需视觉复核。材质缓存校验覆盖率版本及实际混合模式，场景还原点版本也已更新；旧任务需新建转换任务，不能仅重新导入旧 manifest 来获得新遮罩。不会自动修改全局阴影剔除参数。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background --factory-startup --python scripts/blender_surface_graph_smoke.py
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background --factory-startup --python scripts/blender_appearance_smoke.py
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background --factory-startup --python scripts/check_usd_instance_fix.py
```

`scripts/blender_appearance_reference.py` 使用任务的活动相机、参考帧和源画幅渲染 Blender。`scripts/ue_appearance_render.py` 在已部署桥接的 UE 中重开生成关卡，用同画幅 MRQ 渲染并记录原生 SVT、代码哈希和渲染世界。两步串行运行。渲染脚本在专用加共享 GPU 内存达到 90% 时取消当前帧并保留关卡，降至 80% 后重新渲染；停止文件仍然有效。

原生回读、导入完成和 Python 测试不能替代视觉验收。未知字段节点、非闭合体积、不同散射相函数的组合、复杂天空太阳映射、第三方着色器、缺失缓存和特殊合成明确标记 `needs_review`。Eye/Hat/Lyc 分区合成及动画不在本轮范围内。

世界雾没有源裁剪距离时使用 100 米。Cycles 无限世界雾与有限 UE 雾范围的天空透射会产生差异，裁剪范围可调整。纹理导入在阈值前预留 5% 预算，避免单次大纹理分配跨过上限；恢复阈值仍为 80%。

验收输出位于 `work/scene_appearance_acceptance/jobs/*/visual`，小场景位于 `work/scene_appearance_smoke/jobs/*/visual`。具体通过范围以当前验收报告为准。
### 大量复制物体的 USD 几何共享

带外部原型引用的几何叶节点标记为 USD 原生实例。UE 的 USD 转换器可在解析网格时识别相同原型，避免仅在生成资产后才按内容去重造成大量临时几何展开。物体位置、比例、材质和实例数量保留；包含其他源物体的父节点不设为实例，以保留可编辑的父子结构。标记使用 Sdf 批处理，避免逐个实例反复重建 USD 组合。

原生检查见 `scripts/check_usd_instance_fix.py`：共享原型、仿射变换，以及父物体带独立子物体。旧导出清单需要重新生成几何索引才能取得这一修复。

### 材质节点优先转换

自动模式解析源 `.blend` 的活动输出、连接、参数及节点组，不按截图、贴图文件名或物体类别决定烘焙。已经支持的表达式优先生成可编辑 UE 节点。新增 Hue/Saturation/Value、Gamma、Brightness/Contrast、RGB/HSV 分离与组合、RGB to BW、Float/Vector Mix、HSV 颜色混合、Clamp、浮点 Map Range 与常见标量数学运算。UE 5.8 使用原生 RGBToHSV/HSVToRGB；保留 Blender 的中性色相 0.5、因子、饱和度钳制及 HDR 明度，不把源参数写死。

不能等价映射的空间字段在节点边界局部烘焙，其下游数学和调色仍生成节点，并记录 `baked_fields` 中的节点类型、输出和组路径。基础色的局部烘焙采集颜色链，不再使用包含透明闭包权重的整表面 Diffuse Color。对象均匀字段以同一原生对象身份在临时小网格上采样为常量，随后恢复冻结网格；共享原型的实例随机差异仍标记视觉复核。视角或光线依赖字段不会静态采样。强制 bake/nodes 的用户选择继续生效。

材质分析断点有独立转换版本：升级时归档已分析材质与日志，保留源输入校验和冻结网格，重新读取材质树。UE 生成时相同表达式子图共享节点，避免基础色和透光通道重复展开。原生回归 `scripts/blender_color_nodes_regression.py` 与 `scripts/ue_color_nodes_regression.py` 比较 Cycles 发光采样与 UE GPU 渲染目标的线性数值，包含 HDR、零 Gamma、零除数和颜色混合。

MRQ 预览先保存转换关卡，再卸载编辑器世界以释放重复资源；MRQ 打开保存关卡生成自己的渲染世界，完成后恢复编辑器关卡。保存失败时不卸载；原有 GPU 暂停/恢复阈值保持有效。

USD 冻结网格的通道烘焙使用临时场景，仅链接目标及材质显式引用的坐标对象，继承源帧和设备；结束后删除临时场景并恢复引用物体状态。避免每次隐藏源场景物体后仍重复求值整片散布森林。`scripts/blender_isolated_bake_regression.py` 对同一对象的噪声、随机数与颜色比较两个原生烘焙上下文，并检查源场景保留、临时场景清理。

临时烘焙同时同步窗口的场景和活动视图层，随后恢复，确保未链接的导出助手也能进入 UV 编辑模式。空间字段记录原始插槽类型和图像身份：标量字段在 UE 使用单通道输出，三维贴图坐标显式取 XY，避免 float3/float2 和 AppendVector 编译错误。旧断点中已知标量字段可修正输出类型而无需重烘焙像素。乘加运算直接生成 Multiply 与 Add。日志显示节点名称、输出、组路径和回退原因；16×16 对象均匀采样转换为常量，不作为低分辨率表面贴图。

均匀采样临时网格保留原 UV 层名称并预建 BakeUV，防止设置烘焙目标时改写原材质的 UV 引用。`scripts/blender_uniform_uv_regression.py` 使用匿名颜色贴图、Alpha 和对象随机驱动的 HSV 图，检查采样后透明贴图仍直接引用原 UV。材质分析版本 3 重新读取受影响的节点图；依赖源 UV/图像的空间字段使用独立缓存版本，其余字段可继续复用可靠的像素缓存。
