using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace WindowsClaude.Launcher
{
    internal static class AppUpdate
    {
        const string ManifestName = "update-manifest.json";
        const int ManifestSchema = 1;
        // Hosts are only transport; a signed manifest may replace this list (state/update-feeds.json), config/update-sources.json overrides it.
        static readonly string[] BuiltInFeeds = {
            "https://github.com/TheFactChain/pocket-windows/releases/latest/download/" + ManifestName,
            "https://188.130.234.243:8443/pocket-windows/" + ManifestName };
        // The publisher is these ECDSA P-256 keys (CNG ECCPUBLIC blobs), never a URL; scripts/update_sources.json lists the same keys.
        internal static readonly List<string> TrustedKeys = new List<string> {
            "RUNTMSAAAADdjP9TMHcJC1yzo+EMu+3h5v1DoWszB9oUdg5Q0Ia7oovhxPD7lBXBtq+AV8kRzLUmALZKv/5pmGAyVC2eetVx",
            "RUNTMSAAAAAc7ESHeltKr8P7wsTW09Sxg3BQTxKFbQyLR3tiNeM4Yn737ZdvxAjyEpl15JAt1aN28RNYsH8fOh6f3pUZRalx" };
        const long MaxPackage = 8L * 1024 * 1024 * 1024;
        internal sealed class Offer { public string Id, ZipUrl, Sha, IndexUrl, IndexSha, ToolkitUrl, ToolkitSha; public long Bytes, ToolkitBytes; public bool V2, Repair, Offline; }
        internal sealed class CheckResult { public Offer Offer; public string Title, Message; }
        internal sealed class Package { public string Zip, Sha, Id, Work, Tools, Root, Index, IndexSha, Stage; public long DownloadBytes, RequiredFreeBytes; public bool Offline, PreserveTools; }

        // Loopback feed for tests only; it never weakens the signature check.
        static string TestFeed
        {
            get
            {
                string value = Environment.GetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API");
                Uri uri;
                return Uri.TryCreate(value, UriKind.Absolute, out uri) && uri.Scheme == "http" && uri.Host == "127.0.0.1" ? value : null;
            }
        }

        static HttpWebRequest Open(string url)
        {
            ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = "PocketWindows-Launcher";
            request.Timeout = 15000; request.ReadWriteTimeout = 30000;
            return request;
        }

        // Any HTTPS host is acceptable: size and SHA-256 from the signed manifest authenticate the bytes.
        static bool AssetUrl(string url)
        {
            Uri uri;
            if (!Uri.TryCreate(url, UriKind.Absolute, out uri) || uri.UserInfo.Length != 0 || uri.Fragment.Length != 0) return false;
            return uri.Scheme == "https" || TestFeed != null && uri.Scheme == "http" && uri.Host == "127.0.0.1";
        }

        internal static bool Newer(string candidate, string current)
        {
            var a = Parts(candidate); var b = Parts(current);
            if (a == null || b == null) return false;
            for (int i = 0; i < Math.Max(a.Length, b.Length); i++)
            {
                long x = i < a.Length ? a[i] : 0, y = i < b.Length ? b[i] : 0;
                if (x != y) return x > y;
            }
            return false;
        }

        static long[] Parts(string id)
        {
            if (id == null || !Regex.IsMatch(id, @"\A[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,8}\z")) return null;
            var text = id.Split('.'); var parts = new long[text.Length];
            for (int i = 0; i < text.Length; i++) if (!Int64.TryParse(text[i], out parts[i])) return null;
            return parts;
        }

        static async Task<string> ReadText(string url, int maximum, CancellationToken cancel)
        {
            var request = Open(url);
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel))
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(30));
                using (deadline.Token.Register(request.Abort))
                using (var response = await request.GetResponseAsync().ConfigureAwait(false))
                using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                {
                    var text = new StringBuilder(); var buffer = new char[4096]; int read;
                    while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                    {
                        if (text.Length + read > maximum) throw new InvalidDataException("Ответ сервера слишком большой.");
                        text.Append(buffer, 0, read);
                    }
                    return text.ToString();
                }
            }
        }

        static async Task<byte[]> ReadBytes(string url, int maximum, CancellationToken cancel)
        {
            var request = Open(url);
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel))
            using (var output = new MemoryStream())
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(30));
                using (deadline.Token.Register(request.Abort))
                using (var response = await request.GetResponseAsync().ConfigureAwait(false))
                using (var input = response.GetResponseStream())
                {
                    if (response.ContentLength > maximum) throw new InvalidDataException("Индекс обновления слишком большой.");
                    byte[] buffer = new byte[4096]; int read;
                    while ((read = await input.ReadAsync(buffer, 0, buffer.Length, deadline.Token).ConfigureAwait(false)) > 0)
                    {
                        if (output.Length + read > maximum) throw new InvalidDataException("Индекс обновления слишком большой.");
                        output.Write(buffer, 0, read);
                    }
                }
                return output.ToArray();
            }
        }

        internal static async Task<CheckResult> CheckAsync(string currentId, string root = null)
        {
            if (Parts(currentId) == null) return new CheckResult { Title = "Версия не определена", Message = "У этой копии не указана версия выпуска. Обновление доступно в готовом комплекте." };
            root = root ?? Bridge.Root;
            Offer best = null; List<string> feeds = null; var errors = new List<string>(); bool unsupported = false;
            foreach (string feed in Feeds(root))
            {
                try
                {
                    var manifest = OpenManifest(await ReadText(feed, 1024 * 1024, CancellationToken.None).ConfigureAwait(false));
                    var offer = ReadManifest(manifest);
                    if (best == null || Newer(offer.Id, best.Id)) { best = offer; feeds = UrlList(manifest, "feeds").ToList(); }
                }
                catch (NotSupportedException error) { unsupported = true; errors.Add(feed + ": " + error.Message); }
                catch (Exception error) { if (error is WebException || error is IOException || error is InvalidDataException) errors.Add(feed + ": " + error.Message); else throw; }
            }
            Log(root, errors);
            if (best == null)
                return new CheckResult { Title = "Не удалось проверить обновления", Message = unsupported
                    ? "Новый выпуск требует ручной загрузки полного комплекта со страницы проекта."
                    : "Ни один источник обновлений не ответил подписанным манифестом. Проверь подключение к интернету. Подробности в logs/update-check.txt." };
            // A stale mirror or a replayed old manifest must not rewrite where the next check looks.
            if (feeds.Count > 0 && !Newer(currentId, best.Id))
                try { Directory.CreateDirectory(Path.Combine(root, "state")); AtomicJson(Path.Combine(root, "state", "update-feeds.json"), new Dictionary<string, object> { { "schema", 1 }, { "feeds", feeds.ToArray() } }); }
                catch (IOException) { } catch (UnauthorizedAccessException) { }
            if (!Newer(best.Id, currentId) && best.Id != currentId) return new CheckResult { Title = "Установлена последняя версия", Message = "Версия " + currentId + ". Новых обновлений пока нет." };
            best.Repair = best.Id == currentId;
            return new CheckResult { Offer = best, Title = best.Repair ? "Установлена последняя версия" : "Доступно обновление", Message = best.Repair ? "Можно проверить и восстановить файлы программы. Личные данные сохранятся." : "Можно установить новую версию. Личные данные сохранятся." };
        }

        static List<string> Feeds(string root)
        {
            var feeds = new List<string>();
            foreach (string path in new[] { Path.Combine(root, "config", "update-sources.json"), Path.Combine(root, "state", "update-feeds.json") })
                try { feeds.AddRange(UrlList(JsonFile(path, 65536), "feeds")); }
                catch (IOException) { } catch (UnauthorizedAccessException) { } catch (InvalidDataException) { } catch (ArgumentException) { } catch (InvalidOperationException) { }
            if (TestFeed != null) feeds.Add(TestFeed); else feeds.AddRange(BuiltInFeeds);
            return feeds.Distinct(StringComparer.Ordinal).ToList();
        }

        static IEnumerable<string> UrlList(Dictionary<string, object> value, string key)
        {
            object list;
            return value.TryGetValue(key, out list) && list is object[] ? ((object[])list).OfType<string>().Where(AssetUrl).Take(8) : Enumerable.Empty<string>();
        }

        static void Log(string root, List<string> errors)
        {
            try { Directory.CreateDirectory(Path.Combine(root, "logs")); File.WriteAllText(Path.Combine(root, "logs", "update-check.txt"), DateTime.UtcNow.ToString("o") + Environment.NewLine + String.Join(Environment.NewLine, errors), Encoding.UTF8); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
        }

        // Envelope {"manifest": base64 bytes, "signature": base64 r||s}: the exact signed bytes travel with the signature in one file.
        internal static Dictionary<string, object> OpenManifest(string text)
        {
            try
            {
                var envelope = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
                string body = Str(envelope, "manifest"), signature = Str(envelope, "signature");
                if (body == null || signature == null) throw new InvalidDataException("Манифест обновления повреждён.");
                byte[] bytes = Convert.FromBase64String(body), sig = Convert.FromBase64String(signature);
                if (bytes.Length > 1024 * 1024 || !TrustedKeys.Any(key => Verify(key, bytes, sig))) throw new InvalidDataException("Подпись манифеста обновления не подтверждена.");
                var manifest = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 }.DeserializeObject(new UTF8Encoding(false, true).GetString(bytes)) as Dictionary<string, object>;
                if (manifest == null) throw new InvalidDataException("Манифест обновления повреждён.");
                return manifest;
            }
            catch (FormatException error) { throw new InvalidDataException("Манифест обновления повреждён.", error); }
            catch (ArgumentException error) { throw new InvalidDataException("Манифест обновления повреждён.", error); }
            catch (InvalidOperationException error) { throw new InvalidDataException("Манифест обновления повреждён.", error); }
        }

        static bool Verify(string key, byte[] data, byte[] signature)
        {
            try { using (var cng = CngKey.Import(Convert.FromBase64String(key), CngKeyBlobFormat.EccPublicBlob)) using (var ecdsa = new ECDsaCng(cng)) return signature.Length == 64 && ecdsa.VerifyData(data, signature); }
            catch (CryptographicException) { return false; } catch (FormatException) { return false; }
        }

        // Unknown fields are ignored; only a higher schema means this launcher cannot read the release.
        internal static Offer ReadManifest(Dictionary<string, object> manifest)
        {
            object schema;
            if (!manifest.TryGetValue("schema", out schema) || !(schema is int) || (int)schema < 1) throw new InvalidDataException("Манифест обновления повреждён.");
            if ((int)schema > ManifestSchema) throw new NotSupportedException("Формат манифеста " + schema + " новее этого лаунчера.");
            var assets = Dict(manifest, "assets");
            var offer = new Offer { Id = Str(manifest, "release_id") };
            if (Parts(offer.Id) == null || assets == null) throw new InvalidDataException("Манифест обновления повреждён.");
            offer.ZipUrl = Asset(Dict(assets, "app"), MaxPackage, out offer.Bytes, out offer.Sha);
            var index = Dict(assets, "update_v2"); var kit = Dict(assets, "updater");
            if (index != null && kit != null)
            {
                long indexBytes;
                offer.IndexUrl = Asset(index, 16 * 1024 * 1024, out indexBytes, out offer.IndexSha);
                offer.ToolkitUrl = Asset(kit, 256L * 1024 * 1024, out offer.ToolkitBytes, out offer.ToolkitSha);
                offer.V2 = true;
            }
            return offer;
        }

        static string Asset(Dictionary<string, object> value, long maximum, out long bytes, out string sha)
        {
            if (value == null) throw new InvalidDataException("В манифесте нет файла обновления.");
            string url = Str(value, "url"); sha = Str(value, "sha256"); bytes = Number(value, "bytes");
            if (!AssetUrl(url) || !Regex.IsMatch(sha ?? "", @"\A[a-f0-9]{64}\z") || bytes <= 0 || bytes > maximum) throw new InvalidDataException("Файл обновления в манифесте повреждён.");
            return url;
        }

        internal static async Task<Package> DownloadAsync(Offer offer, Action<int> progress, CancellationToken cancel, string root = null)
        {
            Package package = null;
            try
            {
                cancel.ThrowIfCancellationRequested();
                string sha = offer.Sha;
                if (!AssetUrl(offer.ZipUrl) || offer.Bytes <= 0 || offer.Bytes > MaxPackage || sha == null || !Regex.IsMatch(sha, @"\A[a-f0-9]{64}\z")) throw new InvalidDataException("Пакет не прошёл проверку.");
                string targetRoot = CheckedRoot(root ?? Bridge.Root);
                string state = Path.Combine(targetRoot, "state"); Directory.CreateDirectory(state);
                if ((File.GetAttributes(targetRoot) & FileAttributes.ReparsePoint) != 0 || (File.GetAttributes(state) & FileAttributes.ReparsePoint) != 0) throw new IOException("Папка обновления оказалась ссылкой.");
                string work = Path.Combine(state, "app-updater-" + Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(work);
                package = new Package { Root = targetRoot, Work = work, Zip = Path.Combine(work, "app.zip"), Sha = sha.ToLowerInvariant(), Id = offer.Id };
                AtomicJson(Path.Combine(work, "download.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", package.Id }, { "sha256", package.Sha } });
                var request = Open(offer.ZipUrl);
                using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel))
                using (timeout.Token.Register(request.Abort))
                using (var hash = SHA256.Create())
                {
                    timeout.CancelAfter(TimeSpan.FromSeconds(45));
                    using (var response = await request.GetResponseAsync().ConfigureAwait(false))
                    using (var input = response.GetResponseStream())
                    using (var output = new FileStream(package.Zip, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        if (response.ContentLength >= 0 && response.ContentLength != offer.Bytes) throw new InvalidDataException("Размер загрузки не совпадает с выпуском.");
                        byte[] buffer = new byte[1 << 16]; long done = 0; int read, last = -1;
                        while ((read = await input.ReadAsync(buffer, 0, buffer.Length, timeout.Token).ConfigureAwait(false)) > 0)
                        {
                            done += read;
                            if (done > offer.Bytes) throw new InvalidDataException("Сервер прислал больше данных, чем указано в выпуске.");
                            await output.WriteAsync(buffer, 0, read, timeout.Token).ConfigureAwait(false);
                            hash.TransformBlock(buffer, 0, read, null, 0);
                            timeout.CancelAfter(TimeSpan.FromSeconds(45));
                            int percent = (int)(done * 100 / offer.Bytes);
                            if (percent != last) { last = percent; progress(percent); }
                        }
                        hash.TransformFinalBlock(new byte[0], 0, 0);
                        if (done != offer.Bytes || BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant() != package.Sha)
                            throw new InvalidDataException("Загрузка повреждена. Повтори обновление.");
                    }
                }
                cancel.ThrowIfCancellationRequested();
                return package;
            }
            catch (Exception)
            {
                Discard(package);
                cancel.ThrowIfCancellationRequested();
                throw;
            }
        }

        static async Task ReadHelperError(StreamReader reader)
        {
            char[] buffer = new char[1024]; int read, total = 0;
            while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                if ((total += read) > 65536) throw new InvalidDataException("Ответ загрузчика слишком большой.");
        }

        static string Digest(string value)
        {
            if (value == null || !Regex.IsMatch(value, @"\A[a-f0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма повреждена.");
            return value;
        }

        internal static Offer FullOffer(Offer offer)
        { return new Offer { Id = offer.Id, ZipUrl = offer.ZipUrl, Sha = offer.Sha, Bytes = offer.Bytes, Repair = offer.Repair }; }

        internal static Offer SavedOffer(string root)
        {
            try
            {
                var pointer = JsonFile(Path.Combine(root, "state", "components", "current.json"), 8192);
                if (Number(pointer, "schema") != 2) return null;
                string id = Str(pointer, "release_id");
                if (Parts(id) == null) throw new InvalidDataException("Каталог восстановления повреждён.");
                return new Offer { Id = id, V2 = true, Repair = true, Offline = true };
            }
            catch (FileNotFoundException) { return null; }
            catch (DirectoryNotFoundException) { return null; }
        }

        internal static async Task<Package> PrepareOfferAsync(string root, Offer offer, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            Package package = null;
            try
            {
                if (offer.Repair || offer.Offline)
                {
                    try
                    {
                        package = await PlanOfflineAsync(root, progress, cancel).ConfigureAwait(false);
                        await PrepareV2Async(package, progress, cancel).ConfigureAwait(false);
                        return package;
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (Exception)
                    {
                        cancel.ThrowIfCancellationRequested();
                        if (offer.Offline) throw;
                        Discard(package); package = null;
                    }
                }
                if (offer.V2)
                {
                    package = await PlanV2Async(root, offer, progress, cancel).ConfigureAwait(false);
                    await PrepareV2Async(package, progress, cancel).ConfigureAwait(false);
                    return package;
                }
                return await PrepareDownloadAsync(root, offer, false, progress, cancel).ConfigureAwait(false);
            }
            catch { Discard(package); cancel.ThrowIfCancellationRequested(); throw; }
        }

        internal static async Task<Package> PrepareDownloadAsync(string root, Offer offer, bool full, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            Package package = null;
            try
            {
                // The full package must stay installable when the release's v2 index or toolkit is unusable: fall back to the installed Python.
                if (full && offer.V2)
                    try { package = await BootstrapV2Async(root, offer, progress, cancel).ConfigureAwait(false); }
                    catch (InvalidDataException error) { Log(root, new List<string> { "v2 toolkit unusable, full package via installed Python: " + error.Message }); }
                    catch (WebException error) { Log(root, new List<string> { "v2 toolkit unusable, full package via installed Python: " + error.Message }); }
                    catch (IOException error) { Log(root, new List<string> { "v2 toolkit unusable, full package via installed Python: " + error.Message }); }
                var downloaded = await DownloadAsync(full ? FullOffer(offer) : offer, percent => progress(new Dictionary<string, object>
                    { { "phase", "downloading" }, { "done_bytes", offer.Bytes * percent / 100 }, { "total_bytes", offer.Bytes } }), cancel, root).ConfigureAwait(false);
                if (package == null) package = downloaded;
                else { package.Work = downloaded.Work; package.Zip = downloaded.Zip; package.Sha = downloaded.Sha; }
                progress(new Dictionary<string, object> { { "phase", "checking" } });
                await PrepareAsync(root, package, cancel).ConfigureAwait(false);
                return package;
            }
            catch { Discard(package); cancel.ThrowIfCancellationRequested(); throw; }
        }

        internal static async Task<Package> PlanV2Async(string root, Offer offer, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            var package = await BootstrapV2Async(root, offer, progress, cancel).ConfigureAwait(false);
            try
            {
                var result = await RunV2Async(package, "plan", progress, cancel).ConfigureAwait(false);
                package.DownloadBytes = Number(result, "download_bytes"); package.RequiredFreeBytes = Number(result, "required_free_bytes");
                return package;
            }
            catch { Discard(package); throw; }
        }

        internal static async Task<Package> BootstrapV2Async(string root, Offer offer, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            if (!offer.V2) throw new InvalidOperationException("Индекс v2 отсутствует.");
            root = CheckedRoot(root);
            string state = Path.Combine(root, "state");
            Directory.CreateDirectory(state);
            foreach (string path in new[] { root, state }) if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new IOException("Папка обновления оказалась ссылкой.");
            var package = new Package { Root = root, Id = offer.Id, Tools = Path.Combine(state, "app-updater-" + Guid.NewGuid().ToString("N")) };
            package.Work = package.Tools;
            try
            {
                Directory.CreateDirectory(package.Tools);
                package.IndexSha = Digest(offer.IndexSha);
                byte[] bytes = await ReadBytes(offer.IndexUrl, 16 * 1024 * 1024, cancel).ConfigureAwait(false);
                using (var hash = SHA256.Create()) if (BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant() != package.IndexSha) throw new InvalidDataException("Индекс обновления повреждён.");
                package.Index = Path.Combine(package.Tools, "index.json"); File.WriteAllBytes(package.Index, bytes);
                var index = JsonFile(package.Index, 16 * 1024 * 1024); var release = Dict(index, "release"); var archive = Dict(index, "archive"); var toolkit = Dict(index, "toolkit");
                // min_updater is the toolkit's contract; the toolkit ships in this same release, so the launcher checks only the fields it reads.
                if (Number(index, "schema") != 2 || release == null || Number(release, "schema") != 1
                    || Str(release, "release_id") != offer.Id || !Regex.IsMatch(Str(release, "source_commit") ?? "", @"\A[a-f0-9]{40}\z")
                    || archive == null || !AssetUrl(Str(archive, "url")) || Number(archive, "bytes") <= 0 || Number(archive, "bytes") > MaxPackage
                    || !Regex.IsMatch(Str(archive, "sha256") ?? "", @"\A[a-f0-9]{64}\z")
                    || toolkit == null || Str(toolkit, "url") != offer.ToolkitUrl || Number(toolkit, "bytes") != offer.ToolkitBytes
                    || !Regex.IsMatch(Str(toolkit, "sha256") ?? "", @"\A[a-f0-9]{64}\z")
                    || !index.ContainsKey("files") || !index.ContainsKey("chunks") || !index.ContainsKey("gateway")) throw new InvalidDataException("Индекс не соответствует выпуску.");
                string kitSha = Digest(offer.ToolkitSha);
                if (kitSha != Str(toolkit, "sha256")) throw new InvalidDataException("Контрольные суммы обновлятора не совпали.");
                var kitOffer = new Offer { Id = offer.Id, ZipUrl = offer.ToolkitUrl, Sha = kitSha, Bytes = offer.ToolkitBytes };
                Package download = null;
                try
                {
                    download = await DownloadAsync(kitOffer, percent => progress(new Dictionary<string, object> { { "phase", "bootstrap" }, { "done_bytes", offer.ToolkitBytes * percent / 100 }, { "total_bytes", offer.ToolkitBytes } }), cancel, root).ConfigureAwait(false);
                    if (download.Sha != kitSha) throw new InvalidDataException("Обновлятор не соответствует индексу.");
                    ExtractToolkit(download.Zip, package.Tools, cancel);
                }
                finally { Discard(download); }
                return package;
            }
            catch { Discard(package); cancel.ThrowIfCancellationRequested(); throw; }
        }

        internal static void ExtractToolkit(string zip, string target, CancellationToken cancel)
        {
            using (var archive = ZipFile.OpenRead(zip))
            {
                if (archive.Entries.Count > 15002) throw new InvalidDataException("Слишком много файлов обновлятора.");
                foreach (var entry in archive.Entries)
                    if (!entry.FullName.EndsWith("/", StringComparison.Ordinal)) CheckUpdatePath(Path.Combine(target, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                long total = 0; var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (var entry in archive.Entries)
                {
                    cancel.ThrowIfCancellationRequested();
                    string name = entry.FullName;
                    if (name.EndsWith("/", StringComparison.Ordinal)) continue;
                    if (name.Contains("\\") || name.Contains(":") || name.StartsWith("/", StringComparison.Ordinal) || name.Split('/').Any(part => part == "." || part == ".." || part.Length == 0 || part.EndsWith(".") || part.EndsWith(" "))
                        || !ToolkitName(name)
                        || !names.Add(name) || ((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000 || entry.Length > 256L * 1024 * 1024 || (total += entry.Length) > 512L * 1024 * 1024)
                        throw new InvalidDataException("Небезопасный файл обновлятора.");
                    string path = Path.GetFullPath(Path.Combine(target, name.Replace('/', Path.DirectorySeparatorChar)));
                    if (!path.StartsWith(Path.GetFullPath(target).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Небезопасный путь обновлятора.");
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using (var input = entry.Open()) using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        byte[] buffer = new byte[65536]; int read; long done = 0;
                        while ((read = input.Read(buffer, 0, buffer.Length)) > 0) { cancel.ThrowIfCancellationRequested(); if ((done += read) > entry.Length) throw new InvalidDataException("Размер файла обновлятора неверен."); output.Write(buffer, 0, read); }
                        if (done != entry.Length) throw new InvalidDataException("Файл обновлятора обрезан.");
                    }
                }
                foreach (string name in new[] { "py/python.exe", "scripts/app_update_v2.py", "scripts/release_portable.py" })
                    if (!names.Contains(name)) throw new InvalidDataException("Неполный комплект обновлятора.");
            }
        }

        static bool ToolkitName(string name)
        { return name == "toolkit-manifest.json" || name == "docs/third-party.md" || name.StartsWith("py/", StringComparison.Ordinal) || name.StartsWith("scripts/", StringComparison.Ordinal) && name.EndsWith(".py", StringComparison.Ordinal); }

        static void VerifyToolkit(string tools, Dictionary<string, object> release, CancellationToken cancel)
        {
            var manifest = JsonFile(Path.Combine(tools, "toolkit-manifest.json"), 16 * 1024 * 1024);
            object rows;
            if (Number(manifest, "schema") != 1 || Str(manifest, "release_id") != Str(release, "release_id") || Str(manifest, "source_commit") != Str(release, "source_commit")
                || !manifest.TryGetValue("files", out rows) || !(rows is object[]) || ((object[])rows).Length > 15000) throw new InvalidDataException("Сохранённый обновлятор не соответствует выпуску. Подготовь обновление через интернет.");
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (object item in (object[])rows)
            {
                cancel.ThrowIfCancellationRequested(); var row = item as Dictionary<string, object>;
                string name = row == null ? null : Str(row, "path");
                if (name == null || !ToolkitName(name) || name.Contains("\\") || name.Contains(":") || name.Split('/').Any(part => part.Length == 0 || part == "." || part == "..") || !names.Add(name)) throw new InvalidDataException("Повреждён список файлов обновлятора.");
                string path = Path.GetFullPath(Path.Combine(tools, name.Replace('/', Path.DirectorySeparatorChar)));
                if (!path.StartsWith(tools.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Небезопасный путь обновлятора.");
                for (var parent = new DirectoryInfo(Path.GetDirectoryName(path)); parent != null && parent.FullName.StartsWith(tools, StringComparison.OrdinalIgnoreCase); parent = parent.Parent)
                    if ((parent.Attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Папка обновлятора оказалась ссылкой.");
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || new FileInfo(path).Length != Number(row, "bytes")) throw new InvalidDataException("Сохранённый обновлятор повреждён. Подготовь обновление через интернет.");
                using (var input = File.OpenRead(path)) using (var hash = SHA256.Create())
                    if (BitConverter.ToString(hash.ComputeHash(input)).Replace("-", "").ToLowerInvariant() != Str(row, "sha256")) throw new InvalidDataException("Сохранённый обновлятор повреждён. Подготовь обновление через интернет.");
            }
            foreach (string name in new[] { "py/python.exe", "scripts/app_update_v2.py", "scripts/release_portable.py" }) if (!names.Contains(name)) throw new InvalidDataException("Сохранённый обновлятор неполон.");
        }

        internal static async Task<Package> PlanOfflineAsync(string root, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            root = CheckedRoot(root);
            var pointer = JsonFile(Path.Combine(root, "state", "components", "current.json"), 8192);
            string sha = Str(pointer, "sha256");
            if (Number(pointer, "schema") != 2 || !Regex.IsMatch(sha ?? "", @"\A[a-f0-9]{64}\z") || Str(pointer, "catalog") != sha + ".json") throw new InvalidDataException("Нет сохранённого каталога восстановления. Проверь обновления через интернет.");
            string source = Path.Combine(root, "state", "components", "catalogs", sha + ".json");
            var index = JsonFile(source, 16 * 1024 * 1024); byte[] bytes = File.ReadAllBytes(source);
            using (var hash = SHA256.Create()) if (BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant() != sha) throw new InvalidDataException("Сохранённый каталог повреждён. Проверь обновления через интернет.");
            var release = Dict(index, "release");
            if (Number(index, "schema") != 2 || release == null || Str(release, "release_id") != Str(pointer, "release_id")) throw new InvalidDataException("Неверный каталог восстановления.");
            string tools = await Task.Run(() => FindToolkit(root, release, cancel), cancel).ConfigureAwait(false);
            var package = new Package { Root = root, Id = Str(release, "release_id"), Tools = tools, IndexSha = sha, Offline = true, PreserveTools = true, Work = Path.Combine(root, "state", "app-updater-" + Guid.NewGuid().ToString("N")) };
            try
            {
                Directory.CreateDirectory(package.Work); package.Index = Path.Combine(package.Work, "index.json"); File.WriteAllBytes(package.Index, bytes);
                var result = await RunV2Async(package, "plan", progress, cancel).ConfigureAwait(false);
                package.DownloadBytes = Number(result, "download_bytes"); package.RequiredFreeBytes = Number(result, "required_free_bytes");
                if (package.DownloadBytes != 0) throw new InvalidDataException("Сохранённых частей недостаточно для восстановления без интернета. Проверь обновления и явно выбери загрузку.");
                return package;
            }
            catch { Discard(package); throw; }
        }

        static string FindToolkit(string root, Dictionary<string, object> release, CancellationToken cancel)
        {
            string state = Path.Combine(root, "state");
            foreach (string path in new[] { root, state }) if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Папка восстановления оказалась ссылкой.");
            var candidates = new List<string>();
            try { candidates.Add(RecoveryWork(root)); } catch (IOException) { } catch (UnauthorizedAccessException) { } catch (ArgumentException) { }
            foreach (string path in Directory.EnumerateDirectories(state).Take(1024))
                if (Regex.IsMatch(Path.GetFileName(path), @"\Aapp-updater-[a-f0-9]{32}\z") && !candidates.Contains(path) && candidates.Count < 128) candidates.Add(path);
            foreach (string tools in candidates)
            {
                cancel.ThrowIfCancellationRequested();
                try
                {
                    if ((File.GetAttributes(tools) & FileAttributes.ReparsePoint) != 0) continue;
                    var manifest = JsonFile(Path.Combine(tools, "toolkit-manifest.json"), 16 * 1024 * 1024);
                    if (Str(manifest, "release_id") != Str(release, "release_id") || Str(manifest, "source_commit") != Str(release, "source_commit")) continue;
                    VerifyToolkit(tools, release, cancel);
                    return tools;
                }
                catch (IOException) { } catch (UnauthorizedAccessException) { } catch (ArgumentException) { } catch (InvalidOperationException) { }
            }
            throw new InvalidDataException("Сохранённый обновлятор отсутствует или повреждён. Подготовь обновление через интернет.");
        }

        internal static async Task PrepareV2Async(Package package, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            package.Stage = NewStage(package.Root);
            var result = await RunV2Async(package, "prepare", progress, cancel).ConfigureAwait(false);
            if (Str(result, "index_sha256") != package.IndexSha || !String.Equals(Path.GetFullPath(Str(result, "stage")), package.Stage, StringComparison.OrdinalIgnoreCase)
                || !File.Exists(Path.Combine(package.Stage, "index.json")) || !File.Exists(Path.Combine(package.Stage, "prepared.json"))) throw new InvalidDataException("Подготовленный пакет не подтверждён.");
        }

        internal static string NewStage(string root)
        { return Path.Combine(CheckedRoot(root), "state", Guid.NewGuid().ToString("N")); }

        const string LongPathMessage = "Путь к папке программы слишком длинный для безопасного обновления. Перенеси папку Pocket Windows ближе к корню диска и повтори попытку. Файлы программы не изменены.";

        static void CheckUpdatePath(string path)
        {
            try
            {
                string full = Path.GetFullPath(path);
                if (full.Length >= 260 || Path.GetDirectoryName(full).Length >= 248) throw new PathTooLongException();
            }
            catch (PathTooLongException error) { throw new InvalidDataException(LongPathMessage, error); }
        }

        static string CheckedRoot(string root)
        {
            CheckUpdatePath(Path.Combine(root, "state", "app-updater-" + new string('0', 32), "toolkit-manifest.json"));
            return Path.GetFullPath(root);
        }

        internal static bool HasV2Catalog(string root)
        {
            try { return Number(JsonFile(Path.Combine(root, "state", "components", "current.json"), 8192), "schema") == 2; }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
        }

        static async Task<Dictionary<string, object>> RunV2Async(Package package, string action, Action<Dictionary<string, object>> progress, CancellationToken cancel)
        {
            var args = new List<string> { "-X", "utf8", "-I", "-B", Path.Combine(package.Tools, "scripts", "app_update_v2.py"), action, "--index", package.Index, "--index-sha256", package.IndexSha, "--target", package.Root };
            if (action == "prepare") args.AddRange(new[] { "--stage", package.Stage });
            if (action == "prepare" && package.Offline) args.Add("--offline");
            var info = new ProcessStartInfo(Path.Combine(package.Tools, "py", "python.exe"), String.Join(" ", args.Select(Bridge.Quote)))
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = package.Work, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
            using (var process = Process.Start(info))
            {
                var output = ReadV2Output(process.StandardOutput, progress); var error = ReadHelperError(process.StandardError);
                try
                {
                    while (!process.HasExited) { cancel.ThrowIfCancellationRequested(); if (output.IsFaulted) await output.ConfigureAwait(false); if (error.IsFaulted) await error.ConfigureAwait(false); await Task.Delay(100, cancel).ConfigureAwait(false); }
                    cancel.ThrowIfCancellationRequested(); var result = await output.ConfigureAwait(false); await error.ConfigureAwait(false);
                    if (process.ExitCode != 0 || result == null || Str(result, "phase") != (action == "plan" ? "planned" : "prepared")) throw new InvalidDataException("Не удалось " + (action == "plan" ? "оценить" : "подготовить") + " обновление. Можно повторить или выбрать полный пакет.");
                    return result;
                }
                finally { if (!process.HasExited) { process.Kill(); process.WaitForExit(); } }
            }
        }

        static async Task<Dictionary<string, object>> ReadV2Output(StreamReader reader, Action<Dictionary<string, object>> progress)
        {
            var line = new StringBuilder(); char[] buffer = new char[2048]; int read; Dictionary<string, object> last = null;
            while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                for (int i = 0; i < read; i++)
                {
                    if (buffer[i] == '\n') { last = ParseProgress(line.ToString()); progress(last); line.Clear(); }
                    else if (buffer[i] != '\r') { line.Append(buffer[i]); if (line.Length > 65536) throw new InvalidDataException("Ответ обновлятора слишком большой."); }
                }
            if (line.Length > 0) throw new InvalidDataException("Ответ обновлятора не завершён.");
            return last;
        }

        static Dictionary<string, object> JsonFile(string path, int maximum = 1048576)
        {
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || new FileInfo(path).Length > maximum) throw new InvalidDataException("Повреждена запись обновления.");
            var value = new JavaScriptSerializer { MaxJsonLength = maximum }.DeserializeObject(File.ReadAllText(path)) as Dictionary<string, object>;
            if (value == null) throw new InvalidDataException("Повреждена запись обновления.");
            return value;
        }

        static Dictionary<string, object> Dict(Dictionary<string, object> value, string key)
        { object child; return value.TryGetValue(key, out child) ? child as Dictionary<string, object> : null; }
        static string Str(Dictionary<string, object> value, string key)
        { object child; return value != null && value.TryGetValue(key, out child) && child is string ? (string)child : null; }
        static long Number(Dictionary<string, object> value, string key)
        {
            object child; long number;
            if (!value.TryGetValue(key, out child) || !(child is int || child is long) || !Int64.TryParse(Convert.ToString(child), out number) || number < 0)
                throw new InvalidDataException("Неверное число байтов обновления.");
            return number;
        }

        internal static Dictionary<string, object> ParseProgress(string line)
        {
            if (line.Length > 65536) throw new InvalidDataException("Ответ обновлятора слишком большой.");
            var value = new JavaScriptSerializer().DeserializeObject(line) as Dictionary<string, object>;
            if (value == null || String.IsNullOrEmpty(Str(value, "phase"))) throw new InvalidDataException("Неверный этап обновления.");
            foreach (string key in new[] { "done_bytes", "total_bytes", "downloaded_bytes", "download_bytes", "required_free_bytes" })
                if (value.ContainsKey(key)) Number(value, key);
            if (value.ContainsKey("done_bytes") && value.ContainsKey("total_bytes") && Number(value, "done_bytes") > Number(value, "total_bytes"))
                throw new InvalidDataException("Ход обновления превышает размер этапа.");
            return value;
        }

        internal static void AtomicJson(string path, object value)
        {
            CheckUpdatePath(path);
            string temporary = Path.Combine(Path.GetDirectoryName(path), ".wcp-" + Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                byte[] bytes = new UTF8Encoding(false).GetBytes(new JavaScriptSerializer().Serialize(value));
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
                if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }

        internal static bool ValidProbe(string root, string nonce)
        {
            try
            {
                if (nonce == null || !Regex.IsMatch(nonce, @"\A[a-f0-9]{32}\z")) return false;
                foreach (string path in new[] { root, Path.Combine(root, "state") }) if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return false;
                var journal = JsonFile(Path.Combine(root, "state", "app-update.json"));
                var probe = Dict(journal, "probe"); var release = Dict(journal, "release");
                var installed = JsonFile(Path.Combine(root, "release-info.json"), 4096);
                using (var current = Process.GetCurrentProcess())
                    return Number(journal, "schema") == 3 && Str(journal, "phase") == "awaiting_probe"
                        && String.Equals(Path.GetFullPath(Str(journal, "target")).TrimEnd('\\'), Path.GetFullPath(root).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)
                        && probe != null && Str(probe, "nonce") == nonce && Number(probe, "pid") == current.Id
                        && Str(probe, "created") == current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
                        && release != null && Number(release, "schema") == 1 && Number(installed, "schema") == 1
                        && Regex.IsMatch(Str(release, "release_id") ?? "", @"\A[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,8}\z") && Str(release, "release_id") == Str(installed, "release_id")
                        && Regex.IsMatch(Str(release, "source_commit") ?? "", @"\A[a-f0-9]{40}\z") && Str(release, "source_commit") == Str(installed, "source_commit");
            }
            catch (Exception) { return false; }
        }

        internal static async Task ProbeHealthAsync(string root, string nonce)
        {
            if (!ValidProbe(root, nonce)) throw new InvalidDataException("Пробный запуск не подтверждён журналом.");
            var info = new ProcessStartInfo(Path.Combine(root, Bridge.PythonDir, "python.exe"), String.Join(" ", new[] { "-X", "utf8", "-I", "-B", Path.Combine(root, "scripts", "release_portable.py"), "probe-app", "--target", root, "--nonce", nonce }.Select(Bridge.Quote)))
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = root, RedirectStandardOutput = true, RedirectStandardError = true };
            using (var process = Process.Start(info))
            using (var cancel = new CancellationTokenSource(TimeSpan.FromSeconds(110)))
            {
                var output = ReadBoundedOutput(process.StandardOutput); var errors = ReadHelperError(process.StandardError);
                try
                {
                    while (!process.HasExited) { if (output.IsFaulted) await output.ConfigureAwait(false); if (errors.IsFaulted) await errors.ConfigureAwait(false); await Task.Delay(100, cancel.Token).ConfigureAwait(false); }
                    string text = await output.ConfigureAwait(false); await errors.ConfigureAwait(false);
                    if (text.Length > 65536) throw new InvalidDataException("Ответ проверки слишком большой.");
                    var value = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
                    var installed = JsonFile(Path.Combine(root, "release-info.json"), 4096);
                    if (process.ExitCode != 0 || value == null || Number(value, "schema") != 1 || !Object.Equals(value.ContainsKey("ok") ? value["ok"] : null, true)
                        || Str(value, "release_id") != Str(installed, "release_id") || Str(value, "source_commit") != Str(installed, "source_commit") || !ValidProbe(root, nonce))
                        throw new InvalidDataException("Новая программа не прошла проверку запуска.");
                }
                finally { if (!process.HasExited) { process.Kill(); process.WaitForExit(); } }
            }
        }

        static async Task<string> ReadBoundedOutput(StreamReader reader)
        {
            var result = new StringBuilder(); char[] buffer = new char[2048]; int read;
            while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
            { if (result.Length + read > 65536) throw new InvalidDataException("Ответ проверки слишком большой."); result.Append(buffer, 0, read); }
            return result.ToString();
        }

        internal static void AckProbe(string root, string nonce)
        {
            if (!ValidProbe(root, nonce)) throw new InvalidDataException("Пробный запуск не подтверждён.");
            var release = JsonFile(Path.Combine(root, "release-info.json"), 4096);
            using (var current = Process.GetCurrentProcess()) AtomicJson(Path.Combine(root, "state", "update-probe.json"), new Dictionary<string, object>
                { { "schema", 1 }, { "nonce", nonce }, { "release_id", Str(release, "release_id") }, { "source_commit", Str(release, "source_commit") }, { "root", Path.GetFullPath(root) }, { "pid", current.Id }, { "created", current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() }, { "ok", true } });
        }

        internal static bool ProbeCommitted(string root, string nonce)
        {
            try
            {
                if (!JournalAbsent(root)) return false;
                var result = JsonFile(Path.Combine(root, "state", "update-accepted.json"), 8192);
                var release = JsonFile(Path.Combine(root, "release-info.json"), 4096);
                return Number(result, "schema") == 1 && Object.Equals(result.ContainsKey("committed") ? result["committed"] : null, true)
                    && Str(result, "nonce") == nonce && String.Equals(Str(result, "root"), Path.GetFullPath(root), StringComparison.OrdinalIgnoreCase)
                    && Str(result, "release_id") == Str(release, "release_id") && Str(result, "source_commit") == Str(release, "source_commit");
            }
            catch (Exception) { return false; }
        }

        internal static async Task WaitForProbeCommitAsync(string root, string nonce, CancellationToken cancel)
        {
            while (true)
            {
                cancel.ThrowIfCancellationRequested();
                if (ProbeCommitted(root, nonce)) return;
                await Task.Delay(200, cancel).ConfigureAwait(false);
            }
        }

        static bool JournalAbsent(string root)
        {
            try { File.GetAttributes(Path.Combine(root, "state", "app-update.json")); return false; }
            catch (FileNotFoundException) { return true; }
            catch (DirectoryNotFoundException) { return true; }
        }

        internal static async Task PrepareAsync(string root, Package package, CancellationToken cancel)
        {
            package.Root = Path.GetFullPath(root).TrimEnd('\\');
            if (package.Tools == null)
            {
            package.Tools = Path.Combine(package.Root, "state", "app-updater-" + Guid.NewGuid().ToString("N"));
            await Task.Run(delegate
            {
                Copy(Path.Combine(root, Bridge.PythonDir), Path.Combine(package.Tools, "py"), cancel);
                Copy(Path.Combine(root, "scripts"), Path.Combine(package.Tools, "scripts"), cancel);
            }, cancel).ConfigureAwait(false);
            }
            else if (!IsTools(package.Root, package.Tools)) throw new InvalidDataException("Папка независимого обновлятора не прошла проверку.");
            var info = Command(package.Tools, "check-app", "--zip", package.Zip, "--sha256", package.Sha, "--release-id", package.Id, "--target", root);
            info.RedirectStandardOutput = true; info.RedirectStandardError = true;
            using (var process = Process.Start(info))
            {
                var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
                try
                {
                    while (!process.HasExited) { cancel.ThrowIfCancellationRequested(); await Task.Delay(100, cancel).ConfigureAwait(false); }
                    await output.ConfigureAwait(false); string details = await error.ConfigureAwait(false);
                    if (process.ExitCode != 0)
                    {
                        Directory.CreateDirectory(Path.Combine(root, "logs"));
                        File.WriteAllText(Path.Combine(root, "logs", "update-details.txt"), details, Encoding.UTF8);
                        throw new InvalidDataException("Пакет обновления не прошёл проверку. Программа не изменена. Подробности в logs/update-details.txt.");
                    }
                }
                finally { if (!process.HasExited) { process.Kill(); process.WaitForExit(); } }
            }
        }

        static ProcessStartInfo Command(string work, params string[] arguments)
        {
            var args = new[] { "-X", "utf8", "-I", "-B", Path.Combine(work, "scripts", "release_portable.py") }.Concat(arguments);
            return new ProcessStartInfo(Path.Combine(work, "py", "python.exe"), String.Join(" ", args.Select(Bridge.Quote)))
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = work };
        }

        internal static void Launch(string root, Package package)
        {
            string state = Path.Combine(root, "state"); Directory.CreateDirectory(state);
            AtomicJson(Path.Combine(state, "update-recovery.json"), new Dictionary<string, object> { { "schema", 2 }, { "work", Path.GetFileName(package.Tools) } });
            RunInstaller(root, package.Tools, InstallerArguments(package));
        }

        internal static string[] InstallerArguments(Package package)
        {
            if (package.Stage != null) return new[] { "--prepared", package.Stage, "--index-sha256", package.IndexSha, "--release-id", package.Id, "--probe" };
            var args = new List<string> { "--zip", package.Zip, "--sha256", package.Sha, "--release-id", package.Id, "--probe" };
            if (package.Index != null) args.AddRange(new[] { "--catalog", package.Index, "--index-sha256", package.IndexSha });
            return args.ToArray();
        }

        internal static void Recover(string root)
        {
            RunInstaller(root, RecoveryWork(root), new[] { "--recover" });
        }

        internal static string RecoveryWork(string root)
        {
            string path = Path.Combine(root, "state", "update-recovery.json");
            if (new FileInfo(path).Length > 4096) throw new InvalidDataException("Запись восстановления повреждена.");
            var value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            object saved, schema;
            if (value == null || !value.TryGetValue("work", out saved) || !(saved is string))
                throw new InvalidDataException("Запись восстановления повреждена.");
            string name = (string)saved;
            if (value.TryGetValue("schema", out schema) && (!(schema is int) || (int)schema != 1 && (int)schema != 2))
                throw new InvalidDataException("Неизвестная запись восстановления.");
            if (schema == null || (int)schema == 1)
            {
                if (!Path.IsPathRooted(name) || !String.Equals(Path.GetFullPath(name), name, StringComparison.OrdinalIgnoreCase)
                    || !String.Equals(Path.GetFileName(Path.GetDirectoryName(name)), "state", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Запись восстановления повреждена.");
                if (!String.Equals(name, Path.Combine(Path.GetFullPath(root), "state", Path.GetFileName(name)), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Папка перенесена, а сохранённый обновлятор относится к старой версии. Для восстановления нужен полный комплект новой версии в отдельной папке. Сохрани папку state.");
                name = Path.GetFileName(name);
            }
            if (!Regex.IsMatch(name, @"\Aapp-updater-[a-f0-9]{32}\z"))
                throw new InvalidDataException("Запись восстановления повреждена.");
            string work = Path.Combine(Path.GetFullPath(root), "state", name);
            string script = Path.Combine(work, "scripts", "release_portable.py");
            if (!IsTools(root, work) || !File.Exists(script)
                || (File.GetAttributes(Path.GetDirectoryName(script)) & FileAttributes.ReparsePoint) != 0
                || (File.GetAttributes(script) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("Сохранённый обновлятор не найден. Сохрани папку state для восстановления.");
            return work;
        }

        static void RunInstaller(string root, string work, string[] arguments)
        {
            using (var current = Process.GetCurrentProcess())
            {
                var args = new[] { "run-app-update", "--target", root, "--wait-pid", current.Id.ToString(), "--wait-created", current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() }.Concat(arguments).ToArray();
                using (Process.Start(Command(work, args))) { }
            }
        }

        static void Copy(string source, string target, CancellationToken cancel)
        {
            cancel.ThrowIfCancellationRequested();
            if ((File.GetAttributes(source) & FileAttributes.ReparsePoint) != 0) throw new IOException("Компонент программы оказался ссылкой.");
            Directory.CreateDirectory(target);
            foreach (string file in Directory.GetFiles(source))
            {
                cancel.ThrowIfCancellationRequested();
                if ((File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0) throw new IOException("Файл программы оказался ссылкой.");
                File.Copy(file, Path.Combine(target, Path.GetFileName(file)), false);
            }
            foreach (string folder in Directory.GetDirectories(source)) Copy(folder, Path.Combine(target, Path.GetFileName(folder)), cancel);
        }

        static bool IsWork(string path)
        {
            return String.Equals(Path.GetDirectoryName(path), Path.GetTempPath().TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)
                && Regex.IsMatch(Path.GetFileName(path), @"\Apocket-windows-updater-[a-f0-9]{32}\z")
                && (File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0;
        }

        static bool IsTools(string root, string path)
        {
            return String.Equals(Path.GetDirectoryName(path), Path.Combine(Path.GetFullPath(root), "state"), StringComparison.OrdinalIgnoreCase)
                && Regex.IsMatch(Path.GetFileName(path), @"\Aapp-updater-[a-f0-9]{32}\z")
                && (File.GetAttributes(root) & FileAttributes.ReparsePoint) == 0
                && (File.GetAttributes(Path.GetDirectoryName(path)) & FileAttributes.ReparsePoint) == 0
                && (File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0;
        }

        internal static void Discard(Package package)
        {
            if (package == null) return;
            try { if (package.Root != null && !JournalAbsent(package.Root)) return; }
            catch (IOException) { return; } catch (UnauthorizedAccessException) { return; }
            try { if (Directory.Exists(package.Work) && (IsWork(package.Work) || package.Root != null && IsTools(package.Root, package.Work))) Directory.Delete(package.Work, true); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
            try { if (!package.PreserveTools && package.Tools != null && Directory.Exists(package.Tools) && IsTools(package.Root, package.Tools)) Directory.Delete(package.Tools, true); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
            // Storage cleanup never touches unmarked UUID folders, so the stage this package created is removed here or never.
            try { if (package.Stage != null && package.Root != null && Directory.Exists(package.Stage) && IsStage(package.Root, package.Stage)) Directory.Delete(package.Stage, true); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
        }

        static bool IsStage(string root, string path)
        {
            return String.Equals(Path.GetDirectoryName(path), Path.Combine(Path.GetFullPath(root), "state"), StringComparison.OrdinalIgnoreCase)
                && Regex.IsMatch(Path.GetFileName(path), @"\A[a-f0-9]{32}\z")
                && (File.GetAttributes(Path.GetDirectoryName(path)) & FileAttributes.ReparsePoint) == 0
                && (File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0;
        }

        internal static string TakeReport(string root)
        {
            string path = Path.Combine(root, "logs", "update-result.json");
            try
            {
                if (!File.Exists(path))
                {
                    path = Path.Combine(root, "logs", "update-last.txt");
                    if (!File.Exists(path)) return null;
                    if (new FileInfo(path).Length > 8192) return "Не удалось установить обновление. Подробности в logs/update-last.txt.";
                    string legacy = File.ReadAllText(path);
                    var old = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(legacy);
                    var installed = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(root, "release-info.json")));
                    object version, actual;
                    if (old.TryGetValue("release_id", out version) && installed.TryGetValue("release_id", out actual)
                        && Object.Equals(version, actual) && !File.Exists(Path.Combine(root, "state", "app-update.json")))
                    { File.Delete(path); return "Обновлено до версии " + Convert.ToString(version); }
                    return "Результат обновления не подтверждён. Подробности в logs/update-last.txt.";
                }
                if (new FileInfo(path).Length > 8192) return "Результат обновления не удалось прочитать. Открой logs/update-details.txt.";
                var result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
                object message, status;
                if (!result.TryGetValue("status", out status) || !new[] { "committed", "rolled_back", "failed", "recovery_required" }.Contains(Convert.ToString(status)) || !result.TryGetValue("message", out message))
                    return "Результат обновления не подтверждён. Открой logs/update-details.txt.";
                File.Delete(path);
                return Convert.ToString(message);
            }
            catch (Exception) { return "Не удалось прочитать результат обновления. Открой logs/" + Path.GetFileName(path) + "."; }
        }
    }
}
