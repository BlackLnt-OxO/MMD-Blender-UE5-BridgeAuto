# MMD Bridge 项目现状与技术交底（交付给 GPT-5.6 定制方案用）

> 本文档的目的是：让另一位方案设计者（GPT-5.6）在不接触代码库的前提下，准确理解
> **当前工程的真实状态、已具备的能力、缺失的能力、以及既有约束**，从而为下述目标流程
> 定制一份**可落地**的实施方案。
>
> 本文档**只描述现状与约束，不提供方案**。方案由 GPT-5.6 输出。

---

## 0. 一句话概括目标流程

用户当前打算采用的工作流（**这是要设计的目标，不是现状**）：

```
PMX 原模型
  ↓
[已实现] PMX 物理骨显示枠检测与修复 → ProcessedPMX
  ↓
MMM：导入 ProcessedPMX + 导入原始 VMD + 烘焙物理到骨骼
  ↓
MMM 导出完整 baked.vmd
  ↓
Blender + mmd_tools：导入 ProcessedPMX + baked.vmd
  ↓
FBX
  ↓
UE（走"已弃用的历史 FBX 通路"）
```

关键决策说明（来自用户）：

1. **放弃 MMD2Unreal 通路作为主线**。用户实测 MMD2Unreal 导入 PMX 与 VMD 的结果不满意。
2. **主默认流程改为 FBX**：镜头 + 角色模型都走 FBX。
3. **场景（scene）阶段暂缓**，后续再开发，当前不做要求。
4. MMM 物理烘焙是**新增的、当前完全不存在的一环**。

---

## 1. 项目技术栈与版本约束

| 项 | 现状 |
|---|---|
| 语言 | Python（`requires-python = ">=3.11"`） |
| 包名 / 版本 | `mmd-bridge` / `0.1.0` |
| 构建 | setuptools ≥ 68 |
| GUI | PySide6 **6.8.3**（固定版本） |
| 打包 | PyInstaller 6.16.0（`unattended` / 冻结模式有专门处理） |
| 测试 | pytest 8.4.2，`testpaths = ["tests"]`，`pythonpath = ["."]` |
| Blender | 执行适配器**只验收 4.3.x**（代码硬校验 `bpy.app.version[:2] != (4, 3)` 会直接抛错） |
| mmd_tools | **4.5.13**，已 vendored 在 `vendor/blender_mmd_tools-4.5.13/`（GPL-3.0） |
| UE | 目标 **5.8.x**；历史验收在 5.7.4 |
| 许可证 | 主项目 **GPL-3.0-or-later** |
| 平台 | Windows（大量依赖 `ctypes.windll`、`winreg`、`powershell.exe`、`creationflags`） |
| 本地版本控制 | Git 仓库，`main` 为主分支，开发在 `workbuddy` 分支；**当前只有 1 个提交** `3fbcd20` |
| 远程仓库 | **无**（未配置 origin） |

---

## 2. 目录与模块结构

```
MMD_Blender_UE5_Autowork/
├── mmd_bridge/              # 核心 Python 包（桌面 + Blender 端 + UE 端共用）
├── scripts/                 # 34 个入口/验证脚本
├── plugins/unreal/MMDBridge # UE 侧插件（C++/uplugin）
├── vendor/blender_mmd_tools-4.5.13/   # 捆绑的 mmd_tools（GPL-3.0）
├── tests/                   # 8 个测试文件，58 个用例
├── docs/                    # ARCHITECTURE / LEGACY_FBX / VALIDATION / USD_SCENE_TUTORIAL
├── work/                    # 中间产物、验收证据（已 gitignore）
├── build/ dist/             # PyInstaller 产物
├── MMDBridge.spec           # PyInstaller 配置
├── THIRD_PARTY.md  pyproject.toml  README.md
```

### 2.1 `mmd_bridge/` 模块职责

