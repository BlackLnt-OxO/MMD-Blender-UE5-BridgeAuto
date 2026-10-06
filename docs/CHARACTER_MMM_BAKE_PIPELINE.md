# PMX 角色通路：导入动作 → 物理烘焙 → 导入 Blender 的实现方案

> 面向维护者的代码级说明。所有结论均来自当前工作区源码，标注了文件与函数位置。
> 阅读前请先区分「已实现」与「已接入」——第 0 节专门讲这件事。

---

## 0. 现状定位（先看这一段）

链路本身已经打通并可端到端验收，但**只在命令行验收工具里可用**：

| 入口 | 位置 | 能力 |
|---|---|---|
| 端到端验收（人工 / 自动） | `scripts/mmm_e2e.py run [--auto]` | 完整跑通：前处理 → MMM → 烘焙 → 导出 → 四层校验 |
| 只重跑裁判 | `scripts/mmm_e2e.py verify --job <dir>` | 用已有产物重跑 Validator，不需要再开 MMM |
| 桌面 GUI「MMM 物理烘焙」页 | `mmd_bridge/app.py::build_mmm_page()` | **只做环境准备**：检测 MMM 目录、复制隔离 Runtime、安装/卸载 telemetry 插件 |
| 桌面 GUI 主流程 | `mmd_bridge/controller.py::Controller.start()` | **未接入** —— 不调用 `prepare_character_model()`，也不写 `effective.json` |

**由此产生的实际后果**：从 GUI 启动角色任务时，`<job>/effective.json` 不存在，
`blender_host.Character` 里的 `resolve_character_inputs()` 会**整体退回旧行为**（用原 PMX + 源 VMD），
即 MMM 物理烘焙对桌面通路目前不产生任何影响。把 `prepare()` 与 `MmmAutoRunner` 接进 `Controller.start()`
是这条链路从「验收通过」走到「生产可用」的剩余工作。

---

## 1. 模块划分

```
mmd_bridge/
├─ character_prep.py        ★ 前处理总入口：Gate + Router，产出 effective_model / 期望骨骼清单
├─ effective_inputs.py      ★ 下游唯一输入契约（刻意不依赖 PMX/VMD 代码，会被 Blender 进程导入）
│
├─ pmx_binary.py            PMX 字节层（struct 读写原语）
├─ pmx_model.py             数据模型；PHYSICS_BAKE_FRAME_NAME / BAKEABLE_PHYSICS_MODES
├─ pmx_reader.py            解析（Header / ModelInfo / Bone / DisplayFrame / RigidBody）
├─ pmx_analyzer.py          纯分析：Missing = PhysicsBones − DisplayedBones
├─ pmx_planner.py           修复计划（build_repair_plan / apply_plan_to_frames）
├─ pmx_patcher.py           Surgical Patch 写回（write_repaired_pmx）
├─ pmx_physics_displayframe.py  ★ 门面：analyze() / repair() / build_repair_manifest()
│
├─ vmd_name_strategy.py     名称路由：DIRECT / ALIAS_REQUIRED
├─ pmx_alias.py             字节级替换骨骼名 → mmm_working_alias.pmx
├─ vmd_alias.py             动作别名化 → source_alias.vmd + vmd_alias_map.json
├─ vmd_reader.py            严格 VMD 读取器（cp932 + errors="strict"）
├─ vmd_validator.py         ★ 四层校验 + 最终裁判
│
├─ mmm_runtime.py           MMM 发现 / 隔离 Runtime / 插件安装 / 单实例锁
├─ mmm_protocol.py          ★ Python ↔ 插件的文件协议（JobRequest / Heartbeat / Snapshot）
├─ mmm_ui.py                ★ UIA 驱动（唯一能触发物理烘焙的手段）
├─ mmm_auto.py              ★ 状态机 MmmAutoRunner：编排 + 判定
│
├─ blender_host.py          ★ Blender worker：读 effective.json → 导入 → 逐帧采样 → FBX
└─ blender_materials.py     材质分析（FBX 侧）

plugins/mmm/MMDBridgeMMMPlugin/   C# telemetry 插件（net48，对外名 AutoBake）
├─ BridgePlugin.cs          5 个接口 + telemetry 写出
├─ Protocol.cs / Json.cs    协议常量 / 零依赖 JSON 写入
├─ BoneDump.cs              Phase 0 关键帧导出（与官方 VMD 逐字节对照用）
└─ Diag.cs                  自诊断日志
```

