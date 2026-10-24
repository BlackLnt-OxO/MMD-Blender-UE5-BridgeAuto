# 角色 PBR 历史实验归档

归档于2026-10-20，正文来自整理前的角色 PBR 说明。这里保留原阶段描述、版本、命令和失败结果；正文中的“当前”“本轮”“下次”均属于当时语境，不是继续执行的授权。

当前使用方式见 [角色 PBR](../CHARACTER_PBR.md)，已替代/撤回的状态见 [开发历史](../PROJECT_HISTORY.md)，最新验收边界见 [当前状态](../PROJECT_STATUS.md)。本归档不复制模型、图片或 UE 资产。

历史“仅身体/丝袜”开关和独立 A/B/C 候选已合并为四档；二级与原生边界不再安排。头发侧面连续性优化已撤回，不能把旧候选的进行中说明当作待执行计划。阶段结果仅覆盖文中列出的样本和机位。

## 首阶段验收

截至 2026-10-18，GenZ 外观冻结在第8版丝袜线性厚度候选，目录 work/character_pbr_20261018/Hosiery_v8；分类与跨角色测试已进入第9版，最终证据见 CrossCharacter_v9/RESULTS.md。第8版49材质编译成功、白模0，程序只提供薄／中／厚三个厚度按钮；UE实例的Hosiery分组暴露HosieryThickness、滑杆0至3。厚度0、0.65、1.23456、1.85、2.5、3均通过单槽应用、保存重载、撤销，其余48槽绑定不变；动态材质实例另验证5个任意数值。该阶段119项针对性测试通过。原49槽的参数、贴图字节、MatCap及透明模式与第7版一致，只有丝袜厚度的覆盖计算改变；源PMX及1085个旧资产／工程配置文件哈希保持。6张原生1440p对照图、7组原生诊断和参数回执见同级review_receipt.json。程序验证不代表审美通过。

第7版的布料／粉嫩皮肤及水渍能力继续保留，历史图片在 work/character_pbr_20261018/FabricRosy_v7/Review。该版收弱布料亮面并改进纤维反光，皮肤增加粉嫩度；厚度与粉嫩度／水渍组合曾通过单槽应用、保存重载和撤销。黑色裙裤仍取原深色贴图，15个通用材质及部分扣件尚未逐个精修。本轮没有重新宣称这些材质全部完成精修，也没有重跑水渍画面。

第5版围绕鼻部明暗、皮肤毛孔、水滴与流痕开关，以及独立布料织纹和纤维光泽展开。原网格、骨架、眼影面片与 PMX MatCap 开关保留；鼻部使用平滑局部散射遮罩，面部其余区域保留柔和散射。

固定测试工程中，49个材质编译成功、白模0，保存重载仍为49槽，组件实际绑定的49个材质均已核对为第5版。水渍开启和关闭分别完成单槽应用、保存重载与撤销，原撤销记录已恢复；源 PMX 以及471个旧版资产和工程配置文件的哈希保持不变。当前针对性111项通过。本轮提供8张2560×1440原生UE静帧，含原灯光对照、干湿肖像与近景、布料、侧面和湿皮熄灯帧。15个表情／配件等槽仍使用通用PBR，不能据此称所有材质已精修完成。

第2版阶段的全量回归为985项通过、5项跳过；其 MatCap 开关对照和241帧、30fps的1080p转光片仍保留为历史证据。本轮没有无故重跑该全量测试，也未重新生成动态视频。检查结果仅覆盖这个角色，其它模型与更多材质类型仍待后续阶段。

当前画面使用独立审阅MRQ预设：静帧16次空间采样、动态4次空间采样，时间采样1；引擎预热64帧、渲染预热32帧，SSS全分辨率、Burley采样128、光追阴影采样4。机位和手动曝光固定。这些是审阅预设的画质设置，不会改写其它渲染路线的全局配置；低采样输出可能与验收图的噪点表现不同。GPU时间为离线MRQ测量，不代表交互帧率。

第5版皮肤验收文件保留在 work/character_pbr_20261018/SkinWet_v5/Review，第6版衣服／丝袜图保留在 ClothHosiery_v6/Review，其总回执和 RESULTS.md 继续作为历史证据。work/character_pbr_20261018/NEW_CHAT_HANDOFF.md 与原接续入口同步第8版状态。旧 work/character_pbr_20261018/Review 为第2版。所有UE图像直接复制原生输出，没有后期修图；程序预设截图另由现有Qt窗口离屏绘制。

先使用 YYB GenZ Miku 检查皮肤、眼睛、头发及 MatCap，提供同机位转光与 MatCap 关闭对比。视觉确认后再扩展其它模型。真实 Blender 准备脚本为 scripts/validate_character_pbr_blender.py：

    & "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-exit-code 1 --python scripts/validate_character_pbr_blender.py -- "<模型.pmx>" "<测试输出目录>"

