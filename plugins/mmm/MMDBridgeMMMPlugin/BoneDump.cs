using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

// Scene / Model / Bone / MotionLayer / MotionFrameData / InterpolatePoint
// 全部来自 SDK 的 MikuMikuPlugin 命名空间。
// 漏掉这一行会得到一串 CS0246「未能找到类型或命名空间名」——
// 新加的 .cs 文件很容易忘，而报错信息不会直接告诉你缺哪个 using。
using MikuMikuPlugin;

namespace MMDBridge
{
    /// <summary>
    /// Phase 0：把 MMM 里指定骨骼的关键帧**原样 dump** 出来，供与 MMM 官方导出的
    /// VMD 逐字节对照。
    ///
    /// 为什么需要它
    /// ------------
    /// 要由插件直接写 VMD，有三个未知必须先测出来（方案 §9 / §10 / §11）：
    ///
    /// <list type="number">
    /// <item><c>InterpolatePoint.X/Y</c> 的数值域是 0~127 还是 0~1 —— 不能猜；</item>
    /// <item>VMD 插值区那 64 字节里是否还编码了 Physics ON/OFF 标志 ——
    ///       若有，只靠 8 个 InterpolatePoint 就重建不出原生导出；</item>
    /// <item>Bake 的结果写进了哪一层 MotionLayer —— 不能假定是 0。</item>
    /// </list>
    ///
    /// 只要把 MMM 报的值与官方导出的 VMD 字节对齐，这三个问题一次全答。
    /// **本类只读，绝不写 VMD。**
    ///
    /// 输出格式
    /// --------
    /// 制表符分隔的纯文本（不是 JSON）——
    /// 逐帧数据用 TSV 更好解析，也更好用 diff 看差异：
    ///
    /// <code>
    /// #MMDBRIDGE_BONE_DUMP	1
    /// #job	20260920-...
    /// #bake_completed	true
    /// BONE	スカート_0_0	2
    /// LAYER	0	0	4108
    /// F	0	0	0	0	0	0	0	1	20	20	...
    /// </code>
    ///
    /// <c>F</c> 行字段：帧号、位置 xyz、四元数 xyzw、
    /// 然后是 8 个 <c>InterpolatePoint</c> 各 2 个值（X/Y）。
    /// </summary>
    internal static class BoneDump
    {
        internal const string RequestFileName = "dump_bones.txt";
        internal const string OutputFileName = "bone_dump.tsv";
        internal const string MarkerFileName = "bone_dump.done";

        /// <summary>
        /// 存在这个文件就**不等 Bake**，立刻 dump。
        ///
        /// 方案 §9 的插值数值域测试只需要"载入动作"，
        /// 不必真的烘焙一次 —— 而烘焙一轮要几分钟。
        /// </summary>
        internal const string ForceFileName = "dump_now.txt";

