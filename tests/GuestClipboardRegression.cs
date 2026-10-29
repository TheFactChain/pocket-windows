using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using WindowsClaude;

internal static class GuestClipboardRegression
{
    static int checks;
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); checks++; }

    static GuestClipboard.Frame Frame(string nonce, string text, long sequence, string origin = "host")
    { return GuestClipboard.DecodeFrame(GuestClipboard.Encode(nonce, text, sequence, origin), nonce); }

    static string Raw(string nonce, string tail)
    { return "{\"v\":2,\"n\":\"" + nonce + "\",\"s\":1,\"o\":\"host\"," + tail + "}"; }

    static int Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "--decode")
        {
            string value = GuestClipboard.Decode(Console.ReadLine(), args[1]);
            if (value == null) return 2;
            Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(value))); return 0;
        }
        if (args.Length == 4 && args[0] == "--encode")
        {
            string text = Encoding.UTF8.GetString(Convert.FromBase64String(Console.ReadLine()));
            Console.WriteLine(GuestClipboard.Encode(args[1], text, Int64.Parse(args[3]), args[2])); return 0;
        }
        string nonce = new string('a', 32);
        // Text, including Cyrillic and characters outside the BMP, survives the round trip.
        foreach (string text in new[] { "hello", "Привет, мир", "emoji \U0001F600 and tabs\t\r\nlines", new string('x', 50000) })
            Assert(GuestClipboard.Decode(GuestClipboard.Encode(nonce, text), nonce) == text, "round trip of " + text.Length + " chars");
        Assert(GuestClipboard.Encode(nonce, "") == null && GuestClipboard.Encode(nonce, null) == null, "nothing to send for an empty copy");
        Assert(GuestClipboard.Encode(nonce, new string('y', GuestClipboard.MaxChars + 1)) == null, "oversized copies are not sent");
        // Only lines of this session are accepted.
        string line = GuestClipboard.Encode(nonce, "secret");
        Assert(GuestClipboard.Decode(line, new string('b', 32)) == null, "another session's line is dropped");
        Assert(GuestClipboard.Decode("not json", nonce) == null && GuestClipboard.Decode("{}", nonce) == null && GuestClipboard.Decode(null, nonce) == null, "garbage is dropped");
        Assert(GuestClipboard.Decode(line.Replace("\"v\":2", "\"v\":3"), nonce) == null, "unknown version is dropped");
        Assert(GuestClipboard.Decode(Raw(nonce, "\"t\":\"image\",\"z\":0,\"d\":\"aGk=\""), nonce) == null, "other types are dropped");
        Assert(GuestClipboard.Decode(Raw(nonce, "\"t\":\"text\",\"z\":0,\"d\":\"aGk=\""), nonce) == "hi", "uncompressed text is accepted");
        Assert(GuestClipboard.Decode(Raw(nonce, "\"t\":\"text\",\"z\":0,\"d\":\"/w==\""), nonce) == null, "invalid UTF-8 is dropped");
        Assert(GuestClipboard.Decode(Raw(nonce, "\"t\":\"text\",\"z\":1,\"d\":\"!!!\""), nonce) == null, "invalid base64 is dropped");
        // A small line that inflates to far more than the cap is refused.
        string bomb = GuestClipboardBomb(nonce);
        Assert(GuestClipboard.Decode(bomb, nonce) == null, "a decompression bomb is refused");
        CompressedCases(nonce);
        SyncCases(nonce);
        NativeRetryAndReconnectCases(nonce);
        MultiFormatPublicationCases(nonce);
        Assert(GuestClipboard.CombinedStatus(null,"CONNECTED","FILES_FAILED") == "FILES_FAILED", "connected transport does not hide a failed file transfer");
        Assert(GuestClipboard.CombinedStatus(null,"CONNECTED","CLEANUP_FAILED") == "CLEANUP_FAILED", "connected transport does not hide cleanup failure");
        Assert(GuestClipboard.CombinedStatus("CLIPBOARD_BUSY","CONNECTED",null) == "CLIPBOARD_BUSY" && GuestClipboard.CombinedStatus(null,"CONNECTED",null) == "CONNECTED", "successful native retry recovers status without a new transfer");
        var guarded = new GuestClipboard.SyncState(nonce, "guest"); uint nativeSequence = 1; bool erased = false;
        Assert(!GuestClipboard.ReplaceClipboard(guarded, 1, () => nativeSequence, () => { erased = true; return true; }, () => true,
            () => { nativeSequence++; return true; }) && !erased && !guarded.NeedsCapture(nativeSequence), "delayed format materialization defers publication without inventing a new local copy");
        Assert(GuestClipboard.ReplaceClipboard(guarded, guarded.NativeSequence, () => nativeSequence, () => true, () => true,
            () => true), "the retained remote copy applies on the next attempt after delayed rendering");
        string[] paths = { @"C:\Копия\file.txt", @"\\server\share\folder" };
        Assert(String.Join("|", GuestClipboard.DropPaths(GuestClipboard.DropBytes(paths))) == String.Join("|", paths), "native CF_HDROP paths round trip");
        foreach (uint flag in new uint[] { 1, 2, 0x7fffffff, 0x80000000, UInt32.MaxValue })
        {
            byte[] native = GuestClipboard.DropBytes(paths);
            Buffer.BlockCopy(BitConverter.GetBytes(flag), 0, native, 16, 4);
            Assert(String.Join("|", GuestClipboard.DropPaths(native)) == String.Join("|", paths), "every nonzero native BOOL denotes Unicode");
            native[0] = 21;
            bool rejected = false; try { GuestClipboard.DropPaths(native); } catch (InvalidDataException) { rejected = true; }
            Assert(rejected, "noncanonical TRUE retains Unicode alignment check");
        }
        foreach (byte[] bad in new[] { new byte[0], new byte[20], GuestClipboard.DropBytes(paths).Take(20).ToArray() })
        {
            bool rejected = false; try { GuestClipboard.DropPaths(bad); } catch (InvalidDataException) { rejected = true; }
            Assert(rejected, "malformed file clipboard refused");
        }
        Console.WriteLine("PASS guest clipboard: " + checks + " checks");
        return 0;
    }

    static string Compressed(string nonce, byte[] bytes)
    { return Raw(nonce, "\"t\":\"text\",\"z\":1,\"d\":\"" + Convert.ToBase64String(bytes) + "\""); }

    static void CompressedCases(string nonce)
    {
        string text = "complete deflate stream: Привет \U0001F600 " + new string('x', 10000);
        var message = new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<Dictionary<string, object>>(GuestClipboard.Encode(nonce, text));
        byte[] bytes = Convert.FromBase64String((string)message["d"]);
        for (int removed = 1; removed <= 3; removed++)
        {
            var truncated = new byte[bytes.Length - removed]; Array.Copy(bytes, truncated, truncated.Length);
            Assert(GuestClipboard.Decode(Compressed(nonce, truncated), nonce) == null, "compressed input missing its last " + removed + " bytes is rejected");
        }
        foreach (byte[] tail in new[] { new byte[] { 0 }, new byte[] { 255, 1, 2 }, bytes })
        {
            var extended = new byte[bytes.Length + tail.Length]; Array.Copy(bytes, extended, bytes.Length); Array.Copy(tail, 0, extended, bytes.Length, tail.Length);
            Assert(GuestClipboard.Decode(Compressed(nonce, extended), nonce) == null, "trailing bytes or a second deflate stream are rejected");
        }
        Assert(GuestClipboard.Decode(Compressed(nonce, new byte[0]), nonce) == null, "missing compressed input is rejected");
        Assert(GuestClipboard.Decode(Compressed(nonce, new byte[] { 3, 0 }), nonce) == null, "valid compressed empty text is rejected");
        Assert(GuestClipboard.Decode(GuestClipboard.Encode(nonce, new string('z', GuestClipboard.MaxChars)), nonce) == new string('z', GuestClipboard.MaxChars), "maximum sized text survives strict decompression");
        // Python zlib.compress(utf8, level=0/6, wbits=-15): stored and fixed blocks.
        foreach (string fixture in new[] { "ASUA2v9QeXRob24gcmF3IGRlZmxhdGU6INCf0YDQuNCy0LXRgiDwn5iA", "C6gsycjPUyhKLFdISU3LSSxJtVK4MP9iw4UdFzZd2HqxSeHD/BkNAA==" })
        {
            bytes = Convert.FromBase64String(fixture);
            Assert(GuestClipboard.Decode(Compressed(nonce, bytes), nonce) == "Python raw deflate: Привет \U0001F600", "Python raw deflate fixture survives strict .NET decoding");
            for (int removed = 1; removed <= 3; removed++)
            {
                var truncated = new byte[bytes.Length - removed]; Array.Copy(bytes, truncated, truncated.Length);
                Assert(GuestClipboard.Decode(Compressed(nonce, truncated), nonce) == null, "truncated Python fixture is rejected");
            }
            var trailing = new byte[bytes.Length + 1]; Array.Copy(bytes, trailing, bytes.Length);
            Assert(GuestClipboard.Decode(Compressed(nonce, trailing), nonce) == null, "Python fixture with a trailing zero is rejected");
        }
    }

    static string GuestClipboardBomb(string nonce)
    {
        using (var packed = new System.IO.MemoryStream())
        {
            using (var deflate = new System.IO.Compression.DeflateStream(packed, System.IO.Compression.CompressionMode.Compress, true))
            {
                var block = new byte[1 << 20];
                for (int i = 0; i < 8; i++) deflate.Write(block, 0, block.Length);
            }
            return Raw(nonce, "\"t\":\"text\",\"z\":1,\"d\":\"" + Convert.ToBase64String(packed.ToArray()) + "\"");
        }
    }

    static void SyncCases(string nonce)
    {
        var state = new GuestClipboard.SyncState(nonce, "guest");
        var writes = new List<string>();
        var a = Frame(nonce, "A", 10);
        Assert(state.Apply(a, () => { writes.Add(a.Text); return true; }), "first remote A applies");
        state.Local("B");
        var again = Frame(nonce, "A", 12);
        Assert(state.Apply(again, () => { writes.Add(again.Text); return true; }) && writes.Count == 2 && writes[1] == "A", "A/B/A uses versions rather than old text equality");

        state.Local("latest");
        string pending = state.Outgoing;
        try { Action<string> send = value => { throw new IOException("failed write"); }; send(state.Outgoing); } catch (IOException) { }
        Assert(state.Outgoing == pending && GuestClipboard.Decode(state.Outgoing, nonce) == "latest", "failed send retains the pending latest value");
        var outgoing = GuestClipboard.DecodeFrame(pending, nonce);
        state.Acknowledge(new GuestClipboard.Frame { Sequence = outgoing.Sequence - 1, Origin = "guest", Ok = true });
        state.Acknowledge(new GuestClipboard.Frame { Sequence = outgoing.Sequence, Origin = "host", Ok = true });
        state.Acknowledge(new GuestClipboard.Frame { Sequence = outgoing.Sequence, Origin = "guest", Ok = false });
        Assert(state.Outgoing == pending, "only exact successful acknowledgement clears pending");
        state.Acknowledge(new GuestClipboard.Frame { Sequence = outgoing.Sequence, Origin = "guest", Ok = true });
        Assert(state.Outgoing == null, "exact acknowledgement clears pending");
        state.Resend(); Assert(state.Outgoing == pending, "peer reconnect resends the retained state");

        var failed = Frame(nonce, "retry", 100); int attempts = 0;
        long before = state.Sequence;
        Assert(!state.Apply(failed, () => { attempts++; return false; }) && state.Sequence == before, "failed clipboard write is not committed");
        Assert(state.Apply(failed, () => { attempts++; return true; }) && attempts == 2 && state.Sequence == 100, "same incoming frame retries after failed write");

        var queued = Frame(nonce, "old remote", 200);
        state.Observe(queued); state.Local("newer local"); bool wrote = false;
        Assert(state.Apply(queued, () => { wrote = true; return true; }) && !wrote && GuestClipboard.Decode(state.Outgoing, nonce) == "newer local", "queued remote message cannot replace a newer local copy");
        state.Local(null); Assert(GuestClipboard.DecodeFrame(state.Outgoing, nonce).Type == "skip" && GuestClipboard.Decode(state.Outgoing, nonce) == null, "sensitive copy shares only its version");
        state.Resend(); Assert(GuestClipboard.DecodeFrame(state.Outgoing, nonce).Type == "skip", "reconnect does not resurrect cancelled text");
        for (int i = 0; i < 1000; i++) state.Local(i.ToString());
        Assert(GuestClipboard.Decode(state.Outgoing, nonce) == "999", "offline changes retain only the latest value");

        var tied = new GuestClipboard.SyncState(nonce, "guest"); tied.Local("guest");
        Assert(tied.Apply(Frame(nonce, "host", 1), () => true) && tied.Outgoing == null, "host wins equal logical-clock tie consistently");
        string largest = GuestClipboard.Encode(nonce, "large sequence", GuestClipboard.MaxSequence, "host");
        Assert(GuestClipboard.DecodeFrame(largest, nonce).Sequence == GuestClipboard.MaxSequence, "JSON long sequence survives without rounding");
        foreach (string sequence in new[] { "0", "-1", "true", "1.5", "9007199254740992" })
            Assert(GuestClipboard.DecodeFrame(largest.Replace("9007199254740991", sequence), nonce) == null, "invalid sequence rejected: " + sequence);
        Assert(GuestClipboard.DecodeFrame(GuestClipboard.Control(nonce, "ack", "guest", 3, true), nonce).Ok, "successful ack round trip");
        Assert(GuestClipboard.DecodeFrame(GuestClipboard.Control(nonce, "hello", "host"), nonce).Type == "hello", "hello round trip");
        Assert(GuestClipboard.Decode("[" + new string('[', 20000) + "0" + new string(']', 20001), nonce) == null, "deep JSON is bounded");
    }

    static void NativeRetryAndReconnectCases(string nonce)
    {
        var state = new GuestClipboard.SyncState(nonce, "guest");
        uint native = 10;
        state.Capture(native, "initial guest");
        var remote = Frame(nonce, "remote retry", 100);
        bool failed = state.Apply(remote, () => GuestClipboard.ReplaceClipboard(state, native, () => native,
            () => { native++; return true; }, () => false));
        Assert(!failed && state.Sequence == 1, "failed native publish does not commit remote state");
        Assert(!state.NeedsCapture(native), "our own empty after failed publish is not a local copy");
        Assert(state.Apply(remote, () => GuestClipboard.ReplaceClipboard(state, native, () => native,
            () => { native++; return true; }, () => true)) && state.Sequence == 100,
            "remote retry still applies after EmptyClipboard succeeded but publish failed");

        var reopened = state;
        reopened.Capture(native, "remote retry");
        Assert(reopened.Sequence == 100 && reopened.Outgoing == null, "COM reopen does not turn the last remote write into a new local copy");
        reopened.Capture(++native, "copied while disconnected");
        string pending = reopened.Outgoing;
        Assert(GuestClipboard.DecodeFrame(pending, nonce).Sequence == 101, "offline local copy advances the retained logical clock");
        var anotherTransport = reopened;
        anotherTransport.Capture(native, "copied while disconnected");
        anotherTransport.Resend();
        Assert(anotherTransport.Outgoing == pending, "another COM reopen retains the exact unacknowledged newer text");
        bool overwritten = false;
        Assert(anotherTransport.Apply(Frame(nonce, "stale host", 50), () => { overwritten = true; return true; }) && !overwritten,
            "reconnect preserves the high version and rejects stale peer text");
        NativeBackupCases(nonce);
    }

    static void MultiFormatPublicationCases(string nonce)
    {
        string text = "Привет \U0001F600\r\ntext";
        var formats = GuestClipboard.TextFormats(text);
        Assert(new HashSet<uint> { 13, 16, 1, 7 }.SetEquals(formats.Keys), "publish every standard text format before CloseClipboard");
        Assert(formats[13].SequenceEqual(Encoding.Unicode.GetBytes(text + "\0")), "Unicode remains the full primary text");
        Assert(formats[16].Length == 4 && formats[1].Last() == 0 && formats[7].Last() == 0, "legacy formats carry a locale and terminators");
        var state = new GuestClipboard.SyncState(nonce, "guest"); uint native = 100;
        var clipboard = new Dictionary<uint, IntPtr> { { 2, new IntPtr(2) }, { 49152, new IntPtr(3) } };
        var freed = new List<uint>(); int resets = 0;
        Func<List<GuestClipboard.ClipboardCopy>> snapshot = () => new List<GuestClipboard.ClipboardCopy> {
            new GuestClipboard.ClipboardCopy(2, new IntPtr(20)), new GuestClipboard.ClipboardCopy(49152, new IntPtr(30)) };
        Func<bool> empty = () => { clipboard.Clear(); native++; return true; };
        Func<GuestClipboard.ClipboardCopy, bool> restore = copy => { clipboard[copy.Format] = copy.Handle; native++; return true; };
        var prepared = new List<GuestClipboard.ClipboardCopy> { new GuestClipboard.ClipboardCopy(13, new IntPtr(130)),
            new GuestClipboard.ClipboardCopy(16, new IntPtr(160)), new GuestClipboard.ClipboardCopy(1, new IntPtr(10)), new GuestClipboard.ClipboardCopy(7, new IntPtr(70)) };
        using (var recovery = new GuestClipboard.NativeRecovery(state, () => native, restore, copy => freed.Add(copy.Format),
            () => { resets++; return empty(); }, () => false))
        {
            bool applied = GuestClipboard.ReplaceClipboard(state, native, () => native, empty,
                () => GuestClipboard.PublishCopies(prepared, copy => {
                    if (copy.Format == 16) return false;
                    clipboard[copy.Format] = copy.Handle; native++; return true;
                }), () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved);
            Assert(!applied && resets == 1 && recovery.Count == 0, "partial publication resets once before restoration");
            Assert(new HashSet<uint> { 2, 49152 }.SetEquals(clipboard.Keys), "partial new formats never remain beside the original clipboard");
            Assert(prepared.Select(copy => copy.Format).SequenceEqual(new uint[] { 16, 1, 7 }), "only unpublished handles remain caller-owned after a second Set failure");
            Assert(freed.Count == 0, "restored backup handles belong to Windows");
        }

        bool resetWorks = false, foreignOwner = false; int restoreCalls = 0;
        using (var recovery = new GuestClipboard.NativeRecovery(state, () => native,
            copy => { restoreCalls++; native++; return true; }, copy => freed.Add(copy.Format),
            () => resetWorks, () => foreignOwner))
        {
            Assert(recovery.Preserve(snapshot), "capture backup before another partial write");
            recovery.Restore();
            Assert(recovery.Count == 3 && restoreCalls == 0, "failed reset retains every backup and its pending reset");
            native += 3; // Windows synthesizes text formats when a partial publication is closed.
            Assert(!recovery.Retry() && recovery.Count == 3 && restoreCalls == 0 && freed.Count == 0,
                "an ambiguous sequence change cannot discard backup or overwrite compat data");
            foreignOwner = true; native++;
            Assert(recovery.Retry() && recovery.Count == 0 && restoreCalls == 0 && freed.Count == 2,
                "a confirmed newer owner discards pending recovery without overwriting its copy");
        }
        freed.Clear(); resets = 0;
        using (var recovery = new GuestClipboard.NativeRecovery(state, () => native, restore, copy => freed.Add(copy.Format),
            () => { resets++; if (!resetWorks) return false; return empty(); }, () => false))
        {
            Assert(recovery.Preserve(() => new List<GuestClipboard.ClipboardCopy>()), "empty clipboard is a valid original snapshot");
            recovery.Restore(); Assert(recovery.Count == 1, "failed reset of an originally empty clipboard remains pending");
            resetWorks = true;
            Assert(recovery.Retry() && recovery.Count == 0 && resets == 2 && clipboard.Count == 0,
                "retry removes partial data even when there were no original handles");
        }
    }

    static void NativeBackupCases(string nonce)
    {
        var state = new GuestClipboard.SyncState(nonce, "guest"); uint native = 20;
        state.Capture(native, "old copy");
        int empties = 0, snapshots = 0;
        var restored = new List<uint>(); var freed = new List<uint>();
        uint failedFormat = 0;
        Func<List<GuestClipboard.ClipboardCopy>> snapshot = () =>
        {
            snapshots++;
            var copies = new List<GuestClipboard.ClipboardCopy>();
            foreach (uint format in new uint[] { 13, 2, 9, 3, 14, 49152 }) copies.Add(new GuestClipboard.ClipboardCopy(format, new IntPtr(format + 100)));
            return copies;
        };
        Func<bool> empty = () => { empties++; native++; return true; };
        Func<GuestClipboard.ClipboardCopy, bool> restore = copy =>
        {
            restored.Add(copy.Format);
            if (copy.Format == failedFormat) return false;
            native++; return true;
        };
        using (var recovery = new GuestClipboard.NativeRecovery(state, () => native, restore, copy => freed.Add(copy.Format)))
        {
            Assert(!GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => true,
                () => recovery.Preserve(() => { throw new System.Runtime.InteropServices.ExternalException("cannot snapshot"); }), recovery.Restore, recovery.ReleaseSaved),
                "snapshot failure rejects replacement");
            Assert(empties == 0 && restored.Count == 0 && recovery.Count == 0, "snapshot failure never empties or publishes");

            Assert(!GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => false,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved), "failed new Set returns failure");
            Assert(restored.Count == 6 && restored.Contains(2) && restored.Contains(3) && restored.Contains(14) && restored.Contains(49152),
                "failed Set restores every old format including registered and GDI data");
            Assert(recovery.Count == 0 && freed.Count == 0, "successfully restored handles belong to Windows and are not freed");
            Assert(!state.NeedsCapture(native), "empty and all restoration notifications are remembered as our own");

            restored.Clear(); failedFormat = 49152;
            Assert(!GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => false,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved), "partial restore leaves failed commit unacknowledged");
            Assert(recovery.Count == 1 && freed.Count == 0, "failed restore retains owned clone in memory");
            Assert(!recovery.Retry() && recovery.Count == 1 && !state.NeedsCapture(native), "failed retry stays pending without becoming a local copy");
            int beforeRestore = restored.Count, beforeEmpty = empties;
            failedFormat = 0;
            Assert(recovery.Retry() && recovery.Count == 0 && restored.Count == beforeRestore + 1 && empties == beforeEmpty,
                "later retry publishes only retained clone without another EmptyClipboard");

            failedFormat = 13; restored.Clear();
            GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => false,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved);
            Assert(recovery.Count == 1, "another restore can remain pending");
            native++; restored.Clear();
            Assert(recovery.Retry() && restored.Count == 0 && freed.Count == 1 && freed[0] == 13,
                "newer user copy releases pending clone without overwriting any format");
            Assert(state.NeedsCapture(native), "newer user sequence remains available for normal local capture");
            state.Capture(native, "newer user copy");

            freed.Clear(); failedFormat = 0;
            Assert(GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => { native++; return true; },
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved), "successful publication commits normally");
            Assert(freed.Count == 6 && recovery.Count == 0, "successful publication frees every unused backup clone");
            Assert(!state.NeedsCapture(native), "successful native publication is not echoed as local content");

            freed.Clear();
            Assert(!GuestClipboard.ReplaceClipboard(state, native, () => native, () => false, () => true,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved), "failed Empty refuses publication");
            Assert(freed.Count == 6 && recovery.Count == 0, "failed Empty frees snapshot without touching old clipboard");
            int beforeSnapshot = snapshots;
            Assert(!GuestClipboard.ReplaceClipboard(state, native - 1, () => native, empty, () => true,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved) && snapshots == beforeSnapshot,
                "newer sequence before lock refuses snapshot and empty");

            freed.Clear(); failedFormat = 13;
            GuestClipboard.ReplaceClipboard(state, native, () => native, empty, () => false,
                () => recovery.Preserve(snapshot), recovery.Restore, recovery.ReleaseSaved);
            recovery.Dispose();
            Assert(freed.Count == 0 && recovery.Count == 1, "transport dispose never frees an unresolved backup");
            int closeAttempts = 0;
            Func<bool> retryClose = () => { closeAttempts++; return recovery.Retry(); };
            Assert(!recovery.CanClose(true, retryClose) && closeAttempts == 0, "close waits for an active native write without reentering recovery");
            Assert(!recovery.CanClose(false, retryClose) && recovery.Count == 1 && freed.Count == 0,
                "COM failure close leaves backup and owner alive while restoration fails");
            Assert(!recovery.CanClose(false, retryClose) && recovery.Count == 1, "timer close retry retains the same backup until success");
            failedFormat = 0;
            Assert(recovery.CanClose(false, retryClose) && recovery.Count == 0 && freed.Count == 0,
                "timer retry restores old format before allowing HWND and COM transport to close");
            Assert(!state.NeedsCapture(native), "deferred transport close restoration is remembered as our own notification");
        }
    }
}
