using System;
using System.Globalization;
using System.IO;
using System.Text;

namespace MMDBridge
{
    /// <summary>
    /// 插件自诊断埋点。
    ///
    /// 起因：把 DLL 放进 Plugins/ 后启动 MMM，**完全没有 telemetry 产生**。
    /// 此时有三种可能，从外部无法区分：
    /// <list type="number">
    /// <item>MMM 根本没发现/加载这个 DLL；</item>
    /// <item>DLL 加载了，但 MMM 没有实例化插件类（例如默认禁用，需人工启用）；</item>
    /// <item>实例化了，但 <c>Update()</c> 没被调用。</item>
    /// </list>
    ///
    /// 本类在每个关键节点追加一行到
    /// <c>%LOCALAPPDATA%\MMDBridge\mmm_bridge\plugin_startup.log</c>，
    /// 于是这三种情况可以一眼区分。
    ///
    /// 刻意做成**极简、绝不抛异常**：它跑在 MMM 进程里，
    /// 任何异常都会崩掉用户的 MMM，而它存在的意义恰恰是诊断异常情况。
    /// </summary>
    internal static class Diag
    {
        private static readonly object Gate = new object();
        private static bool _updateLogged;

        internal static string LogPath
        {
            get
            {
                try
                {
                    return Path.Combine(
                        Environment.GetFolderPath(
                            Environment.SpecialFolder.LocalApplicationData),
                        "MMDBridge", "mmm_bridge", "plugin_startup.log");
                }
                catch (Exception)
                {
                    return null;
                }
            }
        }

        /// <summary>追加一行诊断记录。任何失败都静默吞掉。</summary>
        internal static void Trace(string message)
        {
            try
            {
                var path = LogPath;
                if (string.IsNullOrEmpty(path)) return;
                var folder = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(folder))
                    Directory.CreateDirectory(folder);

                var line = string.Format(
                    CultureInfo.InvariantCulture,
                    "{0:yyyy-MM-dd HH:mm:ss.fff}  [pid {1}]  {2}{3}",
                    DateTime.Now, System.Diagnostics.Process.GetCurrentProcess().Id,
                    message, Environment.NewLine);

                lock (Gate)
                {
                    File.AppendAllText(path, line, new UTF8Encoding(false));
                }
            }
            catch (Exception)
            {
                // 诊断失败绝不能影响 MMM。
            }
        }

        /// <summary>只在第一次 Update 时记录 —— 否则每帧一行会撑爆日志。</summary>
        internal static void TraceFirstUpdate(float frame, float elapsed)
        {
            if (_updateLogged) return;
            _updateLogged = true;
            Trace(string.Format(CultureInfo.InvariantCulture,
                                "Update() 首次调用 frame={0} elapsed={1}",
                                frame, elapsed));
        }

        /// <summary>记录一次异常，供后续排查。</summary>
        internal static void TraceError(string where, Exception ex)
        {
            Trace(string.Format(CultureInfo.InvariantCulture,
                                "{0} 抛出 {1}: {2}",
                                where, ex.GetType().Name, ex.Message));
        }
    }
}