| 文件 | 职责 | 运行环境 |
|---|---|---|
| `app.py` | PySide6 主窗口，5 个标签页，45KB | 桌面 |
| `core.py` | `Settings` / `Job` / `inspect_vmd` / 工具函数 | 共享 |
| `controller.py` | 任务调度状态机，Blender/UE 进程启动与轮询 | 桌面 |
| `discovery.py` | 扫描 Blender / UE 安装（注册表、Steam VDF、Epic 清单） | 桌面 |
| `process.py` | 子进程启动（冻结环境 DLL 路径隔离）、GPU 显存查询 | 桌面 |
| `blender_host.py` | **角色导出主逻辑**（PMX 导入 → VMD 导入 → 采样 → FBX 导出），22KB | Blender |
| `blender_scene.py` | 静态场景导出（旧 FBX 通路） | Blender |
| `blender_usd.py` | 静态场景导出（USD Stage 通路），31KB | Blender |
| `blender_materials.py` | 材质分析与烘焙 | Blender |
| `blender_ui.py` | 原生导出窗口 | Blender |
| `blender_validate.py` | FBX 导出后回读校验 | Blender |
| `ue_host.py` | UE 端导入调度，56KB | UE |
| `ue_materials.py` | UE 材质创建 | UE |
| `ue_usd.py` | UE 端 USD 导入 | UE |
| `ue_mmd2unreal.py` | MMD2Unreal 交互式导入（官方模态窗口） | UE |
| `ue_camera_cut_baker.py` | CameraCutAuto 镜头处理，131KB | UE |
| **`pmx_binary.py`** | **新增**：PMX 字节读写原语 + 变长有符号 Index | 共享 |
| **`pmx_model.py`** | **新增**：PMXHeader/Bone/DisplayFrame/RigidBody 数据结构 | 共享 |
| **`pmx_reader.py`** | **新增**：最小顺序解析器 | 共享 |
| **`pmx_analyzer.py`** | **新增**：纯函数分析 | 共享 |
| **`pmx_planner.py`** | **新增**：修复计划 | 共享 |
| **`pmx_patcher.py`** | **新增**：Surgical Patch 写回 | 共享 |
| **`pmx_physics_displayframe.py`** | **新增**：对外门面 `analyze()` / `repair()` | 共享 |

### 2.2 模块向 UE 插件复制

`controller.install_ue_bridge()` 会把下列文件复制进 UE 项目的
`Plugins/MMDBridge/Content/Python/mmd_bridge/`：

```
__init__.py  core.py  ue_host.py  ue_materials.py  ue_usd.py
material_slots.py  process.py  ue_camera_cut_baker.py  ue_mmd2unreal.py
```

> **注意**：`pmx_*.py` 系列**目前不在复制列表中**。若新流程需要在 UE 端或任何被复制的
> 模块中调用 PMX 处理，必须同步修改这个列表。

---

## 3. 当前 GUI 结构

主窗口 5 个标签页，顺序为：

```
['素材与软件', '参数', 'CameraCutAuto', 'PhysicsBakeFix', '任务与日志']
```

### 3.1 `素材与软件` 页

- Blender 下拉框、UE 下拉框（自动扫描 + 手动添加）
- 文件选择字段：`model`(PMX) / `motion`(VMD) / `lipsync`(VMD) / `camera`(VMD) /
  `scene`(.blend) / `working_blend`(.blend) / `project`(.uproject)
- UE 桥接状态、载入本机验收素材、保存/载入配置、素材预检

### 3.2 `参数` 页

关键下拉框 **`character_backend`**，仅两个取值：

```
"mmd2unreal"   → "MMD2Unreal 官方窗口（推荐）"   ← 当前默认
"fbx_legacy"   → "历史 Blender / FBX 兼容"
```

其余参数：`scale`(默认 0.08)、`fps`(30)、`start`/`end`、`motion_offset`/
`lipsync_offset`/`camera_offset`、`width`/`height`、`camera_scale`、
`camera_offset_xyz`、`scene_scale`、`scene_frame`、
`scene_format`(`usd_stage` 默认 / `fbx_legacy`)、`material_strategy`、
`scene_master_material`、`use_scene_master_material`、`bake_resolution`、
`usd_payload_triangle_budget`、`exact_collision`、
`ue_vram_pause_percent`、`ue_vram_resume_percent`、`unattended`。

### 3.3 `CameraCutAuto` 页（独立工具）

