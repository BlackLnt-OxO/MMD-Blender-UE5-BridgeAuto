using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Forms;
using MikuMikuPlugin;

namespace MMDBridge
{
    /// <summary>
    /// MMDBridge 的 MMM 侧 telemetry 探针。
    ///
    /// 职责边界（方案 §50 最高原则）：
    /// <list type="bullet">
    /// <item>本插件**只观察**，不执行 Physics Bake —— SDK 中不存在 Bake API，
    /// 触发只能靠 UI Automation。</item>
    /// <item>本插件**不判断任务成功** —— 最终裁判是 Python 侧的 VMD Validator。</item>
    /// </list>
    ///
    /// 所有 MMM API 访问都包在 try/catch 里：插件跑在 MMM 进程内，
    /// 一个未捕获异常会直接崩掉用户的 MMM。
    /// </summary>
    public sealed class BridgePlugin : IBasePlugin, IHaveScenePlugin, IResidentPlugin,
                                       IButtonPlugin, ICommandPlugin
    {
        // ---------------------------------------------------------------
        // 节流（Update 每帧都会被调用，不能每次都做 IO）
        // ---------------------------------------------------------------
        private const int JobPollIntervalMs = 1000;
        private const int HeartbeatIntervalMs = 250;
        private const int SnapshotIntervalMs = 1000;

        /// <summary>Level 2：状态停在 PhysicsBaking 且 Marker 多久不动算卡死。</summary>
        private const double BakeStallSeconds = 20.0;

        private readonly object _sync = new object();

        private IWin32Window _applicationForm;
        private Scene _scene;

        private Protocol.JobRequest _job;
        private DateTime _lastJobPoll = DateTime.MinValue;
        private DateTime _lastHeartbeat = DateTime.MinValue;
        private DateTime _lastSnapshot = DateTime.MinValue;

        // -- Bake 观察状态（方案 §31 三级判定的 Level 1 / Level 2）-------
        private string _observedNonce = "";
        private bool _sawPhysicsBaking;
        private bool _bakeCompleted;
        private long _lastMarker = -1;
        private DateTime _lastMarkerChange = DateTime.MinValue;

        private string _lastError = "";

        // ===============================================================
        // 构造 —— 自诊断起点
        // ===============================================================

        /// <summary>
        /// 静态构造：类型被**首次访问**时触发。
        /// MMM 用 <c>GetTypes()</c> 枚举插件时会走到这里，
        /// 因此它的出现证明「DLL 已被加载且类型可解析」。
        /// </summary>
        static BridgePlugin()
        {
            Diag.Trace("静态构造：类型被首次访问（DLL 已加载）");
        }

        /// <summary>
        /// 实例构造：MMM 真的创建了插件实例。
        /// 没有这行、只有静态构造那行，说明 MMM 发现了插件但没有启用它。
        /// </summary>
        public BridgePlugin()
        {
            Diag.Trace("实例构造：MMM 创建了插件实例");
        }

        // ===============================================================
        // IBasePlugin
        // ===============================================================

        public Guid GUID
        {
            get { return new Guid("8F3C1D42-7A5E-4B9C-9E21-6D0B5C7A1E30"); }
        }

        public string Description
        {
            get { return "AutoBake"; }
        }

        /// <summary>
        /// 实测签名是 <c>System.Windows.Forms.IWin32Window</c>，且**可读可写** ——
        /// MMM 会自己把宿主窗体写进来，插件只需保存并原样返回。
        /// 返回 null 是安全的（MMM 随后会 set）。
        /// </summary>
        public IWin32Window ApplicationForm
        {
            get { return _applicationForm; }
            set
            {
                _applicationForm = value;
                Diag.Trace(value == null
                    ? "ApplicationForm 被写入 null"
                    : "ApplicationForm 被写入 " + value.GetType().FullName);
            }
        }

        // ===============================================================
        // IHaveScenePlugin
        // ===============================================================

        public Scene Scene
        {
            get { return _scene; }
            set
            {
                _scene = value;
                Diag.Trace(value == null
                    ? "Scene 被写入 null"
                    : "Scene 已注入（插件可读场景状态）");
            }
        }

        // ===============================================================
        // IResidentPlugin
        // ===============================================================

        public void Initialize()
        {
            // 不做任何 IO：MMM 启动阶段插件被加载的时机不保证。
            Diag.Trace("Initialize() 被调用");
        }

        public void Enabled()
        {
            Diag.Trace("Enabled() 被调用 —— 插件已被 MMM 启用");
        }

        public void Disabled()
        {
            Diag.Trace("Disabled() 被调用 —— 插件被 MMM 停用");
        }

        /// <summary>
        /// 实测：某个插件接口继承自 <c>IDisposable</c> ——
        /// 反射导出脚本只列**声明**成员、不列继承接口，因此具体是哪一个未确证，
        /// 但编译器明确要求实现 <c>Dispose()</c>。
        ///
        /// **这里只清空本实例持有的引用，绝不释放图标位图。**
        /// MMM 会构造临时实例做探测然后 Dispose 它，而 Ribbon 仍在使用
        /// <see cref="Image"/> 返回的位图 —— 释放它会让 ReloadPlugin 崩溃。
        /// 详见 <see cref="Image"/> 的注释。
        /// </summary>
        public void Dispose()
        {
            Diag.Trace("Dispose() 被调用");
            lock (_sync)
            {
                _job = null;
                _scene = null;
                // 刻意不释放 _icon32 / _icon20：它们是 static，且被 MMM 的 UI 引用。
            }
        }

        /// <summary>MMM 每帧调用。全部工作都按节流间隔触发。</summary>
        public void Update(float Frame, float ElapsedTime)
        {
            try
            {
                Diag.TraceFirstUpdate(Frame, ElapsedTime);
                lock (_sync)
                {
                    var now = DateTime.UtcNow;

                    if ((now - _lastJobPoll).TotalMilliseconds >= JobPollIntervalMs)
                    {
                        _lastJobPoll = now;
                        PollJob();
                    }

                    if (_job == null || !_job.IsUsable) return;

                    ObserveSceneState(now);

                    if ((now - _lastHeartbeat).TotalMilliseconds >= HeartbeatIntervalMs)
                    {
                        _lastHeartbeat = now;
                        WriteHeartbeat(now);
                    }

                    if ((now - _lastSnapshot).TotalMilliseconds >= SnapshotIntervalMs)
                    {
                        _lastSnapshot = now;
                        WriteSnapshots(now);
                    }

                    // Phase 0：Bake 完成后按请求把骨骼关键帧 dump 出来，
                    // 供与 MMM 官方导出的 VMD 逐字节对照（方案 §9/§10/§11）。
                    // 只读，绝不写 VMD。
                    TryBoneDump(_job, _scene);
                }
            }
            catch (Exception ex)
            {
                // 绝不把异常抛回 MMM。
                _lastError = ex.GetType().Name + ": " + ex.Message;
                Diag.TraceError("Update()", ex);
            }
        }

        // ===============================================================
        // IButtonPlugin —— 只有外观，动作在 ICommandPlugin.Run
        // ===============================================================

        public string Text { get { return "AutoBake"; } }

        public string EnglishText { get { return "AutoBake"; } }

        /// <summary>
        /// 插件图标。
        ///
        /// **必须是 static，且永远不能 Dispose。**
        ///
        /// 踩过的坑（实测崩溃栈）：
        /// <code>
        /// 参数无效。
        ///   在 System.Drawing.Image.get_Width()
        ///   在 DevExpress.XtraBars.ViewInfo.BarLinkViewInfo.get_GlyphSize()
        ///   在 MikuMikuMoving.TopForm.ReloadPlugin()
        /// </code>
        /// MMM 会**先构造一个实例做探测、再 Dispose 它**，
        /// 而它的 Ribbon 仍然持有这里返回的 Image 引用。
        /// 早期版本在 Dispose() 里释放图标（本意是"避免 GDI 泄漏"），
        /// 结果把 Ribbon 正在用的位图释放掉 —— 之后一读 Width 就抛"参数无效"，
        /// **ReloadPlugin 直接崩溃，插件永远注册不上，Update() 永远不被调用**。
        ///
        /// 图标与实例生命周期无关：它由整个进程共享，随进程结束一起回收。
        /// </summary>
        public Image Image { get { return _icon32 ?? (_icon32 = MakeIcon(32)); } }

        public Image SmallImage { get { return _icon20 ?? (_icon20 = MakeIcon(20)); } }

        private static Image _icon32;
        private static Image _icon20;

        private static Image MakeIcon(int size)
        {
            // 返回 null 有触发 MMM 内部 NRE 的风险，因此给一个确定的位图。
            try
            {
                var bmp = new Bitmap(size, size);
                using (var g = Graphics.FromImage(bmp))
                {
                    g.Clear(Color.Transparent);
                    using (var brush = new SolidBrush(Color.FromArgb(210, 60, 120, 200)))
                    using (var pen = new Pen(Color.FromArgb(230, 20, 60, 110), Math.Max(1f, size / 16f)))
                    {
                        g.FillRectangle(brush, 1, 1, size - 3, size - 3);
                        g.DrawRectangle(pen, 1, 1, size - 3, size - 3);
                    }
                }
                return bmp;
            }
            catch (Exception)
            {
                return null;
            }
        }

        // ===============================================================
        // ICommandPlugin —— 人工接管入口
        // ===============================================================

        /// <summary>
        /// 用户点击插件按钮时调用。
        ///
        /// 语义：用户声明"我要手动完成 Load / Bake / Export"。
        /// 插件只在任务目录里落一个标记文件，Python 侧据此进入
        /// <c>mmm_waiting_user</c> 并等待 <c>baked.vmd</c> 出现。
        ///
        /// 人工接管**只替代 UIA，绝不替代结果验证** —— Validator 照常运行。
        /// </summary>
        public void Run(CommandArgs e)
        {
            Diag.Trace("Run() 被调用 —— 按钮被点击");
            try
            {
                // 按钮可能在首次 Update 之前就被点击（插件刚被启用时），
                // 那时 _job 还是 null。这里主动轮询一次，避免把
                // "还没轮询到" 误报成 "没有活动任务" 写进 last_error。
                if (_job == null || !_job.IsUsable)
                {
                    PollJob();
                }

                var job = _job;
                if (job == null || !job.IsUsable)
                {
                    Diag.Trace("Run()：active_job.json 不可用，无法进入人工接管");
                    _lastError = "";
                    return;
                }

                var path = Path.Combine(job.JobDir, "manual_takeover.json");
                var json = new Json.ObjectWriter()
                    .Begin()
                    .Num("protocol", Protocol.Version)
                    .Str("job_id", job.JobId)
                    .Str("nonce", job.Nonce)
                    .Str("requested_at", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))
                    .Num("pid", System.Diagnostics.Process.GetCurrentProcess().Id)
                    .End();
                AtomicJson.Write(path, json);
            }
            catch (Exception ex)
            {
                _lastError = ex.GetType().Name + ": " + ex.Message;
            }
        }

