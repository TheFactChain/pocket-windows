using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;

namespace WindowsClaude
{
    // Versioning and file-transfer lifetime only; native publication belongs to the UI adapter.
    internal sealed class ClipboardProtocol : IDisposable
    {
        sealed class Copy
        {
            internal GuestClipboard.Frame Frame;
            internal ClipboardFiles.Receiver Receiver;
            internal ClipboardStage.Cache.Ticket Ticket;
            internal readonly object Io = new object();
            internal volatile bool Cancelled, Published;
            internal bool InNative, CleanupQueued;
        }
        sealed class Outgoing
        {
            internal string Line;
            internal bool End;
        }
        readonly string nonce;
        readonly GuestClipboard.SyncState state;
        readonly ClipboardStage.Cache cache;
        readonly Action<string> status;
        readonly object gate = new object();
        readonly Queue<string> controls = new Queue<string>();
        readonly List<Copy> cleanup = new List<Copy>(2);
        readonly Stopwatch clock = Stopwatch.StartNew();
        readonly Thread worker;
        Copy partial, candidate, applying;
        GuestClipboard.Frame latestSeen, lastApplied;
        GuestClipboard.FileCopy offered, desired;
        Outgoing ready;
        long epoch, textDue, filesDue;
        string lastText;
        bool connected, disposed, pump, pumping, cleanupActive;
        internal int PeakCleanupWorkers;
        internal Action BeforeCleanup;
        internal int QueuedCandidates { get { lock (gate) return candidate == null ? 0 : 1; } }
        internal string PendingMarker { get { lock (gate) return candidate == null || candidate.Ticket == null ? null : candidate.Ticket.Marker; } }
        internal bool CleanupBusy { get { lock (gate) return cleanupActive || cleanup.Count != 0; } }
        internal bool WaitForWorker(int milliseconds) { return worker.Join(milliseconds); }

        internal ClipboardProtocol(string nonce, GuestClipboard.SyncState state, ClipboardStage.Cache cache, Action<string> safeStatus)
        {
            if (!GuestClipboard.ValidTransferId(nonce) || state == null || cache == null || safeStatus == null)
                throw new ArgumentException("Invalid clipboard protocol configuration");
            this.nonce = nonce; this.state = state; this.cache = cache; status = safeStatus;
            worker = new Thread(Work) { IsBackground = true, Name = "clipboard protocol I/O" }; worker.Start();
        }
        void Safe(string value) { try { status(value); } catch (Exception) { } }
        static int Compare(GuestClipboard.Frame a, GuestClipboard.Frame b)
        { return a.Sequence != b.Sequence ? a.Sequence.CompareTo(b.Sequence) : String.CompareOrdinal(a.Origin, b.Origin); }
        static bool Same(GuestClipboard.Frame a, GuestClipboard.Frame b)
        {
            return a != null && b != null && Compare(a, b) == 0 && a.Type == b.Type &&
                (a.Type == "files" ? a.TransferId == b.TransferId : a.Text == b.Text);
        }
        static bool Matches(GuestClipboard.Frame a, GuestClipboard.Frame b)
        { return a != null && b != null && Compare(a, b) == 0 && a.TransferId == b.TransferId; }
        static GuestClipboard.Frame Header(GuestClipboard.FileCopy copy)
        { return new GuestClipboard.Frame { Type = "files", Sequence = copy.Sequence, Origin = copy.Origin, TransferId = copy.Id }; }
        void Control(GuestClipboard.Frame frame, string type, bool ok = false, bool applied = false)
        {
            if (!connected || disposed) return;
            var value = new Dictionary<string, object> { { "v", 2 }, { "n", nonce }, { "t", type }, { "s", frame.Sequence }, { "o", frame.Origin } };
            if (frame.TransferId != null) value["i"] = frame.TransferId;
            if (type == "ack") { value["ok"] = ok; value["applied"] = applied; }
            string line = GuestClipboard.Serialize(value);
            if (line == null || controls.Contains(line)) return;
            if (controls.Count == 16) controls.Dequeue();
            controls.Enqueue(line);
        }
        void QueueCleanup(Copy copy)
        {
            if (copy == null || copy.Published) return;
            copy.Cancelled = true;
            if (copy.InNative || copy.CleanupQueued || (copy.Receiver == null && copy.Ticket == null && copy.Frame.Type != "files")) return;
            // At most the partial/queued copy and one in-flight native copy can require cleanup.
            if (cleanup.Count >= 2) throw new InvalidOperationException("Clipboard cleanup bound exceeded");
            copy.CleanupQueued = true; cleanup.Add(copy); Monitor.PulseAll(gate);
        }
        bool Superseded(GuestClipboard.Frame frame)
        { return state.Compare(frame) < 0 || (latestSeen != null && Compare(frame, latestSeen) < 0); }
        void Reconcile()
        {
            if (partial != null && Superseded(partial.Frame))
            { var old = partial; partial = null; Control(old.Frame, "ack", true); QueueCleanup(old); }
            if (candidate != null && Superseded(candidate.Frame))
            { var old = candidate; candidate = null; Control(old.Frame, "ack", true); QueueCleanup(old); }
        }
        GuestClipboard.Frame ActiveRemote()
        {
            GuestClipboard.Frame newest = null;
            foreach (Copy copy in new[] { partial, candidate, applying })
                if (copy != null && !copy.Cancelled && (newest == null || Compare(copy.Frame, newest) > 0)) newest = copy.Frame;
            return newest;
        }
        void RequestSource(GuestClipboard.FileCopy copy, bool cancelOld)
        {
            if (Object.ReferenceEquals(desired, copy)) return;
            if (cancelOld && desired != null) Control(Header(desired), "cancel");
            desired = copy; ready = null; epoch++; pump = copy != null; filesDue = 0; Monitor.PulseAll(gate);
        }