**不参与主流程**，自成一体：Probe → Detect → 人工复核 → Bake → Verify。
通过 UE 侧邮箱文件通信（`camera_cut_request.json`）。它是"独立功能分区"的**范式先例**。

### 3.4 `PhysicsBakeFix` 页（独立工具，新增）

```
[选择 PMX…]  <路径输入框>
① 检测（不写文件）   ② 修复并输出新 PMX
<检测结果多行文本区>
```

两个按钮都通过 `execute(lambda, callback)` 跑在 `Worker(QThread)` 上。
**该标签页与 CameraCutAuto 一起被排除在主流程锁定之外**：

```python
standalone = {self.camera_cut_tab_index, self.physics_bake_tab_index}
self.start_button.setEnabled(not self.main_flow_locked
                             and self.tabs.currentIndex() not in standalone)
```

### 3.5 `任务与日志` 页

任务状态、日志（带错误高亮，`LogHighlighter` 用正则匹配
`configerror|error|exception|traceback|failed|失败` 标红）、开始/停止/恢复按钮、
连接 UE。

---

## 4. 已实现的 PMX 能力（本次交付）

### 4.1 能力边界（重要）

主项目**原本没有任何 PMX 二进制能力**。`.pmx` 原先是：
- 文件对话框过滤串
- `Settings.validate()` 里的 4 字节魔数检查（`b"PMX "`）
- 由 Blender 侧 `mmd_tools.import_model()` 消费

因此新建了**一套最小实现**，零第三方依赖，纯 stdlib。**不得复制 vendor 中的
`mmd_tools` GPL 解析器代码**（仅可作为格式定义的只读参考）。

### 4.2 核心算法

```
PhysicsBones        = RelatedBone(RigidBody.PhysicsMode ∈ {1,2})
DisplayedBones      = 所有 DisplayFrame 中 Type==0 的条目
MissingPhysicsBones = PhysicsBones - DisplayedBones
MissingPhysicsBones → 写入专用 PhysicsBake 显示枠
```

**完全基于结构判定，绝不依赖骨骼名称。**

Physics Mode 语义：`0 = Follow Bone`（不处理）、`1 = Physics`、`2 = Physics + Bone`。

### 4.3 分层

严格按任务书 §38 拆分，核心算法可离线单测：

```
Analyze   → pmx_analyzer.py（纯函数，无 IO）
Plan      → pmx_planner.py（纯函数）
Repair    → pmx_planner.apply_plan_to_frames()
Serialize → pmx_patcher.py
Report    → format_analysis() / format_plan() / PhysicsDisplayFrameRepairResult
```

### 4.4 写回策略：Surgical Patch

只重写 Display Frame 区段，其余字节**原样保留**：

```
original[0 .. display_frame_start)        → 逐字节复制
Display Frame 区段                         → 重新拼装（见下）
original[rigid_body_start .. EOF)          → 逐字节复制
```

Display Frame 区段内部**尽可能懒惰**：

| 情况 | 行为 |
|---|---|
| A：原模型没有 PhysicsBake | 帧计数 +1，原所有帧字节**整体原样追加**，新 PhysicsBake 记录写在最后 |
| B：已有 PhysicsBake（复用） | 所有原帧原样复制，**只有 PhysicsBake 记录**重新编码（位置不变） |
| C：无需修复 | **完全不写文件** |

未改动帧之所以能原样复制，靠的是帧对象上的 `raw_start` / `raw_end` 字节区间。
一旦帧内容被修改，调用 `invalidate_raw_range()` 清除区间，patcher 才会重新编码。

### 4.5 解析器范围

`pmx_reader.py` 是**顺序跳过式**解析：

- **解析**：Header、ModelInfo、Bones、Display Frames、RigidBodies
- **完全跳过**（不读、不建模）：Vertex、Face、Texture、Material、Morph
- **读完 RigidBody 立即返回**：Joint、SoftBody **一个字节都不碰**

### 4.6 PMX 格式踩坑清单（已全部修正，改动前必看）

1. Header 有固定前导 `globals_count == 8`；字段序为
   `[8][encoding][additional_uvs][6 个 index size]`