        // ===============================================================
        // 任务发现
        // ===============================================================

        private void PollJob()
        {
            var job = Protocol.ReadActiveJob();
            if (job == null)
            {
                _job = null;
                return;
            }

            if (_job == null || _job.Nonce != job.Nonce)
            {
                // 新任务：重置 Bake 观察状态，避免把上一个任务的观测结果带过来。
                _observedNonce = job.Nonce;
                _sawPhysicsBaking = false;
                _bakeCompleted = false;
                _lastMarker = -1;
                _lastMarkerChange = DateTime.UtcNow;
                _lastHeartbeat = DateTime.MinValue;
                _lastSnapshot = DateTime.MinValue;
            }

            _job = job;
        }

        // ===============================================================
        // Level 1 / Level 2 观察
        // ===============================================================

        private void ObserveSceneState(DateTime now)
        {
            var scene = _scene;
            if (scene == null) return;

            SceneState state;
            long marker;
            try
            {
                state = scene.State;
                marker = scene.MarkerPosition;
            }
            catch (Exception)
            {
                return;
            }

            if (state == SceneState.PhysicsBaking)
            {
                // Level 1 前半：必须真实观测到进入 PhysicsBaking。
                _sawPhysicsBaking = true;
            }
            else if (_sawPhysicsBaking)
            {
                // Level 1 后半：从 PhysicsBaking 退出即视为完成信号。
                _bakeCompleted = true;
            }

            if (marker != _lastMarker)
            {
                _lastMarker = marker;
                _lastMarkerChange = now;
            }
        }

