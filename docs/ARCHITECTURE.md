# 源码结构与任务数据

程序分为桌面界面、调度器和 DCC 适配器。转换使用确定的程序规则，本地运行，不依赖模型服务。

## 三条主数据流

```text
Qt GUI / Settings
  └─ Controller：任务、子进程、日志、检查点
      ├─ 角色：PMX / VMD → MMDBridge 物理烘焙 → Blender → 模型与动画 FBX → UE
      ├─ 场景：Blender 最终求值 → USD、材质与贴图 → UE
      └─ 镜头：VMD → Blender 采样 → UE Level Sequence
```

UE 通过本地文件邮箱接收请求并返回状态。PhysicsBakeFix、CameraCutAuto 与已弃用 MMM 工具有独立入口；默认角色流程仍会自动做必要的物理骨登记检查。

`mmdbridge_fbx` 执行物理烘焙后导出 FBX；旧 `fbx_legacy` 不执行烘焙。

## 模块职责

| 路径 | 职责 |
| --- | --- |
| `mmd_bridge/app.py` | Qt 页面、控件、操作连接和配置 |
| `gui_motion.py`、`gui_theme.py` | 折叠、页面、按钮、滚动条动效与窗口／悬停提示样式 |
| `core.py` | Settings、任务数据、路径、资源根目录和原子写入 |
| `controller.py` | 子进程、UE 连接、停止与恢复、材质和渲染调度 |
| `discovery.py` | 软件与上游 MMDBridge 安装目录检测 |
| `mmd_avi.py` | 隔离外部烘焙 Runtime、导出控制和结果检查 |
| `blender_*.py` | Blender 采样、几何、贴图与材质处理 |
| `ue_*.py` | UE 资产、材质、关卡、序列和渲染 |
| `material_*.py` | 有效材质分析、转换计划和局部重建 |
| `character_profiles.py` 等 | 角色母材质契约、部位规则和资源 |
| `standalone_render.py` | 后台 MRQ 计划、预设解析与结果判断 |
| `plugins/unreal/` | Python 桥接和原生插件 |
| `scripts/` | 桌面与 DCC 入口、安装、校验、打包和专用测试 |
| `tests/` | 规则、调度、适配器与界面回归 |
| `profiles/`、`resources/`、`vendor/` | 固定配置、配套资产和带上游许可的依赖源码 |

表内 Python 文件未写目录时位于 `mmd_bridge/`。

## 任务数据

| 文件／目录 | 用途 |
| --- | --- |
| `job.json` | 输入、参数、版本和任务身份 |
| `status.json`、`events.jsonl` | 状态和步骤事件 |
| `manifest.json` | 导出结果、来源与限制 |
| `ue_report.json` | UE 侧实际资产、检查和失败项 |
| `visual/` | 参考帧、渲染回执和差分 |
| `MaterialSurfaces/` | 局部材质切换与撤销事务 |
| `renders/` | 后台输出及每次运行记录 |

恢复依赖任务身份与输入哈希，不能只读取路径后跳过校验。局部材质操作核对实际组件和槽位，应用及撤销都要保留用户已有覆盖。

## 组合任务

新建默认角色通路的组合任务由 `orchestration.py` 调度现有独立入口。父任务保留 `orchestration_version=1`、`module_jobs` 和执行阶段；各子任务分别持有配置、输入哈希、检查点、日志及 UE 回执。多角色按角色拆分，重型步骤串行。

新任务以 `geometry_input_identity_version=1` 固定几何输入身份规则：补选和类型覆盖按原 PMX 哈希读取，缓存同时校验实际导入的修复副本；对应几何契约为 v5。缺少该字段的旧任务沿用 v4／原解释器，旧组合任务新建的子任务也继承父任务的旧规则。切换解释版本必须新建任务，不能复用旧几何检查点。

模块输入或原生处理失败只终止对应模块。本地导出先于 UE 导入执行，使工程连接问题不阻断其它模块导出；共享环境等待与模块失败分别展示。最终清单的 `composition` 引用已验证的模块及回执哈希，装配复用原骨骼资产、材质和 USD 缓存，不改写子任务资产归属。失败项进入汇总报告，禁止以部分资产存在宣称全部完成。

恢复逐模块校验来源；成功结果不会自动重做。没有版本标记的旧任务继续使用原单任务逻辑。

## 扩展与兼容

GUI 参数沿 Settings → 配置与任务 → Controller → Blender／UE 适配器传递。增加或修改字段时检查这些调用者、旧任务缺省值和报告字段。内置默认值变化不应重写已经保存的任务选择。

材质处理先看有效输出与连接，展开支持的节点组；无连接节点不应改变分类。可直接转换的部分保留节点，只对不支持字段烘焙或记录限制。Alpha 裁切与光学透射分开判断。

局部表面切换复用主转换流程，限制写入勾选槽位，并记录可撤销状态，不使用名称猜测替代来源与绑定校验。

## 验证和打包

源码入口是 `scripts/desktop_entry.py`。环境、安装检查、测试和打包命令见 [安装说明](INSTALL_WINDOWS.md#开发与打包)。测试使用独立临时目录，真实素材与长时间渲染脚本不要作为普通启动前提。

Python／Qt 测试证明对应规则和界面行为；真实导入还需在 Blender／UE 检查输出、槽位、重载、动画与渲染回执。发布时分别记录源码检查、独立解压启动和实际 DCC 验证的范围。