2. ModelInfo 是**四个**字符串：name JP / name EN / comment JP / comment EN
3. RigidBody 尾部是 **5 个 float**：mass / linear damping / angular damping /
   restitution / friction
4. 顶点权重只有 BDEF1(0) / BDEF2(1) / BDEF4(2) / SDEF(3)；**QDEF(4) 非标准 PMX**
5. `additional_uvs` 之后**直接**是权重类型，中间无额外 16 字节
6. Bone flag `0x0800` 是**两个** vec3（24 字节）；还需处理 `0x2000`
   external translation key（4 字节）
7. Morph 仅 0~8 有效；material morph(8) 偏移为 `1 + 4*28` floats
8. 材质区 Toon 引用：`is_shared_toon==1` 读 1 字节，否则读 texture_index

### 4.7 输出与安全

- 默认输出 `<原名>_PhysicsBakeFixed.pmx`
- `write_repaired_pmx()` **拒绝覆盖源文件**（路径相同直接抛 `ValueError`）
- 对外门面签名（**注意是二元组**）：

```python
analyze(pmx_path) -> tuple[PhysicsDisplayFrameRepairResult, str]   # (结果对象, 可读报告)
repair(pmx_path, output_path=None, *, model=None, analysis=None)
    -> tuple[PhysicsDisplayFrameRepairResult, str]
```

### 4.8 实测验证结果（已跑）

| 项目 | 结果 |
|---|---|
| 单元测试 | **58 passed**（原 38 + 新增 20），覆盖任务书 §28 全部 10 个用例 |
| F 盘递归扫描 | **137 个 PMX → 135 成功 / 2 失败** |
| 2 个失败 | 均非 bug：PmxEditor 自带的 PMX 0.x 旧格式样例、0 字节空文件 `空.pmx` |
| 需要修复的模型 | **22 个**（`white {socks purple ver.pmx` 555 根、`black_fixed_with_ring.pmx` 445 根、`Bone.pmx` 349 根、`Evil.pmx` 179 根、`安和 すばる` 系列 149~172 根 等） |
| 修复往返抽样 6 个 | 全部 `out_miss=0`、Bone 数不变、**源文件 SHA-256 不变**、体积增量 = 新增枠字节数 |
| GUI 冒烟（offscreen） | 标签页顺序正确；在 PhysicsBakeFix 页 `start_button` 禁用，切回主任务页恢复 |

### 4.9 已知边界

- **SoftBody 物理不处理**（任务书 §18 明确排除），不把 SoftBody 转成 Bone
- Joint / SoftBody 区段完全不解析，其正确性**完全依赖字节级保留**
- Bone Visible=False 时：**只警告，不修改 Flag**（任务书 §14）
- **不做**：改刚体 Mode / 参数、改 Joint、改 Bone Parent/Transform/Deform Layer/IK、
  改 Morph / Vertex Weight / Material / Texture，删除或移动原有显示枠条目，
  按名称重新分类
- 模型若原本存在重复显示条目：**不清理**，只保证自己不制造新重复

---

## 5. 现有角色 FBX 通路（`fbx_legacy`）详解

这是用户要走的通路，**已完整实现且历史上验收通过**。

### 5.1 入口

```
scripts/desktop_entry.py  →  mmd_bridge.app:main
scripts/blender_entry.py  →  Blender 侧：mmd_bridge.blender_host:main
scripts/ue_entry.py       →  UE 侧：mmd_bridge.ue_host
```

`Controller.launch_blender(mode)` 以 `--factory-startup` 启动独立 Blender 进程：

```python
args = [cfg["blender"], "--factory-startup"]
if mode == "scene" or cfg["unattended"]:
    args += ["--background"]
if mode == "character" and cfg["working_blend"]:
    args += ["--disable-autoexec", cfg["working_blend"]]
args += ["--python-exit-code","1","--python", str(resource_root()/"scripts/blender_entry.py"),
         "--", str(self.job.folder), mode]
```

### 5.2 Blender 侧角色导出流程（`blender_host.Character.prepare()`）