        internal void Receive(string line)
        {
            GuestClipboard.Frame frame = GuestClipboard.DecodeFrame(line, nonce);
            if (frame == null) return;
            Copy target = null;
            bool begin = frame.Type == "files" && frame.Body.ContainsKey("event") && frame.Body["event"] as string == "begin";
            lock (gate)
            {
                if (disposed || !connected) return;
                state.Observe(frame);
                if (state.RecoverOwnReplay(frame))
                {
                    Reconcile(); Control(frame, "ack", true); return;
                }
                if (frame.Type == "ack")
                {
                    state.Acknowledge(frame);
                    if (offered != null && !state.IsPending(offered)) offered = null;
                    if (desired != null && !state.IsPending(desired)) RequestSource(null, false);
                    return;
                }
                if (frame.Type == "stored")
                {
                    state.MarkStored(frame);
                    if (desired != null && desired.Stored) RequestSource(null, false);
                    return;
                }
                if (frame.Type == "cancel")
                {
                    if (partial != null && Matches(partial.Frame, frame)) { target = partial; partial = null; QueueCleanup(target); }
                    if (candidate != null && Matches(candidate.Frame, frame)) { target = candidate; candidate = null; QueueCleanup(target); }
                    if (applying != null && Matches(applying.Frame, frame)) QueueCleanup(applying);
                    return;
                }
                if (frame.Type == "skip")
                {
                    if (state.Compare(frame) > 0 && (latestSeen == null || Compare(frame, latestSeen) > 0)) state.Commit(frame);
                    if (latestSeen == null || Compare(frame, latestSeen) > 0) latestSeen = frame;
                    Reconcile(); Control(frame, "ack", true); return;
                }
                if (frame.Type != "text" && frame.Type != "files") return;
                Reconcile();
                if (state.Compare(frame) <= 0 || (latestSeen != null && Compare(frame, latestSeen) < 0))
                { Control(frame, "ack", true, state.Compare(frame) == 0 && Same(frame, lastApplied)); return; }
                if (latestSeen != null && Compare(frame, latestSeen) == 0 && !Same(frame, latestSeen))
                { Control(frame, "ack"); return; }
                if (frame.Type == "text" || begin)
                {
                    if (latestSeen == null || Compare(frame, latestSeen) > 0)
                        latestSeen = new GuestClipboard.Frame { Type = frame.Type, Origin = frame.Origin, Sequence = frame.Sequence, TransferId = frame.TransferId, Text = frame.Text };
                    Reconcile();
                }
                if (frame.Type == "text")
                {
                    if (candidate != null && Same(candidate.Frame, frame)) return;
                    if (applying != null && Same(applying.Frame, frame)) return;
                    if (partial != null) { var old = partial; partial = null; QueueCleanup(old); }
                    if (candidate != null) QueueCleanup(candidate);
                    candidate = new Copy { Frame = frame }; return;
                }
                if (candidate != null && Same(candidate.Frame, frame))
                { if (begin) Control(frame, "stored"); return; }
                if (applying != null && Same(applying.Frame, frame))
                { if (begin) Control(frame, "stored"); return; }
                if (begin)
                {
                    if (partial != null && Same(partial.Frame, frame)) { Control(frame, "ack"); return; }
                    if (partial != null) { var old = partial; partial = null; QueueCleanup(old); }
                    if (candidate != null) { var old = candidate; candidate = null; QueueCleanup(old); }
                    if (cleanupActive || cleanup.Count != 0) { Control(frame, "ack"); return; }
                    target = new Copy { Frame = frame };
                    // Hold Io before publishing the reservation, so cancellation cannot clean an uncreated receiver.
                    Monitor.Enter(target.Io); partial = target;
                }
                else
                {
                    if (partial == null || !Matches(partial.Frame, frame) || partial.Cancelled) return;
                    target = partial;
                }
            }
            bool entered = begin;
            try
            {
                if (!entered) { Monitor.Enter(target.Io); entered = true; }
                if (target.Cancelled) return;
                if (begin) target.Receiver = new ClipboardFiles.Receiver(cache.Parent, frame.TransferId);
                string[] roots = target.Receiver.Accept(frame.Body);
                if (roots != null && !target.Cancelled)
                {
                    target.Ticket = cache.Prepare(target.Receiver);
                    target.Frame = new GuestClipboard.Frame { Type = "files", TransferId = frame.TransferId,
                        Sequence = frame.Sequence, Origin = frame.Origin, Paths = target.Ticket.Roots };
                }
            }
            catch (Exception)
            {
                lock (gate)
                {
                    if (Object.ReferenceEquals(partial, target)) partial = null;
                    Control(frame, "ack"); QueueCleanup(target);
                }
                Safe("FILES_FAILED"); return;
            }
            finally { if (entered) Monitor.Exit(target.Io); }
            lock (gate)
            {
                if (disposed || target.Cancelled || !Object.ReferenceEquals(partial, target)) { QueueCleanup(target); return; }
                if (target.Ticket == null) return;
                partial = null;
                if (Superseded(target.Frame)) { Control(target.Frame, "ack", true); QueueCleanup(target); return; }
                candidate = target; Control(target.Frame, "stored");
            }
            Safe("FILES_READY");
        }