分层的硬边界：`pmx_analyzer` 无文件访问；`effective_inputs` 不 import 任何 PMX/VMD 模块；
`mmm_auto` 不自己实现任何底层能力，只做编排与判定。

---

## 2. 阶段 ①  PMX 前处理 —— `prepare_character_model()`

`mmd_bridge/character_prep.py::prepare_character_model(pmx, source_vmd=, source_lipsync=, output_dir=)`
返回 `(CharacterPrepResult, 可读文本)`。这一步**在 MMM 启动之前**完成，并替下游一次性决定三件事：

### 2.1 分析

```python
model    = read_pmx(source)
analysis = analyze_model(model)
# Missing = RelatedBone(RigidBody.PhysicsMode ∈ {1,2}) − DisplayedBones(任何 DisplayFrame 中 Type==0)
```

判定**纯结构、绝不依赖骨骼名称**。

### 2.2 零物理骨旁路（不是失败）

`analysis.physics_bone_count == 0` → `mmm_skipped=True`，`reason=NO_PHYSICS_BONES`，
MMM 整段跳过，源 VMD 直接进 Blender。
注意：`displayed_physics_bone_count == 0`（有物理骨但一根都没登记）**是主要修复目标，不是旁路条件**——
混淆这两者会恰好跳过本项目存在的理由。

### 2.3 PhysicsBakeFix 修复

`analysis.needs_repair()` 为真时：

1. `repair(source, output_path=<job>/pmx/ProcessedPMX.pmx, model=, analysis=)`
   —— 只重写 Display Frame 区段（Surgical Patch），未改动的枠按 `raw_start/raw_end` 整体复制，
   仅被改/新建的枠重新编码（`invalidate_raw_range()`）。**源 PMX 恒不被修改**。
2. `assert_expectation_not_empty(missing_count, added_bone_names)`
   —— 空集防御，见 §7.2。
3. **回读校验**：重新 `read_pmx` + `analyze_model`，`missing != 0` 则抛 `PMX_REPAIR_VERIFY_FAILED`。
   这是「修复真的写进去了」的唯一证据，不能靠 `repair()` 的返回值相信。

### 2.4 VMD 名称路由（曾经的拒绝器，现在是路由器）

VMD 的骨骼名是 **15 字节 CP932 定长槽**。旧行为是「名字写不进 VMD 就拒绝整个模型」，
现在改为 `analyze_vmd_name_strategy(targets)` 路由：

| 结果 | 动作 |
|---|---|
| `DIRECT` | 原名可直接写，走原流程 |
| `ALIAS_REQUIRED` | 生成 `mmm_working_alias.pmx` + `source_alias.vmd`（+ `lipsync_alias.vmd`），MMM 用别名对烘焙 |

别名规则：`PB{bone_index:06d}`，冲突退回 `PX`。**必须按 Bone Index 分配**——它是唯一不因改名失效的身份标识；
只拿名字会让同名骨骼互相覆盖。

`targets` 的构造（`_representability_targets`）覆盖两条规则且不需要分支：
修复新增的骨 ∪ **所有已显示的物理骨**（后者覆盖 `missing == 0` 但仍不可表示的情况）。
走别名时动作文件**必须一起转**——否则 MMM 在别名模型上找不到骨骼，动作会**静默丢失**；
表情 VMD 也要转，因为实测有些表情文件里混有骨骼轨道。

### 2.5 探针骨与期望清单

```python
probes, unusable = select_bake_probes(analysis, limit=64, index_to_alias=...)
# 候选 = PhysicsBones ∩ DisplayedBones，先过滤 VMD 可表示性再采样（顺序不能反）
expected_bones = sorted(set(repair_expected_bones) | set(bake_probe_bones))
```

- `repair_expected_bones`：修复新增的骨 → 证明**修复抵达了 MMM**（对照 `display_frame_snapshot`）。
- `bake_probe_bones`：已显示的物理骨 → 证明**烘焙真的产生了关键帧**（对照 `bake_snapshot`）。
- `MAX_BAKE_PROBES = 64`：证据量很早就饱和，且每个探针都会加重 Layer D 的工作。

`expected_bones` 对非旁路模型**永不为空**——空清单会让 `∅ ⊆ actual` 恒真。

### 2.6 产物

- `<job>/pmx/ProcessedPMX.pmx`（或复用源文件）
- `<job>/pmx/repair_manifest.json` —— MMM 插件读它拿目标清单，字段：
  `source_model / processed_model / target_display_frame / frame_created / modified /
   physics_bone_count / displayed_physics_bone_count / missing_physics_bone_count /
   added_bone_indices / added_bone_names / bake_probe_bone_names / mmm_skipped / mmm_skipped_reason / source_vmd`