        internal static bool IsForced(string jobDir)
        {
            try
            {
                return File.Exists(Path.Combine(jobDir, ForceFileName));
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>每根骨最多 dump 多少帧，避免超大动作写出几百 MB。</summary>
        internal const int MaxFramesPerBone = 20000;

        /// <summary>
        /// 读请求文件。返回要 dump 的骨骼名；没有请求文件时返回空列表。
        ///
        /// 用**每行一个骨骼名的纯文本**而不是 JSON 数组 ——
        /// 插件里的 JSON 解析器是扁平的，解析数组要额外写不少代码，
        /// 而这里根本不需要那种表达能力。
        /// </summary>
        internal static List<string> ReadRequest(string jobDir)
        {
            var result = new List<string>();
            try
            {
                var path = Path.Combine(jobDir, RequestFileName);
                if (!File.Exists(path)) return result;
                foreach (var raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    var line = raw.Trim();
                    if (line.Length == 0 || line[0] == '#') continue;
                    result.Add(line);
                }
            }
            catch (Exception)
            {
            }
            return result;
        }

        /// <summary>本次 nonce 是否已经 dump 过（避免每帧重写大文件）。</summary>
        internal static bool AlreadyDumped(string jobDir, string nonce)
        {
            try
            {
                var marker = Path.Combine(jobDir, MarkerFileName);
                if (!File.Exists(marker)) return false;
                var text = File.ReadAllText(marker, Encoding.UTF8).Trim();
                return text == nonce;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// 执行 dump。返回写出的文件路径；没有可写内容时返回 null。
        ///
        /// 任何失败都**不抛异常** —— 它跑在 MMM 进程里，不能因为诊断把 MMM 搞崩。
        /// </summary>
        internal static string Dump(Scene scene, Protocol.JobRequest job,
                                    IList<string> boneNames)
        {
            if (scene == null || job == null || boneNames == null || boneNames.Count == 0)
                return null;

            try
            {
                var model = scene.ActiveModel;
                if (model == null) return null;

                var text = new StringBuilder(1 << 20);
                text.Append("#MMDBRIDGE_BONE_DUMP\t1\n");
                text.Append("#job\t").Append(job.JobId).Append('\n');
                text.Append("#nonce\t").Append(job.Nonce).Append('\n');
                text.Append("#model\t").Append(SafeModelName(model)).Append('\n');
                text.Append("#bone_count\t").Append(SafeBoneCount(model)).Append('\n');

                var bones = model.Bones;
                if (bones == null) return null;

                var written = 0;
                foreach (var rawName in boneNames)
                {
                    var name = (rawName ?? "").Trim();
                    if (name.Length == 0) continue;

                    Bone bone = null;
                    try
                    {
                        // BoneCollection 有 this[string] —— 可以按名直接取。
                        bone = bones[name];
                    }
                    catch (Exception)
                    {
                    }
                    if (bone == null)
                    {
                        text.Append("MISSING\t").Append(name).Append('\n');
                        continue;
                    }

                    AppendBone(text, bone, name);
                    written++;
                }

                text.Append("#bones_written\t").Append(written).Append('\n');

                var output = Path.Combine(job.JobDir, OutputFileName);
                var temp = output + ".tmp";
                File.WriteAllText(temp, text.ToString(), new UTF8Encoding(false));
                if (File.Exists(output)) File.Delete(output);
                File.Move(temp, output);

                File.WriteAllText(Path.Combine(job.JobDir, MarkerFileName),
                                  job.Nonce, new UTF8Encoding(false));

                Diag.Trace("BoneDump：已写出 " + output + "（" + written + " 根骨）");
                return output;
            }
            catch (Exception ex)
            {
                Diag.TraceError("BoneDump.Dump", ex);
                return null;
            }
        }

        private static void AppendBone(StringBuilder text, Bone bone, string name)
        {
            var layers = bone.Layers;
            var layerCount = layers == null ? 0 : layers.Count;
            text.Append("BONE\t").Append(name).Append('\t')
                .Append(layerCount).Append('\n');

            for (var index = 0; index < layerCount; index++)
            {
                MotionLayer layer;
                try
                {
                    layer = layers[index];
                }
                catch (Exception)
                {
                    continue;
                }
                if (layer == null) continue;

                var frames = layer.Frames;
                var keyFrames = frames == null ? null : frames.GetKeyFrames();
                var count = keyFrames == null ? 0 : keyFrames.Count;

                text.Append("LAYER\t").Append(index).Append('\t')
                    .Append(layer.LayerID).Append('\t')
                    .Append(count).Append('\n');

                if (keyFrames == null) continue;
                var limit = Math.Min(count, MaxFramesPerBone);
                for (var i = 0; i < limit; i++)
                {
                    AppendFrame(text, keyFrames[i]);
                }
                if (count > limit)
                {
                    text.Append("#truncated\t").Append(name).Append('\t')
                        .Append(count - limit).Append('\n');
                }
            }
        }

        private static void AppendFrame(StringBuilder text, MotionFrameData frame)
        {
            if (frame == null) return;
            text.Append("F\t").Append(frame.FrameNumber);

            var position = frame.Position;
            text.Append('\t').Append(F(position.X))
                .Append('\t').Append(F(position.Y))
                .Append('\t').Append(F(position.Z));

            var rotation = frame.Quaternion;
            text.Append('\t').Append(F(rotation.X))
                .Append('\t').Append(F(rotation.Y))
                .Append('\t').Append(F(rotation.Z))
                .Append('\t').Append(F(rotation.W));

            // 8 个 InterpolatePoint，每个报 X/Y 两个值。
            // 顺序固定为 X A/B、Y A/B、Z A/B、R A/B —— 与 VMD 的 4 条曲线对应。
            AppendPoint(text, frame.InterpolXA);
            AppendPoint(text, frame.InterpolXB);
            AppendPoint(text, frame.InterpolYA);
            AppendPoint(text, frame.InterpolYB);
            AppendPoint(text, frame.InterpolZA);
            AppendPoint(text, frame.InterpolZB);
            AppendPoint(text, frame.InterpolRA);
            AppendPoint(text, frame.InterpolRB);

            text.Append('\n');
        }

        private static void AppendPoint(StringBuilder text, InterpolatePoint point)
        {
            // 刻意用不变文化 —— 否则中文/日文区域设置会写出 "1,5" 这种小数点。
            text.Append('\t').Append(point.X.ToString(CultureInfo.InvariantCulture))
                .Append('\t').Append(point.Y.ToString(CultureInfo.InvariantCulture));
        }

        private static string F(float value)
        {
            return value.ToString("R", CultureInfo.InvariantCulture);
        }

        private static string SafeModelName(Model model)
        {
            try
            {
                if (!string.IsNullOrEmpty(model.Name)) return model.Name;
                if (!string.IsNullOrEmpty(model.DisplayName)) return model.DisplayName;
            }
            catch (Exception)
            {
            }
            return "";
        }

        private static int SafeBoneCount(Model model)
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
    }
}