        internal string NextOutgoing()
        {
            lock (gate)
            {
                if (disposed || !connected) return null;
                Reconcile();
                GuestClipboard.FileCopy copy = state.OutgoingFiles;
                if (offered != null && !Object.ReferenceEquals(offered, copy) && !state.IsPending(offered))
                { Control(Header(offered), "cancel"); offered = null; }
                GuestClipboard.Frame remote = ActiveRemote();
                bool remoteNewer = remote != null && state.Compare(remote) > 0;
                if (copy != null) offered = copy;
                if (copy == null || copy.Stored || remoteNewer) RequestSource(null, false);
                else
                {
                    RequestSource(copy, false);
                    if (ready == null && !pumping && clock.ElapsedMilliseconds >= filesDue) { pump = true; Monitor.PulseAll(gate); }
                }
                if (controls.Count != 0) return controls.Dequeue();
                if (ready != null)
                {
                    string result = ready.Line;
                    if (ready.End) filesDue = clock.ElapsedMilliseconds + 1000;
                    bool end = ready.End;
                    ready = null;
                    if (!end && desired != null && !pumping) { pump = true; Monitor.PulseAll(gate); }
                    return result;
                }
                string text = state.Outgoing;
                if (text == null || remoteNewer) return null;
                if (!Object.ReferenceEquals(text, lastText)) { lastText = text; textDue = 0; }
                if (clock.ElapsedMilliseconds < textDue) return null;
                textDue = clock.ElapsedMilliseconds + 1000; return text;
            }
        }

        // Called immediately before native EmptyClipboard, after potentially slow backup work.
        internal bool IsCurrent(GuestClipboard.Frame frame)
        {
            lock (gate)
                return !disposed && applying != null && applying.InNative && !applying.Cancelled &&
                    Object.ReferenceEquals(applying.Frame, frame) && state.Compare(frame) > 0 &&
                    (latestSeen == null || Compare(frame, latestSeen) > 0 || Same(frame, latestSeen));
        }

        internal bool ApplyLatest(Func<GuestClipboard.Frame, bool> nativePublish)
        {
            if (nativePublish == null) throw new ArgumentNullException("nativePublish");
            Copy copy;
            lock (gate)
            {
                if (disposed || applying != null) return false;
                Reconcile(); copy = candidate;
                if (copy == null) return false;
                if (state.Compare(copy.Frame) <= 0)
                {
                    candidate = null; Control(copy.Frame, "ack", true, Same(copy.Frame, lastApplied));
                    QueueCleanup(copy); return false;
                }
                candidate = null; applying = copy; copy.InNative = true;
            }
            bool published = false;
            try { published = nativePublish(copy.Frame); }
            catch (Exception) { Safe("CLIPBOARD_BUSY"); }
            if (published)
            {
                lock (gate) copy.Published = true;
                // Native success can race a new logical version or Dispose: its paths are already paste sources.
                try { if (copy.Ticket != null) copy.Ticket.Park(); }
                catch (Exception) { Safe("FILES_RETAINED"); }
                state.Commit(copy.Frame);
            }
            lock (gate)
            {
                copy.InNative = false; applying = null;
                if (published)
                { lastApplied = copy.Frame; Control(copy.Frame, "ack", true, true); }
                else if (disposed || copy.Cancelled || Superseded(copy.Frame) ||
                    (candidate != null && Compare(candidate.Frame, copy.Frame) >= 0) || (partial != null && Compare(partial.Frame, copy.Frame) >= 0))
                { Control(copy.Frame, "ack", true); QueueCleanup(copy); }
                else candidate = copy;
                Monitor.PulseAll(gate);
            }
            if (!published) Safe("CLIPBOARD_BUSY");
            return published;
        }

