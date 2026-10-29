using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace WindowsClaude
{
    // Owns TCP only. Caller callbacks return promptly and retain HWND, clipboard backup and file leases.
    internal sealed class ClipboardTransport : IDisposable
    {
        internal const int Port = 27844, MaxLineBytes = 16 * 1024 * 1024, MaxHelloBytes = 4096;
        internal const string HostAddress = "192.168.234.4", SourceAddress = "192.168.234.15", SourceMac = "525400770004";
        const int HelloDeadline = 5000, ConnectDeadline = 2000, PingInterval = 1000, PeerDeadline = 15000;
        static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
        readonly string nonce;
        readonly Func<string> outgoing;
        readonly Action<string> received, status;
        readonly Action<bool> connected;
        readonly Func<bool> networkReady;
        readonly Func<TcpClient> testConnectedClient;
        readonly object gate = new object();
        TcpClient current;
        volatile bool stopping;
        int started, activeReaders;
        internal int PeakReaders;
        internal int ReaderCount { get { return Volatile.Read(ref activeReaders); } }
        string lastStatus;

        internal ClipboardTransport(string nonce, int port, Func<string> nextOutgoingLine, Action<string> receivedLine,
            Action<bool> peerConnected, Func<bool> networkReady, Action<string> safeStatus)
            : this(nonce, port, nextOutgoingLine, receivedLine, peerConnected, networkReady, safeStatus, null) { }

        // Test-only: returns a connected client to an owned loopback peer. Production has no endpoint override.
        internal ClipboardTransport(string nonce, int port, Func<string> nextOutgoingLine, Action<string> receivedLine,
            Action<bool> peerConnected, Func<bool> networkReady, Action<string> safeStatus, Func<TcpClient> connectedClientFactory)
        {
            if (nonce == null || !Regex.IsMatch(nonce, @"\A[0-9a-f]{32}\z") || port != Port ||
                nextOutgoingLine == null || receivedLine == null || peerConnected == null || networkReady == null || safeStatus == null)
                throw new ArgumentException("Invalid clipboard transport configuration");
            this.nonce = nonce; outgoing = nextOutgoingLine; received = receivedLine; connected = peerConnected;
            this.networkReady = networkReady; status = safeStatus; testConnectedClient = connectedClientFactory;
        }

        internal sealed class SourceNic
        {
            internal string Mac;
            internal bool Up;
            internal int Index;
            internal IPAddress[] Addresses;
        }
        internal static int SelectSourceIndex(IEnumerable<SourceNic> adapters)
        {
            int selected = 0;
            foreach (SourceNic adapter in adapters)
            {
                if (adapter == null || !adapter.Up || !String.Equals(adapter.Mac, SourceMac, StringComparison.OrdinalIgnoreCase) ||
                    adapter.Index <= 0 || adapter.Index > 0xFFFFFF || adapter.Addresses == null) continue;
                bool address = false;
                foreach (IPAddress ip in adapter.Addresses) if (IPAddress.Parse(SourceAddress).Equals(ip)) address = true;
                if (!address) continue;
                if (selected != 0) return 0;
                selected = adapter.Index;
            }
            return selected;
        }
        internal static int InterfaceOptionValue(int index)
        {
            if (index <= 0 || index > 0xFFFFFF) throw new ArgumentException("Invalid isolated interface index");
            // Winsock IP_UNICAST_IF (31) takes DWORD IF_INDEX in network byte order, as does the QEMU patch.
            return IPAddress.HostToNetworkOrder(index);
        }
        static int SourceIndex()
        {
            var adapters = new List<SourceNic>();
            foreach (NetworkInterface nic in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (nic.OperationalStatus != OperationalStatus.Up || nic.GetPhysicalAddress().ToString() != SourceMac) continue;
                var properties = nic.GetIPProperties(); var ipv4 = properties.GetIPv4Properties();
                if (ipv4 == null) continue;
                var addresses = new List<IPAddress>();
                foreach (UnicastIPAddressInformation address in properties.UnicastAddresses) addresses.Add(address.Address);
                adapters.Add(new SourceNic { Mac = SourceMac, Up = true, Index = ipv4.Index, Addresses = addresses.ToArray() });
            }
            return SelectSourceIndex(adapters);
        }
        sealed class ProtocolFailure : IOException { }
        sealed class NetworkUnavailable : IOException { }
        sealed class Channel
        {
            internal volatile bool Done, ProtocolFailed;
            internal readonly Stopwatch Clock = Stopwatch.StartNew();
            internal long LastPong;
        }

        void Report(string value)
        {
            if (lastStatus == value) return;
            lastStatus = value;
            try { status(value); } catch (Exception) { }
        }
        void Pause(int milliseconds)
        { lock (gate) if (!stopping) Monitor.Wait(gate, milliseconds); }
        static void Close(TcpClient client)
        {
            if (client == null) return;
            try { Socket socket = client.Client; if (socket != null) socket.Shutdown(SocketShutdown.Both); } catch (SocketException) { } catch (ObjectDisposedException) { }
            try { client.Close(); } catch (SocketException) { } catch (ObjectDisposedException) { }
        }
        internal void Run()
        {
            if (Interlocked.CompareExchange(ref started, 1, 0) != 0) throw new InvalidOperationException("Transport already started");
            Report("WAITING");
            try
            {
                while (!stopping)
                {
                    TcpClient client = null; Thread reader = null; bool authenticated = false;
                    var channel = new Channel(); string failure = "RECONNECTING";
                    try
                    {
                        if (!networkReady()) throw new NetworkUnavailable();
                        int source = testConnectedClient == null ? SourceIndex() : 0;
                        if (testConnectedClient == null && source == 0) throw new NetworkUnavailable();
                        client = testConnectedClient == null ? new TcpClient(AddressFamily.InterNetwork) : testConnectedClient();
                        if (client == null) throw new IOException();
                        lock (gate)
                        {
                            if (stopping) { Close(client); break; }
                            current = client;
                        }
                        client.NoDelay = true; client.SendTimeout = 5000;
                        if (testConnectedClient == null)
                        {
                            client.Client.SetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31, InterfaceOptionValue(source));
                            client.Client.Bind(new IPEndPoint(IPAddress.Parse(SourceAddress), 0));
                            if (!networkReady()) throw new NetworkUnavailable();
                            var connection = client.ConnectAsync(IPAddress.Parse(HostAddress), Port);
                            connection.ContinueWith(done => { var observed = done.Exception; },
                                System.Threading.Tasks.TaskContinuationOptions.OnlyOnFaulted | System.Threading.Tasks.TaskContinuationOptions.ExecuteSynchronously);
                            if (!connection.Wait(ConnectDeadline)) throw new IOException();
                        }
                        if (stopping) break;
                        if (!networkReady()) throw new NetworkUnavailable();
                        NetworkStream stream = client.GetStream(); stream.WriteTimeout = 5000;
                        stream.WriteByte(10); // QEMU keeps the character stream across guest TCP reconnects.
                        WriteLine(stream, "{\"v\":2,\"n\":\"" + nonce + "\",\"t\":\"hello\",\"o\":\"guest\",\"ready\":true}");
                        var lines = new Lines(stream);
                        try { ReadHostHello(lines); }
                        catch (IOException) { throw new ProtocolFailure(); }
                        stream.ReadTimeout = Timeout.Infinite;
                        if (stopping) break;
                        authenticated = true; connected(true); Report("CONNECTED");
                        Interlocked.Exchange(ref channel.LastPong, channel.Clock.ElapsedMilliseconds);
                        long pingAt = channel.Clock.ElapsedMilliseconds + PingInterval;
                        reader = new Thread(() => ReadLoop(lines, client, channel)) { IsBackground = true, Name = "clipboard TCP reader" };
                        reader.Start();
                        while (!stopping && !channel.Done)
                        {
                            long now = channel.Clock.ElapsedMilliseconds;
                            if (now - Interlocked.Read(ref channel.LastPong) >= PeerDeadline) throw new IOException();
                            if (now >= pingAt)
                            {
                                WriteLine(stream, "{\"v\":2,\"n\":\"" + nonce + "\",\"t\":\"ping\",\"o\":\"guest\"}");
                                pingAt = now + PingInterval;
                            }
                            string line = outgoing();
                            if (line == null) Pause(25); else WriteLine(stream, line);
                        }
                        if (channel.ProtocolFailed) failure = "PROTOCOL_FAILED";
                    }
                    catch (NetworkUnavailable) { failure = "NETWORK_UNAVAILABLE"; }
                    catch (ProtocolFailure) { failure = "PROTOCOL_FAILED"; }
                    catch (EncoderFallbackException) { failure = "PROTOCOL_FAILED"; }
                    catch (DecoderFallbackException) { failure = "PROTOCOL_FAILED"; }
                    catch (Exception) { }
                    finally
                    {
                        Close(client);
                        if (reader != null) reader.Join(); // Closing the socket interrupts an idle Read; never overlap readers.
                        lock (gate) if (Object.ReferenceEquals(current, client)) current = null;
                        if (authenticated) try { connected(false); } catch (Exception) { }
                    }
                    if (!stopping) { Report(failure); Pause(250); }
                }
            }
            finally { Report("WAITING"); }
        }
        void ReadLoop(Lines lines, TcpClient client, Channel channel)
        {
            int count = Interlocked.Increment(ref activeReaders);
            PeakReaders = Math.Max(PeakReaders, count);
            try
            {
                while (!stopping)
                {
                    string line = lines.Read(MaxLineBytes, 0);
                    if (line == null) break;
                    if (line.Length == 0) continue;
                    if (HostControl(line, "pong") || HostHello(line))
                    {
                        Interlocked.Exchange(ref channel.LastPong, channel.Clock.ElapsedMilliseconds);
                        continue;
                    }
                    received(line);
                }
            }
            catch (ProtocolFailure) { channel.ProtocolFailed = true; }
            catch (DecoderFallbackException) { channel.ProtocolFailed = true; }
            catch (Exception) { }
            finally
            {
                Interlocked.Decrement(ref activeReaders);
                channel.Done = true; Close(client);
                lock (gate) Monitor.PulseAll(gate);
            }
        }
        void ReadHostHello(Lines lines)
        {
            var elapsed = Stopwatch.StartNew(); long skipped = 0;
            while (!stopping)
            {
                int remaining = HelloDeadline - (int)elapsed.ElapsedMilliseconds;
                if (remaining <= 0) throw new ProtocolFailure();
                string line = lines.Read(MaxLineBytes, remaining);
                if (line == null) throw new ProtocolFailure();
                if (HostHello(line)) return;
                skipped += Utf8.GetByteCount(line) + 1;
                if (skipped > 2L * MaxLineBytes || (line.Length != 0 && !StaleHostFrame(line) && !HostControl(line, "pong")))
                    throw new ProtocolFailure();
            }
            throw new IOException();
        }
        bool StaleHostFrame(string line)
        {
            try
            {
                var value = new JavaScriptSerializer { MaxJsonLength = MaxLineBytes, RecursionLimit = 16 }.DeserializeObject(line) as Dictionary<string, object>;
                if (value == null || !value.ContainsKey("v") || !(value["v"] is int) || (int)value["v"] != 2 ||
                    !value.ContainsKey("n") || value["n"] as string != nonce || !value.ContainsKey("t") ||
                    !value.ContainsKey("o") || (value["o"] as string != "host" && value["o"] as string != "guest") ||
                    !value.ContainsKey("s") || (!(value["s"] is int) && !(value["s"] is long))) return false;
                long sequence = Convert.ToInt64(value["s"]);
                if (sequence <= 0 || sequence > 9007199254740991L) return false;
                string type = value["t"] as string;
                return type == "text" || type == "skip" || type == "files" || type == "ack" || type == "stored" || type == "cancel";
            }
            catch (Exception) { return false; }
        }
        bool HostHello(string line) { return HostControl(line, "hello"); }
        bool HostControl(string line, string type)
        {
            if (line == null) return false;
            try
            {
                var value = new JavaScriptSerializer { MaxJsonLength = MaxHelloBytes, RecursionLimit = 8 }.DeserializeObject(line) as Dictionary<string, object>;
                return value != null && value.Count == (type == "hello" ? 5 : 4) && value.ContainsKey("v") && value["v"] is int && (int)value["v"] == 2 &&
                    value.ContainsKey("n") && value["n"] as string == nonce && value.ContainsKey("t") && value["t"] as string == type &&
                    value.ContainsKey("o") && value["o"] as string == "host" && (type != "hello" ||
                    value.ContainsKey("ready") && value["ready"] is bool && (bool)value["ready"]);
            }
            catch (Exception) { return false; }
        }
        static void WriteLine(NetworkStream stream, string line)
        {
            if (line.IndexOfAny(new[] { '\r', '\n' }) >= 0 || Utf8.GetByteCount(line) > MaxLineBytes) throw new ProtocolFailure();
            byte[] bytes = Utf8.GetBytes(line);
            stream.Write(bytes, 0, bytes.Length); stream.WriteByte(10);
        }
        sealed class Lines
        {
            readonly NetworkStream stream;
            readonly byte[] buffer = new byte[4096];
            int offset, count;
            internal Lines(NetworkStream stream) { this.stream = stream; }
            internal string Read(int limit, int deadline)
            {
                var elapsed = Stopwatch.StartNew();
                using (var line = new MemoryStream(Math.Min(limit, 4096)))
                {
                    while (true)
                    {
                        if (deadline != 0 && elapsed.ElapsedMilliseconds >= deadline) throw new ProtocolFailure();
                        if (offset == count)
                        {
                            if (deadline != 0) stream.ReadTimeout = Math.Max(1, deadline - (int)elapsed.ElapsedMilliseconds);
                            count = stream.Read(buffer, 0, buffer.Length); offset = 0;
                            if (count == 0) { if (line.Length != 0) throw new ProtocolFailure(); return null; }
                        }
                        int end = Array.IndexOf(buffer, (byte)10, offset, count - offset);
                        int take = end < 0 ? count - offset : end - offset;
                        if (line.Length + take > limit) throw new ProtocolFailure();
                        line.Write(buffer, offset, take); offset += take;
                        if (end < 0) continue;
                        offset++;
                        int length = (int)line.Length;
                        if (length != 0 && line.GetBuffer()[length - 1] == 13) length--;
                        return Utf8.GetString(line.GetBuffer(), 0, length);
                    }
                }
            }
        }
        public void Dispose()
        {
            TcpClient client;
            lock (gate) { stopping = true; client = current; Monitor.PulseAll(gate); }
            Close(client);
        }
    }
}
