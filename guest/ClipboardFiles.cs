using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

namespace WindowsClaude
{
    // Transport-independent, preorder file transfer. Sender paths must come from local selection.
    public static class ClipboardFiles
    {
        public const int Chunk = 65536, MaxEntries = 100000, EntryReserve = 4096;
        public const long MaxBytes = 9007199254740991L, Margin = 16 * 1024 * 1024;
        internal const long MaxMetadata = 256 * 1024 * 1024;
        internal const int MaxDepth = 256, MaxPins = 16384;
        static readonly Regex Identifier = new Regex(@"\A[0-9a-f]{32}\z");
        static readonly Regex Reserved = new Regex(@"\A(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])\z", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInformation info);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref byte info, uint size);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int CompareStringOrdinal(string a, int aLength, string b, int bLength, bool ignoreCase);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int LCMapStringEx(string locale, uint flags, string source, int count, StringBuilder destination, int capacity, IntPtr version, IntPtr reserved, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool GetDiskFreeSpaceEx(string path, out ulong available, out ulong total, out ulong free);
        [StructLayout(LayoutKind.Sequential)]
        struct FileInformation
        {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
            public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        static IOException Error(string message) { return new IOException(message); }
        internal static string NativePath(string absolute)
        {
            // Prefix only normalized ordinary paths; shell paths and identity records stay unchanged.
            if (String.IsNullOrEmpty(absolute) || absolute.IndexOf('/') >= 0 ||
                absolute.StartsWith(@"\\?\", StringComparison.Ordinal) || absolute.StartsWith(@"\\.\", StringComparison.Ordinal)) throw Error("Ordinary absolute path required");
            int stream = absolute.IndexOf(':', 2);
            if (stream >= 0 && absolute.Substring(stream) != ":Zone.Identifier") throw Error("Unexpected file stream");
            string ordinary = stream < 0 ? absolute : absolute.Substring(0, stream), normalized;
            try { normalized = Path.GetFullPath(ordinary); }
            catch (ArgumentException) { throw Error("Ordinary absolute path required"); }
            catch (NotSupportedException) { throw Error("Ordinary absolute path required"); }
            if (!String.Equals(normalized, ordinary, StringComparison.Ordinal)) throw Error("Ordinary absolute path required");
            if (absolute.StartsWith(@"\\", StringComparison.Ordinal))
            {
                int share = absolute.IndexOf('\\', 2);
                if (share <= 2 || share == absolute.Length - 1) throw Error("UNC share path required");
                return @"\\?\UNC\" + absolute.Substring(2);
            }
            if (absolute.Length < 3 || !((absolute[0] >= 'A' && absolute[0] <= 'Z') || (absolute[0] >= 'a' && absolute[0] <= 'z')) || absolute[1] != ':' || absolute[2] != '\\') throw Error("Ordinary absolute path required");
            return @"\\?\" + absolute;
        }
        sealed class OrdinalNames : IEqualityComparer<string>
        {
            internal static readonly OrdinalNames Instance = new OrdinalNames();
            public bool Equals(string a, string b)
            {
                int result = CompareStringOrdinal(a, -1, b, -1, true);
                if (result == 0) throw Error("Cannot compare entry names");
                return result == 2;
            }
            public int GetHashCode(string value)
            {
                // Invariant uppercasing can merge extra names; ordinal equality decides the collision.
                int size = LCMapStringEx("", 0x200, value, -1, null, 0, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
                if (size == 0) throw Error("Cannot hash entry name");
                var upper = new StringBuilder(size);
                if (LCMapStringEx("", 0x200, value, -1, upper, size, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero) == 0) throw Error("Cannot hash entry name");
                return StringComparer.Ordinal.GetHashCode(upper.ToString());
            }
        }
        static void Id(string id) { if (id == null || !Identifier.IsMatch(id)) throw Error("Invalid transfer id"); }
        internal static string Relative(string value)
        {
            if (String.IsNullOrEmpty(value) || value.Length > 24000 || value.Count(c => c == '/') + 1 > MaxDepth) throw Error("Invalid path");
            foreach (string part in value.Split('/'))
                if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") ||
                    part.Any(c => Char.IsControl(c) || "\\:*?\"<>|".IndexOf(c) >= 0) || Reserved.IsMatch(part.Split('.')[0].TrimEnd(' '))) throw Error("Unsafe path");
            return value;
        }
        internal static string Selection(string absolute)
        {
            string root = Path.GetPathRoot(absolute);
            if (absolute.Length > 24000 || absolute.Substring(root.Length).Split(new[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries).Length > MaxDepth)
                throw Error("Selection path limit");
            return absolute;
        }
        internal static long Charge(long used, string relative, string absolute, int zoneBytes = 0)
        {
            if (used < 0 || used > MaxMetadata) throw Error("Metadata budget exceeded");
            long total = checked(used + zoneBytes + 2L * (relative.Length * 2L) + 2L * (absolute.Length * 2L) + 512);
            if (total > MaxMetadata) throw Error("Metadata budget exceeded");
            return total;
        }
        static long Number(Dictionary<string, object> f, string key)
        {
            object v;
            if (!f.TryGetValue(key, out v) || !(v is int || v is long)) throw Error("Invalid number");
            long n = Convert.ToInt64(v);
            if (n < 0 || n > MaxBytes) throw Error("Invalid number");
            return n;
        }
        static string Str(Dictionary<string, object> f, string key)
        { object v; return f.TryGetValue(key, out v) ? v as string : null; }
        static SafeFileHandle Open(string path, uint access, uint creation, bool directory)
        {
            if (directory) access = 0x80000000 | (access & 0x10000);
            var h = CreateFile(NativePath(path), access, 1, IntPtr.Zero, creation, 0x00200000U | (directory ? 0x02000000U : 0), IntPtr.Zero);
            if (h.IsInvalid)
            {
                int code = Marshal.GetLastWin32Error(); h.Dispose();
                throw new System.ComponentModel.Win32Exception(code, "Cannot open selected/staged path");
            }
            FileInformation info;
            if (!GetFileInformationByHandle(h, out info) || (info.Attributes & 0x400) != 0 || ((info.Attributes & 16) != 0) != directory)
            { h.Dispose(); throw Error("Reparse or unexpected file type"); }
            return h;
        }
        static void PinDirectory(string path, List<IDisposable> pins, HashSet<string> known, ref long metadata)
        {
            if (known.Contains(path)) return;
            if (pins.Count >= MaxPins) throw Error("Pin handle budget exceeded");
            metadata = Charge(metadata, "", path);
            pins.Add(Open(path, 0x80000000, 3, true)); known.Add(path);
        }
        static void PinParents(string path, List<IDisposable> pins, HashSet<string> known, ref long metadata)
        {
            Selection(path);
            string current = Path.GetPathRoot(path);
            PinDirectory(current, pins, known, ref metadata);
            foreach (string part in path.Substring(current.Length).Split(new[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries))
            {
                current = Path.Combine(current, part);
                PinDirectory(current, pins, known, ref metadata);
            }
        }
        static FileInformation Info(SafeFileHandle handle)
        { FileInformation info; if (!GetFileInformationByHandle(handle, out info)) throw Error("Cannot stat source"); return info; }
        static bool Same(FileInformation a, FileInformation b)
        {
            return a.Attributes == b.Attributes && a.Volume == b.Volume && a.IndexHigh == b.IndexHigh && a.IndexLow == b.IndexLow &&
                a.SizeHigh == b.SizeHigh && a.SizeLow == b.SizeLow && a.Written.dwHighDateTime == b.Written.dwHighDateTime && a.Written.dwLowDateTime == b.Written.dwLowDateTime;
        }
        static long Size(FileInformation info) { return ((long)info.SizeHigh << 32) | info.SizeLow; }
        static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
        static byte[] Zone(string path)
        {
            try
            {
                using (var h = Open(path + ":Zone.Identifier", 0x80000000, 3, false))
                using (var input = new FileStream(h, FileAccess.Read))
                {
                    if (input.Length > Chunk) throw Error("Zone.Identifier too large");
                    byte[] raw = new byte[(int)input.Length];
                    int at = 0, n;
                    while (at < raw.Length && (n = input.Read(raw, at, raw.Length - at)) != 0) at += n;
                    if (at != raw.Length) throw Error("Truncated Zone.Identifier");
                    return raw;
                }
            }
            catch (System.ComponentModel.Win32Exception e) { if (e.NativeErrorCode == 2 || e.NativeErrorCode == 3) return null; throw; }
        }
        static bool EqualBytes(byte[] a, byte[] b) { return a == null ? b == null : b != null && a.SequenceEqual(b); }
        static void Space(string path, long bytes)
        {
            ulong available, total, free;
            if (!GetDiskFreeSpaceEx(NativePath(path), out available, out total, out free) || available < (ulong)bytes + (ulong)Margin) throw Error("Insufficient disk space");
        }
        static Dictionary<string, object> Frame(string id, string kind, params object[] values)
        {
            var f = new Dictionary<string, object> { { "id", id }, { "event", kind } };
            for (int i = 0; i < values.Length; i += 2) f.Add((string)values[i], values[i + 1]);
            return f;
        }
        public sealed class Sender
        {
            readonly string id;
            readonly string[] paths;
            internal long MetadataBytes;
            internal int PeakPinCount;
            sealed class Entry { public string Path, Relative; public FileInformation Info; public byte[] Zone; public bool Directory; }
            public Sender(IEnumerable<string> locallySelectedPaths, string transferId)
            {
                Id(transferId); id = transferId;
                var selected = new List<string>();
                foreach (string value in locallySelectedPaths.Take(MaxEntries + 1))
                {
                    if (selected.Count == MaxEntries) throw Error("Selection limit");
                    string path = Selection(Path.GetFullPath(value));
                    MetadataBytes = Charge(MetadataBytes, Path.GetFileName(path), path);
                    selected.Add(path);
                }
                paths = selected.ToArray();
            }
            void Visit(string path, string relative, List<Entry> entries, HashSet<string> used, List<IDisposable> pins, HashSet<string> known, ref long total, ref long metadata)
            {
                Relative(relative);
                if (used.Contains(relative)) throw Error("Case collision");
                bool directory = (File.GetAttributes(path) & FileAttributes.Directory) != 0;
                FileInformation info;
                using (var h = Open(path, 0x80, 3, directory)) info = Info(h);
                byte[] zone = directory ? null : ClipboardFiles.Zone(path);
                metadata = Charge(metadata, relative, path, zone == null ? 0 : zone.Length);
                total = checked(total + (directory ? 0 : Size(info)) + (zone == null ? 0 : zone.Length));
                if (entries.Count >= MaxEntries || total > MaxBytes) throw Error("Transfer limit");
                used.Add(relative);
                entries.Add(new Entry { Path = path, Relative = relative, Info = info, Zone = zone, Directory = directory });
                if (directory)
                {
                    PinDirectory(path, pins, known, ref metadata);
                    PeakPinCount = Math.Max(PeakPinCount, pins.Count);
                    foreach (string child in Directory.EnumerateFileSystemEntries(path))
                    {
                        string childRelative = Relative(relative + "/" + Path.GetFileName(child));
                        Visit(child, childRelative, entries, used, pins, known, ref total, ref metadata);
                    }
                }
            }
            public IEnumerable<Dictionary<string, object>> Frames()
            {
                var pins = new List<IDisposable>();
                var entries = new List<Entry>();
                var used = new HashSet<string>(OrdinalNames.Instance);
                var known = new HashSet<string>(OrdinalNames.Instance);
                long total = 0, metadata = MetadataBytes;
                try
                {
                    foreach (string p in paths)
                    {
                        PinParents(Path.GetDirectoryName(p), pins, known, ref metadata);
                        PeakPinCount = Math.Max(PeakPinCount, pins.Count);
                        string name = Path.GetFileName(p), alias = name; int suffix = 2;
                        string stem = name, extension = "";
                        if (!Directory.Exists(p))
                        {
                            int dot = name.LastIndexOf('.');
                            if (dot > 0) { stem = name.Substring(0, dot); extension = name.Substring(dot); }
                        }
                        Relative(name);
                        while (used.Contains(alias)) alias = stem + " (" + suffix++ + ")" + extension;
                        Visit(p, alias, entries, used, pins, known, ref total, ref metadata);
                    }
                    if (entries.Count == 0) throw Error("Empty selection");
                    yield return Frame(id, "begin", "count", entries.Count, "bytes", total);
                    foreach (var e in entries)
                    {
                        using (var h = Open(e.Path, 0x80000000, 3, e.Directory))
                        {
                            if (!Same(e.Info, Info(h))) throw Error("Source changed");
                            yield return Frame(id, "entry", "path", e.Relative, "kind", e.Directory ? "dir" : "file", "size", e.Directory ? 0 : Size(e.Info), "zone", e.Zone == null ? null : Convert.ToBase64String(e.Zone));
                            if (e.Directory) continue;
                            using (var input = new FileStream(h, FileAccess.Read))
                            using (var hash = SHA256.Create())
                            {
                                byte[] buffer = new byte[Chunk]; int n; long offset = 0;
                                while ((n = input.Read(buffer, 0, buffer.Length)) != 0)
                                {
                                    hash.TransformBlock(buffer, 0, n, null, 0);
                                    yield return Frame(id, "data", "offset", offset, "data", Convert.ToBase64String(buffer, 0, n));
                                    offset += n;
                                }
                                hash.TransformFinalBlock(new byte[0], 0, 0);
                                if (offset != Size(e.Info) || !Same(e.Info, Info(h)) || !EqualBytes(e.Zone, ClipboardFiles.Zone(e.Path))) throw Error("Source changed");
                                yield return Frame(id, "file_end", "sha256", Hex(hash.Hash));
                            }
                        }
                    }
                    foreach (var e in entries)
                        using (var h = Open(e.Path, 0x80, 3, e.Directory))
                            if (!Same(e.Info, Info(h)) || (!e.Directory && !EqualBytes(e.Zone, ClipboardFiles.Zone(e.Path)))) throw Error("Source changed");
                    yield return Frame(id, "end");
                }
                finally
                {
                    for (int i = pins.Count - 1; i >= 0; i--) pins[i].Dispose();
                    pins.Clear(); entries.Clear(); used.Clear(); known.Clear();
                }
            }
        }
        public sealed class Receiver : IDisposable
        {
            readonly string id;
            readonly List<IDisposable> pins = new List<IDisposable>();
            readonly List<FileStream> outputs = new List<FileStream>();
            readonly Dictionary<string, string> entries = new Dictionary<string, string>(OrdinalNames.Instance);
            readonly List<string> names = new List<string>();
            sealed class Owned { internal string Path; internal FileInformation Identity; internal bool Directory; internal SafeFileHandle Pin; }
            readonly List<Owned> owned = new List<Owned>();
            FileStream file;
            SHA256 hash;
            string target;
            byte[] zone;
            long expectedCount, expectedBytes, count, bytes, size, offset;
            bool begun, closed, detached;
            internal Action<string> BeforeCleanupDelete;
            internal Func<ulong> AvailableBytes;
            internal long MetadataBytes;
            internal int PinCount;
            public string Stage { get; private set; }
            public string[] Roots { get; private set; }
            public Receiver(string privateStageParent, string transferId)
            {
                Id(transferId); id = transferId;
                try
                {
                    string parent = Selection(Path.GetFullPath(privateStageParent));
                    PinParents(parent, pins, new HashSet<string>(OrdinalNames.Instance), ref MetadataBytes);
                    PinCount = pins.Count;
                    Space(parent, EntryReserve);
                    string stage = Path.Combine(parent, "clipboard-" + Guid.NewGuid().ToString("N"));
                    MetadataBytes = Charge(MetadataBytes, "", stage);
                    if (PinCount >= MaxPins) throw Error("Pin handle budget exceeded");
                    // CreateDirectory alone is not exclusive; CreateDirectoryW reports collisions.
                    if (!CreateDirectory(NativePath(stage), IntPtr.Zero)) throw Error("Cannot create fresh stage");
                    Stage = stage;
                    TrackDirectory(Stage);
                }
                catch { Dispose(); throw; }
            }
            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            static extern bool CreateDirectory(string path, IntPtr attributes);
            void TrackDirectory(string path)
            {
                if (PinCount >= MaxPins) throw Error("Pin handle budget exceeded");
                var handle = Open(path, 0x80000000, 3, true);
                pins.Add(handle);
                PinCount++;
                owned.Add(new Owned { Path = path, Identity = Info(handle), Directory = true, Pin = handle });
            }
            void CheckSpace(long bytes)
            {
                if (AvailableBytes == null) Space(Stage, bytes);
                else if (AvailableBytes() < (ulong)bytes + (ulong)Margin) throw Error("Insufficient disk space");
            }
            public string[] Accept(Dictionary<string, object> frame)
            {
                try { return AcceptCore(frame); }
                catch { Dispose(); throw; }
            }
            string[] AcceptCore(Dictionary<string, object> f)
            {
                if (closed || Roots != null || f == null || Str(f, "id") != id ||
                    Encoding.UTF8.GetByteCount(new JavaScriptSerializer { MaxJsonLength = 1048576, RecursionLimit = 16 }.Serialize(f)) >= 1048576) throw Error("Invalid frame");
                string ev = Str(f, "event"), fields;
                switch (ev)
                {
                    case "begin": fields = "count,bytes"; break;
                    case "entry": fields = "path,kind,size,zone"; break;
                    case "data": fields = "offset,data"; break;
                    case "file_end": fields = "sha256"; break;
                    case "end": fields = ""; break;
                    default: throw Error("Unexpected event");
                }
                var keys = new HashSet<string>(fields.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)); keys.Add("id"); keys.Add("event");
                if (!keys.SetEquals(f.Keys)) throw Error("Unexpected fields");
                if (ev == "begin")
                {
                    if (begun) throw Error("Duplicate begin");
                    expectedCount = Number(f, "count"); expectedBytes = Number(f, "bytes");
                    if (expectedCount < 1 || expectedCount > MaxEntries) throw Error("Entry limit");
                    CheckSpace(expectedBytes + expectedCount * EntryReserve * 2); begun = true;
                }
                else if (!begun) throw Error("Missing begin");
                else if (ev == "entry")
                {
                    if (file != null) throw Error("Unfinished file");
                    string relative = Relative(Str(f, "path")), kind = Str(f, "kind");
                    int slash = relative.LastIndexOf('/'); string parentKind;
                    if (entries.ContainsKey(relative) || (slash >= 0 && (!entries.TryGetValue(relative.Substring(0, slash), out parentKind) || parentKind != "dir")) ||
                        (kind != "dir" && kind != "file") || count >= expectedCount) throw Error("Invalid entry order/collision");
                    size = Number(f, "size"); byte[] entryZone = null;
                    if (f["zone"] != null)
                    {
                        string encoded = Str(f, "zone");
                        if (encoded == null || encoded.Length > 87384 || encoded.Any(Char.IsWhiteSpace)) throw Error("Invalid zone");
                        entryZone = Convert.FromBase64String(encoded);
                        if (entryZone.Length > Chunk) throw Error("Invalid zone");
                    }
                    if (kind == "dir" && (size != 0 || entryZone != null)) throw Error("Directory payload");
                    if (bytes + size + (entryZone == null ? 0 : entryZone.Length) > expectedBytes) throw Error("Excess bytes");
                    target = Path.Combine(Stage, relative.Replace('/', Path.DirectorySeparatorChar));
                    MetadataBytes = Charge(MetadataBytes, relative, target, entryZone == null ? 0 : entryZone.Length);
                    if (kind == "dir" && PinCount >= MaxPins) throw Error("Pin handle budget exceeded");
                    zone = entryZone;
                    CheckSpace(size + (zone == null ? 0 : zone.Length) + EntryReserve);
                    if (kind == "dir")
                    {
                        if (!CreateDirectory(NativePath(target), IntPtr.Zero)) throw Error("Cannot create directory");
                        TrackDirectory(target);
                    }
                    else
                    {
                        file = new FileStream(Open(target, 0x40000000, 1, false), FileAccess.Write, 1);
                        owned.Add(new Owned { Path = target, Identity = Info(file.SafeFileHandle), Directory = false });
                        offset = 0; hash = SHA256.Create();
                    }
                    entries.Add(relative, kind); count++;
                    if (slash < 0) names.Add(relative);
                }
                else if (ev == "data")
                {
                    string encoded = Str(f, "data");
                    if (file == null || Number(f, "offset") != offset || encoded == null || encoded.Length > 87384 || encoded.Any(Char.IsWhiteSpace)) throw Error("Invalid data offset");
                    byte[] raw = Convert.FromBase64String(encoded);
                    if (raw.Length < 1 || raw.Length > Chunk || offset + raw.Length > size) throw Error("Invalid data size");
                    CheckSpace(raw.Length);
                    file.Write(raw, 0, raw.Length); hash.TransformBlock(raw, 0, raw.Length, null, 0); offset += raw.Length;
                }
                else if (ev == "file_end")
                {
                    if (file == null || offset != size) throw Error("Incomplete file");
                    hash.TransformFinalBlock(new byte[0], 0, 0);
                    if (Str(f, "sha256") != Hex(hash.Hash)) throw Error("Corrupt file");
                    file.Flush(); outputs.Add(file); file = null;
                    hash.Dispose(); hash = null;
                    if (zone != null)
                    {
                        CheckSpace(zone.Length + EntryReserve);
                        var output = new FileStream(Open(target + ":Zone.Identifier", 0x40000000, 1, false), FileAccess.Write, 1);
                        outputs.Add(output); output.Write(zone, 0, zone.Length); output.Flush();
                    }
                    bytes += size + (zone == null ? 0 : zone.Length);
                }
                else if (ev == "end")
                {
                    if (file != null || count != expectedCount || bytes != expectedBytes) throw Error("Incomplete transfer");
                    foreach (var output in outputs) output.Dispose();
                    outputs.Clear();
                    Roots = names.Select(n => Path.Combine(Stage, n)).ToArray(); return Roots;
                }
                return null;
            }
            public Dictionary<string, object> ExportLease(string bootId)
            {
                if (closed || Roots == null || file != null) throw Error("Not completed");
                if (bootId != null && !Regex.IsMatch(bootId, @"\A[0-9]{14}\.[0-9]{6}Z\z")) throw Error("Invalid boot id");
                var json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 8 };
                long budget = 256;
                Func<object, long> charge = value => json.Serialize(value).Sum(c => c > 127 ? 6L : 1L) + 1;
                foreach (string name in names) { budget += charge(name); if (budget > 64 * 1024 * 1024) throw Error("Lease metadata limit"); }
                var records = new List<object>();
                foreach (var item in owned)
                {
                    ulong index = ((ulong)item.Identity.IndexHigh << 32) | item.Identity.IndexLow;
                    if (index == 0) throw Error("Unusable stage identity");
                    var record = new Dictionary<string, object> { { "path", item.Path == Stage ? "" : item.Path.Substring(Stage.Length + 1).Replace('\\', '/') },
                        { "kind", item.Directory ? "dir" : "file" }, { "volume", item.Identity.Volume.ToString("x16") }, { "file_id", index.ToString("x16") } };
                    budget += charge(record); if (budget > 64 * 1024 * 1024) throw Error("Lease metadata limit");
                    records.Add(record);
                }
                return new Dictionary<string, object> { { "schema", 1 }, { "stage", Path.GetFileName(Stage) }, { "boot_id", bootId }, { "roots", names.ToArray() }, { "entries", records.ToArray() } };
            }
            public void Park()
            {
                if (closed || Roots == null || file != null) throw Error("Not completed");
                foreach (var output in outputs) output.Dispose();
                for (int i = pins.Count - 1; i >= 0; i--) pins[i].Dispose();
                outputs.Clear(); pins.Clear(); owned.Clear(); entries.Clear(); names.Clear(); PinCount = 0;
                zone = null; target = null; if (hash != null) { hash.Dispose(); hash = null; }
                Stage = null; Roots = null; closed = detached = true;
            }
            public static void ResumeCleanup(string privateStageParent, Dictionary<string, object> lease)
            {
                var keys = new HashSet<string> { "schema", "stage", "boot_id", "roots", "entries" };
                if (lease == null || !keys.SetEquals(lease.Keys) || !(lease["schema"] is int) || (int)lease["schema"] != 1 ||
                    !(lease["stage"] is string) || !Regex.IsMatch((string)lease["stage"], @"\Aclipboard-[0-9a-f]{32}\z") ||
                    lease["boot_id"] != null && (!(lease["boot_id"] is string) || !Regex.IsMatch((string)lease["boot_id"], @"\A[0-9]{14}\.[0-9]{6}Z\z"))) throw Error("Invalid stage lease");
                var roots = lease["roots"] as System.Collections.IList; var items = lease["entries"] as System.Collections.IList;
                if (roots == null || roots.Count < 1 || roots.Count > MaxEntries || items == null || items.Count < 2 || items.Count > MaxEntries + 1) throw Error("Invalid lease count");
                var json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 8 };
                long budget = 256;
                Func<object, long> charge = value => json.Serialize(value).Sum(c => c > 127 ? 6L : 1L) + 1;
                foreach (object value in roots)
                {
                    string name = value as string; Relative(name); if (name.Contains("/")) throw Error("Invalid root"); budget += charge(name);
                }
                var known = new Dictionary<string, string>(OrdinalNames.Instance); var top = new List<string>(); var records = new List<Owned>();
                string parent = Selection(Path.GetFullPath(privateStageParent));
                if (!Regex.IsMatch(parent, @"\A[A-Za-z]:\\")) throw Error("Local private parent required");
                string stage = Path.Combine(parent, (string)lease["stage"]);
                foreach (object value in items)
                {
                    var item = value as Dictionary<string, object>;
                    if (item == null || !new HashSet<string> { "path", "kind", "volume", "file_id" }.SetEquals(item.Keys)) throw Error("Invalid stage entry");
                    string relative = Str(item, "path"), kind = Str(item, "kind"), volume = Str(item, "volume"), fileId = Str(item, "file_id");
                    if (relative == null || (kind != "dir" && kind != "file") || volume == null || fileId == null ||
                        !Regex.IsMatch(volume, @"\A[0-9a-f]{16}\z") || !Regex.IsMatch(fileId, @"\A[0-9a-f]{16}\z")) throw Error("Invalid stage identity");
                    ulong serial = Convert.ToUInt64(volume, 16), index = Convert.ToUInt64(fileId, 16);
                    if (serial > UInt32.MaxValue || index == 0) throw Error("Unusable stage identity");
                    if (records.Count == 0) { if (relative != "" || kind != "dir") throw Error("Missing stage identity"); }
                    else
                    {
                        Relative(relative); string parentKind; int slash = relative.LastIndexOf('/');
                        if (known.ContainsKey(relative) || slash >= 0 && (!known.TryGetValue(relative.Substring(0, slash), out parentKind) || parentKind != "dir")) throw Error("Invalid stage entry order");
                        known.Add(relative, kind); if (slash < 0) top.Add(relative);
                    }
                    budget += charge(item); if (budget > 64 * 1024 * 1024) throw Error("Lease metadata limit");
                    records.Add(new Owned { Path = relative.Length == 0 ? stage : Path.Combine(stage, relative.Replace('/', '\\')), Directory = kind == "dir", Identity = new FileInformation { Volume = (uint)serial, IndexHigh = (uint)(index >> 32), IndexLow = (uint)index } });
                }
                if (!top.SequenceEqual(roots.Cast<string>())) throw Error("Stage roots differ");
                var parents = new List<IDisposable>(); var directoryPins = new Dictionary<string, SafeFileHandle>(StringComparer.Ordinal); long metadata = 0;
                try
                {
                    PinParents(parent, parents, new HashSet<string>(OrdinalNames.Instance), ref metadata);
                    foreach (var item in records)
                    {
                        try
                        {
                            FileInformation actual;
                            if (item.Directory)
                            {
                                if (parents.Count + directoryPins.Count >= MaxPins) throw Error("Pin handle budget exceeded");
                                var pin = Open(item.Path, 0x80000000, 3, true); directoryPins.Add(item.Path, pin); actual = Info(pin);
                            }
                            else using (var pin = Open(item.Path, 0x80000000, 3, false)) actual = Info(pin);
                            if (actual.Volume != item.Identity.Volume || actual.IndexHigh != item.Identity.IndexHigh || actual.IndexLow != item.Identity.IndexLow) throw Error("Staging cleanup refused a replaced entry");
                        }
                        catch (System.ComponentModel.Win32Exception error)
                        {
                            if (error.NativeErrorCode != 2 && error.NativeErrorCode != 3) throw;
                            if (item.Path == stage) return;
                        }
                    }
                    for (int i = records.Count - 1; i >= 0; i--)
                    {
                        var item = records[i]; SafeFileHandle pin;
                        if (directoryPins.TryGetValue(item.Path, out pin)) { pin.Dispose(); directoryPins.Remove(item.Path); }
                        try
                        {
                            using (var ownedHandle = Open(item.Path, 0x80010000, 3, item.Directory))
                            {
                                var actual = Info(ownedHandle);
                                if (actual.Volume != item.Identity.Volume || actual.IndexHigh != item.Identity.IndexHigh || actual.IndexLow != item.Identity.IndexLow) throw Error("Staging cleanup refused a replaced entry");
                                byte delete = 1; if (!SetFileInformationByHandle(ownedHandle, 4, ref delete, 1)) throw Error("Cannot remove owned staging entry");
                            }
                        }
                        catch (System.ComponentModel.Win32Exception error) { if (error.NativeErrorCode != 2 && error.NativeErrorCode != 3) throw; }
                    }
                }
                finally
                {
                    foreach (var pin in directoryPins.Values) pin.Dispose();
                    for (int i = parents.Count - 1; i >= 0; i--) parents[i].Dispose();
                }
            }
            public string Detach()
            {
                if (closed || Roots == null) throw Error("Not completed");
                detached = true; return Stage;
            }
            public void Abort() { Dispose(); }
            public void Dispose()
            {
                if (!detached) Release();
            }
            // Keep this Receiver alive after Detach, then Release when the clipboard lifetime ends.
            public void Release()
            {
                if (closed) return;
                if (file != null) { file.Dispose(); file = null; }
                if (hash != null) { hash.Dispose(); hash = null; }
                foreach (var output in outputs) output.Dispose();
                outputs.Clear();
                Exception failure = Stage != null && owned.Count == 0 ? Error("Staging identity was unavailable; cleanup refused") : null;
                try
                {
                    for (int i = owned.Count - 1; i >= 0; i--)
                    {
                        var entry = owned[i];
                        if (entry.Pin != null) { entry.Pin.Dispose(); pins.Remove(entry.Pin); }
                        try
                        {
                            if (BeforeCleanupDelete != null) BeforeCleanupDelete(entry.Path);
                            using (var handle = Open(entry.Path, 0x80010000, 3, entry.Directory))
                            {
                                var actual = Info(handle);
                                if (actual.Volume != entry.Identity.Volume || actual.IndexHigh != entry.Identity.IndexHigh || actual.IndexLow != entry.Identity.IndexLow)
                                    throw Error("Staging cleanup refused a replaced entry");
                                byte delete = 1;
                                if (!SetFileInformationByHandle(handle, 4, ref delete, 1)) throw Error("Cannot remove owned staging entry");
                            }
                        }
                        catch (Exception error) { if (failure == null) failure = error; }
                    }
                }
                finally
                {
                    for (int i = pins.Count - 1; i >= 0; i--) pins[i].Dispose();
                    pins.Clear(); owned.Clear(); entries.Clear(); names.Clear(); PinCount = 0; closed = true;
                }
                if (failure != null) throw new IOException("Identity-safe staging cleanup could not remove all entries", failure);
                Stage = null;
            }
        }
    }
}
