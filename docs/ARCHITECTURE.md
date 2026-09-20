# 实现与扩展

## 路由与状态

新 `Settings.character_backend` 默认 `mmd2unreal`，历史任务缺少该字段时按 `fbx_legacy` 恢复。新角色任务创建空几何清单，跳过 Blender 角色导出；存在场景时仍等待 USD 清单完整后进入 UE。schema 继续为 2，旧 schema 1/2 读取不变。

桌面项目邮箱仍采用本地文件通信。UE 端核对目标项目和 5.8 版本，加载 MMD2Unreal/KawaiiPhysics 原生模块后，通过显式 MMDPmxFactory/MMDVmdFactory 和 `AssetImportTask(automated=False)` 进入官方窗口。不能用无人值守脚本入口执行模态导入；正式桥接在正常编辑器的 Slate 回调中调度。

状态包括 `waiting_user_import`、`upstream_imported`、`cancelled`、`upstream_blocked`；终态回执防止编辑器重启后自行重弹窗口。每份源文件成功后写 `mmd2unreal_receipt.json`，记录输入哈希与实际资产路径。恢复验证资产仍存在，未知残留目录拒绝覆盖。

上游资源使用独立的 `Upstream_<jobID>` 目录，自建场景使用 `A_<assetID>`。场景材质扫描、重建与所有权标记不处理上游材质。相机组合创建上游序列副本，必要时将副本的顶层 possessable 转为 spawnable，再嵌入主序列。动作与独立表情先核对骨架；非叠加表情不能直接叠在身体动作上，保留资产并报告待配置。

## 场景和材质

Blender 冻结最终依赖图，继续保持原型、实例、Payload 和逐三角形碰撞。对象类别、尺度与坐标依赖进入冻结键；位置相关材质不会错误复用另一实例的烘焙结果。

`material_policy.py` 提供无 DCC 依赖的分类逻辑。材质记录新增 `surface_policy`、`bake_layout`、`bake_margin_pixels` 和 `requires_visual_review`；原 `strategy/channels/special_masks` 接口保留。auto 先检测有效 Surface：基础 Principled PBR、静态贴图及可翻译的简单计算直接 nodes；无节点材质映射基础常量。复杂小物件默认 bake，大面积平铺表面默认 hybrid。混合闭包、体积、位移与特殊着色层不归入简单材质。对象覆盖优先于材质覆盖，再到任务设置；auto 才执行分类。

烘焙材质身份包含求值几何及 UV 哈希、策略、分类、分辨率；位置相关图还包含世界变换及投影范围。同源材质平面遮罩共用世界范围，写入独立 UV；非平面使用独立非重叠图集。基础 UV 不被改为遮罩 UV。UE 独立 UV 法线通过 DeriveTangentBasis 转换至网格切线空间，遮罩采样边缘使用 clamp。

原始用户母材质只读，默认使用工具生成母材质和实例；显式启用后，仅完全匹配参数时复用。复杂 Shader 闭包保留既有主表面近似，但必须报告视觉复核，不能宣称任意 Blender 节点均等价。

## CameraCutAuto

仅更新用户可见入口名称。模块文件名、CAMCUT 环境变量、旧标签和备份路径继续兼容。UE 5.8 执行前 Probe；保留历史 UE 5.7 路径。已有非本工具多段切换先只读检查并停止重复处理。Verify 失败必须传到桌面失败状态，不把脚本正常返回等同于验收通过。

## 验证边界

依赖记录见 `upstream-lock.json`。真实 DCC 验证在工作区隔离工程执行，源码、API 探测、单元测试、交互结果与渲染结果分开记录，见 `VALIDATION.md`。历史 5.7.4 的精度指标仅对应原 FBX 实现。
