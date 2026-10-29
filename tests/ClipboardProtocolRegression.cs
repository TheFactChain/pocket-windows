using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using WindowsClaude;

// Own fixtures only: optional loopback TCP, fake publication; no Form, native clipboard, NIC or VM.
internal static class ClipboardProtocolRegression
{
    static readonly object output = new object();
    static void Emit(object value)
    { lock (output) { Console.WriteLine(new JavaScriptSerializer { MaxJsonLength = 4194304 }.Serialize(value)); Console.Out.Flush(); } }
    static string[] Strings(object value) { return ((IEnumerable)value).Cast<object>().Select(item => (string)item).ToArray(); }
    static TcpClient OwnLoopbackClient(int port)
    {
        var client = new TcpClient(AddressFamily.InterNetwork);
        try
        {
            var connection = client.ConnectAsync(IPAddress.Loopback, port);
            connection.ContinueWith(done => { var ignored = done.Exception; }, TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously);
            if (!connection.Wait(2000)) throw new IOException("Own loopback connection timed out");
            return client;
        }
        catch { client.Close(); throw; }
    }
    static bool StopTransport(ref ClipboardTransport transport, ref Thread thread)
    {
        if (transport == null) return true;
        transport.Dispose();
        bool stopped = thread == null || thread.Join(6000);
        if (stopped) { transport = null; thread = null; }
        return stopped;
    }
    static int Main()
    {
        var json = new JavaScriptSerializer { MaxJsonLength = 4194304, RecursionLimit = 32 };
        ClipboardProtocol protocol = null;
        ClipboardTransport transport = null; Thread transportThread = null;
        var transportGate = new object(); bool transportConnected = false;
        string transportStatus = "STOPPED"; int transportConnections = 0;
        var cleanupRelease = new ManualResetEventSlim(true);
        try
        {
            var config = json.Deserialize<Dictionary<string, object>>(Console.ReadLine());
            string nonce = (string)config["nonce"], parent = (string)config["parent"], boot = (string)config["boot"];
            var state = new GuestClipboard.SyncState(nonce, (string)config["origin"]);
            var cache = new ClipboardStage.Cache(parent, boot);
            protocol = new ClipboardProtocol(nonce, state, cache, value => Emit(new { kind = "status", value }));
            int attempts = 0, publications = 0, nativeEmpties = 0;
            string[] nativePaths = null;
            string nativeText = null;
            protocol.Reconnected(true);
            Emit(new { kind = "ready" });
            string line;
            while ((line = Console.ReadLine()) != null)
            {
                var command = json.Deserialize<Dictionary<string, object>>(line);
                int id = (int)command["id"]; string op = (string)command["op"];
                try
                {
                    if (op == "receive") protocol.Receive((string)command["line"]);
                    else if (op == "local_text") state.Local(command["text"] as string);
                    else if (op == "local_files") state.LocalFiles(Strings(command["paths"]));
                    else if (op == "legacy_apply_text")
                    {
                        var frame = GuestClipboard.DecodeFrame((string)command["line"], nonce);
                        if (frame == null || frame.Type != "text") throw new Exception("Text fixture required");
                        state.Apply(frame, () => { attempts++; publications++; nativePaths = null; nativeText = frame.Text; return true; });
                    }
                    else if (op == "reconnected") protocol.Reconnected((bool)command["value"]);
                    else if (op == "transport-start")
                    {
                        if (transport != null) throw new InvalidOperationException("Stop the fixture transport before replacing it");
                        int port = (int)command["port"];
                        if (port < 1024 || port > 65535) throw new ArgumentException("Own loopback port required");
                        protocol.Reconnected(false);
                        lock (transportGate) { transportConnected = false; transportStatus = "STARTING"; }
                        transport = new ClipboardTransport(nonce, ClipboardTransport.Port, protocol.NextOutgoing, protocol.Receive,
                            value => { protocol.Reconnected(value); lock (transportGate) { transportConnected = value; if (value) transportConnections++; } },
                            () => true, value => { lock (transportGate) transportStatus = value; Emit(new { kind = "status", value = "TRANSPORT_" + value }); },
                            () => OwnLoopbackClient(port));
                        transportThread = new Thread(transport.Run) { IsBackground = true, Name = "fixture exact clipboard transport" };
                        transportThread.Start();
                    }
                    else if (op == "transport-stop")
                    {
                        bool stopped = StopTransport(ref transport, ref transportThread);
                        lock (transportGate) { if (stopped) { transportConnected = false; transportStatus = "STOPPED"; } }
                        Emit(new { kind = "response", id, stopped }); continue;
                    }
                    else if (op == "transport-status")
                    {
                        lock (transportGate)
                            Emit(new { kind = "response", id, connected = transportConnected, status = transportStatus,
                                connections = transportConnections, alive = transportThread != null && transportThread.IsAlive,
                                readers = transport == null ? 0 : transport.ReaderCount, peakReaders = transport == null ? 0 : transport.PeakReaders });
                        continue;
                    }
                    else if (op == "hold_cleanup") { cleanupRelease.Reset(); protocol.BeforeCleanup = () => cleanupRelease.Wait(); }
                    else if (op == "release_cleanup") cleanupRelease.Set();
                    else if (op == "next")
                    {
                        int wait = command.ContainsKey("wait") ? (int)command["wait"] : 0;
                        var elapsed = Stopwatch.StartNew(); string frame;
                        do { frame = protocol.NextOutgoing(); if (frame != null || elapsed.ElapsedMilliseconds >= wait) break; Thread.Sleep(1); } while (true);
                        Emit(new { kind = "response", id, line = frame }); continue;
                    }
                    else if (op == "apply")
                    {
                        string marker = protocol.PendingMarker;
                        bool prepared = marker != null && File.Exists(marker);
                        bool ok = (bool)command["ok"];
                        bool? guardAllowed = null;
                        bool initiallyCurrent = false, cloneCurrent = false;
                        GuestClipboard.Frame attempted = null;
                        bool applied = protocol.ApplyLatest(frame =>
                        {
                            attempts++;
                            attempted = frame;
                            initiallyCurrent = protocol.IsCurrent(frame);
                            cloneCurrent = protocol.IsCurrent(new GuestClipboard.Frame { Type = frame.Type,
                                Origin = frame.Origin, Sequence = frame.Sequence, TransferId = frame.TransferId, Text = frame.Text, Paths = frame.Paths });
                            if (frame.Type == "files" && (!prepared || frame.Paths == null || frame.Paths.Length == 0 || frame.Paths.Any(path => !File.Exists(path) && !Directory.Exists(path))))
                                throw new Exception("Publication attempted before prepare");
                            if (command.ContainsKey("local_during")) state.Local(command["local_during"] as string);
                            if (command.ContainsKey("dispose_during") && (bool)command["dispose_during"]) protocol.Dispose();
                            if (command.ContainsKey("throw") && (bool)command["throw"]) throw new IOException("private fixture must not be logged");
                            if (command.ContainsKey("preserve_lines"))
                            {
                                Exception failure = null;
                                var reader = new Thread(() =>
                                {
                                    try { foreach (string incoming in Strings(command["preserve_lines"])) protocol.Receive(incoming); }
                                    catch (Exception error) { failure = error; }
                                });
                                reader.Start();
                                if (!reader.Join(2000)) throw new Exception("Native callback held protocol gate");
                                if (failure != null) throw failure;
                                guardAllowed = protocol.IsCurrent(frame);
                                if (!guardAllowed.Value) return false;
                            }
                            nativeEmpties++;
                            if (ok) { publications++; nativePaths = frame.Paths == null ? null : (string[])frame.Paths.Clone(); nativeText = frame.Text; }
                            if (ok && command.ContainsKey("after_set_lines"))
                                foreach (string incoming in Strings(command["after_set_lines"])) protocol.Receive(incoming);
                            return ok;
                        });
                        Emit(new { kind = "response", id, applied, attempts, publications, nativeEmpties, guardAllowed,
                            initiallyCurrent, cloneCurrent, currentOutside = protocol.IsCurrent(attempted), prepared, marker, paths = nativePaths, text = nativeText }); continue;
                    }
                    else if (op == "stats")
                    {
                        var files = state.OutgoingFiles;
                        Emit(new { kind = "response", id, sequence = state.Sequence, outgoing = state.Outgoing,
                            fileId = files == null ? null : files.Id, fileSequence = files == null ? 0 : files.Sequence,
                            stored = files != null && files.Stored, candidate = protocol.QueuedCandidates,
                            cleanup = protocol.CleanupBusy, cleanupPeak = protocol.PeakCleanupWorkers,
                            marker = protocol.PendingMarker, attempts, publications, paths = nativePaths, text = nativeText }); continue;
                    }
                    else if (op == "cleanup")
                    {
                        var other = new ClipboardStage.Cache(parent, (string)command["boot"]);
                        var result = other.Cleanup(Strings(command["paths"]));
                        Emit(new { kind = "response", id, removed = result.Removed, retained = result.Retained, failed = result.Failed, unconfirmed = result.CleanupUnconfirmed }); continue;
                    }
                    else if (op == "stop")
                    {
                        cleanupRelease.Set(); bool transportStopped = StopTransport(ref transport, ref transportThread);
                        protocol.Dispose(); bool workerStopped = protocol.WaitForWorker(6000); bool stopped = transportStopped && workerStopped;
                        Emit(new { kind = "response", id, stopped, publications, attempts, paths = nativePaths,
                            cleanupPeak = protocol.PeakCleanupWorkers });
                        return stopped ? 0 : 3;
                    }
                    else throw new Exception("Unknown fixture operation");
                    Emit(new { kind = "response", id, ok = true });
                }
                catch (Exception error) { Emit(new { kind = "response", id, error = error.GetType().Name }); }
            }
            cleanupRelease.Set(); bool closed = StopTransport(ref transport, ref transportThread);
            protocol.Dispose(); bool settled = protocol.WaitForWorker(6000); return closed && settled ? 0 : 3;
        }
        catch (Exception) { Emit(new { kind = "fixture_failed" }); return 2; }
        finally { cleanupRelease.Set(); StopTransport(ref transport, ref transportThread); if (protocol != null) { protocol.Dispose(); protocol.WaitForWorker(6000); } cleanupRelease.Dispose(); }
    }
}
