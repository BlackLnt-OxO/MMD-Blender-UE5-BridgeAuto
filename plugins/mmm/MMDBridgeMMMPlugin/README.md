# MMDBridgeMMMPlugin

**已弃用：暂时停止维护，现有功能仍可使用。** 默认角色流程使用 MMDBridge；本插件仅用于独立 MMM 工具。

MMM 侧的 telemetry 探针。跑在 MikuMikuMoving 进程内，**只观察、不操作**。

---

## 1. 构建

目标框架必须是 **net48** —— MMM 1.2.9.2 是 .NET Framework 应用，插件 DLL 由 MMM 进程加载。

```bat
dotnet build MMDBridgeMMMPlugin.csproj -c Release ^
  -p:MmmPath="F:\MikuMikuDance Exe\MikuMikuMoving64_v1292"
```

或先设环境变量：

```bat
set MMM_PATH=F:\MikuMikuDance Exe\MikuMikuMoving64_v1292
dotnet build MMDBridgeMMMPlugin.csproj -c Release
```

产物：`bin\Release\MMDBridgeMMMPlugin.dll`

安装到 MMM Runtime（由桌面工具自动完成，手工时）：

```
%LOCALAPPDATA%\MMDBridge\mmm_runtime\1.2.9.2\runtime\Plugins\MMDBridgeMMMPlugin.dll
```

> `MikuMikuPlugin.dll` 用 `<Private>false</Private>` 引用 —— **绝不随插件复制**，
> 运行期由 MMM 自己提供。

---

## 2. 已实测确证的接口签名

事实源：本机 `MikuMikuPlugin.dll`（Version=0.3.1.0）的反射导出。

### 必须实现的 5 个接口

```csharp
interface IBasePlugin {
    Guid GUID { get; }
    string Description { get; }
    System.Windows.Forms.IWin32Window ApplicationForm { get; set; }   // ← 注意
}

interface IHaveScenePlugin {
    MikuMikuPlugin.Scene Scene { get; set; }
}

interface IResidentPlugin {
    void Initialize();
    void Enabled();
    void Disabled();
    void Update(float Frame, float ElapsedTime);
}

interface IButtonPlugin {          // 全部只读，只有外观
    string Text { get; }
    string EnglishText { get; }
    System.Drawing.Image Image { get; }        // 32x32
    System.Drawing.Image SmallImage { get; }   // 20x20
}

interface ICommandPlugin {
    void Run(MikuMikuPlugin.CommandArgs e);    // 按钮点击的真正入口
}
```

### ⚠ 三个容易写错的点

| 点 | 实测结果 | 写错的后果 |
|---|---|---|
| `ApplicationForm` | 类型是 **`IWin32Window`**（不是 `Form`），且 **`get;set`** | 写成 `Form` 或只读属性 → 编译不过 |
| `Scene.MarkerPosition` | **`System.Int64`**（不是 `int`） | 类型不匹配 |
| `IButtonPlugin` | 属性**全部只读**；动作必须在 `ICommandPlugin.Run` | 只实现 `IButtonPlugin` → 按钮点了没反应 |

### 枚举（实测值）

```
SceneState : Editing=0  Playing=1  KinectCapturing=2  PhysicsBaking=3  AVI_Rendering=4
EditMode   : None=0  ModelMode=1  AccessoryMode=2  CameraMode=3  EffectMode=4  CaptionMode=5
```

`SceneState.PhysicsBaking` 是烘焙完成的**直接信号**（方案 §31 Level 1）。

### Scene / Model / Bone / DisplayFrame

```csharp
interface Scene {          // 注意：是 interface
    SceneState State { get; }        // 只读
    EditMode   Mode  { get; }        // 只读
    long       MarkerPosition { get; set; }   // 可读写
    Model      ActiveModel { get; set; }
    ModelCollection Models { get; }
    // ... Cameras / Lights / Accessories / PropertyFrames 等
}

interface Model {
    string Name { get; }
    string DisplayName { get; }
    int    ID { get; }
    BoneCollection         Bones { get; }
    DisplayFrameCollection DisplayFrame { get; }
}

interface Bone {
    int    BoneID { get; }
    string Name { get; }
    string DisplayName { get; }
    MotionLayerCollection Layers { get; }
}

interface MotionLayer {
    int LayerID { get; }
    MotionFrameCollection Frames { get; }
}

interface MotionFrameCollection {
    int Count { get; }
    List<MotionFrameData> GetKeyFrames();
    MotionFrameData       GetFrame(long frameNumber);
}

interface DisplayFrame {
    string Name { get; }
    string DisplayName { get; }
    DisplayFrameBoneCollection  Bones { get; }
    DisplayFrameMorphCollection Morphs { get; }
}
```

---

## 3. 第二次导出确证的类型（全部公开类型）

事实源：`mmm_all_types.txt`（Assembly Version=0.3.1.0，784 行）。

### ⚠ `DisplayFrame.Bones` 不是集合包装，而是直接枚举 `Bone`