- `<job>/pmx/mmm_working_alias.pmx`、`source_alias.vmd`、`lipsync_alias.vmd`、`vmd_alias_map.json`（仅 Alias 路径）

---

## 3. 阶段 ②  MMM 环境准备 —— `mmm_runtime.py`

1. **发现**：手动指定为主（`validate_mmm_dir`），辅以受限启发式 `discover_mmm`
   （depth ≤ 3、20s 预算、第 1 层以下只进入名字带 `miku/mmm/moving/dance/mmd/初音` 的目录）。
   版本读 **PE Version Resource**，绝不按目录名判断。
2. **隔离 Runtime**：`prepare_runtime()` 把整个 MMM 复制到
   `%LOCALAPPDATA%/MMDBridge/mmm_runtime/1.2.9.2/runtime`，源目录只读。
   同时把 `Settings.xml / DockLayout.xml / CommandBarLayout.xml` 存为 baseline，
   供每个 Job 前 `restore_baseline()` 复位。
   **刻意不锁定界面语言**——用户实际发行的 MMM 可能只带 ja 语言包，强制 zh-CN 反而回退到不确定资源；
   正确做法是让 UIA 的候选名表覆盖中/日/英。
3. **插件安装**：`install_plugin(target, dll)` → `<target>/Plugins/MMDBridgeMMMPlugin.dll`，
   同名旧文件先备份为 `.dll.bak-<时间戳>`。
4. **单实例锁**：`active_job.json` / `heartbeat.json` 是固定路径的共享资源，
   因此同一 Runtime 同时只服务一个 Job。`acquire_lock(job_id, nonce)` 对**同一 job_id+nonce 幂等**（重入刷新），
   对不同活锁抛 `MMM_RUNTIME_BUSY`，对失效锁（pid 不存在）安全清理后重建。

---

## 4. 阶段 ③  Python ↔ 插件的握手协议 —— `mmm_protocol.py`

```
Python ──write──▶ %LOCALAPPDATA%/MMDBridge/mmm_bridge/active_job.json
                                        │
MMM 插件 ──read──────────────────────────┘
MMM 插件 ──write──▶ <job_dir>/heartbeat.json           （250 ms 节流）
                    <job_dir>/state.json               （1000 ms 节流）
                    <job_dir>/{model,motion,display_frame,bake}_snapshot.json
                    <job_dir>/manual_takeover.json     （用户点插件按钮时）
```

**为什么要 `job_id` + `nonce` 两个标识**：`active_job.json` 是全局单一路径，
而 MMM 进程的寿命长于启动它的那次运行。少了任一标识，
上一次任务残留的 `heartbeat.json` 或遗留的 MMM 进程都会被误判成「本次任务的插件已上线」。

每次读取都要过 `heartbeat_is_current()` 五道检查：
协议版本（`PROTOCOL_VERSION = 1`）、`job_id`、`nonce`、新鲜度（`DEFAULT_MAX_AGE = 5.0s`）、
pid 存活（`process_alive()`，Windows 走 `OpenProcess` + `GetExitCodeProcess == STILL_ACTIVE`）。

`JobRequest` 的参数字段：

| 字段 | 来源 / 含义 |
|---|---|
| `job_id` | `<时间戳>-<uuid6>` |
| `job_dir` | `<job_root>/mmm` |
| `effective_model` | `prep.alias_model or prep.effective_model` —— MMM 要加载的模型 |
| `source_vmd` | `prep.alias_motion or str(vmd)` —— MMM 要加载的动作（别名路径下必须是别名版） |
| `manifest_path` | `<job_root>/pmx/repair_manifest.json` 的**绝对路径** |
| `start_frame` / `end_frame` | `read_vmd(vmd).bone_frame_range` |
| `nonce` | 每任务新生成，`uuid4().hex` |

> `manifest_path` 必须是显式绝对路径：实测它在 `<job_root>/pmx/` 下，而 `job_dir` 是 `<job_root>/mmm/`。
> 早期版本让插件去 `job_dir` 找清单，读到空 → `bake_snapshot.expected_count` 恒为 0 → **断言静默失效**。