```
1. 校验 bpy.app.version[:2] == (4, 3)
2. enable_mmd()：校验 mmd_tools 版本 == 4.5.13，启用 addon，
   设置 shared_toon_folder
3. scene 单位设为 METRIC / scale_length=1.0；fps=30；frame_set(0)
4. bpy.ops.mmd_tools.import_model(
       scale=cfg["scale"], types={"MESH","ARMATURE","MORPHS","DISPLAY"},
       rename_bones=False, clean_model=False, remove_doubles=False)
   → 必须返回 "FINISHED"，否则抛 RuntimeError
   → 从新建对象中取 mmd_type=="ROOT" 的 root 和 type=="ARMATURE" 的 armature
5. bpy.ops.mmd_tools.import_vmd(motion, bone_mapper="PMX", create_new_action=True,
       use_nla=False, update_scene_settings=False)
6. shift_actions(新 action, 1.0, motion_offset * 30 / fps)
7. 若有 lipsync：按通道合并（替换完全相同的曲线）
8. 若有 camera：新建相机对象，import_vmd 相机轨道，同样做 offset
9. shift_actions(所有新 action, fps / 30)   ← 统一帧率缩放
10. scene.render.fps = fps；设置分辨率；计算 frame_start / frame_end
11. scene.rigidbody_world.enabled = False   ← 关键：关闭刚体，不做物理模拟
12. 骨骼名改为稳定 ASCII：b_{i:04d}_{stable_id(original)[:8]}，原名记入 manifest
13. 材质分析：self.materials.analyze(mat, pmx_index=i)
14. 复制层级：export_arm 深拷贝，清除所有约束
```

### 5.3 动画采样（**自研，不是 bake_anim**）

这是关键设计，必须在方案中理解：

```python
# 逐帧手动采样到 numpy 数组
for frame_index, frame in enumerate(range(frame_start, frame_end+1)):
    scene.frame_set(frame)
    deps = bpy.context.evaluated_depsgraph_get()
    evaluated = source_arm.evaluated_get(deps)      # 采样 SOURCE rig（带约束）
    for bone_index, target in enumerate(targets):
        src = evaluated.pose.bones[target.name]
        basis = target.bone.convert_local_to_pose(
            src.matrix, target.bone.matrix_local,
            parent_matrix=src.parent.matrix if src.parent else ...,
            parent_matrix_local=target.parent.bone.matrix_local if target.parent else ...,
            invert=True)
        loc, quat, scale = basis.decompose()
        if frame_index and np.dot(poses[bone_index,frame_index-1,3:7], quat) < 0:
            quat.negate()                            # 四元数连续性
        poses[bone_index,frame_index] = (*loc,*quat,*scale)
```

**为什么手动采样**（代码注释原意）：

- MMD 的 IK 控制骨常关闭继承（`use_inherit_rotation=False`），
  而 FBX/UE 只用普通父子继承 → 必须重新求解所有采样姿态
- 必须对 **evaluated SOURCE parent** 求解局部 basis，不能用部分更新的
  target pose（会在长骨骼链上累积误差）
- FBX 不编码 Blender 的 connected-head 约束 → 导出前统一 `use_connect = False`
- 存在多个根骨骼时插入 `MMD_Root` 以满足 UE 单骨架根要求
- 采完后用 `fcurve.keyframe_points.foreach_set('co', ...)` 批量写曲线，
  避免逐点插入的平方复杂度；插值统一 `LINEAR`

### 5.4 FBX 导出参数

```python
FBX_DEFAULTS = dict(
    use_selection=True, global_scale=1.0, apply_unit_scale=True,
    apply_scale_options="FBX_SCALE_UNITS",
    axis_forward="-Y", axis_up="Z",
    use_space_transform=True, bake_space_transform=False,
    add_leaf_bones=False, use_armature_deform_only=False,
    use_mesh_modifiers=False, mesh_smooth_type="FACE",
    bake_anim_use_all_bones=True, bake_anim_use_nla_strips=False,
    bake_anim_use_all_actions=False, bake_anim_force_startend_keying=True,
    bake_anim_step=1.0, bake_anim_simplify_factor=0.0,
    path_mode="RELATIVE",
)
```