UE 编译与场景建立脚本为 scripts/validate_character_pbr_ue.py；MRQ 脚本为 scripts/run_character_pbr_render.py，显式传入 --fixture 和 --project，使用 --variant source 或 --variant matcap_off。

跨角色夹具按实际材质清单核对槽数；Blender 准备增加 `--fixture-kind prop` 时按道具处理，默认仍为角色。道具跳过头眼动画检查，机位按可见材质的网格包围盒构图，避免隐藏机械结构扩大画幅。角色由头部和包围盒确定机位，原生审阅尺寸为2560×1440。

跨角色机位、干湿／MatCap 对照及稳定熄灯关卡由 `scripts/prepare_character_pbr_cross_views_ue.py` 建立，逐槽应用、保存重载和撤销由 `scripts/validate_character_pbr_cross_ue.py` 验证。稳定熄灯使用单独保存的零灯关卡，加载前即将直射、天光和补光设为零；不能只凭序列中出现零值就称熄灯验收通过。模型没有启用的 MatCap 时，对照注明“不适用”，不要求产生材质替换。

单槽应用与撤销验证脚本为 scripts/validate_character_pbr_edit_ue.py，须使用 UE 的 -ExecutePythonScript 编辑器模式；commandlet 模式不保留角色选择。脚本生成 1440p 静帧预设后，渲染时增加 --portrait --width 2560 --height 1440 --start 0 --end 1。

脚本输出与代码检查分别记录。只有真实 UE 编译、材质槽、重新加载及画面检查通过，才计入视觉验收。

## 可选焊接与加权法向对照

`scripts/validate_character_pbr_blender.py`增加实验参数`--mesh-finish weld_weighted --weld-distance 0.0001`。独立验收夹具默认不处理网格；新建内置PBR角色任务的默认行为见下文。PMX通常导入为一个网格；实验按原材质槽拆成部件，每个部件按Weld、Weighted Normal顺序处理，保留原Armature变形。默认焊接距离为导入后Blender空间的0.1mm，加权方式为面面积、权重50，保留锐边。

输出的`Character.blend`保存逐部件的修改器计算网格，另保留原拓扑作为导出网格。通过原面角索引将计算结果回传为自定义法线，原顶点、UV、蒙皮、形态键坐标及相对关系保持；被焊接消除的微小面保留原法线。此方案用于比较焊接平滑及加权法向的外观，不减少UE导出网格的顶点数。直接应用拓扑修改器或重建形态键的初轮方案在UE中出现表情破裂，已拒绝作为有效流程。

`scripts/prepare_character_pbr_mesh_comparison_ue.py`将实验网格导入独立UE根，复用原始分支已编译的母材质和相同参数，并按纯数值对齐相机、灯光尺寸及位置。它保存重载后核对机位、槽位和表情目标，同时检查原始分支文件哈希。材质槽按原名匹配，运行时诊断使用PMX索引到实际UE槽位的映射。该独立入口继续用于对照；新任务的身体／丝袜处理在动画采样后的导出副本上执行。

该阶段额外模型、安比基础色回退及粉嫩度0／0.3／1对照证据见`work/character_pbr_20261018/ExtendedModels_v10/RESULTS.md`。该历史批次的分类器为v9、着色为v8；`v10`是测试批次名。2026-10-19 的后续材质修正与原生对照保存在 `work/character_pbr_20261019/MaterialCorrections_v1`。

颜色贴图导入明确使用颜色压缩，覆盖UE对蓝紫色图片的法线自动检测；PBR缓存签名包含该导入策略，旧缓存会经过受保护的重新构建。共享原始图片用于颜色和法线时仍生成独立用途资产。机位遇到远离身体的停放配件时，用头骨、足骨及原材质几何范围确定身体构图，仅排除配件对构图的影响，不删除或隐藏源几何。

## 可选轮廓平滑实验

新建内置PBR角色任务通过“角色几何处理”选择四档，默认“焊接＋加权法向”。旧“身体／丝袜一级细分”开关仅用于旧任务兼容，其原有 `skin`、`hosiery` 范围不会自动扩大；旧资产不自动重建。新四档的配置及补选流程见下文。

细分写入动画采样后的导出副本，自动及手动FBX导出使用同一结果。保护组在原始Basis上读取开放边界、原始锐边和折痕；非软表面增加45°硬边保护，并保护闭合、非退化邻域中面角总和不超过180°的尖端。所有形态键共用保护集合，并保留各自保护点的位置。法线回传后生成的Sharp标记不参与原始保护集合。