插件侧（`BridgePlugin.cs`）只做三件事：**观察**场景状态、**写** telemetry、**读** manifest 目标清单。
它**不执行烘焙**（SDK 里没有 Bake API），也**不判定成功**（裁判是 Python 侧的 Validator）。
所有 MMM API 访问都包在 try/catch 里——插件跑在 MMM 进程内，未捕获异常会直接崩掉用户的 MMM。

---

## 5. 阶段 ④  动作与物理烘焙的先后顺序 —— `MmmAutoRunner.run()`

这是本流程最核心的时序约束。

```
STARTING        等插件上线
  ↓
MMM_READY
  ↓
MODEL_LOADING   → MODEL_READY       ① 加载模型
  ↓
MOTION_LOADING  → MOTION_READY      ② 加载主动作
  ↓
LIPSYNC_LOADING → LIPSYNC_READY     ③ 加载表情（可选）
  ↓
PHYSICS_READY                       ④ 设物理为「总是计算」
  ↓
（Bake 前置检查，全过才继续）
  ↓
BAKING          → BAKE_FINISHED     ⑤ 点「记录」触发物理烘焙
  ↓
EXPORTING       → EXPORTED          ⑥ 导出动作 → baked.vmd
  ↓
VALIDATING                          ⑦ 四层 Validator（硬 Gate）
```

**为什么顺序是这样：**

- **物理模式在动作全部就绪之后才设置**（方案 §10）。动作导入会重置场景播放状态，
  先设「总是计算」会在后续导入中被冲掉。
- **每次导入动作前先归零 Marker**（`_reset_marker()`）。MMM 的 Motion 以**当前 Marker** 为起始帧，
  不能假设它本来是 0。注意当前实现是 UIA 点「从零帧播放」，**语义错误**（它会真的开始播放动画），
  应改为插件写 `Scene.MarkerPosition = 0`——该属性实测可写。
- **表情 VMD 的导入方式与主动作完全一致**（同一个「导入动作」按钮），在主动作之后导入。
- **每一步都以 telemetry 为闸门，绝不以 sleep 作为成功判据**。
  `click(); sleep(3); click()` 是明令禁止的——对话框关闭 ≠ 加载成功。

### 5.1 各步的闸门与参数

| 步骤 | 动作（UIA） | 成功判据（telemetry / 文件） | 超时参数 |
|---|---|---|---|
| 插件上线 | 点「**常驻**」组的 AutoBake | `heartbeat.json` 通过五道检查 | `mmm_ready = 120s` |
| 加载模型 | 点「加载模型」→ 文件对话框填路径 → 关许可弹窗 | `model_snapshot.model_count > 0 且 bone_count > 0` | `load_model = 180s` |
| 校验修复闭环 | — | `display_frame_snapshot.bones_in_physicsbake ⊇ repair_expected_bones` | — |
| 加载动作 | `_reset_marker()` → 点「导入动作」→ 填路径 | `motion_snapshot.bones_with_frames > 0 且 max_frame > 0` | `load_motion = 180s` |
| 加载表情 | `_reset_marker()` → 点「导入动作」→ 填路径 | `motion_snapshot.morph_frames` 比导入前增加 | `load_lipsync = 180s` |
| 设物理 | 点「总是计算」 | （当前实现不校验，直接记 pass） | `physics_ready = 30s` |
| 烘焙前置检查 | — | 模型在 / 动作有帧 / 表情有 morph / `end_frame > 0` / 期望骨骼全在枠内 | — |
| 触发烘焙 | 点「记录」（限定 `control_types=("Button",)`） | `state.bake_observed` → `bake_completed_observed` | `bake_base 60s + end_frame × 0.02s` |
| 导出 | 点「导出动作」→ 点「选择所有骨骼」→ 填路径 | 文件存在、非空、大小连续 `export_quiet = 2.0s` 不变 | `export = 300s` |
| 裁判 | — | 四层 Validator | `validate = 300s` |

烘焙超时**随帧数放大**是必须的：固定 60 秒对 6754 帧的动作远远不够。

### 5.2 三个必须点对的地方

1. **插件启用必须点「常驻 / Resident」组那个 AutoBake**。
   MMM 启动时只**探测**插件（构造 → `Initialize()` → `Dispose()`），不会调用 `Enabled()`。
   界面上有两个同名按钮：`命令` 组（只触发 `ICommandPlugin.Run()`）与 `常驻` 组（触发 `IResidentPlugin.Enabled()`）。
   点错的症状是日志里 `Run()` 出现多次而 `Enabled()` 一次都没有，流程卡在「等待插件上线」直到超时。
   而且它是**开关**——调用方必须先查 heartbeat，插件已在跑时绝不能点（会把它关掉）。