        private bool BakeStalled(DateTime now)
        {
            var scene = _scene;
            if (scene == null) return false;
            try
            {
                if (scene.State != SceneState.PhysicsBaking) return false;
            }
            catch (Exception)
            {
                return false;
            }
            return (now - _lastMarkerChange).TotalSeconds >= BakeStallSeconds;
        }

        // ===============================================================
        // telemetry 输出
        // ===============================================================

        private void WriteHeartbeat(DateTime now)
        {
            var job = _job;
            var scene = _scene;

            var sceneState = "";
            long marker = 0;
            try
            {
                if (scene != null)
                {
                    sceneState = scene.State.ToString();
                    marker = scene.MarkerPosition;
                }
            }
            catch (Exception)
            {
                sceneState = "<unreadable>";
            }

            var json = new Json.ObjectWriter()
                .Begin()
                .Num("protocol", Protocol.Version)
                .Str("job_id", job.JobId)
                .Str("nonce", job.Nonce)
                .Num("pid", System.Diagnostics.Process.GetCurrentProcess().Id)
                .Str("plugin_version", Protocol.PluginVersion)
                .Str("updated", now.ToString("o", CultureInfo.InvariantCulture))
                .Str("scene_state", sceneState)
                .Num("marker_position", marker)
                .End();

            AtomicJson.Write(Path.Combine(job.JobDir, "heartbeat.json"), json);
        }