`export_options(kind)`：相机用 `{"CAMERA"}`，其余用 `{"MESH","ARMATURE"}`；
`bake_anim = (kind != "model")`——**模型导出时不做 bake_anim，动画才做**。

### 5.5 输出产物

```
<output_dir>/
├── MMD_<asset_id[:10]>_<job_id>_Model.fbx        # 静止模型（REST pose）
├── MMD_<asset_id[:10]>_<job_id>_Animated.fbx     # 采样动画（POSE）
├── MMD_<asset_id[:10]>_<job_id>_Camera.fbx       # 可选
└── MMD_<asset_id[:10]>_<job_id>_Data/
    ├── manifest.json            # 骨骼/Morph/材质映射，警告
    ├── reference_samples.json   # 首/中/末帧骨骼矩阵与 Morph 快照
    ├── camera.json              # 相机逐帧矩阵
    └── 贴图等
```

`output_dir` 规则：有 `output_override` 用之；否则若 Blender 已打开文件则用其目录；
否则用 `desktop_path()`（Windows 真实桌面，走 `SHGetFolderPathW` 处理 OneDrive 重定向）。

### 5.6 关键限制

- `scene.rigidbody_world.enabled = False`——**当前 FBX 通路明确不启用物理**。
  代码注释：*"骨骼/顶点动画采样属于动画传递，不启用 MMD 刚体或 UE Physics Asset。"*
- `bake_anim_use_all_bones=True` 但实际骨骼曲线是自研采样写入的

---

## 6. 任务调度与状态机（`controller.py`）

### 6.1 任务目录

```
%LOCALAPPDATA%/MMDBridge/jobs/<YYYYMMDD_HHMMSS>_<8位hex>/
```

可用环境变量 `MMDBRIDGE_HOME` 覆盖根目录。

### 6.2 任务文件协议

| 文件 | 作用 |
|---|---|
| `job.json` | schema=2、id、asset_id、settings、inputs(sha256)、vmds、created |
| `status.json` | `{state, message, updated, ...extra}`，原子写 |
| `command.json` | `{name, nonce, ...}`，桌面→子进程命令 |
| `manifest.json` | Blender 导出产物、骨骼/Morph/材质映射 |
| `stop` | 停止标记文件 |
| `ue_checkpoint.json` | UE 导入检查点 |
| `mmd2unreal_receipt.json` | MMD2Unreal 逐源文件回执 |

**原子写工具**：`core.atomic_json()`（临时文件 + `os.replace`）。
**输入指纹**：`Job.verify_inputs()` 逐个 sha256 校验，变了就要求新建任务。

### 6.3 状态取值

```
created / preparing / baking_animation / exporting / character_done
scene_export / scene_done
waiting_user_import / upstream_imported / awaiting_ue / awaiting_project
cancelled / upstream_blocked / stopped / failed
camera_cut_waiting_ue / camera_cut_queued
```

### 6.4 `character_backend` 的路由影响

| 位置 | `mmd2unreal` | `fbx_legacy` |
|---|---|---|
| `controller.start()` | 不启动 Blender 角色进程；只写一个空 manifest，等 UE 弹官方窗口 | 启动 Blender 角色导出 |
| `controller.resume()` | 不检查 Blender 是否还在跑 | 调 `ensure_blender_job_stopped()` |
| `controller.exports_ready()` | `needed = set()`（不要求 Blender 产物） | 要求 `{model, animation}` 齐备 |
| `Settings.validate()` | `model` 与 `motion` 可独立 | **`model` 与 `motion` 必须同时提供** |
| `check_upstream_installation()` | 检查 MMD2Unreal/KawaiiPhysics 插件 | 跳过 |
| `blender_host` | 不使用 | 使用 |

> 若目标流程改为"FBX 为默认"，`character_backend` 的**默认值**需要从
> `"mmd2unreal"` 改为 `"fbx_legacy"`，且新旧任务兼容逻辑需一并考虑
> （历史任务缺该字段时按 `fbx_legacy` 恢复——代码已如此处理）。

---

## 7. 缺失能力清单（方案必须处理）