2. **「记录」必须限定 `Button`**。MMM 里同时存在 `name='记录' type=Button` 与 `name='记录...' type=SplitButton`，
   点 SplitButton 主体只会展开下拉菜单且不报错。
3. **UIA 定位必须先切到对应页签**。非活动页签上的控件不在 UIA 树里
   （`AutoBake` 仅在「插件」页签，`记录` 在「物理」与「Neuron」页签——**有歧义**，
   而「加载模型 / 导入动作 / 选择所有骨骼」在所有页签都有）。
   页签按名字匹配，不能按 index（布局持久化在 `CommandBarLayout.xml`，每次运行可能不同）。

控件名一律经 `normalize_label()` 归一化（去 `\r\n`、省略号、快捷键后缀）后匹配——
实测「总是计算」的 UIA 文本是 `'总是\r\n计算'`，「导出动作」是 `'导出\r\n动作'`。

---

## 6. 阶段 ⑤  烘焙完成的判定 —— 三级组合信号

方案 §12 明确：**仅看 Marker 到终点不够**。可能出现「时间轴跑完但一根物理骨都没烤出关键帧」，
那必须 FAIL，不能继续导出。因此判定分三级，全部由插件写进 telemetry：

| 级别 | 信号 | 位置 | 含义 |
|---|---|---|---|
| L1 前 | `state.bake_observed` | `_sawPhysicsBaking` | 真实观测到 `SceneState.PhysicsBaking`（=「物理焼きこみ中」） |
| L1 后 | `state.bake_completed_observed` | `_bakeCompleted` | 从 `PhysicsBaking` 退出即视为完成 |
| L2 | `state.bake_stalled` | `BakeStalled()` | 仍处于 `PhysicsBaking` 且 `MarkerPosition` 连续 **20s** 未变 |
| L3 | `bake_snapshot.baked_bones` | `WriteBakeSnapshot()` | manifest 目标清单里的骨**是否真有关键帧** |

L3 的实现细节：`TryGetBoneFrameStats()` **遍历该骨骼的所有 MotionLayer**，不能只看 `layers[0]`——
烘焙结果可能落在别的层上，只看第 0 层会把「已烘焙」误判成「未烘焙」。
骨骼名匹配用 `bone.DisplayName`，回退 `bone.Name`。

最终硬规则（`mmm_auto._bake()`）：

```python
if not baked:                      # bake_snapshot.baked_bones 为空
    raise MmmAutoError(MmmAutoState.BAKING,
        "时间轴跑完但没有任何物理骨产生关键帧 —— 拒绝导出（§12）")
```

`state.json` 里同时带 `scene_state` 与 `marker_position`，供人工诊断与 `wait_for_bake()` 的进度显示。

---

## 7. 阶段 ⑥⑦  导出与四层校验

### 7.1 导出

`MmmUiDriver.export_vmd(path)` = 点「导出动作」→ 点「选择所有骨骼」→ 文件对话框直接输入完整路径。
输出前先 `unlink()` 旧文件，避免把上一次的残留当成本次结果。

导出的文件名不可靠：MMM 的「导出动作」默认**沿用原文件名再加后缀**，
因此 `mmm_e2e.find_baked_vmd()` 接受任何名字含 `baked` 的 `.vmd`（大小写不敏感，取最新），
找不到就返回 `None`——**绝不猜一个不相干的 VMD 来「验证通过」**。

### 7.2 四层 Validator（`vmd_validator.validate_baked_vmd()`）

| 层 | 内容 | 失败码 |
|---|---|---|
| A | 二进制结构 / CP932 名称解码 | `VMD_INVALID` 等 |
| B | 源动作的骨骼轨道仍保留（只要求**携带运动**的轨道，否则空/恒等姿态轨道会假阳性） | `VMD_SOURCE_TRACK_LOST` |
| C | 期望骨骼全部出现在 `baked.vmd` | `VMD_EXPORT_MISSING_PHYSICS_BONES` |
| D | 这些骨在 `bake_range` 内关键帧数 ≥ `DEFAULT_MIN_FRAMES_PER_BONE = 2` | `PHYSICS_NOT_BAKED_TO_BONES` |

**空集防御是这一层的核心设计**——Layer C 是子集断言，而 `∅ ⊆ anything` 恒真：

