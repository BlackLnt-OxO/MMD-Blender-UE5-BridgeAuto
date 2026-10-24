# 柔和阴影与皮肤散射

在左侧“工作区 → 角色设置”新建“内置PBR角色”任务时，“导入角色时使用柔和阴影”默认勾选，可以在导入前关闭。“皮肤散射默认值”默认使用“中间散射”，也可选择“原暖色散射”。两个选项分别控制照明和皮肤散射；粉嫩度、水渍与可选网格法线处理仍独立。

| 散射参数 | 原暖色 | 中间散射默认值 |
|---|---|---|
| 面部 SSSStrength | 0.30 | 0.27 |
| 身体 SSSStrength | 0.40 | 0.35 |
| Mean Free Path Color | (1, 0.32, 0.17) | (1, 0.55, 0.435) |
| Surface Albedo | (0.8, 0.56, 0.48) | (0.8, 0.62, 0.56) |

中间散射取原暖色与已验收柔和分支的散射参数中值；不混合最终图片。面部和身体散射距离仍为 0.18／0.32 cm。当前着色 v11／默认 v6 的面部与所有皮肤粉嫩度均为 0.35，双层高光另有逐槽控制，见 [皮肤参数](CHARACTER_PBR.md#皮肤织物与水渍)。显式参数覆盖优先。新配置使用独立的 `_Balanced1` 母材质；单槽应用保留导入时的散射风格，新的高光配置仍使用该风格的散射颜色和距离。

只有角色、没有场景时，柔和阴影建立角色预览灯：6 lux 主光、7° 光源角、0.65 天光及矩形补光。包含场景时，只增大工具导入灯的光源角或尺寸，保留灯位、强度及原本更大的光源；没有工具标记的手工灯保持。关闭选项时沿用原有灯光路径。旧任务缺少新字段时恢复原暖色与原灯光，不迁移既有角色。

配置字段为 `character_skin_scattering`（`balanced_v1`／`legacy_v8`）和 `character_soft_shadows`（布尔值），随任务保存。新导入验证入口为 `work/character_pbr_20261018/ImportSoftDefaults_v1`。

原独立实验分支仍保留在 `work/character_pbr_20261018/SoftShadow_v1/RESULTS.md`，其历史数值和图片不改。该独立实验当时使用分类 v9、着色 v8；这是历史版本说明。当前源码为着色 v11，见 [当前状态](PROJECT_STATUS.md)。历史候选和冻结 GenZ v8 均保留，不自动改写。

## 手动调节

逐槽强度、粉嫩度和水渍可在程序中调整；具体散射颜色和灯光源尺寸也可在 UE 材质实例、Profile 和关卡灯光中编辑。以下 `SSP_Soft_*` 和关卡名称指原独立实验分支，新默认使用各新角色自己目录内的 `_Balanced1` 配置与 `Preview Soft` 灯。

| 目标 | 调节位置 | 影响 |
|---|---|---|
| 整体粉嫩度 | 程序皮肤／面部槽的“皮肤粉嫩度”；UE 材质实例 `SkinRosiness`，0–1 | 皮肤基础颜色，包含受光面与阴影面 |
| 散射强度 | 程序“皮肤散射”；UE 实例 `SSSStrength`，0–1 | 皮下散射贡献；不是单独的色相滑条 |
| 散射偏暖／偏红 | 新分支专属 `SSP_Soft_*` 的 `Mean Free Path Color` 与 `Surface Albedo` | 控制不同颜色光的散射。原配置的路径颜色是 `(1, .32, .17)`，实验采用 `(1, .78, .70)`，减小红通道与其它通道的距离差异 |
| 投影柔和程度 | `L_SoftShadow_Lighting` 内 Directional Light 的 `Source Angle` | 放大发光角度，使投影边缘柔和；保留阴影开关 |
| 阴影亮度及明暗对比 | 同关卡的 `PBR_Softbox` 强度与 SkyLight 强度 | 提供真实补光；不会让全黑环境中的皮肤自行发光 |
| 水渍 | 新分支实例 `WetEnabled`，0／1；其它 `Wet*` 参数保留 | 默认关闭，独立于散射与粉嫩度 |

每个角色有独立的材质实例与散射配置。一个 `SSP_Soft_*` 可能供同角色使用相同母材质的多个槽共享；只想改某一个槽的散射颜色时，先复制该配置，再让该槽的专属母材质引用新副本。每个槽的 `SSSStrength` 和 `SkinRosiness` 本来就是独立的实例参数。

UE 5.8 的 Substrate Shading Models 节点也包含散射配置引用。脚本同时设置材质属性和该节点的引用；手动更换母材质的散射配置时也需要检查两者一致。不要只改名为 `SubsurfaceTint` 的颜色参数并据此判断 Profile 已更换。

播放实验 Level Sequence 时，灯光强度受轨道驱动。直接调灯光前停止播放，或修改对应强度轨道。材质参数仍可直接在实例编辑器中调整。编辑后资产保护会保留用户修改，脚本不会覆盖已保存的手工编辑；下一次实验应使用新的输出目录。

## 构建方式

`scripts/prepare_character_pbr_soft_shadow_ue.py` 在 UE 编辑器 Python 中执行；环境变量 `MMDBRIDGE_PBR_FIXTURE` 指向独立夹具目录。该目录包含复制的 `manifest.json` 和 `soft_shadow_config.json`，后者指定已验收的来源夹具、新的 `SoftShadow_*` 资产根、散射与灯光数值。

脚本复制网格和材质实例，仅为皮肤复制母材质图与散射配置；贴图和动画保留只读引用。所有表达式与原分支一致。它创建原灯光对照关卡、柔光关卡、稳定熄灯关卡、专属 MRQ 配置和表情序列，验证槽位、表情数量、相机保存重载以及单槽散射参数保存、重载和撤销。

同机位诊断包括：原暖色散射、散射关闭、粉嫩度关闭、仅改散射配置，以及柔光组合。柔光组合同时调整照明与散射，不能用它单独衡量法线差异。法线差异另用相同角索引的真实 Blender 法线数据计算。

柔和程度还取决于角色几何、遮挡和场景灯光。该分支用于向参考图的明暗过渡靠近，不承诺在任意场景中自动复现参考图。

原理参考：[UE Subsurface Profile](https://dev.epicgames.com/documentation/en-us/unreal-engine/subsurface-profile-shading-model-in-unreal-engine)、[UE Virtual Shadow Maps](https://dev.epicgames.com/documentation/en-us/unreal-engine/virtual-shadow-maps-in-unreal-engine)。