`SubdivisionAll_v1` 是此前独立实验，二级真实模型组已取消。历史验收入口的 `--boundary-policy native|protected` 保持兼容；本轮 `SurfaceRepair_v1` 候选只运行一级、保护边界，不再安排原生边界或 Simple 对照。保护组记录来源、索引、坐标和逐形态键误差，Blender文件保留带折痕的输入副本。原生回归入口为 `scripts/validate_subdivision_all_blender.py`。历史六组真实链路、原生图及外观限制见本地 `work/character_pbr_20261019/SubdivisionAll_v1/RESULTS.md`。

验收入口另提供 `--contour-smoothing none|smooth|subdivision`，独立夹具默认 `none`。启用后必须同时指定 `--mesh-finish weld_weighted` 和 `--contour-material-indices`；索引使用原 PMX 材质槽，从 0 开始。所有分支都保留 0.1 mm 焊接与面面积加权法向，几何处理先于法线计算和骨架变形。

例如，对 GenZ 的原槽 1、2 进行保持面数的轻度平滑：

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python-exit-code 1 --python scripts/validate_character_pbr_blender.py -- "<模型.pmx>" "<独立输出目录>" --mesh-finish weld_weighted --weld-distance 0.0001 --contour-smoothing smooth --contour-material-indices 1 2
```

`smooth` 将系数 0.15、两次迭代的坐标写入导出副本，固定目标材质的开放边界及相邻两圈顶点。`subdivision` 对目标槽执行一级 Catmull-Clark，并为开放边界设置完整边与顶点折痕；它会增加真实顶点与面数。Basis 和全部形态键使用相同映射，保留相对关系、驱动、UV、材质及插值权重；未处理槽通过坐标、UV、权重和形态键指纹检查。无法保留的数据或映射会使实验失败，不删除形态键绕过校验；形态键 NLA 轨道目前会明确拒绝。

这两项会改变真实几何，不能只凭视口修改器判定导出成功。GenZ 与赛车初音的三组对照、FBX 逐点回读和独立 UE 验收保存在 `work/character_pbr_20261019/ContourSmooth_v1`。开放边界保护可能保留接缝附近的折面，细分也不保证消除贴图或法线造成的分块。原生 Blender 回归入口为 `scripts/validate_character_contour_blender.py`。

此前 `ContourSmooth_v1` 中 GenZ 轻度平滑出现新增露肤，外观不通过；一级细分存在重叠层遮挡变化。丝袜与身体原本重合的顶点被不同邻接关系分别处理，边界固定无法阻止内部偏移。赛车初音的裸露身体轮廓略有改善，未处理的黑色服装折面继续保留。这些历史结果不代表轮廓平滑已经适用于所有角色。

## 历史独立表面修复候选

旧“独立修复候选”的 A／B／C 对应配置 `character_geometry_candidate_scope=none|local|all`；留空为旧日常流程。该入口已合并进新四档下拉框，以下保留历史记录及旧任务解释：A 保留焊接＋加权法向并验证共面贴层；B 对身体、面部／嘴唇、贴腿软层及兼容贴层一级细分；C 对全部角色渲染网格一级细分。旧任务恢复保持原规则。

材质分析窗口将物理材质与几何用途分开。局部细分可选“自动／纳入／排除”，也可指定贴层宿主；设置独立保存在 `character_geometry_overrides[模型SHA256][原槽索引]`，不改变材质覆盖格式。缺少骨架、语义冲突及待验证宿主会标黄。单凭 `skin` 类型或空间接近不会自动选择全部细分；不确定项保留待确认。“应用当前材质到UE”只发送材质设置，不应用几何改动。

候选在原始几何上寻找完整共面三角面关系，验证真实骨骼权重及全部形态键后，将贴层绑定到同一细分曲面，正常表面保留0.1毫米间距。MMD辅助顶点组不作为蒙皮；原有塌缩隐藏形态键保持塌缩。RGB、Alpha、UV和材质槽保留，非共面部件不统一外扩。独立运动、冲突宿主和无法确定Basis方向的情况保留原数据并报告失败或待确认。

细分候选在最终三角化后计算法线并核对角点映射，仅在选中表面修正焊接代理与实际表面明显不一致或没有回传的角点。自动头发方向图使用最终三角面重新生成，手动图不覆盖。关闭阴影、细节法线、各向异性和MatCap仅用于独立诊断。当前验收工作与原生图片保存在 `work/character_pbr_20261019/SurfaceRepair_v1`，数据链路、自动判断和外观分别记录；编译成功不代表外观通过。

本轮六组导出、UE绑定和独立重载通过，A／B在已检查画面通过，两个C因残留侧面亮带及衣物接缝问题保持待复核。Logo三组均完整显示，Alpha保留；身体／丝袜历史显隐带已消除。129张成功输出的原生1440p图及中文对照、197项测试、面数和资源数据见该目录的 `RESULTS.md`。自动绑定只覆盖完整共面三角面关系，不会盲目投射独立运动或含混的贴片；分析窗口保留黄色待确认项。
