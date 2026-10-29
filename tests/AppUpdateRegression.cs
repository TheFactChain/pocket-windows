using System;
using System.IO;
using System.Threading;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Web.Script.Serialization;
using WindowsClaude.Launcher;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

internal static class AppUpdateRegression
{
    sealed class FixtureServer : IDisposable
    {
        readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        readonly Thread thread;
        internal readonly Dictionary<string, byte[]> Files = new Dictionary<string, byte[]>();
        internal string Url;
        internal FixtureServer()
        {
            listener.Start(); Url = "http://127.0.0.1:" + ((IPEndPoint)listener.LocalEndpoint).Port;
            thread = new Thread(delegate()
            {
                try
                {
                    while (true) using (var client = listener.AcceptTcpClient()) using (var stream = client.GetStream())
                    {
                        var reader = new StreamReader(stream, Encoding.ASCII, false, 1024, true);
                        string request = reader.ReadLine(), header;
                        if (request == null) continue;
                        while (!String.IsNullOrEmpty(header = reader.ReadLine())) { }
                        byte[] body; string path = request.Split(' ')[1];
                        bool found = Files.TryGetValue(path, out body); if (!found) body = Encoding.UTF8.GetBytes("missing");
                        byte[] prefix = Encoding.ASCII.GetBytes("HTTP/1.1 " + (found ? "200 OK" : "404 Not Found") + "\r\nContent-Length: " + body.Length + "\r\nConnection: close\r\n\r\n");
                        stream.Write(prefix, 0, prefix.Length); stream.Write(body, 0, body.Length);
                    }
                }
                catch (SocketException) { } catch (ObjectDisposedException) { }
            });
            thread.IsBackground = true;
        }
        internal void Start() { thread.Start(); }
        public void Dispose() { listener.Stop(); if (thread.IsAlive) thread.Join(2000); }
    }
    static string Hash(byte[] bytes)
    { using (var hash = System.Security.Cryptography.SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant(); }
    static void LongPaths()
    {
        string root = Path.Combine(Path.GetDirectoryName(typeof(AppUpdateRegression).Assembly.Location), "long-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(root);
            string deep = root;
            while (deep.Length < 280) deep = Path.Combine(deep, new string('d', 32));
            string json = Path.Combine(root, new string('j', 250 - root.Length - 6) + ".json");
            AppUpdate.AtomicJson(json, new Dictionary<string, object> { { "ok", true } });
            Assert(File.ReadAllText(json).Contains("true"), "Atomic write does not extend an otherwise supported path beyond MAX_PATH");
            string zip = Path.Combine(root, "toolkit.zip"), target = Path.Combine(deep, "kit");
            using (var archive = ZipFile.Open(zip, ZipArchiveMode.Create))
                foreach (string name in new[] { "py/python.exe", "scripts/app_update_v2.py", "scripts/release_portable.py", "py/Lib/encodings/fixture.py" })
                    using (var writer = new StreamWriter(archive.CreateEntry(name).Open())) writer.Write("fixture");
            try { AppUpdate.ExtractToolkit(zip, target, CancellationToken.None); throw new Exception("Unsupported toolkit path accepted"); }
            catch (InvalidDataException error) { Assert(error.Message.Contains("ближе к корню"), "Unsupported toolkit path has a clear remedy before extraction"); }
            Assert(!Directory.Exists(deep), "Unsupported toolkit root is rejected before writing files");
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }
    static void BootstrapWithoutInstalledPython()
    {
        string root = Path.Combine(Path.GetDirectoryName(typeof(AppUpdateRegression).Assembly.Location), "bootstrap-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "runtime", "python-3.12.14"));
        File.WriteAllText(Path.Combine(root, "runtime", "python-3.12.14", "python.exe"), "broken installed interpreter");
        string previous = Environment.GetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API");
        try
        {
            using (var server = new FixtureServer())
            {
                string id = "2026.10.27.1", kitName = "Pocket-Windows-" + id + "-updater.zip";
                byte[] kit;
                using (var output = new MemoryStream())
                {
                    using (var zip = new ZipArchive(output, ZipArchiveMode.Create, true))
                    {
                        using (var stream = zip.CreateEntry("py/python.exe").Open()) { byte[] exe = File.ReadAllBytes(typeof(AppUpdateRegression).Assembly.Location); stream.Write(exe, 0, exe.Length); }
                        foreach (string name in new[] { "scripts/app_update_v2.py", "scripts/release_portable.py" }) using (var writer = new StreamWriter(zip.CreateEntry(name).Open())) writer.Write("fixture");
                    }
                    kit = output.ToArray();
                }
                var index = new Dictionary<string, object> {
                    { "schema", 2 }, { "min_updater", 2 }, { "release", new Dictionary<string, object> { { "schema", 1 }, { "release_id", id }, { "source_commit", new string('b', 40) } } },
                    { "archive", new Dictionary<string, object> { { "url", server.Url + "/Pocket-Windows-" + id + "-chunks.zip" }, { "bytes", 99 }, { "sha256", new string('c', 64) } } },
                    { "toolkit", new Dictionary<string, object> { { "url", server.Url + "/" + kitName }, { "bytes", kit.Length }, { "sha256", Hash(kit) } } },
                    { "files", new object[0] }, { "chunks", new object[0] }, { "gateway", new Dictionary<string, object>() } };
                byte[] json = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(index));
                server.Files.Add("/index.json", json); server.Files.Add("/index.sha256", Encoding.ASCII.GetBytes(Hash(json)));
                server.Files.Add("/" + kitName, kit); server.Files.Add("/kit.sha256", Encoding.ASCII.GetBytes(Hash(kit)));
                byte[] fullBytes = Encoding.UTF8.GetBytes("full-fixture");
                server.Files.Add("/full.zip", fullBytes); server.Files.Add("/full.sha256", Encoding.ASCII.GetBytes(Hash(fullBytes)));
                Environment.SetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API", server.Url + "/release"); server.Start();
                var offer = new AppUpdate.Offer { V2 = true, Id = id, ZipUrl = server.Url + "/full.zip", ShaUrl = server.Url + "/full.sha256", Bytes = fullBytes.Length, ToolkitUrl = server.Url + "/" + kitName, ToolkitShaUrl = server.Url + "/kit.sha256", ToolkitBytes = kit.Length, IndexUrl = server.Url + "/index.json", IndexShaUrl = server.Url + "/index.sha256" };
                bool bootstrap = false;
                var package = AppUpdate.PrepareOfferAsync(root, offer, value => { if (value["phase"].Equals("bootstrap")) bootstrap = true; }, CancellationToken.None).GetAwaiter().GetResult();
                Assert(bootstrap && package.DownloadBytes == 17 && package.RequiredFreeBytes == 1234 && package.Stage != null, "One action uses toolkit despite broken installed Python and completes plan plus prepare");
                AppUpdate.Discard(package);
                var fullPackage = AppUpdate.PrepareDownloadAsync(root, offer, true, value => { }, CancellationToken.None).GetAwaiter().GetResult();
                try
                {
                    Assert(File.ReadAllText(Path.Combine(root, "runtime", "python-3.12.14", "python.exe")) == "broken installed interpreter", "Full fallback uses independent toolkit instead of installed Python");
                    Assert(Array.IndexOf(AppUpdate.InstallerArguments(fullPackage), "--catalog") >= 0, "Full fallback keeps the authenticated V2 catalog");
                }
                finally { AppUpdate.Discard(fullPackage); }
                using (var cancel = new CancellationTokenSource())
                {
                    cancel.Cancel();
                    try { AppUpdate.PlanV2Async(root, offer, value => { }, cancel.Token).GetAwaiter().GetResult(); throw new Exception("Cancelled bootstrap succeeded"); }
                    catch (OperationCanceledException) { }
                }
            }
        }
        finally { Environment.SetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API", previous); Directory.Delete(root, true); }
    }
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
    static void RecoveryPaths()
    {
        string original = Path.Combine(Path.GetDirectoryName(typeof(AppUpdateRegression).Assembly.Location), "recovery-" + Guid.NewGuid().ToString("N"));
        string moved = original + "-moved";
        string name = "app-updater-" + Guid.NewGuid().ToString("N");
        Directory.CreateDirectory(Path.Combine(original, "state", name, "scripts"));
        File.WriteAllText(Path.Combine(original, "state", name, "scripts", "release_portable.py"), "fixture");
        Directory.Move(original, moved);
        try
        {
            foreach (int schema in new[] { 1, 2 })
            {
                var value = new Dictionary<string, object> { { "work", schema == 1 ? Path.Combine(original, "state", name) : name } };
                if (schema == 2) value.Add("schema", 2);
                string marker = Path.Combine(moved, "state", "update-recovery.json");
                File.WriteAllText(marker, new JavaScriptSerializer().Serialize(value));
                if (schema == 1)
                {
                    try { AppUpdate.RecoveryWork(moved); throw new Exception("Moved saved old runner must require external recovery"); }
                    catch (InvalidDataException error) { Assert(error.Message.Contains("новой"), "Explain complete new recovery tools"); }
                    value["work"] = Path.Combine(moved, "state", name);
                    File.WriteAllText(marker, new JavaScriptSerializer().Serialize(value));
                }
                Assert(AppUpdate.RecoveryWork(moved) == Path.Combine(moved, "state", name), "Supported recovery path");
                foreach (string bad in new[] { "../" + name, Path.Combine(original, name), name + "/scripts", "app-updater-invalid" })
                {
                    value["work"] = bad;
                    File.WriteAllText(marker, new JavaScriptSerializer().Serialize(value));
                    try { AppUpdate.RecoveryWork(moved); throw new Exception("Unsafe recovery work accepted"); }
                    catch (InvalidDataException) { }
                }
            }
        }
        finally { Directory.Delete(moved, true); }
    }
    static void V2Contracts()
    {
        try { AppUpdate.ParseProgress("{\"phase\":\"download\",\"done_bytes\":-1,\"total_bytes\":4}"); throw new Exception("Negative byte count accepted"); }
        catch (InvalidDataException) { }
        Assert(AppUpdate.ParseProgress("{\"phase\":\"planned\",\"download_bytes\":0,\"required_free_bytes\":42}")["phase"].Equals("planned"), "V2 terminal JSON");
        string root = Path.Combine(Path.GetDirectoryName(typeof(AppUpdateRegression).Assembly.Location), "probe-" + Guid.NewGuid().ToString("N"));
        string nonce = new string('a', 32), commit = new string('b', 40);
        Directory.CreateDirectory(Path.Combine(root, "state")); Directory.CreateDirectory(Path.Combine(root, "logs"));
        try
        {
            Assert(!AppUpdate.ValidProbe(root, nonce), "An argv alone cannot bypass the update journal");
            string stage = AppUpdate.NewStage(root);
            Assert(Path.GetDirectoryName(stage) == Path.Combine(root, "state") && System.Text.RegularExpressions.Regex.IsMatch(Path.GetFileName(stage), "\\A[a-f0-9]{32}\\z"), "Prepared stage is a direct state UUID child");
            Assert(!AppUpdate.HasV2Catalog(root), "Legacy copies without a catalog retain component preparation");
            var release = new Dictionary<string, object> { { "schema", 1 }, { "release_id", "2026.10.27.1" }, { "source_commit", commit } };
            AppUpdate.AtomicJson(Path.Combine(root, "release-info.json"), release);
            using (var current = Process.GetCurrentProcess())
            {
                var probe = new Dictionary<string, object> { { "nonce", nonce }, { "pid", current.Id }, { "created", current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() } };
                var journal = new Dictionary<string, object> { { "schema", 3 }, { "phase", "awaiting_probe" }, { "target", root }, { "release", release }, { "probe", probe } };
                string path = Path.Combine(root, "state", "app-update.json"); AppUpdate.AtomicJson(path, journal);
                Assert(AppUpdate.ValidProbe(root, nonce), "Journal process identity permits probe");
                Assert(!AppUpdate.ValidProbe(root, new string('c', 32)), "Wrong nonce blocked");
                probe["created"] = "0"; AppUpdate.AtomicJson(path, journal); Assert(!AppUpdate.ValidProbe(root, nonce), "PID reuse blocked");
                probe["created"] = current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(); AppUpdate.AtomicJson(path, journal);
                AppUpdate.AckProbe(root, nonce); Assert(File.Exists(Path.Combine(root, "state", "update-probe.json")), "Atomic loaded ACK");
                var receipt = new Dictionary<string, object> { { "schema", 1 }, { "nonce", nonce }, { "root", root }, { "committed", true }, { "release_id", release["release_id"] }, { "source_commit", commit } };
                AppUpdate.AtomicJson(Path.Combine(root, "state", "update-accepted.json"), receipt);
                Assert(!AppUpdate.ProbeCommitted(root, nonce), "Receipt alone cannot release gate");
                using (var deadline = new CancellationTokenSource())
                {
                    deadline.Cancel();
                    try { AppUpdate.WaitForProbeCommitAsync(root, nonce, deadline.Token).GetAwaiter().GetResult(); throw new Exception("Probe wait ignored deadline"); }
                    catch (OperationCanceledException) { Assert(File.Exists(path), "Probe deadline leaves journal for recovery"); }
                }
                File.Delete(path); Assert(AppUpdate.ProbeCommitted(root, nonce), "Journal removed and matching receipt release gate");
                Assert(!AppUpdate.ProbeCommitted(root, new string('d', 32)), "Receipt from another probe cannot release gate");
                receipt["source_commit"] = new string('c', 40); AppUpdate.AtomicJson(Path.Combine(root, "state", "update-accepted.json"), receipt);
                Assert(!AppUpdate.ProbeCommitted(root, nonce), "Stale receipt cannot release gate");
            }
            var fallback = AppUpdate.FullOffer(new AppUpdate.Offer { V2 = true, IndexUrl = "index", ToolkitUrl = "kit", ZipUrl = "archive", Bytes = 42 });
            Assert(!fallback.V2 && fallback.IndexUrl == null && fallback.ZipUrl == "archive" && fallback.Bytes == 42, "Explicit fallback excludes differential download");
            var fullArgs = AppUpdate.InstallerArguments(new AppUpdate.Package { Zip = "full.zip", Sha = "fullsha", Id = "id", Index = "catalog.json", IndexSha = "indexsha" });
            Assert(Array.IndexOf(fullArgs, "--catalog") >= 0 && Array.IndexOf(fullArgs, "catalog.json") >= 0 && Array.IndexOf(fullArgs, "--probe") >= 0, "V2 full fallback forwards the verified catalog into the probed transaction");
            string zip = Path.Combine(root, "toolkit.zip");
            using (var archive = ZipFile.Open(zip, ZipArchiveMode.Create)) using (var writer = new StreamWriter(archive.CreateEntry("py/../escape").Open())) writer.Write("bad");
            try { AppUpdate.ExtractToolkit(zip, Path.Combine(root, "extract"), CancellationToken.None); throw new Exception("Unsafe toolkit path accepted"); }
            catch (InvalidDataException) { }
            string savedTools = Path.Combine(root, "state", "app-updater-" + Guid.NewGuid().ToString("N"));
            string work = Path.Combine(root, "state", "app-updater-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(savedTools, "scripts")); Directory.CreateDirectory(Path.Combine(savedTools, "py")); Directory.CreateDirectory(work);
            File.WriteAllText(Path.Combine(savedTools, "scripts", "release_portable.py"), "saved"); File.WriteAllText(Path.Combine(savedTools, "py", "python.exe"), "bad");
            AppUpdate.Discard(new AppUpdate.Package { Root = root, Tools = savedTools, Work = work, PreserveTools = true });
            Assert(!Directory.Exists(work) && Directory.Exists(savedTools), "Offline cancellation preserves saved toolkit and removes only new work");
            var catalog = new Dictionary<string, object> { { "schema", 2 }, { "release", release } };
            byte[] catalogBytes = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(catalog)); string catalogSha = Hash(catalogBytes);
            Directory.CreateDirectory(Path.Combine(root, "state", "components", "catalogs"));
            File.WriteAllBytes(Path.Combine(root, "state", "components", "catalogs", catalogSha + ".json"), catalogBytes);
            AppUpdate.AtomicJson(Path.Combine(root, "state", "components", "current.json"), new Dictionary<string, object> { { "schema", 2 }, { "catalog", catalogSha + ".json" }, { "sha256", catalogSha }, { "release_id", release["release_id"] } });
            Assert(AppUpdate.HasV2Catalog(root), "V2 repair uses the saved catalog");
            AppUpdate.AtomicJson(Path.Combine(root, "state", "update-recovery.json"), new Dictionary<string, object> { { "schema", 2 }, { "work", Path.GetFileName(savedTools) } });
            AppUpdate.AtomicJson(Path.Combine(savedTools, "toolkit-manifest.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", release["release_id"] }, { "source_commit", commit }, { "files", new object[] { new Dictionary<string, object> { { "path", "py/python.exe" }, { "bytes", 3 }, { "sha256", new string('0', 64) } } } } });
            try { AppUpdate.PlanOfflineAsync(root, value => { }, CancellationToken.None).GetAwaiter().GetResult(); throw new Exception("Corrupt offline toolkit executed"); }
            catch (InvalidDataException error) { Assert(error.Message.Contains("повреждён"), "Corrupt offline toolkit fails before execution and network"); }
            string goodTools = Path.Combine(root, "state", "app-updater-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(goodTools, "py")); Directory.CreateDirectory(Path.Combine(goodTools, "scripts"));
            File.Copy(typeof(AppUpdateRegression).Assembly.Location, Path.Combine(goodTools, "py", "python.exe"));
            File.WriteAllText(Path.Combine(goodTools, "scripts", "app_update_v2.py"), "offlinefixture");
            File.WriteAllText(Path.Combine(goodTools, "scripts", "release_portable.py"), "fixture");
            var toolkitRows = new List<object>();
            foreach (string name in new[] { "py/python.exe", "scripts/app_update_v2.py", "scripts/release_portable.py" })
            {
                byte[] file = File.ReadAllBytes(Path.Combine(goodTools, name.Replace('/', Path.DirectorySeparatorChar)));
                toolkitRows.Add(new Dictionary<string, object> { { "path", name }, { "bytes", file.Length }, { "sha256", Hash(file) } });
            }
            AppUpdate.AtomicJson(Path.Combine(goodTools, "toolkit-manifest.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", release["release_id"] }, { "source_commit", commit }, { "files", toolkitRows.ToArray() } });
            AppUpdate.AtomicJson(Path.Combine(savedTools, "toolkit-manifest.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", "2026.10.28.1" }, { "source_commit", new string('d', 40) }, { "files", new object[0] } });
            var savedOffer = AppUpdate.SavedOffer(root);
            Assert(savedOffer != null && savedOffer.Offline && savedOffer.Repair, "Saved catalog offers repair without network metadata");
            bool preparedInOneAction = false;
            var offline = AppUpdate.PrepareOfferAsync(root, savedOffer, value => { if (value["phase"].Equals("prepared")) preparedInOneAction = true; }, CancellationToken.None).GetAwaiter().GetResult();
            Assert(offline.Tools == goodTools && offline.DownloadBytes == 0, "Rolled-back catalog A selects verified toolkit A despite marker B without network");
            Assert(preparedInOneAction && offline.Stage != null && File.Exists(Path.Combine(offline.Stage, "prepared.json")), "One update action plans and prepares offline repair without a second click");
            Assert(AppUpdate.RecoveryWork(root) == savedTools, "Offline planning preserves recovery marker B");
            AppUpdate.Discard(offline);
        }
        finally { Directory.Delete(root, true); }
    }
    static int Main(string[] args)
    {
        try
        {
            if (args.Length > 5 && args[0] == "-X")
            {
                Console.OutputEncoding = new UTF8Encoding(false);
                if (args[5] == "prepare")
                {
                    string stage = args[Array.IndexOf(args, "--stage") + 1], indexPath = args[Array.IndexOf(args, "--index") + 1], digest = args[Array.IndexOf(args, "--index-sha256") + 1];
                    Directory.CreateDirectory(stage); File.Copy(indexPath, Path.Combine(stage, "index.json")); File.WriteAllText(Path.Combine(stage, "prepared.json"), "{}");
                    Console.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object> { { "phase", "prepared" }, { "stage", stage }, { "index_sha256", digest }, { "downloaded_bytes", 0 } }));
                }
                else Console.WriteLine("{\"phase\":\"planned\",\"download_bytes\":" + (File.ReadAllText(args[4]) == "offlinefixture" ? "0" : "17") + ",\"required_free_bytes\":1234}");
                return 0;
            }
            V2Contracts();
            LongPaths();
            if (args.Length == 1 && args[0] == "v2-bootstrap") { BootstrapWithoutInstalledPython(); return 0; }
            Assert(!AppUpdate.Newer("99999999999999999999999.1", "2026.10.24.13"), "Overflow must be invalid, not throw");
            Assert(AppUpdate.Newer("2026.10.25.1", "2026.10.24.13"), "New date ordering");
            Assert(!AppUpdate.Newer("2026.10.24.13.0", "2026.10.24.13"), "Trailing zero equality");
            if (args.Length == 0) return 0;
            if (args[1] == "recovery") { RecoveryPaths(); return 0; }
            Environment.SetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API", args[0] + "/release");
            var result = AppUpdate.CheckAsync("2026.10.24.13").GetAwaiter().GetResult();
            if (args[1] == "unavailable") { Assert(result.Offer == null && result.Message.Contains("пакет"), "Missing asset must be explained"); return 0; }
            Assert(result.Offer != null && result.Offer.Id == "2026.10.25.1", "Expected complete update offer");
            bool indexed = args[1].StartsWith("indexed-", StringComparison.Ordinal);
            Assert((result.Offer.IndexUrl != null) == indexed, "Index assets must be offered as a complete pair");
            var cancel = new CancellationTokenSource();
            if (args[1] == "cancel") cancel.Cancel();
            if (args[1] == "indexed-cancel") cancel.CancelAfter(500);
            try
            {
                var package = AppUpdate.DownloadAsync(result.Offer, delegate(int percent) { Assert(percent >= 0 && percent <= 100, "progress range"); }, cancel.Token, args.Length > 2 ? args[2] : null).GetAwaiter().GetResult();
                Assert(args[1] == "valid" || args[1] == "index-missing" || args[1] == "indexed-valid", "Bad/cancelled download must not succeed");
                Assert(File.ReadAllText(package.Zip) == "test package", "Downloaded bytes");
                Assert(File.Exists(Path.Combine(package.Work, "download.json")), "Download work has an ownership marker for archive cleanup");
                AppUpdate.Discard(package);
            }
            catch (OperationCanceledException) { Assert(args[1] == "cancel" || args[1] == "indexed-cancel", "Unexpected cancellation"); }
            catch (InvalidDataException) { Assert(args[1] == "corrupt" || args[1] == "size" || indexed && args[1] != "indexed-valid", "Unexpected validation failure"); }
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