        private void WriteSnapshots(DateTime now)
        {
            var job = _job;
            var scene = _scene;

            // -- state.json（Level 1 / Level 2 判定依据）------------------
            var sceneState = "";
            long marker = 0;
            try
            {
                if (scene != null)
                {
                    sceneState = scene.State.ToString();
                    marker = scene.MarkerPosition;
                }
            }
            catch (Exception)
            {
            }

            AtomicJson.Write(
                Path.Combine(job.JobDir, "state.json"),
                new Json.ObjectWriter()
                    .Begin()
                    .Num("protocol", Protocol.Version)
                    .Str("job_id", job.JobId)
                    .Str("nonce", job.Nonce)
                    .Str("updated", now.ToString("o", CultureInfo.InvariantCulture))
                    .Str("scene_state", sceneState)
                    .Num("marker_position", marker)
                    .Bool("bake_observed", _sawPhysicsBaking)
                    .Bool("bake_completed_observed", _bakeCompleted)
                    .Bool("bake_stalled", BakeStalled(now))
                    .Str("last_error", _lastError)
                    .End());

            WriteModelSnapshot(job, scene, now);
            WriteMotionSnapshot(job, scene, now);
            WriteDisplayFrameSnapshot(job, scene, now);
            WriteBakeSnapshot(job, scene, now);
        }

        private void WriteModelSnapshot(Protocol.JobRequest job, Scene scene, DateTime now)
        {
            var writer = new Json.ObjectWriter()
                .Begin()
                .Str("updated", now.ToString("o", CultureInfo.InvariantCulture));

            try
            {
                if (scene != null)
                {
                    var model = scene.ActiveModel;
                    writer.Num("model_count", CountModels(scene));
                    if (model != null)
                    {
                        writer.Str("active_model_name", SafeName(model));
                        writer.Num("bone_count", CountBones(model));
                    }
                    else
                    {
                        writer.Str("active_model_name", "");
                        writer.Num("bone_count", 0);
                    }
                }
            }
            catch (Exception ex)
            {
                writer.Str("error", ex.GetType().Name + ": " + ex.Message);
            }

            AtomicJson.Write(Path.Combine(job.JobDir, "model_snapshot.json"), writer.End());
        }