| # | 缺失能力 | 说明 |
|---|---|---|
| **1** | **MMM 完全不存在** | 项目中没有任何 MikuMikuMoving 相关代码、进程控制、窗口自动化或插件。仅 README 与 docstring 提到 MMM。**这是最大的新增工程。** |
| **2** | **VMD 只读** | `core.inspect_vmd()` 只能读 VMD 1/2 的轨道计数与名称，**没有写回能力**。若需校验 baked.vmd 的骨骼轨道内容，需要扩展或新写解析器。 |
| **3** | **PMX 只有部分能力** | 只解析 Header/ModelInfo/Bone/DisplayFrame/RigidBody。**不解析** Vertex/Material/Morph/Joint/SoftBody，**不能写回** 除 DisplayFrame 外的任何区段。 |
| **4** | **PMX 处理未接入 GUI 主流程** | `PhysicsBakeFix` 是独立标签页，**不进入"开始流程"**。若新流程需要自动串联，需要在 `controller.py` 里新增阶段。 |
| **5** | **`pmx_*.py` 未被复制到 UE 插件** | 见 §2.2 的复制列表。 |
| **6** | **无 MMM Bake 结果校验能力** | "关闭实时物理后仍保持轨迹"、"VMD 中目标骨骼存在关键帧"这类判定，当前没有任何基础设施。 |
| **7** | **场景通路（USD）状态未定** | 用户明确"场景阶段之后再进行开发，目前不做要求"。现有 USD 通路在 UE 端仍有未通过验收的接口问题（见 `VALIDATION.md`）。 |
| **8** | **无 baked.vmd 命名/目录规范** | 现有任务目录体系里没有 MMM 阶段的位置。 |
| **9** | **无 VMD 与 PMX 的一致性校验** | 无法验证 baked.vmd 的骨骼名是否与 ProcessedPMX 对齐。 |
| **10** | **Blender 端不接受"外部已烘焙 VMD"概念** | `blender_host` 直接 `import_vmd(motion)`，没有"这是已烘焙动作、不要再做物理"的语义标记——虽然它本来就关掉了 `rigidbody_world`，行为上可能已够，但需要方案明确确认。 |

---

## 8. 现有集成范式（新功能应该照抄的形态）

`CameraCutAuto` 是本项目"独立功能分区"的**既有范式**，`PhysicsBakeFix` 已照抄：

1. **独立标签页**，不进入主流程
2. 标签页索引加入 `standalone` 集合，不受主流程锁定影响
3. **独立会话目录**：`%LOCALAPPDATA%/MMDBridge/camera_cut/<id>/`
4. **邮箱文件通信**：桌面写 `camera_cut_request.json` 到
   `%LOCALAPPDATA%/MMDBridge/connections/<stable_id(project)>/`，
   UE 侧心跳 `heartbeat.json`
5. **状态机 + 分步命令**：`probe` / `detect` / `bake` / `verify`，
   每步有独立 status
6. **不重写宿主的既有自动化**，只改输入源

> 对 MMM 而言：**项目里既没有 MMM 自动化，也没有可复用的邮箱通道**。
> 方案需要决定 MMM 是用哪种机制驱动（GUI 自动化 / 命令行 / 插件 / 其他），
> 以及它是否要套用这套"独立操作单元"的形态。

---

## 9. 测试与验证现状

### 9.1 测试

```
tests/
├── test_bridge_receipts.py
├── test_controller.py
├── test_core.py
├── test_desktop.py
├── test_material_slots.py
├── test_pmx_physics_displayframe.py     ← 新增，覆盖任务书 §28 全部 10 用例
├── test_process.py
└── test_upstream.py
```

