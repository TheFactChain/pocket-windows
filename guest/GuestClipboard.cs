using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net.Sockets;
using System.Linq;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace WindowsClaude
{
    // The gateway-free file NIC forwards only this session's local clipboard listener.
    public static class GuestClipboard
    {
        public const int MaxChars = 4 * 1024 * 1024, MaxLine = 16 * 1024 * 1024;
        internal const long MaxSequence = 9007199254740991L;
        const string Exclude = "ExcludeClipboardContentFromMonitorProcessing";
        static volatile string statusCode = "WAITING";
        public static string StatusCode { get { return statusCode; } }
        static void Status(string code) { statusCode = code; }
        internal static string CombinedStatus(string native, string transport, string protocol)
        { return native ?? (transport == "CONNECTED" ? protocol ?? "CONNECTED" : transport); }

        internal sealed class Frame
        {
            internal string Type, Origin, Text, TransferId;
            internal Dictionary<string, object> Body;
            internal string[] Paths;
            internal long Sequence;
            internal bool Ok, Applied;
        }

        internal sealed class FileCopy
        {
            internal string[] Paths;
            internal string Id, Origin;
            internal long Sequence;
            internal bool Stored;
        }

        internal static string Serialize(Dictionary<string, object> message)
        {
            try
            {
                string line = new JavaScriptSerializer { MaxJsonLength = MaxLine, RecursionLimit = 16 }.Serialize(message);
                return new UTF8Encoding(false, true).GetByteCount(line) <= MaxLine ? line : null;
            }
            catch (InvalidOperationException) { return null; }
            catch (EncoderFallbackException) { return null; }
        }

        public static string Encode(string nonce, string text)
        { return Encode(nonce, text, 1, "guest"); }

        public static string Encode(string nonce, string text, long sequence, string origin)
        {
            if (String.IsNullOrEmpty(text) || text.Length > MaxChars || sequence < 1 || sequence > MaxSequence || !Origin(origin)) return null;
            byte[] raw;
            try { raw = new UTF8Encoding(false, true).GetBytes(text); }
            catch (EncoderFallbackException) { return null; }
            using (var packed = new MemoryStream())
            {
                using (var deflate = new DeflateStream(packed, CompressionLevel.Optimal, true)) deflate.Write(raw, 0, raw.Length);
                return Serialize(new Dictionary<string, object>
                    { { "v", 2 }, { "n", nonce }, { "t", "text" }, { "s", sequence }, { "o", origin }, { "z", 1 }, { "d", Convert.ToBase64String(packed.ToArray()) } });
            }
        }

        internal static string Control(string nonce, string type, string origin, long sequence = 0, bool ok = false, bool applied = false)
        {
            var message = new Dictionary<string, object> { { "v", 2 }, { "n", nonce }, { "t", type }, { "o", origin } };
            if (type == "ack") { message["s"] = sequence; message["ok"] = ok; message["applied"] = applied; }
            if (type == "skip") message["s"] = sequence;
            if (type == "hello") message["ready"] = ok;
            return Serialize(message);
        }

        // The text of a valid line of this session; anything unexpected is dropped.
        public static string Decode(string line, string nonce)
        {
            Frame frame = DecodeFrame(line, nonce);
            return frame != null && frame.Type == "text" ? frame.Text : null;
        }

        internal static Frame DecodeFrame(string line, string nonce)
        {
            try
            {
                if (line == null || line.Length > MaxLine || new UTF8Encoding(false, true).GetByteCount(line) > MaxLine) return null;
                var message = new JavaScriptSerializer { MaxJsonLength = MaxLine, RecursionLimit = 16 }.DeserializeObject(line) as Dictionary<string, object>;
                if (message == null || Number(message, "v") != 2 || Str(message, "n") != nonce || !Origin(Str(message, "o"))) return null;
                var frame = new Frame { Type = Str(message, "t"), Origin = Str(message, "o") };
                if (frame.Type == "hello")
                {
                    object ready;
                    if (!message.TryGetValue("ready", out ready) || !(ready is bool)) return null;
                    frame.Ok = (bool)ready; return frame;
                }
                object sequence;
                if (!message.TryGetValue("s", out sequence) || !(sequence is int || sequence is long)) return null;
                frame.Sequence = Convert.ToInt64(sequence);
                if (frame.Sequence < 1 || frame.Sequence > MaxSequence) return null;
                if (frame.Type == "skip") return frame;
                if (frame.Type == "ack")
                {
                    object ok;
                    if (!message.TryGetValue("ok", out ok) || !(ok is bool)) return null;
                    object applied;
                    if (message.TryGetValue("applied", out applied)) { if (!(applied is bool)) return null; frame.Applied = (bool)applied; }
                    if (message.ContainsKey("i"))
                    {
                        frame.TransferId = Str(message, "i");
                        if (!ValidTransferId(frame.TransferId)) return null;
                    }
                    frame.Ok = (bool)ok; return frame;
                }
                if (frame.Type == "files" || frame.Type == "stored" || frame.Type == "cancel")
                {
                    if (frame.Type == "files")
                    {
                        object body;
                        if (!message.TryGetValue("b", out body) || (frame.Body = body as Dictionary<string, object>) == null) return null;
                        frame.TransferId = Str(frame.Body, "id");
                    }
                    else frame.TransferId = Str(message, "i");
                    return ValidTransferId(frame.TransferId) ? frame : null;
                }
                if (frame.Type != "text" || (Number(message, "z") != 0 && Number(message, "z") != 1) || Str(message, "d") == null) return null;
                byte[] raw = Convert.FromBase64String(Str(message, "d"));
                if (Number(message, "z") == 1)
                {
                    using (var input = new StrictInputStream(raw))
                    using (var inflate = new DeflateStream(input, CompressionMode.Decompress))
                    using (var output = new MemoryStream())
                    {
                        var buffer = new byte[16384]; int read;
                        while ((read = inflate.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            output.Write(buffer, 0, read);
                            if (output.Length > MaxChars * 4) return null;
                        }
                        if (input.Position != input.Length) return null;
                        raw = output.ToArray();
                    }
                }
                frame.Text = new UTF8Encoding(false, true).GetString(raw);
                return frame.Text.Length > 0 && frame.Text.Length <= MaxChars ? frame : null;
            }
            catch (Exception) { return null; }
        }

        // Prevent inflater read-ahead from consuming trailing data and make a missing
        // final deflate block fail instead of looking like a normal stream EOF.
        sealed class StrictInputStream : MemoryStream
        {
            internal StrictInputStream(byte[] bytes) : base(bytes, false) { }
            public override int Read(byte[] buffer, int offset, int count)
            {
                if (count > 0 && Position == Length) throw new InvalidDataException("Incomplete deflate stream");
                return base.Read(buffer, offset, Math.Min(count, 1));
            }
        }

        static bool Origin(string value) { return value == "host" || value == "guest"; }
        internal static bool ValidTransferId(string value)
        { return value != null && value.Length == 32 && value.All(c => (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')); }
        static int Number(Dictionary<string, object> message, string key)
        {
            object value;
            return message.TryGetValue(key, out value) && value is int ? (int)value : -1;
        }

        static string Str(Dictionary<string, object> message, string key)
        {
            object value;
            return message.TryGetValue(key, out value) ? value as string : null;
        }

        internal sealed class SyncState
        {
            readonly string nonce, origin;
            readonly object gate = new object();
            long clock, sequence;
            string currentOrigin = "", current, pending;
            FileCopy currentFiles, pendingFiles;
            bool observed;
            string textError;
            internal string TextError { get { lock (gate) return textError; } }
            uint observedSequence;
            internal SyncState(string nonce, string origin) { this.nonce = nonce; this.origin = origin; }
            internal string Outgoing { get { lock (gate) return pending; } }
            internal FileCopy OutgoingFiles { get { lock (gate) return pendingFiles; } }
            internal long Sequence { get { lock (gate) return sequence; } }
            internal uint NativeSequence { get { lock (gate) return observedSequence; } }
            internal bool NeedsCapture(uint nativeSequence) { lock (gate) return !observed || nativeSequence != observedSequence; }
            internal void RememberNative(uint nativeSequence) { lock (gate) { observedSequence = nativeSequence; observed = true; } }
            internal void Capture(uint nativeSequence, string text)
            {
                lock (gate)
                {
                    if (!NeedsCapture(nativeSequence)) return;
                    Local(text); RememberNative(nativeSequence);
                }
            }
            internal void CaptureFiles(uint nativeSequence, string[] paths)
            {
                lock (gate)
                {
                    if (!NeedsCapture(nativeSequence)) return;
                    LocalFiles(paths); RememberNative(nativeSequence);
                }
            }
            internal int Compare(Frame frame)
            {
                lock (gate) return frame.Sequence != sequence ? frame.Sequence.CompareTo(sequence) : String.CompareOrdinal(frame.Origin, currentOrigin);
            }
            internal void Observe(Frame frame) { lock (gate) clock = Math.Max(clock, frame.Sequence); }
            internal bool RecoverOwnReplay(Frame frame)
            {
                lock (gate)
                {
                    if (frame.Origin != origin || frame.Sequence <= sequence ||
                        (frame.Type != "text" && frame.Type != "skip" && frame.Type != "files")) return false;
                    // A restarted helper must keep its new local snapshot instead of applying its own older copy.
                    if (frame.Sequence == clock && clock < MaxSequence && currentOrigin == origin)
                    {
                        string error = textError;
                        if (pendingFiles != null) LocalFiles(pendingFiles.Paths);
                        else if (pending != null)
                        {
                            Frame captured = DecodeFrame(pending, nonce);
                            if (captured != null) Local(captured.Type == "text" ? captured.Text : null);
                        }
                        textError = error;
                    }
                    return true;
                }
            }
            internal void Local(string text)
            {
                lock (gate)
                {
                    textError = text != null && text.Length > MaxChars ? "CLIPBOARD_TEXT_TOO_LARGE" : null;
                    pendingFiles = currentFiles = null;
                    if (clock == MaxSequence) { pending = current = null; return; }
                    sequence = ++clock; currentOrigin = origin;
                    pending = current = Encode(nonce, text, sequence, origin) ?? Control(nonce, "skip", origin, sequence);
                }
            }
            internal void LocalFiles(string[] paths)
            {
                lock (gate)
                {
                    textError = null;
                    pending = current = null;
                    if (clock == MaxSequence) { pendingFiles = currentFiles = null; return; }
                    sequence = ++clock; currentOrigin = origin;
                    currentFiles = pendingFiles = new FileCopy { Paths = (string[])paths.Clone(),
                        Id = Guid.NewGuid().ToString("N"), Sequence = sequence, Origin = origin };
                }
            }
            internal void Resend()
            {
                lock (gate)
                {
                    pending = current; pendingFiles = currentFiles;
                    if (pendingFiles != null) pendingFiles.Stored = false;
                }
            }
            internal bool IsPending(FileCopy copy) { lock (gate) return Object.ReferenceEquals(copy, pendingFiles); }
            internal void CancelPending(FileCopy copy) { lock (gate) if (Object.ReferenceEquals(copy, pendingFiles)) pendingFiles = null; }
            internal void MarkStored(Frame frame)
            {
                lock (gate)
                    if (pendingFiles != null && frame.Sequence == sequence && frame.Origin == currentOrigin && frame.TransferId == pendingFiles.Id)
                        pendingFiles.Stored = true;
            }
            internal void Acknowledge(Frame frame)
            {
                lock (gate)
                    if (frame.Ok && frame.Sequence == sequence && frame.Origin == currentOrigin &&
                        (pendingFiles == null || frame.TransferId == pendingFiles.Id)) { pending = null; pendingFiles = null; }
            }
            internal void Commit(Frame frame)
            {
                lock (gate)
                {
                    if (Compare(frame) <= 0) return;
                    if (frame.Type != "skip") textError = null;
                    sequence = frame.Sequence; currentOrigin = frame.Origin;
                    currentFiles = frame.Paths == null ? null : new FileCopy { Paths = (string[])frame.Paths.Clone(),
                        Id = frame.TransferId, Sequence = sequence, Origin = currentOrigin };
                    current = frame.Paths == null ? Encode(nonce, frame.Text, sequence, currentOrigin) : null;
                    pending = null; pendingFiles = null;
                }
            }
            internal bool Apply(Frame frame, Func<bool> write)
            {
                Observe(frame);
                if (Compare(frame) <= 0) return true;
                if (!write()) return false;
                Commit(frame); return true;
            }
        }

        internal sealed class ClipboardCopy
        {
            internal readonly uint Format;
            internal readonly IntPtr Handle;
            internal ClipboardCopy(uint format, IntPtr handle) { Format = format; Handle = handle; }
        }

        internal static bool PublishCopies(List<ClipboardCopy> prepared, Func<ClipboardCopy, bool> publish)
        {
            while (prepared.Count != 0)
            {
                if (!publish(prepared[0])) return false;
                prepared.RemoveAt(0); // Windows owns only successfully published handles.
            }
            return true;
        }

        internal static class NativeBackup
        {
            const long MaxBytes = 64 * 1024 * 1024;
            [DllImport("user32.dll", SetLastError = true)] static extern uint EnumClipboardFormats(uint format);
            [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
            [DllImport("kernel32.dll")] static extern void SetLastError(uint error);
            [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr memory);
            [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr memory);
            [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr memory);
            [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr memory);
            [DllImport("ole32.dll")] static extern IntPtr OleDuplicateData(IntPtr source, ushort format, uint flags);
            [DllImport("gdi32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CopyEnhMetaFile(IntPtr source, string name);
            [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
            [DllImport("gdi32.dll")] static extern bool DeleteMetaFile(IntPtr value);
            [DllImport("gdi32.dll")] static extern bool DeleteEnhMetaFile(IntPtr value);
            [DllImport("gdi32.dll", CharSet = CharSet.Unicode)] static extern int GetObject(IntPtr value, int size, out BitmapInfo info);
            [DllImport("gdi32.dll")] static extern uint GetPaletteEntries(IntPtr value, uint start, uint count, IntPtr entries);
            [DllImport("gdi32.dll")] static extern uint GetMetaFileBitsEx(IntPtr value, uint count, IntPtr bits);
            [DllImport("gdi32.dll")] static extern uint GetEnhMetaFileBits(IntPtr value, uint count, IntPtr bits);
            [StructLayout(LayoutKind.Sequential)]
            struct BitmapInfo { internal int Type, Width, Height, Stride; internal ushort Planes, Bits; internal IntPtr Pixels; }
            [StructLayout(LayoutKind.Sequential)]
            struct MetafilePicture { internal int Mode, X, Y; internal IntPtr Metafile; }
            static uint BaseFormat(uint format)
            { return format == 0x82 ? 2U : format == 0x83 ? 3U : format == 0x8E ? 14U : format; }
            static IntPtr Metafile(IntPtr memory)
            {
                if (GlobalSize(memory).ToUInt64() < (ulong)Marshal.SizeOf(typeof(MetafilePicture))) throw new ExternalException("Invalid clipboard metafile");
                IntPtr pointer = GlobalLock(memory);
                if (pointer == IntPtr.Zero) throw new ExternalException("Clipboard metafile is unavailable");
                try { return ((MetafilePicture)Marshal.PtrToStructure(pointer, typeof(MetafilePicture))).Metafile; }
                finally { GlobalUnlock(memory); }
            }
            internal static void Free(ClipboardCopy copy)
            {
                uint format = BaseFormat(copy.Format);
                if (format == 2 || format == 9) DeleteObject(copy.Handle);
                else if (format == 14) DeleteEnhMetaFile(copy.Handle);
                else
                {
                    if (format == 3)
                        try { DeleteMetaFile(Metafile(copy.Handle)); } catch (ExternalException) { }
                    GlobalFree(copy.Handle);
                }
            }
            internal static List<ClipboardCopy> Snapshot()
            {
                var copies = new List<ClipboardCopy>(); var seen = new HashSet<uint>(); long total = 0; uint format = 0;
                try
                {
                    while (true)
                    {
                        SetLastError(0); format = EnumClipboardFormats(format);
                        if (format == 0)
                        {
                            if (Marshal.GetLastWin32Error() != 0) throw new ExternalException("Clipboard enumeration failed");
                            return copies;
                        }
                        if (!seen.Add(format) || seen.Count > 256 || format > UInt16.MaxValue || format == 0x80 || (format >= 0x200 && format < 0x400))
                            throw new ExternalException("Clipboard format cannot be preserved safely");
                        IntPtr source = GetClipboardData(format); uint kind = BaseFormat(format); long bytes;
                        if (source == IntPtr.Zero) throw new ExternalException("Clipboard data could not be materialized");
                        if (kind == 2)
                        {
                            BitmapInfo bitmap;
                            if (GetObject(source, Marshal.SizeOf(typeof(BitmapInfo)), out bitmap) == 0) throw new ExternalException("Clipboard bitmap is unavailable");
                            bytes = checked(Math.Abs((long)bitmap.Height) * bitmap.Stride * bitmap.Planes);
                        }
                        else if (kind == 9) bytes = (long)GetPaletteEntries(source, 0, 0, IntPtr.Zero) * 4;
                        else if (kind == 14) bytes = GetEnhMetaFileBits(source, 0, IntPtr.Zero);
                        else if (kind == 3) bytes = GetMetaFileBitsEx(Metafile(source), 0, IntPtr.Zero);
                        else
                        {
                            ulong size = GlobalSize(source).ToUInt64();
                            if (size > MaxBytes) throw new ExternalException("Clipboard backup exceeds its limit");
                            bytes = (long)size;
                        }
                        total = checked(total + bytes);
                        if (bytes <= 0 || total > MaxBytes) throw new ExternalException("Clipboard backup exceeds its limit");
                        IntPtr clone = kind == 14 ? CopyEnhMetaFile(source, null) : OleDuplicateData(source, (ushort)kind, 2);
                        if (clone == IntPtr.Zero) throw new ExternalException("Clipboard data could not be preserved");
                        copies.Add(new ClipboardCopy(format, clone));
                    }
                }
                catch { foreach (var copy in copies) Free(copy); throw; }
            }
        }

        // Retry runs under OpenClipboard; an unresolved backup must outlive transport shutdown.
        internal sealed class NativeRecovery : IDisposable
        {
            readonly SyncState state;
            readonly Func<uint> sequence;
            readonly Func<ClipboardCopy, bool> publish;
            readonly Action<ClipboardCopy> free;
            readonly Func<bool> reset, changedOwner;
            List<ClipboardCopy> copies = new List<ClipboardCopy>();
            uint ownSequence;
            bool recovering, resetPending;
            internal int Count { get { return copies.Count + (recovering && resetPending ? 1 : 0); } }
            internal NativeRecovery(SyncState state, Func<uint> sequence, Func<ClipboardCopy, bool> publish, Action<ClipboardCopy> free,
                Func<bool> reset = null, Func<bool> changedOwner = null)
            { this.state = state; this.sequence = sequence; this.publish = publish; this.free = free; this.reset = reset; this.changedOwner = changedOwner; }
            internal bool Preserve(Func<List<ClipboardCopy>> snapshot)
            {
                if (Count != 0) return false;
                try { copies = snapshot(); recovering = false; resetPending = reset != null; return true; }
                catch (ExternalException) { return false; }
                catch (OverflowException) { return false; }
            }
            internal void Restore()
            {
                recovering = true;
                try
                {
                    if (resetPending) { if (!reset()) return; resetPending = false; }
                    for (int i = 0; i < copies.Count;)
                        if (publish(copies[i])) copies.RemoveAt(i); else i++;
                }
                finally { ownSequence = sequence(); state.RememberNative(ownSequence); }
            }
            internal bool Retry()
            {
                if (Count == 0) return true;
                // Close can synthesize formats; an unchanged owner cannot prove a newer user copy.
                if (sequence() != ownSequence) { if (changedOwner == null || changedOwner()) FreeCopies(); }
                else Restore();
                return Count == 0;
            }
            internal bool CanClose(bool writing, Func<bool> restorePending)
            { return !writing && restorePending() && Count == 0; }
            internal void ReleaseSaved() { if (!recovering) Dispose(); }
            void FreeCopies() { foreach (var copy in copies) free(copy); copies.Clear(); recovering = resetPending = false; }
            public void Dispose() { if (!recovering) FreeCopies(); }
        }

        internal static bool ReplaceClipboard(SyncState state, uint expected, Func<uint> sequence, Func<bool> empty, Func<bool> publish,
            Func<bool> preserve = null, Action restore = null, Action release = null, Func<bool> current = null)
        {
            if (sequence() != expected || current != null && !current()) return false;
            bool saved = false, emptied = false;
            try
            {
                if (preserve != null)
                {
                    try { if (!preserve()) return false; saved = true; }
                    finally { state.RememberNative(sequence()); } // Materializing a delayed format under OpenClipboard is not a new user copy.
                }
                if (sequence() != expected || current != null && !current()) return false;
                if (!empty()) return false;
                emptied = true;
                bool published = false;
                try { published = publish(); return published; }
                finally { if (!published && restore != null) restore(); }
            }
            finally
            {
                if (emptied) state.RememberNative(sequence());
                if (saved && release != null) release();
            }
        }

        // Runs on its own thread for the whole session; the helper process stays alive while it does.
        public static Thread StartBackground(string nonce, int port, Func<bool> networkReady)
        {
            if (port != 27844 || networkReady == null) throw new ArgumentException("Invalid clipboard endpoint");
            var state = new SyncState(nonce, "guest");
            var thread = new Thread(() => { while (true) { try { Run(nonce, state, port, networkReady); } catch (Exception) { Status("START_FAILED"); } Thread.Sleep(1000); } }) { IsBackground = true, Name = "clipboard" };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            return thread;
        }

        static void Run(string nonce, SyncState state, int port, Func<bool> networkReady)
        {
            string parent = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"WindowsClaudePortable\clipboard-sessions");
            PrivateDirectory(parent);
            var cache = new ClipboardStage.Cache(parent, ClipboardStage.DetectBootId());
            using (var window = new Listener(nonce, state, port, networkReady, cache)) Application.Run();
        }

        internal static void PrivateDirectory(string path)
        {
            var user = WindowsIdentity.GetCurrent().User;
            var system = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var security = new DirectorySecurity(); security.SetOwner(user); security.SetAccessRuleProtection(true, false);
            foreach (var sid in new[] { user, system }) security.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            for (string part = Path.GetFullPath(path); part != null; part = Path.GetDirectoryName(part))
                if (Directory.Exists(part) && (File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0) throw new IOException("Reparse cache refused");
            if (!Directory.Exists(path)) Directory.CreateDirectory(path, security);
            for (string part = Path.GetFullPath(path); part != null; part = Path.GetDirectoryName(part))
                if ((File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0) throw new IOException("Reparse cache refused");
            var actual = Directory.GetAccessControl(path);
            if (!actual.AreAccessRulesProtected || !user.Equals(actual.GetOwner(typeof(SecurityIdentifier)))) throw new IOException("Untrusted clipboard cache");
            foreach (FileSystemAccessRule rule in actual.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                if (rule.AccessControlType == AccessControlType.Allow && !user.Equals(rule.IdentityReference) && !system.Equals(rule.IdentityReference))
                    throw new IOException("Clipboard cache is not private");
        }

        [DllImport("kernel32.dll")] static extern uint GetThreadLocale();
        [DllImport("kernel32.dll", EntryPoint = "GetLocaleInfoW", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int LocaleNumber(uint locale, uint kind, out uint value, int characters);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int WideCharToMultiByte(uint page, uint flags, string text, int characters, [Out] byte[] bytes, int size, IntPtr fallback, IntPtr usedFallback);
        static byte[] LegacyText(string text, uint locale, uint kind)
        {
            uint page;
            if (LocaleNumber(locale, kind | 0x20000000, out page, 2) != 2) throw new ExternalException("Clipboard locale unavailable");
            int size = WideCharToMultiByte(page, 0, text, text.Length + 1, null, 0, IntPtr.Zero, IntPtr.Zero);
            if (size <= 0 || size > (MaxChars + 1L) * 4) throw new ExternalException("Clipboard encoding unavailable");
            var bytes = new byte[size];
            if (WideCharToMultiByte(page, 0, text, text.Length + 1, bytes, size, IntPtr.Zero, IntPtr.Zero) != size) throw new ExternalException("Clipboard encoding unavailable");
            return bytes;
        }
        internal static Dictionary<uint, byte[]> TextFormats(string text)
        {
            if (text == null || text.Length > MaxChars) throw new InvalidDataException("Clipboard text limit");
            uint locale = GetThreadLocale();
            // Publish the complete text set while locked: CloseClipboard otherwise changes its sequence.
            return new Dictionary<uint, byte[]> { { 13, Encoding.Unicode.GetBytes(text + "\0") }, { 16, BitConverter.GetBytes(locale) },
                { 1, LegacyText(text, locale, 0x1004) }, { 7, LegacyText(text, locale, 0xB) } };
        }

        internal static byte[] DropBytes(string[] paths)
        {
            if (paths == null || paths.Length == 0 || paths.Length > ClipboardFiles.MaxEntries || paths.Any(p =>
                String.IsNullOrEmpty(p) || p.Length > 32767 || p.IndexOf('\0') >= 0 || !Path.IsPathRooted(p) || Path.GetPathRoot(p).Length < 3))
                throw new InvalidDataException("Invalid file clipboard paths");
            byte[] names = Encoding.Unicode.GetBytes(String.Join("\0", paths) + "\0\0");
            if (names.Length > 64 * 1024 * 1024 - 20) throw new InvalidDataException("Clipboard path limit");
            byte[] bytes = new byte[names.Length + 20]; bytes[0] = 20; bytes[16] = 1;
            Buffer.BlockCopy(names, 0, bytes, 20, names.Length); return bytes;
        }

        internal static string[] DropPaths(byte[] bytes)
        {
            if (bytes == null || bytes.Length < 20 || bytes.Length > 64 * 1024 * 1024) throw new InvalidDataException("Invalid file clipboard size");
            uint offset = BitConverter.ToUInt32(bytes, 0), wide = BitConverter.ToUInt32(bytes, 16);
            bool unicode = wide != 0;
            if (offset < 20 || offset >= bytes.Length || unicode && offset % 2 != 0) throw new InvalidDataException("Invalid file clipboard header");
            int unit = unicode ? 2 : 1, end = (int)offset;
            for (; end + 2 * unit <= bytes.Length; end += unit)
                if (bytes[end] == 0 && (unit == 1 || bytes[end + 1] == 0) && bytes[end + unit] == 0 && (unit == 1 || bytes[end + unit + 1] == 0)) break;
            if (end + 2 * unit > bytes.Length) throw new InvalidDataException("Unterminated file clipboard");
            string[] paths = (unicode ? new UnicodeEncoding(false, false, true) : Encoding.Default).GetString(bytes, (int)offset, end - (int)offset).Split('\0');
            DropBytes(paths); return paths;
        }

        sealed class TextTooLargeException : Exception { }

        sealed class Listener : Form
        {
            readonly SyncState state;
            readonly NativeRecovery recovery;
            readonly ClipboardProtocol protocol;
            readonly ClipboardTransport transport;
            readonly Thread network;
            readonly uint exclude;
            volatile bool stopping;
            volatile string transportStatus = "WAITING", protocolError, nativeError;
            bool writing;
            string[] currentPaths;
            readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer { Interval = 100 };

            [DllImport("user32.dll", SetLastError = true)] static extern bool AddClipboardFormatListener(IntPtr hwnd);
            [DllImport("user32.dll")] static extern bool RemoveClipboardFormatListener(IntPtr hwnd);
            [DllImport("user32.dll")] static extern uint GetClipboardSequenceNumber();
            [DllImport("user32.dll")] static extern IntPtr GetClipboardOwner();
            [DllImport("user32.dll")] static extern bool OpenClipboard(IntPtr hwnd);
            [DllImport("user32.dll")] static extern bool CloseClipboard();
            [DllImport("user32.dll")] static extern bool EmptyClipboard();
            [DllImport("user32.dll")] static extern IntPtr SetClipboardData(uint format, IntPtr memory);
            [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
            [DllImport("user32.dll")] static extern bool IsClipboardFormatAvailable(uint format);
            [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern uint RegisterClipboardFormat(string format);
            [DllImport("kernel32.dll")] static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
            [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr memory);
            [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr memory);
            [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr memory);
            [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr memory);

            internal Listener(string nonce, SyncState state, int port, Func<bool> networkReady, ClipboardStage.Cache cache)
            {
                this.state = state;
                exclude = RegisterClipboardFormat(Exclude);
                if (exclude == 0) throw new ExternalException("CLIPBOARD_LISTENER_FAILED");
                recovery = new NativeRecovery(state, GetClipboardSequenceNumber,
                    copy => SetClipboardData(copy.Format, copy.Handle) != IntPtr.Zero, NativeBackup.Free,
                    EmptyClipboard, () => { IntPtr owner = GetClipboardOwner(); return owner != IntPtr.Zero && owner != Handle; });
                ShowInTaskbar = false; FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
                Location = new Point(-32000, -32000); Size = new Size(1, 1); Opacity = 0;
                CreateHandle(); CaptureClipboard();
                var cleanup = cache.Cleanup(currentPaths);
                if (cleanup.Failed != 0) Status("CLEANUP_FAILED");
                protocol = new ClipboardProtocol(nonce, state, cache, ProtocolStatus);
                transport = new ClipboardTransport(nonce, port, protocol.NextOutgoing, protocol.Receive, protocol.Reconnected, networkReady,
                    value => { transportStatus = value; PublishStatus(); });
                network = new Thread(transport.Run) { IsBackground = true, Name = "clipboard transport" }; network.Start();
                timer.Tick += delegate { if (stopping) Close(); else { CaptureClipboard(); ApplyIncoming(); } }; timer.Start();
            }

            protected override void SetVisibleCore(bool value) { base.SetVisibleCore(false); }

            protected override void OnHandleCreated(EventArgs e)
            {
                base.OnHandleCreated(e);
                if (!AddClipboardFormatListener(Handle)) throw new ExternalException("CLIPBOARD_LISTENER_FAILED");
            }

            protected override void OnFormClosing(FormClosingEventArgs e)
            {
                stopping = true;
                base.OnFormClosing(e);
                if (!recovery.CanClose(writing, RestorePending)) e.Cancel = true;
            }

            protected override void OnFormClosed(FormClosedEventArgs e)
            { base.OnFormClosed(e); Application.ExitThread(); }

            protected override void WndProc(ref Message m)
            {
                if (m.Msg == 0x031D) CaptureClipboard();
                base.WndProc(ref m);
            }

            protected override void Dispose(bool disposing)
            {
                if (disposing)
                {
                    stopping = true;
                    if (recovery != null && !recovery.CanClose(writing, RestorePending)) return;
                    timer.Dispose();
                    if (transport != null) transport.Dispose();
                    if (network != null) network.Join();
                    if (protocol != null) protocol.Dispose();
                    if (recovery != null) recovery.Dispose();
                    if (IsHandleCreated) RemoveClipboardFormatListener(Handle);
                }
                base.Dispose(disposing);
            }

            bool RestorePending()
            {
                if (writing) return false;
                if (recovery.Count == 0) return true;
                writing = true;
                try
                {
                    if (!OpenClipboard(Handle)) return false;
                    try { return recovery.Retry(); }
                    finally { CloseClipboard(); }
                }
                finally { writing = false; }
            }

            bool CaptureClipboard()
            {
                if (writing) return false;
                if (!RestorePending()) { nativeError = "CLIPBOARD_RESTORING"; PublishStatus(); return false; }
                if (nativeError != "CLIPBOARD_UNSUPPORTED" && nativeError != "CLIPBOARD_TEXT_TOO_LARGE") nativeError = null;
                if (protocolError == "CLIPBOARD_BUSY" && protocol != null && protocol.QueuedCandidates == 0) protocolError = null;
                PublishStatus();
                uint sequence = GetClipboardSequenceNumber();
                if (!state.NeedsCapture(sequence)) return true;
                if (!OpenClipboard(Handle)) { nativeError = "CLIPBOARD_BUSY"; PublishStatus(); return false; }
                try
                {
                    string text = null; string[] paths = IsClipboardFormatAvailable(exclude) ? null : new string[0];
                    if (!IsClipboardFormatAvailable(exclude))
                    {
                        if (IsClipboardFormatAvailable(15)) paths = DropPaths(ReadNative(15, 64 * 1024 * 1024));
                        else if (IsClipboardFormatAvailable(13))
                        {
                            byte[] raw = ReadNative(13, (MaxChars + 1) * 2);
                            if (raw.Length % 2 != 0) throw new InvalidDataException("Invalid text clipboard");
                            string value = new UnicodeEncoding(false, false, true).GetString(raw);
                            int zero = value.IndexOf('\0');
                            if (zero < 0) throw new InvalidDataException("Unterminated text clipboard");
                            text = value.Substring(0, zero);
                        }
                    }
                    if (GetClipboardSequenceNumber() != sequence) return false;
                    currentPaths = paths;
                    if (paths != null && paths.Length != 0) state.CaptureFiles(sequence, paths); else state.Capture(sequence, text);
                    nativeError = protocolError = null; PublishStatus();
                    return true;
                }
                catch (TextTooLargeException) { state.Capture(sequence, null); currentPaths = null; nativeError = "CLIPBOARD_TEXT_TOO_LARGE"; PublishStatus(); return true; }
                catch (InvalidDataException) { state.Capture(sequence, null); currentPaths = null; nativeError = "CLIPBOARD_UNSUPPORTED"; PublishStatus(); return true; }
                catch (DecoderFallbackException) { state.Capture(sequence, null); currentPaths = null; nativeError = "CLIPBOARD_UNSUPPORTED"; PublishStatus(); return true; }
                catch (ExternalException) { nativeError = "CLIPBOARD_READ_FAILED"; PublishStatus(); return false; }
                finally { CloseClipboard(); }
            }

            byte[] ReadNative(uint format, int maximum)
            {
                IntPtr memory = GetClipboardData(format);
                if (memory == IntPtr.Zero) throw new ExternalException("Clipboard data unavailable");
                ulong size = GlobalSize(memory).ToUInt64();
                if (format == 13 && size > (ulong)maximum) throw new TextTooLargeException();
                if (size == 0 || size > (ulong)maximum) throw new InvalidDataException("Clipboard data limit");
                IntPtr pointer = GlobalLock(memory);
                if (pointer == IntPtr.Zero) throw new ExternalException("Clipboard data unavailable");
                try { var raw = new byte[(int)size]; Marshal.Copy(pointer, raw, 0, raw.Length); return raw; }
                finally { GlobalUnlock(memory); }
            }

            bool Write(Frame frame)
            {
                if (writing || !RestorePending()) return false;
                writing = true;
                try
                {
                    var formats = frame.Paths == null ? TextFormats(frame.Text) : new Dictionary<uint, byte[]> { { 15, DropBytes(frame.Paths) } };
                    var prepared = new List<ClipboardCopy>();
                    try
                    {
                        foreach (var item in formats)
                        {
                            IntPtr memory = GlobalAlloc(2, new UIntPtr((uint)item.Value.Length));
                            if (memory == IntPtr.Zero) return false;
                            prepared.Add(new ClipboardCopy(item.Key, memory));
                            IntPtr pointer = GlobalLock(memory); if (pointer == IntPtr.Zero) return false;
                            try { Marshal.Copy(item.Value, 0, pointer, item.Value.Length); } finally { GlobalUnlock(memory); }
                        }
                        if (!OpenClipboard(Handle)) return false;
                        try
                        {
                            return ReplaceClipboard(state, state.NativeSequence, GetClipboardSequenceNumber, EmptyClipboard, () =>
                            {
                                return PublishCopies(prepared, copy => SetClipboardData(copy.Format, copy.Handle) != IntPtr.Zero);
                            }, () => recovery.Preserve(NativeBackup.Snapshot), recovery.Restore, recovery.ReleaseSaved,
                                () => !stopping && protocol.IsCurrent(frame));
                        }
                        finally { CloseClipboard(); }
                    }
                    finally { foreach (var copy in prepared) NativeBackup.Free(copy); }
                }
                finally { writing = false; }
            }

            void ApplyIncoming()
            {
                if (CaptureClipboard() && protocol.ApplyLatest(Write)) { nativeError = protocolError = null; PublishStatus(); }
            }

            void PublishStatus() { Status(CombinedStatus(nativeError ?? state.TextError, transportStatus, protocolError)); }
            void ProtocolStatus(string value)
            { protocolError = value == "FILES_READY" ? null : value; PublishStatus(); }
        }
    }
}