| 调用方式 | 结果 |
|---|---|
| 不提供期望清单（`None`） | C/D `skipped` → **inconclusive**（绝不 PASS） |
| 提供空清单 `[]` | **FAIL** + `VMD_EXPECTATION_EMPTY` |
| manifest 称 `missing > 0` 但未列名称 | **FAIL** + `REPAIR_MANIFEST_EMPTY` |
| manifest `missing == 0` 且无名称 | 合法 |

原则：**从未运行的检查不能算通过**。`ok=True` 仅当所有适用层真正跑过并通过。
同样的防御在 `pmx_physics_displayframe.assert_expectation_not_empty()` 与
`build_repair_manifest()` 里各有一份。

报告写到 `<job>/mmm/validation.json`。

---

## 8. 阶段 ⑧  数据传递到 Blender

### 8.1 `effective.json` —— 下游唯一的输入契约

`mmd_bridge/effective_inputs.py`。规则很简单，但**必须只有一个地方实现**：

```
需要修复 → effective_model = ProcessedPMX.pmx      无需修复 → effective_model = 原 PMX
经过 MMM → effective_motion = baked.vmd            零物理骨 → effective_motion = 源 VMD
```

文件字段：`source_model / source_vmd / effective_model / effective_motion / repaired /
mmm_skipped / mmm_skipped_reason / expected_bone_count / effective_lipsync /
uses_vmd_alias / alias_map / alias_model`。

该模块**刻意不 import 任何 PMX/VMD 代码**——它会被 Blender 进程导入，
把整个解析链拖进去既浪费又增加耦合。

Blender 侧（`blender_host.Character.__init__`）：

```python
self.cfg = dict(self.data["settings"])          # 用副本，不回写 job.json
effective_model, effective_motion, note = resolve_character_inputs(self.cfg, job.folder)
self.cfg["model"], self.cfg["motion"] = effective_model, effective_motion
```

没有 `effective.json` 时**完全退回旧行为**，因此对历史任务零影响。

### 8.2 Blender 导入与采样（`Character.prepare()`）

1. `enable_mmd()` —— 校验 `mmd_tools` 版本必须等于 `MMD_VERSION = "4.5.13"`，否则抛错要求独立会话。
2. 场景单位与帧率：`unit_settings.system = "METRIC"`、`scale_length = 1.0`、`fps = 30`。
3. `bpy.ops.mmd_tools.import_model(filepath=cfg["model"], scale=cfg["scale"],
   types={"MESH","ARMATURE","MORPHS","DISPLAY"}, rename_bones=False, clean_model=False,
   remove_doubles=False, save_log=False)` —— 按 `mmd_type` 找 ROOT 与 ARMATURE。
4. `bpy.ops.mmd_tools.import_vmd(filepath=cfg["motion"], scale=cfg["scale"], margin=0,
   bone_mapper="PMX", create_new_action=True, use_nla=False, update_scene_settings=False)`
   —— **这里读的就是 `baked.vmd`**。
5. `shift_actions(新增 action, 1.0, cfg["motion_offset"] * 30 / fps)`，最后整体 `shift_actions(..., fps/30)`
   —— MMD 是 30 fps，目标 fps 可配。
6. 表情 VMD 单独导入，按 channel 与旧 action 合并（同名曲线用新的，旧曲线里多出来的补进去）。
7. **`scene.rigidbody_world.enabled = False`** —— Blender 侧**不做物理解算**，
   因为物理已经被 MMM 固化成骨骼关键帧了。这一步是整个链路的语义分界点。
8. 复制出 `MMD_ExportRig` / 导出网格：清空约束、`use_connect = False`
   （评估后的 IK 可能把受控骨移离父骨末端，而 FBX 不编码 Blender 的 connected-head 约束）、
   `use_inherit_rotation = True`、`inherit_scale = "FULL"`、多根时补一个 `MMD_Root`。
9. **逐帧采样**（`baking_animation` 状态）：
   - 对每根骨用 `bone.convert_local_to_pose(src.matrix, bone.matrix_local, parent_matrix=..., invert=True)`
     反解局部 basis，**父矩阵用评估后的源骨**而不是半更新的目标骨（否则长骨链会累积误差）。
   - 四元数连续性：`if np.dot(prev_quat, quat) < 0: quat.negate()`。
   - 同时采样 morph 值与镜头（位置 + XYZ 欧拉 + lens，欧拉带上一帧作为连续性提示）。
   - 用 `fcurve.keyframe_points.foreach_set('co', ...)` 批量写曲线，避免二次复杂度。
