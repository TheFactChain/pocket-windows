using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using WindowsClaude;

internal static class ClipboardTransportRegression
{
    static readonly object output = new object();
    static void Emit(object value) { lock (output) { Console.WriteLine(new JavaScriptSerializer { MaxJsonLength = 2097152 }.Serialize(value)); Console.Out.Flush(); } }
    static void Assert(bool ok) { if (!ok) throw new Exception("Transport regression assertion"); }
    static string Digest(string line)
    { using (var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(line))).Replace("-", "").ToLowerInvariant(); }
    static void Metadata()
    {
        Func<string, bool, int, string, ClipboardTransport.SourceNic> nic = (mac, up, index, ip) => new ClipboardTransport.SourceNic
            { Mac = mac, Up = up, Index = index, Addresses = new[] { IPAddress.Parse(ip) } };
        var exact = nic(ClipboardTransport.SourceMac, true, 23, ClipboardTransport.SourceAddress);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { exact }) == 23);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { nic("525400770005", true, 9, ClipboardTransport.SourceAddress) }) == 0);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { nic(ClipboardTransport.SourceMac, false, 23, ClipboardTransport.SourceAddress) }) == 0);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { nic(ClipboardTransport.SourceMac, true, 23, "127.0.0.1") }) == 0);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { exact, exact }) == 0);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { nic(ClipboardTransport.SourceMac, true, 0, ClipboardTransport.SourceAddress) }) == 0);
        Assert(ClipboardTransport.SelectSourceIndex(new[] { nic(ClipboardTransport.SourceMac, true, 0x1000000, ClipboardTransport.SourceAddress) }) == 0);
        byte[] option = BitConverter.GetBytes(ClipboardTransport.InterfaceOptionValue(0x123456));
        Assert(option[0] == 0 && option[1] == 0x12 && option[2] == 0x34 && option[3] == 0x56);
        Assert(ClipboardTransport.Port == 27844 && ClipboardTransport.HostAddress == "192.168.234.4" && ClipboardTransport.SourceAddress == "192.168.234.15");
        bool refused = false;
        try { new ClipboardTransport(new string('a', 32), 27845, () => null, line => { }, value => { }, () => true, status => { }); }
        catch (ArgumentException) { refused = true; }
        Assert(refused); Emit(new { kind = "metadata", ok = true });
    }
    static int Main(string[] args)
    {
        Console.InputEncoding = new UTF8Encoding(false, true); Console.OutputEncoding = new UTF8Encoding(false, true);
        try
        {
            if (args[0] == "metadata") { Metadata(); return 0; }
            var json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 16 };
            var config = json.Deserialize<Dictionary<string, object>>(Console.ReadLine());
            string mode = (string)config["mode"];
            var frames = (System.Collections.ArrayList)config["frames"];
            var queue = new Queue<string>(); foreach (string frame in frames) queue.Enqueue(frame);
            string retained = queue.Count == 0 ? null : queue.Peek();
            bool ready = (bool)config["ready"], sent = false;
            bool failBeforeHello = config.ContainsKey("fail_before_hello") && (bool)config["fail_before_hello"];
            object gate = new object();
            int outgoingCalls = 0, received = 0, peers = 0, losses = 0, factories = 0, checks = 0;
            TcpClient fixture = null;
            ClipboardTransport transport = null;
            int baselineThreads; using (var process = Process.GetCurrentProcess()) baselineThreads = process.Threads.Count;
            transport = new ClipboardTransport(args[2], ClipboardTransport.Port,
                () =>
                {
                    Interlocked.Increment(ref outgoingCalls);
                    lock (gate)
                    {
                        if (mode == "resend") { if (sent) return null; sent = true; return retained; }
                        return queue.Count == 0 ? null : queue.Dequeue();
                    }
                },
                line =>
                {
                    Interlocked.Increment(ref received);
                    Emit(new { kind = "received", sha256 = Digest(line), bytes = Encoding.UTF8.GetByteCount(line), chars = line.Length });
                    if (mode == "throw_receive") throw new IOException("raw-fixture-MUST_NOT_BE_LOGGED");
                    if (mode == "echo") lock (gate) queue.Enqueue(line);
                },
                value =>
                {
                    if (value) { Interlocked.Increment(ref peers); lock (gate) sent = false; }
                    else Interlocked.Increment(ref losses);
                    int send = 0, receive = 0;
                    if (value) { send = fixture.SendTimeout; receive = fixture.ReceiveTimeout; }
                    Emit(new { kind = "peer", value, readers = transport.ReaderCount, send, receive });
                },
                () => Volatile.Read(ref ready) && (!failBeforeHello || Interlocked.Increment(ref checks) == 1),
                value => Emit(new { kind = "status", value }),
                () =>
                {
                    Interlocked.Increment(ref factories);
                    var client = new TcpClient(AddressFamily.InterNetwork);
                    try { client.Connect(IPAddress.Loopback, Int32.Parse(args[1])); fixture = client; return client; }
                    catch { client.Close(); throw; }
                });
            var worker = new Thread(transport.Run) { IsBackground = true, Name = "transport regression" }; worker.Start();
            Emit(new { kind = "started" });
            string command;
            while ((command = Console.ReadLine()) != null)
            {
                var control = json.Deserialize<Dictionary<string, object>>(command);
                string op = (string)control["op"];
                if (op == "stop") break;
                if (op == "ready") Volatile.Write(ref ready, (bool)control["value"]);
                if (op == "stats")
                {
                    int threads; using (var process = Process.GetCurrentProcess()) threads = process.Threads.Count;
                    Emit(new { kind = "stats", outgoingCalls, received, peers, losses, factories, readers = transport.ReaderCount,
                        peakReaders = transport.PeakReaders, threadGrowth = threads - baselineThreads });
                }
            }
            transport.Dispose(); Assert(worker.Join(6000));
            Emit(new { kind = "stopped", outgoingCalls, received, peers, losses, factories, readers = transport.ReaderCount, peakReaders = transport.PeakReaders });
            return 0;
        }
        catch (Exception) { Emit(new { kind = "harness_failed" }); return 2; }
    }
}
