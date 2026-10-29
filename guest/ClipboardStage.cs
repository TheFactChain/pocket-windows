using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

namespace WindowsClaude
{
    // A completed clipboard copy survives clipboard changes/process exit until a
    // different confirmed Windows boot and a confirmed empty/non-file clipboard.
    public static class ClipboardStage
    {
        public const int MaxLeaseBytes = 64 * 1024 * 1024;
        static readonly Regex StageName = new Regex(@"\Aclipboard-[0-9a-f]{32}\z"), BootName = new Regex(@"\A[0-9]{14}\.[0-9]{6}Z\z");
        [StructLayout(LayoutKind.Sequential)]
        internal struct Info
        {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
            public uint Volume, SizeHigh, SizeLow, Links, High, Low;
        }
        [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle OpenFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Info info);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetFileInformationByHandle(SafeFileHandle handle, int kind, ref byte value, uint length);
        static IOException Error(string message) { return new IOException(message); }
        static Info Identity(SafeFileHandle handle)
        {
            Info info;
            if (!GetFileInformationByHandle(handle, out info) || (info.Attributes & 0x400) != 0 || (info.High == 0 && info.Low == 0)) throw Error("Unusable/reparse identity");
            return info;
        }
        static bool Same(Info a, Info b) { return a.Volume == b.Volume && a.High == b.High && a.Low == b.Low; }
        static SafeFileHandle Open(string path, uint access, uint creation, bool directory)
        {
            var handle = OpenFile(ClipboardFiles.NativePath(path), access, 1, IntPtr.Zero, creation, 0x00200000U | (directory ? 0x02000000U : 0), IntPtr.Zero);
            if (handle.IsInvalid) { int code = Marshal.GetLastWin32Error(); handle.Dispose(); throw new System.ComponentModel.Win32Exception(code); }
            try { if (((Identity(handle).Attributes & 16) != 0) != directory) throw Error("Unexpected entry type"); }
            catch { handle.Dispose(); throw; }
            return handle;
        }
        static Dictionary<string, object> Record(Info value)
        { return new Dictionary<string, object> { { "volume", value.Volume.ToString("x16") }, { "file_id", (((ulong)value.High << 32) | value.Low).ToString("x16") } }; }
        static Info ParseIdentity(object value)
        {
            var item = value as Dictionary<string, object>;
            if (item == null || !new HashSet<string> { "volume", "file_id" }.SetEquals(item.Keys)) throw Error("Invalid marker identity");
            string volume = item["volume"] as string, index = item["file_id"] as string;
            if (volume == null || index == null || !Regex.IsMatch(volume, @"\A[0-9a-f]{16}\z") || !Regex.IsMatch(index, @"\A[0-9a-f]{16}\z")) throw Error("Invalid marker identity");
            ulong serial = Convert.ToUInt64(volume, 16), fileId = Convert.ToUInt64(index, 16);
            if (serial > UInt32.MaxValue || fileId == 0) throw Error("Unusable marker identity");
            return new Info { Volume = (uint)serial, High = (uint)(fileId >> 32), Low = (uint)fileId };
        }
        static void Delete(string path, Info expected)
        {
            using (var handle = Open(path, 0x80010000, 3, false))
            {
                if (!Same(Identity(handle), expected)) throw Error("Marker was replaced");
                byte delete = 1; if (!SetFileInformationByHandle(handle, 4, ref delete, 1)) throw Error("Cannot remove owned marker");
            }
        }
        static byte[] Encode(Dictionary<string, object> value)
        {
            string text = new JavaScriptSerializer { MaxJsonLength = MaxLeaseBytes, RecursionLimit = 8 }.Serialize(value);
            var ascii = new StringBuilder();
            foreach (char item in text)
            {
                if (item > 127) ascii.Append("\\u" + ((int)item).ToString("x4")); else ascii.Append(item);
                if (ascii.Length > MaxLeaseBytes) throw Error("Lease metadata limit");
            }
            return Encoding.ASCII.GetBytes(ascii.ToString());
        }
        sealed class ObjectKeys { internal bool Object, Key; internal readonly HashSet<string> Names = new HashSet<string>(StringComparer.Ordinal); }
        static Dictionary<string, object> Decode(string text)
        {
            // JavaScriptSerializer otherwise silently overwrites duplicate keys.
            var stack = new Stack<ObjectKeys>(); var json = new JavaScriptSerializer { MaxJsonLength = MaxLeaseBytes, RecursionLimit = 8 };
            for (int at = 0; at < text.Length; at++)
            {
                char item = text[at];
                if (item == '{' || item == '[') { if (stack.Count >= 8) throw Error("Lease nesting limit"); stack.Push(new ObjectKeys { Object = item == '{', Key = item == '{' }); }
                else if (item == '}' || item == ']') { if (stack.Count == 0) throw Error("Invalid JSON"); stack.Pop(); }
                else if (item == ',' && stack.Count > 0 && stack.Peek().Object) stack.Peek().Key = true;
                else if (item == '"')
                {
                    int start = at;
                    for (at++; at < text.Length && text[at] != '"'; at++) if (text[at] == '\\') at++;
                    if (at >= text.Length) throw Error("Invalid JSON string");
                    if (stack.Count > 0 && stack.Peek().Object && stack.Peek().Key)
                    {
                        string name = json.Deserialize<string>(text.Substring(start, at - start + 1));
                        if (!stack.Peek().Names.Add(name)) throw Error("Duplicate lease key");
                        stack.Peek().Key = false;
                    }
                }
            }
            return json.DeserializeObject(text) as Dictionary<string, object>;
        }
        public sealed class CleanupResult
        {
            public int Removed, Retained, Failed;
            public bool CleanupUnconfirmed;
        }
        public sealed class Cache
        {
            public string Parent { get; private set; }
            readonly string boot;
            readonly Info identity;
            public Cache(string parent, string bootId)
            {
                Parent = Path.GetFullPath(parent);
                if (Parent.Length > 3) Parent = Parent.TrimEnd('\\');
                if (!Regex.IsMatch(Parent, @"\A[A-Za-z]:\\")) throw Error("Local private parent required");
                if (bootId != null && !BootName.IsMatch(bootId)) throw Error("Invalid boot id");
                boot = bootId;
                var pins = Pins(false); try { identity = Identity(pins[pins.Count - 1]); } finally { Close(pins); }
            }
            List<SafeFileHandle> Pins(bool compare = true)
            {
                var paths = new List<string>(); string current = Parent;
                while (current != null) { paths.Add(current); current = Path.GetDirectoryName(current); }
                paths.Reverse(); var pins = new List<SafeFileHandle>();
                try
                {
                    foreach (string path in paths) { if (pins.Count >= ClipboardFiles.MaxPins) throw Error("Pin limit"); pins.Add(Open(path, 0x80000000, 3, true)); }
                    if (compare && !Same(Identity(pins[pins.Count - 1]), identity)) throw Error("Private parent was replaced");
                    return pins;
                }
                catch { Close(pins); throw; }
            }
            static void Close(List<SafeFileHandle> pins) { for (int i = pins.Count - 1; i >= 0; i--) pins[i].Dispose(); }
            public Ticket Prepare(ClipboardFiles.Receiver receiver)
            {
                var value = receiver.ExportLease(boot); string stage = receiver.Stage;
                if (!String.Equals(Path.GetDirectoryName(stage), Parent, StringComparison.OrdinalIgnoreCase) || !StageName.IsMatch(Path.GetFileName(stage))) throw Error("Receiver belongs to another parent");
                string marker = stage + ".lease.json"; var pins = Pins(); Info owned = new Info(); bool created = false;
                try
                {
                    using (var receiverParent = Open(Path.GetDirectoryName(stage), 0x80000000, 3, true))
                        if (!Same(Identity(receiverParent), identity)) throw Error("Receiver belongs to another parent");
                    using (var handle = Open(marker, 0xC0000000, 1, false))
                    {
                        owned = Identity(handle); created = true; value.Add("marker", Record(owned)); byte[] bytes = Encode(value);
                        using (var output = new FileStream(handle, FileAccess.ReadWrite)) { output.Write(bytes, 0, bytes.Length); output.Flush(true); }
                    }
                    var ticket = new Ticket(this, receiver, stage, marker, owned, (string[])receiver.Roots.Clone());
                    receiver.Detach(); // Durable ticket owns explicit Abort/Park from here.
                    return ticket;
                }
                catch { if (created) try { Delete(marker, owned); } catch { } throw; }
                finally { Close(pins); }
            }
            Dictionary<string, object> Read(string marker, out Info owned)
            {
                using (var handle = Open(marker, 0x80000000, 3, false))
                {
                    owned = Identity(handle);
                    using (var input = new FileStream(handle, FileAccess.Read))
                    {
                        if (input.Length > MaxLeaseBytes) throw Error("Lease metadata limit");
                        string text; using (var reader = new StreamReader(input, new UTF8Encoding(false, true), true)) text = reader.ReadToEnd();
                        var value = Decode(text);
                        if (value == null || !new HashSet<string> { "schema", "stage", "boot_id", "roots", "entries", "marker" }.SetEquals(value.Keys) ||
                            !(value["schema"] is int) || (int)value["schema"] != 1 || !(value["stage"] is string) ||
                            (string)value["stage"] != Path.GetFileName(marker).Substring(0, Path.GetFileName(marker).Length - 11) ||
                            value["boot_id"] != null && (!(value["boot_id"] is string) || !BootName.IsMatch((string)value["boot_id"])) || !Same(ParseIdentity(value["marker"]), owned)) throw Error("Invalid/replaced marker");
                        return value;
                    }
                }
            }
            public CleanupResult Cleanup(IEnumerable<string> currentPaths)
            {
                bool clear = currentPaths != null && !currentPaths.Any(); var result = new CleanupResult { CleanupUnconfirmed = boot == null || !clear }; List<SafeFileHandle> pins;
                try { pins = Pins(); } catch { result.Failed = 1; result.CleanupUnconfirmed = true; return result; }
                var stages = new HashSet<string>(StringComparer.Ordinal); var markers = new HashSet<string>(StringComparer.Ordinal);
                try
                {
                    int scanned = 0;
                    foreach (string marker in Directory.EnumerateFileSystemEntries(Parent))
                    {
                        if (++scanned > 2 * ClipboardFiles.MaxEntries) { result.CleanupUnconfirmed = true; break; }
                        string name = Path.GetFileName(marker);
                        if (StageName.IsMatch(name)) stages.Add(name);
                        if (!name.EndsWith(".lease.json", StringComparison.Ordinal) || !StageName.IsMatch(name.Substring(0, name.Length - 11))) continue;
                        markers.Add(name.Substring(0, name.Length - 11));
                        try
                        {
                            Info owned; var value = Read(marker, out owned);
                            if (boot == null || value["boot_id"] == null || !clear) { result.Retained++; result.CleanupUnconfirmed = true; continue; }
                            if ((string)value["boot_id"] == boot) { result.Retained++; continue; }
                            value.Remove("marker"); ClipboardFiles.Receiver.ResumeCleanup(Parent, value); Delete(marker, owned); result.Removed++;
                        }
                        catch (Exception) { result.Failed++; result.CleanupUnconfirmed = true; }
                    }
                }
                finally { Close(pins); }
                int unknown = stages.Except(markers).Count(); if (unknown != 0) { result.Retained += unknown; result.CleanupUnconfirmed = true; }
                return result;
            }
            public sealed class Ticket
            {
                readonly Cache cache; ClipboardFiles.Receiver receiver; readonly Info identity; bool parked, aborted;
                public string Stage { get; private set; }
                public string Marker { get; private set; }
                public string[] Roots { get; private set; }
                internal Ticket(Cache owner, ClipboardFiles.Receiver source, string stage, string marker, Info markerIdentity, string[] roots)
                { cache = owner; receiver = source; Stage = stage; Marker = marker; identity = markerIdentity; Roots = roots; }
                public void Park()
                {
                    if (parked) return; if (aborted) throw Error("Ticket already aborted");
                    // prepare already flushed the marker. A later marker failure must
                    // never delete a copy that native publication may have exposed.
                    receiver.Park(); receiver = null; parked = true;
                }
                public void Abort()
                {
                    if (parked) throw Error("Published/parked copies cannot be aborted"); if (aborted) return;
                    var pins = cache.Pins();
                    try { receiver.Release(); if (receiver.Stage != null) throw Error("Stage cleanup unconfirmed"); Delete(Marker, identity); receiver = null; aborted = true; }
                    finally { Close(pins); }
                }
            }
        }
        static readonly object BootGate = new object(); static readonly ManualResetEvent BootDone = new ManualResetEvent(false);
        static bool bootStarted; static string bootValue;
        public static string DetectBootId()
        {
            lock (BootGate)
            {
                if (!bootStarted)
                {
                    bootStarted = true; new Thread(() =>
                    {
                        object locator = null, service = null, rows = null;
                        try
                        {
                            locator = Activator.CreateInstance(Type.GetTypeFromProgID("WbemScripting.SWbemLocator"));
                            service = ((dynamic)locator).ConnectServer(".", "root\\cimv2"); rows = ((dynamic)service).ExecQuery("SELECT LastBootUpTime FROM Win32_OperatingSystem");
                            foreach (dynamic row in (System.Collections.IEnumerable)rows)
                            {
                                try
                                {
                                    string raw = (string)row.LastBootUpTime;
                                    if (!Regex.IsMatch(raw, @"\A[0-9]{14}\.[0-9]{6}[+-][0-9]{3}\z")) break;
                                    DateTime date = DateTime.ParseExact(raw.Substring(0, 21), "yyyyMMddHHmmss.ffffff", CultureInfo.InvariantCulture);
                                    int offset = Int32.Parse(raw.Substring(22), CultureInfo.InvariantCulture) * (raw[21] == '+' ? 1 : -1);
                                    bootValue = date.AddMinutes(-offset).ToString("yyyyMMddHHmmss.ffffff'Z'", CultureInfo.InvariantCulture); break;
                                }
                                finally { if (Marshal.IsComObject(row)) Marshal.FinalReleaseComObject(row); }
                            }
                        }
                        catch { bootValue = null; }
                        finally { foreach (object item in new[] { rows, service, locator }) try { if (item != null && Marshal.IsComObject(item)) Marshal.FinalReleaseComObject(item); } catch { } BootDone.Set(); }
                    }) { IsBackground = true }.Start();
                }
            }
            return BootDone.WaitOne(5000) ? bootValue : null;
        }
    }
}