10. 骨骼改名成 `b_{i:04d}_{stable_id[:8]}`，morph 改成 `m_<stable_id>`，
    `manifest["bones"]` / `["morphs"]` 保留原名 ↔ 导出名对照表。
11. 导出 FBX（`FBX_DEFAULTS`）：
    `axis_forward="-Y"`、`axis_up="Z"`、`use_space_transform=True`、`bake_space_transform=False`、
    `add_leaf_bones=False`、`bake_anim_use_all_bones=True`、`bake_anim_use_nla_strips=False`、
    `bake_anim_step=1.0`、`bake_anim_simplify_factor=0.0`、`path_mode="RELATIVE"`；
    `object_types` 按 kind 取 `{"MESH","ARMATURE"}` 或 `{"CAMERA"}`，`bake_anim = kind != "model"`。

---

## 9. 关键实现细节与踩坑清单

1. **插件图标必须 `static` 且永不 `Dispose`**。MMM 会构造实例探测再 Dispose 它，
   但 Ribbon 仍持有 `IButtonPlugin.Image` 的位图引用。在 `Dispose()` 里释放图标会让
   `System.Drawing.Image.get_Width()` 抛「参数无效」，崩在 `MikuMikuMoving.TopForm.ReloadPlugin()`
   → 插件注册失败 → `Update()` 永不调用 → 无 telemetry。症状是「插件被识别但不上报」，极难定位。
2. **`DisplayFrame.Bones` 只实现 `IEnumerable<Bone>`**——没有 `Count`、没有索引器，直接 `foreach`。
   写成 `.Count` / `[i].Bone` 编译失败。
3. **`Bone.DisplayName` 随 UI 语言变化**。本机 zh-CN 下 `Skirt_0_0`，而 manifest 与 VMD 里是日文 `スカート_0_0`。
   早期版本只报 DisplayName，导致断言永远匹配不上，而且是**静默的**。
   因此 `display_frame_snapshot` 同时输出 `bones_in_physicsbake`（`Name`）与 `bones_in_physicsbake_display`（`DisplayName`）两个数组。
4. **UIA 枚举极慢**（约 244 ms/节点，遍历 239 节点 55~58s）。`find()` 每轮重走全树且走完才判超时，
   所以 `timeout=60s` 实际要花 113s。未解决的加速方向：UIA `CacheRequest`、键盘快捷键、缓存枚举结果。
5. **`_attach()` 必须分两阶段**：前 80% 超时只走快路径 `connect(path=...)`（约 0.5s/次），
   之后才**只扫一次**全量桌面（枚举全部顶层窗口约 44s，放进重试循环会造成 133s 延迟）。
6. **`root()` 必须包 `_PywinautoAdapter`**。漏包会让 `walk_elements` 在每个节点抛 `AttributeError`，
   而该异常被逐节点吞掉，表现是「整棵树一个控件都找不到」。
7. **点击前必须把窗口拉到前台**（`click_input` 模拟真实鼠标，窗口不在前台会落空且无报错）。
8. **`_jsonable()` 清洗诊断数据**。`Heartbeat` 是 dataclass 不可 JSON 序列化，
   直接塞进 `to_dict()` 会在**「报告失败」的路径上**抛 `TypeError`，把真正的错误信息整个吃掉。
9. **失败时必须先记下发生在哪一步**（`run()` 里先取 `self.state.value` 再 `_enter(FAILED)`），
   否则「失败在哪一阶段」这个最关键的诊断信息就丢了。
10. **`_reset_marker()` 失败不致命但要记录**，因为它会影响 Motion 的起始帧。
11. **VMD 解析的格式事实**（详见 `memory/MEMORY.md`）：IK 记录是 **21 字节**（不是常说的 19）；
    64 字节插值块是 **block layout**（值域 0~127）；超长名称在 15 字节槽内截断属**格式固有行为**，不是损坏。

---