        internal void Reconnected(bool value)
        {
            lock (gate)
            {
                if (disposed) return;
                connected = value; controls.Clear(); RequestSource(null, false); ready = null;
                if (!value && partial != null) { var old = partial; partial = null; QueueCleanup(old); }
                if (value) { state.Resend(); lastText = null; textDue = filesDue = 0; }
            }
        }
        void Work()
        {
            IEnumerator<Dictionary<string, object>> iterator = null;
            GuestClipboard.FileCopy sending = null;
            long generation = -1;
            try
            {
                while (true)
                {
                    Copy[] discarded = null;
                    GuestClipboard.FileCopy requested;
                    long token;
                    bool advance;
                    lock (gate)
                    {
                        while (cleanup.Count == 0 && !pump && generation == epoch && !(disposed && applying == null)) Monitor.Wait(gate);
                        if (cleanup.Count != 0)
                        {
                            discarded = cleanup.ToArray(); cleanup.Clear(); cleanupActive = true;
                            PeakCleanupWorkers = Math.Max(PeakCleanupWorkers, 1);
                        }
                        requested = desired; token = epoch; advance = discarded == null && pump && ready == null;
                        if (advance) { pump = false; pumping = true; }
                        if (disposed && applying == null && discarded == null && iterator == null) return;
                    }
                    if (discarded != null)
                    {
                        foreach (Copy copy in discarded)
                        {
                            try
                            {
                                if (BeforeCleanup != null) BeforeCleanup();
                                lock (copy.Io)
                                    if (!copy.Published)
                                    { if (copy.Ticket != null) copy.Ticket.Abort(); else if (copy.Receiver != null) copy.Receiver.Abort(); }
                            }
                            catch (Exception) { Safe("CLEANUP_FAILED"); }
                        }
                        lock (gate) { cleanupActive = false; Monitor.PulseAll(gate); }
                        continue;
                    }
                    if (generation != token || !Object.ReferenceEquals(sending, requested))
                    {
                        if (iterator != null) iterator.Dispose(); iterator = null; sending = requested; generation = token;
                    }
                    if (disposed && requested == null) continue;
                    if (!advance || requested == null) continue;
                    try
                    {
                        if (!state.IsPending(requested) || requested.Stored) continue;
                        if (iterator == null) iterator = new ClipboardFiles.Sender(requested.Paths, requested.Id).Frames().GetEnumerator();
                        if (!iterator.MoveNext()) { iterator.Dispose(); iterator = null; continue; }
                        var body = iterator.Current;
                        string line = GuestClipboard.Serialize(new Dictionary<string, object> { { "v", 2 }, { "n", nonce }, { "t", "files" },
                            { "s", requested.Sequence }, { "o", requested.Origin }, { "b", body } });
                        if (line == null) throw new InvalidOperationException("File frame cannot be serialized");
                        bool end = body["event"] as string == "end";
                        if (end) { iterator.Dispose(); iterator = null; }
                        lock (gate)
                            if (!disposed && connected && token == epoch && Object.ReferenceEquals(desired, requested) && state.IsPending(requested) && !requested.Stored)
                                ready = new Outgoing { Line = line, End = end };
                    }
                    catch (Exception)
                    {
                        if (iterator != null) iterator.Dispose(); iterator = null;
                        lock (gate)
                        {
                            if (token == epoch && state.IsPending(requested))
                            { state.CancelPending(requested); Control(Header(requested), "cancel"); offered = null; RequestSource(null, false); }
                        }
                        Safe("FILES_FAILED");
                    }
                    finally { lock (gate) { pumping = false; Monitor.PulseAll(gate); } }
                }
            }
            finally { if (iterator != null) iterator.Dispose(); }
        }
        public void Dispose()
        {
            lock (gate)
            {
                if (disposed) return;
                disposed = true; connected = false; controls.Clear(); ready = null; RequestSource(null, false);
                if (partial != null) { QueueCleanup(partial); partial = null; }
                if (candidate != null) { QueueCleanup(candidate); candidate = null; }
                if (applying != null) QueueCleanup(applying);
                Monitor.PulseAll(gate);
            }
        }
    }
}
