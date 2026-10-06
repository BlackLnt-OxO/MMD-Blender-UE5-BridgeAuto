using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace MMDBridge
{
    /// <summary>
    /// Python 侧与插件之间的协议。
    ///
    /// 入口是一个固定路径的共享文件：
    /// <c>%LOCALAPPDATA%\MMDBridge\mmm_bridge\active_job.json</c>
    ///
    /// Python 在启动 MMM 之前写入它，插件在 <c>Update()</c> 里读取它，
    /// 然后把 telemetry 写进 <c>job_dir</c>。
    ///
    /// <c>job_id</c> + <c>nonce</c> 是必需的：没有它们，一个残留的 MMM 进程
    /// 或者上一次任务的 heartbeat 会被误认为"当前任务已连接"。
    /// </summary>
    internal static class Protocol
    {
        /// <summary>协议版本。与 Python 侧不一致时插件拒绝工作。</summary>
        internal const int Version = 1;

        internal const string PluginVersion = "1.0";

        internal static string BridgeDirectory
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "MMDBridge", "mmm_bridge");
            }
        }

        internal static string ActiveJobPath
        {
            get { return Path.Combine(BridgeDirectory, "active_job.json"); }
        }

        /// <summary>一次任务请求。字段缺失时对应属性为默认值。</summary>
        internal sealed class JobRequest
        {
            internal int ProtocolVersion;
            internal string JobId = "";
            internal string Nonce = "";
            internal string JobDir = "";
            internal string EffectiveModel = "";
            internal string SourceVmd = "";
            /// <summary>
            /// repair_manifest.json 的**完整路径**。
            ///
            /// 不能假定它在 job_dir 下 —— 实测它在 <job_root>\pmx\ 里，
            /// 而 job_dir 是 <job_root>\mmm\。早期版本因此拿到空的目标清单，
            /// bake_snapshot 里 expected_count 恒为 0（断言静默失效）。
            /// </summary>
            internal string ManifestPath = "";
            internal long StartFrame;
            internal long EndFrame;

            /// <summary>协议版本匹配、job_id / nonce / job_dir 齐备才可用。</summary>
            internal bool IsUsable
            {
                get
                {
                    return ProtocolVersion == Version
                        && !string.IsNullOrEmpty(JobId)
                        && !string.IsNullOrEmpty(Nonce)
                        && !string.IsNullOrEmpty(JobDir);
                }
            }
        }

        /// <summary>读取 active_job.json。任何失败都返回 null，绝不抛异常。</summary>
        internal static JobRequest ReadActiveJob()
        {
            try
            {
                var path = ActiveJobPath;
                if (!File.Exists(path)) return null;

                string text;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                                   FileShare.ReadWrite))
                using (var reader = new StreamReader(stream, new UTF8Encoding(false)))
                {
                    text = reader.ReadToEnd();
                }

                var flat = Json.ParseFlat(text);
                var request = new JobRequest
                {
                    ProtocolVersion = ParseInt(Get(flat, "protocol"), 0),
                    JobId = Get(flat, "job_id"),
                    Nonce = Get(flat, "nonce"),
                    JobDir = Get(flat, "job_dir"),
                    EffectiveModel = Get(flat, "effective_model"),
                    SourceVmd = Get(flat, "source_vmd"),
                    ManifestPath = Get(flat, "manifest_path"),
                    StartFrame = ParseLong(Get(flat, "start_frame"), 0),
                    EndFrame = ParseLong(Get(flat, "end_frame"), 0),
                };
                return request.IsUsable ? request : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static string Get(Dictionary<string, string> flat, string key)
        {
            string value;
            return flat.TryGetValue(key, out value) ? value : "";
        }

        private static int ParseInt(string s, int fallback)
        {
            int v;
            return int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out v)
                ? v : fallback;
        }

        private static long ParseLong(string s, long fallback)
        {
            long v;
            return long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out v)
                ? v : fallback;
        }
    }

    /// <summary>
    /// 原子写：先写临时文件、flush 到磁盘，再整体替换目标。
    ///
    /// 直接覆盖目标文件会让 Python 侧读到写了一半的 JSON。
    /// 语义必须与 Python 的 <c>mmd_bridge.core.atomic_json()</c> 保持一致。
    /// </summary>
    internal static class AtomicJson
    {
        internal static bool Write(string path, string json)
        {
            try
            {
                var directory = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory))
                    Directory.CreateDirectory(directory);

                var temp = path + ".tmp";
                using (var stream = new FileStream(temp, FileMode.Create, FileAccess.Write,
                                                   FileShare.None))
                using (var writer = new StreamWriter(stream, new UTF8Encoding(false)))
                {
                    writer.Write(json);
                    writer.Flush();
                    stream.Flush(true);          // 落盘，不只是进 OS 缓存
                }

                if (File.Exists(path)) File.Delete(path);
                File.Move(temp, path);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }
}