        private void WriteMotionSnapshot(Protocol.JobRequest job, Scene scene, DateTime now)
        {
            var writer = new Json.ObjectWriter()
                .Begin()
                .Str("updated", now.ToString("o", CultureInfo.InvariantCulture));

            try
            {
                var model = scene != null ? scene.ActiveModel : null;
                if (model != null)
                {
                    var bones = model.Bones;
                    var total = bones != null ? bones.Count : 0;
                    var withFrames = 0;
                    long maxFrame = 0;

                    if (bones != null)
                    {
                        for (var i = 0; i < total; i++)
                        {
                            long last;
                            if (TryGetBoneFrameStats(bones[i], out last))
                            {
                                if (last >= 0) withFrames++;
                                if (last > maxFrame) maxFrame = last;
                            }
                        }
                    }

                    writer.Num("bone_count", total);
                    writer.Num("bones_with_frames", withFrames);
                    writer.Num("max_frame", maxFrame);
                    // 层数用于排查"MMM 报的 max_frame 与源 VMD 不符"：
                    // 只看第 0 层会漏掉别的层上的帧。
                    writer.Num("max_layer_count", MaxLayerCount(bones));
                }
            }
            catch (Exception ex)
            {
                writer.Str("error", ex.GetType().Name + ": " + ex.Message);
            }

            AtomicJson.Write(Path.Combine(job.JobDir, "motion_snapshot.json"), writer.End());
        }

        /// <summary>
        /// 把 DisplayFrame 的内容倒出来，供 Python 侧断言
        /// <c>repair_expected_bones ⊆ DisplayFrame.Bones</c>（方案 §27）。
        /// </summary>
        private void WriteDisplayFrameSnapshot(Protocol.JobRequest job, Scene scene, DateTime now)
        {
            var writer = new Json.ObjectWriter()
                .Begin()
                .Str("updated", now.ToString("o", CultureInfo.InvariantCulture));

            try
            {
                var model = scene != null ? scene.ActiveModel : null;
                if (model != null)
                {
                    var frames = model.DisplayFrame;
                    var names = new List<string>();
                    if (frames != null)
                    {
                        foreach (var frame in frames)
                        {
                            if (frame != null) names.Add(frame.Name ?? "");
                        }
                    }
                    writer.StrArray("frame_names", names);
                    writer.StrArray("bones_in_physicsbake",
                                    BonesInFrame(model, "PhysicsBake", useDisplayName: false));
                    // 同时报告"显示名"版本，供人工核对。
                    //
                    // 为什么必须区分：MMM 的 Bone.DisplayName 会**随 UI 语言变化**
                    // （本机 Language=zh-CN，它返回的是英文名 Skirt_0_0），
                    // 而 repair_manifest 里记的是 PMX 的日文名 スカート_0_0，
                    // 也正是 VMD 实际使用的名字。
                    // 早期版本只报 DisplayName，导致断言永远匹配不上 —— 而且是**静默**的。
                    writer.StrArray("bones_in_physicsbake_display",
                                    BonesInFrame(model, "PhysicsBake", useDisplayName: true));
                }
            }
            catch (Exception ex)
            {
                writer.Str("error", ex.GetType().Name + ": " + ex.Message);
            }

            AtomicJson.Write(Path.Combine(job.JobDir, "display_frame_snapshot.json"), writer.End());
        }

        /// <summary>
        /// Level 3：目标物理骨是否真的出现了烘焙关键帧。
        /// 目标清单来自 Python 写的 <c>repair_manifest.json</c>。
        /// </summary>
        /// <summary>
        /// Phase 0 的骨骼关键帧 dump。
        ///
        /// 触发条件（三条都满足才做，且每个 nonce 只做一次）：
        /// <list type="number">
        /// <item><c>&lt;job_dir&gt;/dump_bones.txt</c> 存在（由 Python 写出，每行一个骨骼名）；</item>
        /// <item>Bake 已完成 —— 否则 dump 到的是烘焙前的状态，没有对照价值；</item>
        /// <item>本次 nonce 还没 dump 过。</item>
        /// </list>
        ///
        /// 放在 <c>Update()</c> 里做，但真正的重活只在满足条件的那一帧执行一次。
        /// </summary>
        private void TryBoneDump(Protocol.JobRequest job, Scene scene)
        {
            if (job == null || scene == null) return;
            // 没 Bake 完成时，只有 dump_now.txt 在场才做 ——
            // 插值数值域的测试只需要"动作已载入"，不必真烘焙一轮。
            if (!_bakeCompleted && !BoneDump.IsForced(job.JobDir)) return;
            if (BoneDump.AlreadyDumped(job.JobDir, job.Nonce)) return;

            var requested = BoneDump.ReadRequest(job.JobDir);
            if (requested.Count == 0) return;

            BoneDump.Dump(scene, job, requested);
        }