```csharp
interface DisplayFrameBoneCollection {
    IEnumerator<Bone> GetEnumerator();     // ← 只有这一个成员
}
interface DisplayFrameMorphCollection {
    IEnumerator<Morph> GetEnumerator();
}
```

**没有 `Count`、没有索引器、没有 `entry.Bone` 包装层** —— 直接 `foreach` 得到 `Bone`。
写成 `items.Count` / `items[i].Bone` 会编译失败。

### 其它集合都是「Count + 索引器 + 可枚举」

```csharp
interface ModelCollection       { int Count {get;} Model Item[int] {get;} IEnumerator<Model> GetEnumerator(); }
interface BoneCollection        { int Count {get;} Bone RootBone {get;}
                                  Bone Item[int] {get;} Bone Item[string] {get;}   // ← 支持按名取
                                  IEnumerator<Bone> GetEnumerator(); }
interface MotionLayerCollection { int Count {get;} MotionLayer Item[int] {get;} IEnumerator<MotionLayer> GetEnumerator(); }
interface DisplayFrameCollection{ int Count {get;} DisplayFrame Item[int] {get;} IEnumerator<DisplayFrame> GetEnumerator(); }
```

> `BoneCollection` 有 `this[string]` 重载，可按名直接取骨骼。

### `MotionFrameData`（class，不是 interface）

```csharp
class MotionFrameData {
    long FrameNumber { get; set; }        // ← Int64
    DxMath.Vector3 Position { get; set; }
    DxMath.Quaternion Quaternion { get; set; }
    bool Selected { get; set; }
    MotionFrameData Clone();
}
```

### ⚠ 某个插件接口继承自 `IDisposable`

反射导出脚本只列**声明**成员、不列继承接口，因此**具体是哪一个接口未确证**，
但编译器明确要求 `BridgePlugin` 实现 `Dispose()`。

本插件已实现 `Dispose()` 并做真实清理（释放图标位图）。

### 顺带确证的枚举

```
BoneType       : LinkTargetByBoneIndex=1 Rotate=2 XYZ=4 Draw=8 Operatable=16 IK=32 InhereRotate=256 ...
EditColumnType : None=0 Bone=1 Morph=2 Property=4
```

---

## 4. 文件

| 文件 | 职责 |
|---|---|
| `MMDBridgeMMMPlugin.csproj` | net48 类库；引用 `MikuMikuPlugin.dll`（不复制） |
| `BridgePlugin.cs` | 插件本体：5 个接口 + telemetry 采集 |
| `Protocol.cs` | `active_job.json` 读取、原子写、心跳/状态结构 |
| `Json.cs` | 极简 JSON 读写（零依赖） |

---

## 5. 设计边界（不可违反）

1. **不执行 Physics Bake。** SDK 中不存在 Bake API，触发只能靠 UI Automation。
2. **不判断任务成功。** 插件只报告 MMM 内部发生了什么；
   最终裁判是 Python 侧的 `vmd_validator`。
3. **不抛异常到 MMM。** 所有 API 访问包在 try/catch 里 ——
   插件跑在 MMM 进程内，未捕获异常会崩掉用户的 MMM。
4. **不静默覆盖文件。** 所有 JSON 都用 `AtomicJson`（temp → flush → replace），
   与 Python 侧 `atomic_json()` 语义一致。
5. **必须校验 `job_id` + `nonce`。** 否则残留 MMM 进程或上一次任务的 heartbeat
   会被误认为"当前任务已连接"。

---

## 6. telemetry 输出

写入 `<job_dir>`（来自 `active_job.json`）：

| 文件 | 用途 |
|---|---|
| `heartbeat.json` | 插件存活 + 场景状态；Python 用它判断插件是否上线 |
| `state.json` | **Level 1 / Level 2**：`bake_observed` / `bake_completed_observed` / `bake_stalled` |
| `model_snapshot.json` | 活动模型名与骨骼数（方案 §26 模型加载验证） |
| `motion_snapshot.json` | 有帧的骨骼数、最大帧（方案 §29 动作加载验证） |
| `display_frame_snapshot.json` | 表示枠名 + `PhysicsBake` 内的骨骼（方案 §27 修复闭环断言） |
| `bake_snapshot.json` | **Level 3**：目标骨骼是否真的出现烘焙帧 |
| `manual_takeover.json` | 用户点击按钮时写入（人工接管信号） |

`bake_snapshot.json` 的目标清单来自 `<job_dir>/repair_manifest.json` 的
`added_bone_names ∪ bake_probe_bone_names`。

---

## 7. Stage 2.1 首次加载必须实测的三件事

1. **DLL 放进 `Plugins/` 后 MMM 是否自动加载**（方案 §50 Gate）——
   观察 `heartbeat.json` 是否出现。
2. **`ApplicationForm` 返回 `null` 是否被 MMM 安全接受** ——
   本插件实现了 setter 并返回被写入的值，理论上安全，但仍需实测。
3. **`Image` / `SmallImage` 返回位图是否正常** ——
   本插件生成了纯色位图而非返回 `null`，以规避 MMM 内部 NRE 风险。
