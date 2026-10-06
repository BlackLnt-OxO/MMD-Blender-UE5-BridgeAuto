using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace MMDBridge
{
    /// <summary>
    /// 极简 JSON 读写。
    ///
    /// 刻意不引入任何 JSON 库：插件 DLL 必须能在 MMM 进程里无依赖加载，
    /// 而 .NET Framework 4.8 自带的可选项（DataContractJsonSerializer /
    /// JavaScriptSerializer）要么需要额外引用，要么行为不透明。
    /// 本插件只写扁平对象、只读扁平对象，自己实现反而最可控。
    /// </summary>
    internal static class Json
    {
        // ---------------------------------------------------------------
        // 写
        // ---------------------------------------------------------------

        /// <summary>按插入顺序拼装一个扁平 JSON 对象。</summary>
        internal sealed class ObjectWriter
        {
            private readonly StringBuilder _sb = new StringBuilder(512);
            private bool _first = true;

            internal ObjectWriter Begin()
            {
                _sb.Append('{');
                return this;
            }

            private void Sep()
            {
                if (!_first) _sb.Append(',');
                _first = false;
            }

            internal ObjectWriter Str(string key, string value)
            {
                Sep();
                AppendString(key);
                _sb.Append(':');
                if (value == null) _sb.Append("null");
                else AppendString(value);
                return this;
            }

            internal ObjectWriter Num(string key, long value)
            {
                Sep();
                AppendString(key);
                _sb.Append(':').Append(value.ToString(CultureInfo.InvariantCulture));
                return this;
            }

            internal ObjectWriter Num(string key, double value)
            {
                Sep();
                AppendString(key);
                _sb.Append(':');
                if (double.IsNaN(value) || double.IsInfinity(value))
                    _sb.Append('0');
                else
                    _sb.Append(value.ToString("R", CultureInfo.InvariantCulture));
                return this;
            }

            internal ObjectWriter Bool(string key, bool value)
            {
                Sep();
                AppendString(key);
                _sb.Append(':').Append(value ? "true" : "false");
                return this;
            }

            /// <summary>字符串数组，用于骨骼名等清单。</summary>
            internal ObjectWriter StrArray(string key, IEnumerable<string> values)
            {
                Sep();
                AppendString(key);
                _sb.Append(":[");
                var first = true;
                if (values != null)
                {
                    foreach (var v in values)
                    {
                        if (!first) _sb.Append(',');
                        first = false;
                        if (v == null) _sb.Append("null");
                        else AppendString(v);
                    }
                }
                _sb.Append(']');
                return this;
            }

            internal string End()
            {
                _sb.Append('}');
                return _sb.ToString();
            }

            private void AppendString(string s)
            {
                _sb.Append('"');
                foreach (var c in s)
                {
                    switch (c)
                    {
                        case '"': _sb.Append("\\\""); break;
                        case '\\': _sb.Append("\\\\"); break;
                        case '\b': _sb.Append("\\b"); break;
                        case '\f': _sb.Append("\\f"); break;
                        case '\n': _sb.Append("\\n"); break;
                        case '\r': _sb.Append("\\r"); break;
                        case '\t': _sb.Append("\\t"); break;
                        default:
                            if (c < ' ')
                                _sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                            else
                                _sb.Append(c);
                            break;
                    }
                }
                _sb.Append('"');
            }
        }

        // ---------------------------------------------------------------
        // 读（只支持扁平对象，值只支持 string / number / bool / null）
        // ---------------------------------------------------------------

        /// <summary>
        /// 解析扁平 JSON 对象。解析失败返回空字典而不是抛异常 ——
        /// 插件运行在 MMM 进程里，一个坏文件不能让 MMM 崩。
        /// </summary>
        internal static Dictionary<string, string> ParseFlat(string text)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            if (string.IsNullOrEmpty(text)) return result;

            var i = 0;
            SkipWs(text, ref i);
            if (i >= text.Length || text[i] != '{') return result;
            i++;

            while (true)
            {
                SkipWs(text, ref i);
                if (i >= text.Length) return result;
                if (text[i] == '}') return result;
                if (text[i] == ',') { i++; continue; }

                if (text[i] != '"') return result;
                var key = ReadString(text, ref i);
                if (key == null) return result;

                SkipWs(text, ref i);
                if (i >= text.Length || text[i] != ':') return result;
                i++;

                SkipWs(text, ref i);
                if (i >= text.Length) return result;

                string value;
                var c = text[i];
                if (c == '"')
                {
                    value = ReadString(text, ref i);
                    if (value == null) return result;
                }
                else if (c == '[')
                {
                    // 数组整体跳过：本插件只读标量字段。
                    SkipValue(text, ref i);
                    continue;
                }
                else
                {
                    var start = i;
                    while (i < text.Length && text[i] != ',' && text[i] != '}') i++;
                    value = text.Substring(start, i - start).Trim();
                    if (value == "null") value = "";
                }

                result[key] = value;
            }
        }

        private static void SkipValue(string text, ref int i)
        {
            var depth = 0;
            while (i < text.Length)
            {
                var c = text[i];
                if (c == '[' || c == '{') depth++;
                else if (c == ']' || c == '}')
                {
                    depth--;
                    i++;
                    if (depth <= 0) return;
                    continue;
                }
                else if (c == '"')
                {
                    ReadString(text, ref i);
                    continue;
                }
                i++;
            }
        }

        private static void SkipWs(string text, ref int i)
        {
            while (i < text.Length && (text[i] == ' ' || text[i] == '\t' ||
                                       text[i] == '\r' || text[i] == '\n')) i++;
        }

        private static string ReadString(string text, ref int i)
        {
            if (i >= text.Length || text[i] != '"') return null;
            i++;
            var sb = new StringBuilder();
            while (i < text.Length)
            {
                var c = text[i];
                if (c == '"') { i++; return sb.ToString(); }
                if (c != '\\') { sb.Append(c); i++; continue; }

                i++;
                if (i >= text.Length) return null;
                switch (text[i])
                {
                    case '"': sb.Append('"'); i++; break;
                    case '\\': sb.Append('\\'); i++; break;
                    case '/': sb.Append('/'); i++; break;
                    case 'b': sb.Append('\b'); i++; break;
                    case 'f': sb.Append('\f'); i++; break;
                    case 'n': sb.Append('\n'); i++; break;
                    case 'r': sb.Append('\r'); i++; break;
                    case 't': sb.Append('\t'); i++; break;
                    case 'u':
                        if (i + 4 >= text.Length) return null;
                        int code;
                        if (!int.TryParse(text.Substring(i + 1, 4),
                                          NumberStyles.HexNumber,
                                          CultureInfo.InvariantCulture, out code))
                            return null;
                        sb.Append((char)code);
                        i += 5;
                        break;
                    default: i++; break;
                }
            }
            return null;
        }
    }
}
