using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using WindowsClaude;
using Microsoft.Win32.SafeHandles;

internal static class ClipboardFilesRegression
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    static void Assert(bool ok, string reason) { if (!ok) throw new IOException(reason); }
    static bool Denied(Action action)
    { try { action(); return false; } catch (IOException) { return true; } }
    static Dictionary<string, object> Frame(string id, string ev, params object[] fields)
    {
        var result = new Dictionary<string, object> { { "id", id }, { "event", ev } };
        for (int i = 0; i < fields.Length; i += 2) result.Add((string)fields[i], fields[i + 1]);
        return result;
    }
    static void ResourceCases(string id, string parent)
    {
        string foreign = Path.Combine(parent, "resource-foreign"); File.WriteAllText(foreign, "keep");
        string relative = "emoji-\U0001F600", absolute = Path.Combine(parent, "data-\U0001F431");
        long charge = 19 + 2L * (relative.Length * 2L) + 2L * (absolute.Length * 2L) + 512;
        Assert("\U0001F600".Length * 2 == 4 && ClipboardFiles.Charge(0, relative, absolute, 19) == charge, "UTF16 metadata formula includes surrogate pairs");
        Assert(ClipboardFiles.Charge(ClipboardFiles.MaxMetadata - charge, relative, absolute, 19) == ClipboardFiles.MaxMetadata, "Exact metadata boundary accepted");
        Assert(Denied(() => ClipboardFiles.Charge(ClipboardFiles.MaxMetadata - charge + 1, relative, absolute, 19)), "Metadata boundary plus one rejected");
        Assert(Denied(() => ClipboardFiles.Charge(Int64.MaxValue, relative, absolute, 19)), "Invalid or overflowing counter rejected");

        var components = new string[ClipboardFiles.MaxDepth];
        for (int index = 0; index < components.Length; index++) components[index] = "a";
        string validDepth = String.Join("/", components), invalidDepth = validDepth + "/a";
        Assert(ClipboardFiles.Relative(validDepth) == validDepth, "Relative depth 256 accepted");
        Assert(Denied(() => ClipboardFiles.Relative(invalidDepth)), "Relative depth 257 rejected before recursion");
        string deepSelection = Path.GetPathRoot(parent) + invalidDepth.Replace('/', '\\');
        Assert(Denied(() => ClipboardFiles.Selection(deepSelection)), "Absolute selection depth rejected before pinning");
        Assert(Denied(() => new ClipboardFiles.Sender(new[] { deepSelection }, id)), "Sender validates absolute selection depth before native I/O");

        for (int mode = 0; mode < 3; mode++)
        {
            var receiver = new ClipboardFiles.Receiver(parent, id); string stage = receiver.Stage;
            receiver.Accept(Frame(id, "begin", "count", 1, "bytes", mode == 2 ? ClipboardFiles.Chunk : 0));
            var removed = new List<string>(); receiver.BeforeCleanupDelete = removed.Add;
            if (mode == 1) receiver.PinCount = ClipboardFiles.MaxPins;
            else receiver.MetadataBytes = ClipboardFiles.MaxMetadata - 1;
            var entry = Frame(id, "entry", "path", mode == 2 ? "zone-\U0001F600" : "refused", "kind", mode == 2 ? "file" : "dir", "size", 0,
                "zone", mode == 2 ? Convert.ToBase64String(new byte[ClipboardFiles.Chunk]) : null);
            Assert(Denied(() => receiver.Accept(entry)), "Resource rejection occurs before creating directory/file or retaining zone metadata");
            Assert(removed.Count == 1 && removed[0] == stage && !Directory.Exists(stage), "Budget failure creates no payload entry and cleans only its own stage");
            Assert(File.ReadAllText(foreign) == "keep", "Foreign data survives resource-limit cancellation");
        }
        {
            var receiver = new ClipboardFiles.Receiver(parent, id); string stage = receiver.Stage;
            receiver.Accept(Frame(id, "begin", "count", 1, "bytes", 0));
            Assert(Denied(() => receiver.Accept(Frame(id, "entry", "path", invalidDepth, "kind", "dir", "size", 0, "zone", null))), "Receiver rejects depth before directory creation");
            Assert(!Directory.Exists(stage) && File.ReadAllText(foreign) == "keep", "Depth rejection preserves foreign fixture");
        }
        string source = Path.Combine(parent, "resource-zone-\U0001F600"); File.WriteAllText(source, "");
        using (var handle = CreateFile(source + ":Zone.Identifier", 0x40000000, 1, IntPtr.Zero, 1, 0x00200000, IntPtr.Zero))
        {
            Assert(!handle.IsInvalid, "Create fixture ADS with the native API");
            using (var output = new FileStream(handle, FileAccess.Write)) output.Write(new byte[ClipboardFiles.Chunk], 0, ClipboardFiles.Chunk);
        }
        long ancestorCharge = 0;
        for (string p = Path.GetDirectoryName(source); p != null; p = Path.GetDirectoryName(p)) ancestorCharge += ClipboardFiles.Charge(0, "", p);
        long entryCharge = ClipboardFiles.Charge(0, Path.GetFileName(source), source, ClipboardFiles.Chunk);
        var boundedSender = new ClipboardFiles.Sender(new[] { source }, id);
        boundedSender.MetadataBytes = ClipboardFiles.MaxMetadata - ancestorCharge - entryCharge + 1;
        Assert(Denied(() => { using (var iterator = boundedSender.Frames().GetEnumerator()) iterator.MoveNext(); }), "Sender accounts for zone metadata before retaining manifest entry");
        var selected = new string[128];
        for (int index = 0; index < selected.Length; index++)
        { selected[index] = Path.Combine(parent, "resource-file-" + index); File.WriteAllText(selected[index], ""); }
        var sender = new ClipboardFiles.Sender(selected, id);
        using (var iterator = sender.Frames().GetEnumerator())
        {
            Assert(iterator.MoveNext() && Convert.ToInt32(iterator.Current["count"]) == selected.Length, "Many locally selected files remain supported");
            Assert(sender.PeakPinCount < 32 && sender.PeakPinCount < selected.Length, "Shared ancestor pin handles are deduplicated across selected files");
        }
        Console.WriteLine("Resource budgets: OK (UTF16, selection, zone, depth, pins, dedupe, foreign cleanup)");
    }
    static int Main(string[] args)
    {
        Console.SetIn(new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true)));
        Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false, true)) { AutoFlush = true });
        Console.SetError(new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false, true)) { AutoFlush = true });
        var json = new JavaScriptSerializer { MaxJsonLength = 1048576, RecursionLimit = 16 };
        try
        {
            if (args[0] == "native-paths")
            {
                Assert(ClipboardFiles.NativePath(@"C:\folder\file:Zone.Identifier") == @"\\?\C:\folder\file:Zone.Identifier", "Native drive/ADS path");
                Assert(ClipboardFiles.NativePath(@"\\server\share\folder\file") == @"\\?\UNC\server\share\folder\file", "Native UNC path without network I/O");
                foreach (string invalid in new[] { "relative", @"C:relative", @"\root-relative", @"C:\a\..\b", @"\\?\C:\file", @"\\.\pipe\name", @"\\?\GLOBALROOT\Device\name", @"\\server" })
                    Assert(Denied(() => ClipboardFiles.NativePath(invalid)), "Native prefix must not accept an unnormalized/device namespace");
                Console.WriteLine("Native absolute drive/UNC conversion: OK");
            }
            else if (args[0] == "send")
            {
                string[] paths = json.Deserialize<string[]>(Console.ReadLine());
                foreach (var f in new ClipboardFiles.Sender(paths, args[1]).Frames()) Console.WriteLine(json.Serialize(f));
            }
            else if (args[0] == "receive")
            {
                using (var receiver = new ClipboardFiles.Receiver(args[2], args[1]))
                {
                    string line;
                    while ((line = Console.ReadLine()) != null)
                    {
                        if (line.Length >= 1048576) throw new IOException("Oversized line");
                        receiver.Accept(json.DeserializeObject(line) as Dictionary<string, object>);
                    }
                    if (receiver.Roots == null) throw new IOException("Truncated transfer");
                    string[] roots = receiver.Roots;
                    string stage = receiver.Detach();
                    Console.WriteLine(json.Serialize(new Dictionary<string, object> { { "roots", roots }, { "stage", stage } }));
                }
            }
            else if (args[0] == "selfcheck")
            {
                string source = Path.Combine(args[2], "source.bin");
                File.WriteAllBytes(source, new byte[ClipboardFiles.Chunk * 2]);
                using (var iterator = new ClipboardFiles.Sender(new[] { source }, args[1]).Frames().GetEnumerator())
                {
                    Assert(iterator.MoveNext(), "Missing begin");
                    File.WriteAllText(source, "changed after manifest");
                    Assert(Denied(() => { while (iterator.MoveNext()) { } }), "Mutation must reject");
                }
                File.WriteAllBytes(source, new byte[ClipboardFiles.Chunk * 2]);
                using (var iterator = new ClipboardFiles.Sender(new[] { source }, args[1]).Frames().GetEnumerator())
                {
                    while (iterator.MoveNext() && (string)iterator.Current["event"] != "data") { }
                    Assert(Denied(() => File.WriteAllText(source, "tamper")), "Read must deny mutation");
                    Assert(Denied(() => File.Move(source, source + ".moved")), "Read must deny replacement");
                }
                using (var receiver = new ClipboardFiles.Receiver(args[2], args[1]))
                {
                    Assert(Denied(() => Directory.Move(receiver.Stage, receiver.Stage + ".moved")), "Stage must stay pinned");
                    foreach (var f in new ClipboardFiles.Sender(new[] { source }, args[1]).Frames())
                    {
                        if ((string)f["event"] == "end") break;
                        receiver.Accept(f);
                    }
                    Assert(receiver.Roots == null, "No early publication");
                    Assert(Denied(() => File.WriteAllText(Path.Combine(receiver.Stage, "source.bin"), "tamper")), "Staged file must stay pinned");
                }
                foreach (bool detached in new[] { false, true })
                {
                    var receiver = new ClipboardFiles.Receiver(args[2], args[1]);
                    foreach (var f in new ClipboardFiles.Sender(new[] { source }, args[1]).Frames())
                    {
                        if (!detached && (string)f["event"] == "end") break;
                        receiver.Accept(f);
                    }
                    string stage = receiver.Stage;
                    string foreign = stage + ".foreign";
                    Directory.CreateDirectory(foreign);
                    File.WriteAllText(Path.Combine(foreign, "foreign-sentinel"), "keep");
                    if (detached)
                    {
                        receiver.Detach(); receiver.Dispose();
                        Assert(Directory.Exists(stage), "Detached data must survive Dispose");
                        Assert(Denied(() => Directory.Move(stage, stage + ".premature")), "Detached lease must retain parent pins");
                    }
                    bool replaced = false, blocked = false;
                    receiver.BeforeCleanupDelete = path =>
                    {
                        if (path != stage) return;
                        try { Directory.Move(stage, stage + ".moved"); }
                        catch (IOException) { blocked = true; return; }
                        Directory.Move(foreign, stage); replaced = true;
                    };
                    bool refused = Denied(receiver.Release);
                    Assert((blocked && !refused) || (replaced && refused), "Cleanup must block substitution or refuse changed identity");
                    Assert(File.ReadAllText(Path.Combine(replaced ? stage : foreign, "foreign-sentinel")) == "keep", "Foreign sentinel must survive cleanup");
                    Assert(!File.Exists(Path.Combine(stage + ".moved", "source.bin")), "Known owned child must still be removed");
                }
                foreach (string kind in new[] { "dir", "file" })
                {
                    var receiver = new ClipboardFiles.Receiver(args[2], args[1]); string stage = receiver.Stage;
                    receiver.Accept(Frame(args[1], "begin", "count", 1, "bytes", 0));
                    receiver.AvailableBytes = () => (ulong)ClipboardFiles.Margin;
                    Assert(Denied(() => receiver.Accept(Frame(args[1], "entry", "path", "empty", "kind", kind, "size", 0, "zone", null))), "Metadata allocation needs free space");
                    Assert(!Directory.Exists(stage), "Rejected entry must clean its stage");
                }
                {
                    var receiver = new ClipboardFiles.Receiver(args[2], args[1]);
                    foreach (var f in new ClipboardFiles.Sender(new[] { source }, args[1]).Frames()) receiver.Accept(f);
                    string target = Path.Combine(receiver.Stage, "source.bin");
                    receiver.BeforeCleanupDelete = path =>
                    {
                        if (path != target) return;
                        File.Delete(target); File.WriteAllText(target, "foreign replacement");
                    };
                    Assert(Denied(receiver.Release), "File ID mismatch must refuse delete");
                    Assert(File.ReadAllText(target) == "foreign replacement", "Replaced foreign file must survive cleanup");
                }
                {
                    var receiver = new ClipboardFiles.Receiver(args[2], args[1]); string stage = receiver.Stage;
                    receiver.Accept(Frame(args[1], "begin", "count", 1, "bytes", 1));
                    receiver.Accept(Frame(args[1], "entry", "path", "empty", "kind", "file", "size", 0, "zone", "eA=="));
                    receiver.AvailableBytes = () => (ulong)ClipboardFiles.Margin;
                    Assert(Denied(() => receiver.Accept(Frame(args[1], "file_end", "sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"))), "ADS allocation needs free space");
                    Assert(!Directory.Exists(stage), "Rejected ADS must clean its stage");
                }
                {
                    var receiver = new ClipboardFiles.Receiver(args[2], args[1]);
                    receiver.AvailableBytes = () => (ulong)ClipboardFiles.Margin + 100000;
                    Assert(Denied(() => receiver.Accept(Frame(args[1], "begin", "count", 100000, "bytes", 0))), "Empty entry manifest must reserve metadata space");
                }
                Console.WriteLine("C# mutation, replacement, parent pins, cancellation: OK; identity cleanup and allocation space: OK");
            }
            else if (args[0] == "resources") ResourceCases(args[1], args[2]);
            else throw new ArgumentException("Unknown harness command");
            return 0;
        }
        catch (Exception e) { Console.Error.WriteLine(e.GetType().Name + ": " + e.Message); return 2; }
    }
}