**运行命令（必须显式指定 basetemp，否则 Windows 上 pytest 临时目录会 PermissionError）**：

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider --basetemp=work/pytest-workbuddy
```

当前结果：**58 passed**。

GUI 测试用 `QT_QPA_PLATFORM=offscreen` + `Window` 类 + `monkeypatch` 打桩 `Window.scan`。

### 9.2 真实 DCC 验收

在 `work/acceptance` 内的独立 UE 工程执行，**不保存或修改 F 盘原始素材**。
已通过的项见 `docs/VALIDATION.md`（16 项）与 `docs/validation-summary.json`。

### 9.3 素材位置

本机真实 MMD 素材在 **F 盘**：`F:/MMD_Model`、`F:/MMD_Clothes`、`F:/MMD_Motion`、
`F:/MikumikuDance Exe`。F 盘递归共 137 个 PMX。

---

## 10. 需要在方案中明确的决策点

以下是当前代码**没有答案**、必须由方案给出的：

1. **MMM 驱动方式**：进程控制？窗口自动化（UI Automation / 键鼠）？
   MMM 是否有可用插件/脚本接口？如何判定"烘焙完成"而不靠固定 sleep？
2. **MMM 阶段的产物落盘位置**：沿用 `jobs/<id>/` 还是新开会话目录？
3. **MMM 阶段的输入如何从 `PhysicsBakeFix` 拿 ProcessedPMX**：
   是自动串联（改 `controller.py` 主流程），还是像 CameraCutAuto 一样独立？
4. **`character_backend` 的默认值切换**：改 `"fbx_legacy"` 后，
   现有 `mmd2unreal` 相关代码是保留、弃用还是删除？历史任务兼容如何处理？
5. **MMM 与 Blender 之间的交接契约**：ProcessedPMX + baked.vmd 如何传？
   是手工放置还是有约定目录？命名规范？
6. **baked.vmd 校验深度**：是否要新增 VMD 写/深度读能力来验证骨骼轨道？
7. **Blender 端的语义标记**：是否需要显式告诉 `blender_host`"这是已烘焙 VMD"？
8. **MMM 失败分类**：`docs` 中列出的
   `MMM_MODEL_LOAD_FAILED` / `PHYSICS_BAKE_FAILED` / `PHYSICS_NOT_BAKED_TO_BONES` /
   `VMD_EXPORT_MISSING_PHYSICS_BONES` 等状态如何落到现有 `status.json` 体系？
9. **是否需要 `pmx_*.py` 进入 UE 插件的复制列表**。
10. **场景通路**：用户说"之后再进行开发"，方案应明确**本次不动场景**，
    并说明现有 `scene_format` 参数是否需要临时置为某种状态。

---

## 11. 相关文档与规范文件索引

| 文件 | 内容 |
|---|---|
| `README.md` | 用户视角的三部分职责、安装、启动、`## PhysicsBakeFix` 章节 |
| `docs/ARCHITECTURE.md` | 路由与状态、场景材质、CameraCutAuto、验证边界 |
| `docs/LEGACY_FBX.md` | **旧 FBX 通路的详细说明**（迁移前文档，但正是用户要走的通路） |
| `docs/VALIDATION.md` | UE 5.8 验证进度 + 本机验收记录（16 项） |
| `docs/USD_SCENE_TUTORIAL.md` | USD 场景通路（本次可不看） |
| `docs/upstream-lock.json` | 上游插件来源与提交 |
| `THIRD_PARTY.md` | 第三方依赖与许可证 |
| `D:/Codex_Projects/PMX 物理骨骼显示枠自动检测与修复方案.md` | PMX 修复的规格（SPEC） |
| `D:/Codex_Projects/PMX 物理骨骼显示枠自动修复功能——开发任务书.md` | PMX 修复的任务书 |
| `D:/Codex_Projects/新增章节：MMM 物理烘焙与 VMD 导出流程规范.md` | **MMM 阶段的规范（§24.1.1 ~ §24.1.33）** |

---

## 12. 一句话总结给方案设计者

> 这个项目**已经有**一个完整、经真实模型验证的 **PMX 物理骨显示枠修复器**，
> 和一条**历史悠久、验收通过的 Blender→FBX 角色通路**；
> 它**完全没有** MMM 相关能力，也**没有** VMD 写回与深度校验能力；
> 用户希望把主线从 MMD2Unreal 切到 FBX，并在中间**新插入一个 MMM 物理烘焙环节**。
> 方案的难点集中在 **MMM 如何被可靠驱动与验证**，而不在 PMX 或 FBX 本身。