## 10. 已知缺口与风险（按优先级）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | **GUI 未接入 MMM 阶段** | `controller.py::start()` / `app.py` | 桌面通路下 `effective.json` 不存在 → 烘焙结果不生效，静默退回原 VMD |
| 2 | **`effective_motion` 从未被回填为 `baked.vmd`** | `scripts/mmm_e2e.py` | `write_effective_inputs()` 只在 `prepare()` 里调用一次，此时 `effective_motion = prep.alias_motion or str(vmd)`（**源动作**）。`_run_auto()` 烘焙成功后**没有再次写入**，因此 Blender 拿到的仍是未烘焙的动作。文档里写的「MMM 阶段在导出 `baked.vmd` 后填入」目前没有实现 |
| 3 | **别名没有反向还原** | `blender_host.py` | 走 Alias 时 `effective_model` 指向别名模型，Blender 里骨骼名就是 `PB000123`。`vmd_alias_map.json` 只被 Validator 用来转换期望清单，Blender 侧不做还原，`manifest.bones[].original` 记的也是别名 |
| 4 | **`set_physics_always()` 大概率会失败** | `mmm_ui.py` / `mmm_auto._set_physics()` | 实测「总是计算 / 不计算 / 仅播放时计算」**在任何页签都不是 Button**，控件类型未确认。该步会抛 `MMM_UI_ELEMENT_NOT_FOUND` → 流程停在 `PHYSICS_READY`。另外 `_set_physics()` 无条件记 `check(ok=True)`，与实际结果不符 |
| 5 | **`MmmBakeResult.uses_alias` / `alias_map` 字段赋值可疑** | `mmm_auto.py::_result()` | `uses_alias=bool(self.expected_bones and self.expected_lipsync)` 与别名无关；`alias_map=str(self.request.manifest_path)` 装的是修复清单路径而非别名映射表。两处疑似笔误 |
| 6 | **`_reset_marker()` 语义错误** | `mmm_auto.py` | 用 UIA 点「从零帧播放」会真的开始播放动画。`Scene.MarkerPosition` 实测可写，应改由插件写 0 |
| 7 | **导出动作的多步 UI 未实测** | `mmm_ui.export_vmd()` | 方案 §15 指出「导出动作」不是普通另存为，有格式/帧范围/骨骼选择多步 UI，是 UIA 最难的一点 |
| 8 | **`load_model` 前没有归零 Marker** | `mmm_auto._load_model()` | 只在导入动作前归零；若 MMM 上次退出时 Marker 非 0，模型加载后的初始帧不确定 |

---

## 11. 端到端验收命令

```bash
# 主验收（人工在 MMM 里点按钮，程序自动判定每一步）
./.venv/Scripts/python.exe scripts/mmm_e2e.py run \
    --pmx "F:/MMD_Model/Miku/Sano_YYB·Era·Miku（二分裙）/final.pmx" \
    --vmd "F:/MMD_Motion/mobiusP_Prhythmatic_motion_ver3.0/Prhythmatic攝晍梡3.0.vmd" \
    --mmm "F:/MikuMikuDance Exe/MikuMikuMoving64_v1292" --isolate

# 无人值守（UIA 状态机走完加载/烘焙/导出）
./.venv/Scripts/python.exe scripts/mmm_e2e.py run --pmx ... --vmd ... --mmm ... --isolate --auto

# 只重跑最终裁判
./.venv/Scripts/python.exe scripts/mmm_e2e.py verify --job <任务目录>
```

验收清单共 12 项：前处理 → 名称路由 → 隔离 Runtime → 插件安装 → 插件上线 →
模型加载 → 修复闭环 → Marker → 动作加载 → 人工确认 → Bake 观测 → 导出 → 四层 Validator（硬 Gate）。
第 12 项是唯一能判定「MMM 阶段成功」的地方。

> **选动作时的陷阱**：`BakedFor_2SkirtMiku.vmd` / `mmd_LoveTrial_motion_02.vmd` 这类**本身已烘焙**的动作
> 对 349 根待烘焙骨是全覆盖的，无法区分「烘焙生效」与「轨道本来就有」，只适合当验证器的 ground truth。
> 烘焙 POC 应选**无裙轨**的动作（如 `Prhythmatic攝晍梡3.0.vmd`：283 轨 / 0 裙轨 / 36 身体轨 / 帧 0–6754），
> 让烘焙从零生成裙轨。
# MMDBridge 临时 AVI 清理

MMDBridge Mode 1 通过 AVI 导出驱动物理 VMD 烘焙，捕获视频仍需暂时生成。短验证与完整导出各自校验 VMD 成功、关闭对应 MMD 进程后，工具自动删除该次任务的 `capture.avi`。AVI 的原始大小、时间和清理状态保留在 `mmd_short/attempt.json`、`mmd_full/attempt.json`；有效 VMD 及验证文件保留。恢复已完成的烘焙也会清理遗留捕获视频。失败或未验证的导出保留 AVI；清理失败记录具体原因，不把已通过的 VMD 导出改成失败。不会遍历删除输入视频或其他 AVI。