        private void WriteBakeSnapshot(Protocol.JobRequest job, Scene scene, DateTime now)
        {
            var writer = new Json.ObjectWriter()
                .Begin()
                .Str("updated", now.ToString("o", CultureInfo.InvariantCulture));

            try
            {
                var targets = ReadManifestTargets(job);
                var model = scene != null ? scene.ActiveModel : null;

                var baked = new List<string>();
                var unbaked = new List<string>();

                if (model != null && targets.Count > 0)
                {
                    var bones = model.Bones;
                    if (bones != null)
                    {
                        var byName = new Dictionary<string, Bone>(StringComparer.Ordinal);
                        for (var i = 0; i < bones.Count; i++)
                        {
                            var bone = bones[i];
                            if (bone == null) continue;
                            var key = bone.DisplayName;
                            if (string.IsNullOrEmpty(key)) key = bone.Name;
                            if (!string.IsNullOrEmpty(key) && !byName.ContainsKey(key))
                                byName[key] = bone;
                        }

                        foreach (var name in targets)
                        {
                            Bone bone;
                            long last;
                            if (byName.TryGetValue(name, out bone) && TryGetBoneFrameStats(bone, out last))
                                baked.Add(name);
                            else
                                unbaked.Add(name);
                        }
                    }
                }

                writer.Num("expected_count", targets.Count);
                writer.StrArray("baked_bones", baked);
                writer.StrArray("unbaked_bones", unbaked);
            }
            catch (Exception ex)
            {
                writer.Str("error", ex.GetType().Name + ": " + ex.Message);
            }

            AtomicJson.Write(Path.Combine(job.JobDir, "bake_snapshot.json"), writer.End());
        }

        // ===============================================================
        // 辅助
        // ===============================================================

        private static string SafeName(Model model)
        {
            try
            {
                var name = model.DisplayName;
                if (string.IsNullOrEmpty(name)) name = model.Name;
                return name ?? "";
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static int CountModels(Scene scene)
        {
            try
            {
                var models = scene.Models;
                return models == null ? 0 : models.Count;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        private static int CountBones(Model model)
        {
            try
            {
                var bones = model.Bones;
                return bones == null ? 0 : bones.Count;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        /// <summary>活动模型里任意一根骨骼拥有的最大 MotionLayer 数。</summary>
        private static int MaxLayerCount(BoneCollection bones)
        {
            var max = 0;
            try
            {
                if (bones == null) return 0;
                for (var i = 0; i < bones.Count; i++)
                {
                    var bone = bones[i];
                    if (bone == null) continue;
                    var layers = bone.Layers;
                    if (layers != null && layers.Count > max) max = layers.Count;
                }
            }
            catch (Exception)
            {
            }
            return max;
        }

        /// <summary>
        /// 取某骨骼在其**所有** MotionLayer 里的最后帧号。
        ///
        /// 必须遍历全部层，不能只看 <c>layers[0]</c>：
        /// 烘焙结果可能落在别的层上，只看第 0 层会把"已烘焙"误判成"未烘焙"。
        ///
        /// 返回 false 表示读取失败；lastFrame 为 -1 表示没有任何关键帧。
        /// </summary>
        private static bool TryGetBoneFrameStats(Bone bone, out long lastFrame)
        {
            lastFrame = -1;
            try
            {
                var layers = bone.Layers;
                if (layers == null || layers.Count == 0) return true;

                var max = long.MinValue;
                for (var layerIndex = 0; layerIndex < layers.Count; layerIndex++)
                {
                    var layer = layers[layerIndex];
                    if (layer == null) continue;
                    var frames = layer.Frames;
                    if (frames == null || frames.Count == 0) continue;

                    var keyFrames = frames.GetKeyFrames();
                    if (keyFrames == null) continue;
                    foreach (var f in keyFrames)
                    {
                        if (f != null && f.FrameNumber > max) max = f.FrameNumber;
                    }
                }
                if (max != long.MinValue) lastFrame = max;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// 取某个表示枠里的骨骼名。
        ///
        /// <paramref name="useDisplayName"/> 必须为 false 才能和
        /// repair_manifest / VMD 对上：MMM 的 <c>Bone.DisplayName</c>
        /// **随 UI 语言变化**（本机 zh-CN 下返回英文名 Skirt_0_0），
        /// 而 VMD 用的是 PMX 的日文名 スカート_0_0。
        /// </summary>
        private static List<string> BonesInFrame(Model model, string frameName,
                                                 bool useDisplayName)
        {
            var result = new List<string>();
            try
            {
                var frames = model.DisplayFrame;
                if (frames == null) return result;

                foreach (var frame in frames)
                {
                    if (frame == null || frame.Name != frameName) continue;

                    // 实测：DisplayFrameBoneCollection 只实现 IEnumerable<Bone> ——
                    // 没有 Count、没有索引器，也没有包装层，直接枚举 Bone 本身。
                    var bones = frame.Bones;
                    if (bones == null) continue;

                    foreach (var bone in bones)
                    {
                        if (bone == null) continue;
                        var name = useDisplayName ? bone.DisplayName : bone.Name;
                        if (string.IsNullOrEmpty(name)) name = bone.Name;
                        if (!string.IsNullOrEmpty(name)) result.Add(name);
                    }
                }
            }
            catch (Exception)
            {
            }
            return result;
        }

        /// <summary>
        /// 从 repair_manifest.json 读目标骨骼清单：
        /// <c>added_bone_names ∪ bake_probe_bone_names</c>。
        /// </summary>
        private static List<string> ReadManifestTargets(
            Protocol.JobRequest job)
        {
            var result = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            try
            {
                // 优先用协议给的绝对路径；旧协议没有该字段时回退到 job_dir。
                var path = !string.IsNullOrEmpty(job.ManifestPath)
                    ? job.ManifestPath
                    : Path.Combine(job.JobDir, "repair_manifest.json");
                if (!File.Exists(path)) return result;

                string text;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                                   FileShare.ReadWrite))
                using (var reader = new StreamReader(stream, new UTF8Encoding(false)))
                {
                    text = reader.ReadToEnd();
                }

                foreach (var key in new[] { "added_bone_names", "bake_probe_bone_names" })
                {
                    foreach (var name in ReadStringArray(text, key))
                    {
                        if (seen.Add(name)) result.Add(name);
                    }
                }
            }
            catch (Exception)
            {
            }
            return result;
        }

        /// <summary>从 JSON 文本里取一个字符串数组字段（顶层）。</summary>
        private static List<string> ReadStringArray(string text, string key)
        {
            var result = new List<string>();
            if (string.IsNullOrEmpty(text)) return result;

            var marker = "\"" + key + "\"";
            var at = text.IndexOf(marker, StringComparison.Ordinal);
            if (at < 0) return result;

            at = text.IndexOf('[', at + marker.Length);
            if (at < 0) return result;

            var i = at + 1;
            while (i < text.Length && text[i] != ']')
            {
                if (text[i] == '"')
                {
                    i++;
                    var sb = new StringBuilder();
                    while (i < text.Length && text[i] != '"')
                    {
                        if (text[i] == '\\' && i + 1 < text.Length)
                        {
                            i++;
                            sb.Append(text[i]);
                        }
                        else
                        {
                            sb.Append(text[i]);
                        }
                        i++;
                    }
                    if (i < text.Length) i++;
                    var value = sb.ToString();
                    if (value.Length > 0) result.Add(value);
                }
                else
                {
                    i++;
                }
            }
            return result;
        }
    }
}
