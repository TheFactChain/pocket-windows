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
        const string GitHubApi = "https://api.github.com/repos/nekontritce11123-lab/pocket-windows/releases/latest";
        const string AssetPrefix = "https://github.com/nekontritce11123-lab/pocket-windows/releases/download/";
        const long MaxPackage = 8L * 1024 * 1024 * 1024;
        internal sealed class Offer { public string Id, ZipUrl, ShaUrl, IndexUrl, IndexShaUrl, ToolkitUrl, ToolkitShaUrl; public long Bytes, ToolkitBytes; public bool V2, Repair; }
        internal sealed class CheckResult { public Offer Offer; public string Title, Message; }
        internal sealed class Package { public string Zip, Sha, Id, Work, Tools, Root, Index, IndexSha, Stage; public long DownloadBytes, RequiredFreeBytes; public bool Offline, PreserveTools; }

        static string Api
        {
            get
            {
                string value = Environment.GetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API");
                Uri uri;
                return Uri.TryCreate(value, UriKind.Absolute, out uri) && uri.Scheme == "http" && uri.Host == "127.0.0.1" ? value : GitHubApi;
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

        static bool AssetUrl(string url)
        {
            Uri uri, api;
            if (!Uri.TryCreate(url, UriKind.Absolute, out uri) || uri.UserInfo.Length != 0) return false;
            if (Api != GitHubApi && Uri.TryCreate(Api, UriKind.Absolute, out api))
                return uri.Scheme == "http" && uri.Host == "127.0.0.1" && uri.Port == api.Port;
            return url.StartsWith(AssetPrefix, StringComparison.Ordinal) && uri.Scheme == "https" && uri.IsDefaultPort;
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
            if (id == null || id.Length > 80 || !Regex.IsMatch(id, @"\A[0-9]+(\.[0-9]+){1,5}\z")) return null;
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

        internal static async Task<CheckResult> CheckAsync(string currentId)
        {
            if (Parts(currentId) == null) return new CheckResult { Title = "Версия не определена", Message = "У этой копии не указана версия выпуска. Обновление доступно в готовом комплекте." };
            string text = await ReadText(Api, 1024 * 1024, CancellationToken.None).ConfigureAwait(false);
            var release = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
            if (release == null || !release.ContainsKey("tag_name") || !release.ContainsKey("assets"))
                throw new InvalidDataException("Сервер вернул непонятные сведения о выпуске.");
            string id = Convert.ToString(release["tag_name"]).TrimStart('v');
            if (Parts(id) == null) throw new InvalidDataException("Номер выпуска на сервере повреждён.");
            if (!Newer(id, currentId) && id != currentId) return new CheckResult { Title = "Установлена последняя версия", Message = "Версия " + currentId + ". Новых обновлений пока нет." };
            if ((release.ContainsKey("draft") && Object.Equals(release["draft"], true)) || (release.ContainsKey("prerelease") && Object.Equals(release["prerelease"], true)))
                return new CheckResult { Title = "Выпуск готовится", Message = "Новый выпуск ещё проходит проверку." };
            var assets = release["assets"] as IEnumerable;
            if (assets == null) throw new InvalidDataException("Список файлов выпуска повреждён.");
            var items = assets.Cast<object>().OfType<Dictionary<string, object>>().ToList();
            string name = "Pocket-Windows-" + id + "-app.zip";
            var zip = items.SingleOrDefault(item => item.ContainsKey("name") && Convert.ToString(item["name"]) == name);
            var sha = items.SingleOrDefault(item => item.ContainsKey("name") && Convert.ToString(item["name"]) == name + ".sha256");
            if (zip == null || sha == null) return new CheckResult { Title = "Пакет ещё не опубликован", Message = "Выпуск " + id + " уже есть, но пакет обновления ещё не опубликован." };
            long bytes;
            string zipUrl = zip.ContainsKey("browser_download_url") ? Convert.ToString(zip["browser_download_url"]) : "";
            string shaUrl = sha.ContainsKey("browser_download_url") ? Convert.ToString(sha["browser_download_url"]) : "";
            if (!zip.ContainsKey("size") || !Int64.TryParse(Convert.ToString(zip["size"]), out bytes) || bytes <= 0 || bytes > MaxPackage || !AssetUrl(zipUrl) || !AssetUrl(shaUrl))
                throw new InvalidDataException("Ссылки или размер пакета обновления не прошли проверку.");
            var offer = new Offer { Id = id, ZipUrl = zipUrl, ShaUrl = shaUrl, Bytes = bytes };
            string indexName = "Pocket-Windows-" + id + "-update.json";
            var index = items.SingleOrDefault(item => item.ContainsKey("name") && Convert.ToString(item["name"]) == indexName);
            var indexSha = items.SingleOrDefault(item => item.ContainsKey("name") && Convert.ToString(item["name"]) == indexName + ".sha256");
            if (index != null && indexSha != null)
            {
                offer.IndexUrl = index.ContainsKey("browser_download_url") ? Convert.ToString(index["browser_download_url"]) : "";
                offer.IndexShaUrl = indexSha.ContainsKey("browser_download_url") ? Convert.ToString(indexSha["browser_download_url"]) : "";
                long indexBytes;
                if (!AssetUrl(offer.IndexUrl) || !AssetUrl(offer.IndexShaUrl) || !index.ContainsKey("size")
                    || !Int64.TryParse(Convert.ToString(index["size"]), out indexBytes) || indexBytes <= 0 || indexBytes > 16 * 1024 * 1024)
                    throw new InvalidDataException("Индекс обновления не прошёл проверку.");
            }
            string v2Name = "Pocket-Windows-" + id + "-update-v2.json", kitName = "Pocket-Windows-" + id + "-updater.zip";
            var v2 = items.SingleOrDefault(item => Object.Equals(item.ContainsKey("name") ? item["name"] : null, v2Name));
            var v2Sha = items.SingleOrDefault(item => Object.Equals(item.ContainsKey("name") ? item["name"] : null, v2Name + ".sha256"));
            var kit = items.SingleOrDefault(item => Object.Equals(item.ContainsKey("name") ? item["name"] : null, kitName));
            var kitSha = items.SingleOrDefault(item => Object.Equals(item.ContainsKey("name") ? item["name"] : null, kitName + ".sha256"));
            if (v2 != null && v2Sha != null && kit != null && kitSha != null)
            {
                offer.IndexUrl = Convert.ToString(v2["browser_download_url"]); offer.IndexShaUrl = Convert.ToString(v2Sha["browser_download_url"]);
                offer.ToolkitUrl = Convert.ToString(kit["browser_download_url"]); offer.ToolkitShaUrl = Convert.ToString(kitSha["browser_download_url"]);
                if (!AssetUrl(offer.IndexUrl) || !AssetUrl(offer.IndexShaUrl) || !AssetUrl(offer.ToolkitUrl) || !AssetUrl(offer.ToolkitShaUrl)
                    || !Int64.TryParse(Convert.ToString(kit["size"]), out offer.ToolkitBytes) || offer.ToolkitBytes <= 0 || offer.ToolkitBytes > 256L * 1024 * 1024)
                    throw new InvalidDataException("Комплект обновлятора повреждён.");
                offer.V2 = true;
            }
            offer.Repair = id == currentId;
            return new CheckResult { Offer = offer, Title = offer.Repair ? "Установлена последняя версия" : "Доступно обновление", Message = offer.Repair ? "Можно проверить и восстановить файлы программы. Личные данные сохранятся." : "Можно установить новую версию. Личные данные сохранятся." };
        }

        internal static async Task<Package> DownloadAsync(Offer offer, Action<int> progress, CancellationToken cancel, string root = null)
        {
            Package package = null;
            try
            {
                cancel.ThrowIfCancellationRequested();
                if (!AssetUrl(offer.ZipUrl) || !AssetUrl(offer.ShaUrl) || offer.Bytes <= 0 || offer.Bytes > MaxPackage) throw new InvalidDataException("Пакет не прошёл проверку.");
                string sha = (await ReadText(offer.ShaUrl, 4096, cancel).ConfigureAwait(false)).Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
                if (sha == null || !Regex.IsMatch(sha, @"\A[a-fA-F0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма обновления повреждена.");
                string targetRoot = CheckedRoot(root ?? Bridge.Root);
                string state = Path.Combine(targetRoot, "state"); Directory.CreateDirectory(state);
                if ((File.GetAttributes(targetRoot) & FileAttributes.ReparsePoint) != 0 || (File.GetAttributes(state) & FileAttributes.ReparsePoint) != 0) throw new IOException("Папка обновления оказалась ссылкой.");
                string work = Path.Combine(state, "app-updater-" + Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(work);
                package = new Package { Root = targetRoot, Work = work, Zip = Path.Combine(work, "app.zip"), Sha = sha.ToLowerInvariant(), Id = offer.Id };
                AtomicJson(Path.Combine(work, "download.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", package.Id }, { "sha256", package.Sha } });
                if (offer.IndexUrl != null)
                {
                    await DownloadChangesAsync(root ?? Bridge.Root, package, offer, progress, cancel).ConfigureAwait(false);
                    cancel.ThrowIfCancellationRequested();
                    AtomicJson(Path.Combine(work, "download.json"), new Dictionary<string, object> { { "schema", 1 }, { "release_id", package.Id }, { "sha256", package.Sha } });
                    return package;
                }
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

        static async Task DownloadChangesAsync(string root, Package package, Offer offer, Action<int> progress, CancellationToken cancel)
        {
            if (!AssetUrl(offer.IndexUrl) || !AssetUrl(offer.IndexShaUrl)) throw new InvalidDataException("Ссылка на индекс обновления повреждена.");
            string expected = (await ReadText(offer.IndexShaUrl, 4096, cancel).ConfigureAwait(false)).Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
            if (expected == null || !Regex.IsMatch(expected, @"\A[a-fA-F0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма индекса повреждена.");
            byte[] bytes = await ReadBytes(offer.IndexUrl, 16 * 1024 * 1024, cancel).ConfigureAwait(false);
            using (var hash = SHA256.Create())
                if (!String.Equals(BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", ""), expected, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Индекс обновления повреждён. Программа не изменена.");
            var value = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 }.DeserializeObject(new UTF8Encoding(false, true).GetString(bytes).TrimStart('\uFEFF')) as Dictionary<string, object>;
            object releaseValue, archiveValue, id, url, size, archiveSha;
            var release = value != null && value.TryGetValue("release", out releaseValue) ? releaseValue as Dictionary<string, object> : null;
            var archive = value != null && value.TryGetValue("archive", out archiveValue) ? archiveValue as Dictionary<string, object> : null;
            long count;
            if (release == null || archive == null || !release.TryGetValue("release_id", out id) || !Object.Equals(id, offer.Id)
                || !archive.TryGetValue("url", out url) || !Object.Equals(url, offer.ZipUrl)
                || !archive.TryGetValue("bytes", out size) || !Int64.TryParse(Convert.ToString(size), out count) || count != offer.Bytes
                || !archive.TryGetValue("sha256", out archiveSha) || !Object.Equals(archiveSha, package.Sha))
                throw new InvalidDataException("Индекс не соответствует выбранному выпуску.");
            string indexPath = Path.Combine(package.Work, "update.json");
            File.WriteAllBytes(indexPath, bytes);
            root = CheckedRoot(root);
            string python = Path.Combine(root, "runtime", "python-3.12.14", "python.exe");
            string helper = Path.Combine(root, "scripts", "app_download.py");
            foreach (string path in new[] { python, helper })
                if (!File.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("Не найден компонент выборочного обновления. Восстанови файлы программы.");
            var args = new[] { "-X", "utf8", "-I", "-B", helper, "--index", indexPath, "--index-sha256", expected.ToLowerInvariant(), "--target", root, "--output", package.Zip };
            var info = new ProcessStartInfo(python, String.Join(" ", args.Select(Bridge.Quote)))
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = package.Work, RedirectStandardOutput = true, RedirectStandardError = true };
            using (var process = Process.Start(info))
            {
                var output = ReadDownloadOutput(process.StandardOutput, progress);
                var error = ReadHelperError(process.StandardError);
                try
                {
                    while (!process.HasExited)
                    {
                        cancel.ThrowIfCancellationRequested();
                        if (output.IsFaulted) await output.ConfigureAwait(false);
                        if (error.IsFaulted) await error.ConfigureAwait(false);
                        await Task.Delay(100, cancel).ConfigureAwait(false);
                    }
                    cancel.ThrowIfCancellationRequested();
                    var result = await output.ConfigureAwait(false);
                    await error.ConfigureAwait(false);
                    object digest, length;
                    long total;
                    if (process.ExitCode != 0 || result == null || !result.TryGetValue("sha256", out digest) || !(digest is string)
                        || !Regex.IsMatch((string)digest, @"\A[a-f0-9]{64}\z") || !result.TryGetValue("bytes", out length)
                        || !Int64.TryParse(Convert.ToString(length), out total) || total <= 0 || total > MaxPackage
                        || !File.Exists(package.Zip) || (File.GetAttributes(package.Zip) & FileAttributes.ReparsePoint) != 0
                        || new FileInfo(package.Zip).Length != total)
                        throw new InvalidDataException("Не удалось загрузить изменённые файлы. Программа не изменена.");
                    using (var stream = File.OpenRead(package.Zip))
                    using (var hash = SHA256.Create())
                        if (!String.Equals(BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", ""), (string)digest, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidDataException("Загруженное обновление повреждено.");
                    package.Sha = (string)digest;
                }
                finally { if (!process.HasExited) { process.Kill(); process.WaitForExit(); } }
            }
        }

        static async Task<Dictionary<string, object>> ReadDownloadOutput(StreamReader reader, Action<int> progress)
        {
            var line = new StringBuilder(); var buffer = new char[1024]; int read, total = 0;
            Dictionary<string, object> result = null;
            while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
            {
                total += read;
                if (total > 1024 * 1024) throw new InvalidDataException("Ответ загрузчика слишком большой.");
                for (int index = 0; index < read; index++)
                {
                    char character = buffer[index];
                    if (character == '\r') continue;
                    if (character != '\n')
                    {
                        if (line.Length >= 8192) throw new InvalidDataException("Ответ загрузчика слишком большой.");
                        line.Append(character); continue;
                    }
                    if (line.Length == 0) continue;
                    var value = new JavaScriptSerializer { MaxJsonLength = 8192 }.Deserialize<Dictionary<string, object>>(line.ToString());
                    line.Clear();
                    object percent;
                    if (value == null || result != null) throw new InvalidDataException("Ответ загрузчика повреждён.");
                    if (value.TryGetValue("progress", out percent))
                    {
                        if (!(percent is int) || (int)percent < 0 || (int)percent > 100) throw new InvalidDataException("Неверный ход загрузки.");
                        progress((int)percent);
                    }
                    else result = value;
                }
            }
            if (line.Length != 0) throw new InvalidDataException("Ответ загрузчика не завершён.");
            return result;
        }

        static async Task ReadHelperError(StreamReader reader)
        {
            char[] buffer = new char[1024]; int read, total = 0;
            while ((read = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                if ((total += read) > 65536) throw new InvalidDataException("Ответ загрузчика слишком большой.");
        }

        static async Task<string> DigestAsync(string url, CancellationToken cancel)
        {
            string value = (await ReadText(url, 4096, cancel).ConfigureAwait(false)).Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
            if (value == null || !Regex.IsMatch(value, @"\A[a-fA-F0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма повреждена.");
            return value.ToLowerInvariant();
        }

        internal static Offer FullOffer(Offer offer)
        { return new Offer { Id = offer.Id, ZipUrl = offer.ZipUrl, ShaUrl = offer.ShaUrl, Bytes = offer.Bytes, Repair = offer.Repair }; }

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
                package.IndexSha = await DigestAsync(offer.IndexShaUrl, cancel).ConfigureAwait(false);
                byte[] bytes = await ReadBytes(offer.IndexUrl, 16 * 1024 * 1024, cancel).ConfigureAwait(false);
                using (var hash = SHA256.Create()) if (BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant() != package.IndexSha) throw new InvalidDataException("Индекс обновления повреждён.");
                package.Index = Path.Combine(package.Tools, "index.json"); File.WriteAllBytes(package.Index, bytes);
                var index = JsonFile(package.Index, 16 * 1024 * 1024); var release = Dict(index, "release"); var archive = Dict(index, "archive"); var toolkit = Dict(index, "toolkit");
                if (Number(index, "schema") != 2 || Number(index, "min_updater") != 2 || release == null || Number(release, "schema") != 1
                    || Str(release, "release_id") != offer.Id || !Regex.IsMatch(Str(release, "source_commit") ?? "", @"\A[a-f0-9]{40}\z")
                    || archive == null || !AssetUrl(Str(archive, "url")) || Number(archive, "bytes") <= 0 || Number(archive, "bytes") > MaxPackage
                    || Str(archive, "url") != offer.ZipUrl.Substring(0, offer.ZipUrl.LastIndexOf('/') + 1) + "Pocket-Windows-" + offer.Id + "-chunks.zip"
                    || !Regex.IsMatch(Str(archive, "sha256") ?? "", @"\A[a-f0-9]{64}\z")
                    || toolkit == null || Str(toolkit, "url") != offer.ToolkitUrl || Number(toolkit, "bytes") != offer.ToolkitBytes
                    || !Regex.IsMatch(Str(toolkit, "sha256") ?? "", @"\A[a-f0-9]{64}\z")
                    || !index.ContainsKey("files") || !index.ContainsKey("chunks") || !index.ContainsKey("gateway")) throw new InvalidDataException("Индекс не соответствует выпуску.");
                string kitSha = await DigestAsync(offer.ToolkitShaUrl, cancel).ConfigureAwait(false);
                if (kitSha != Str(toolkit, "sha256")) throw new InvalidDataException("Контрольные суммы обновлятора не совпали.");
                var kitOffer = new Offer { Id = offer.Id, ZipUrl = offer.ToolkitUrl, ShaUrl = offer.ToolkitShaUrl, Bytes = offer.ToolkitBytes };
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
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = package.Work, RedirectStandardOutput = true, RedirectStandardError = true };
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
            var info = new ProcessStartInfo(Path.Combine(root, "runtime", "python-3.12.14", "python.exe"), String.Join(" ", new[] { "-X", "utf8", "-I", "-B", Path.Combine(root, "scripts", "release_portable.py"), "probe-app", "--target", root, "--nonce", nonce }.Select(Bridge.Quote)))
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
                Copy(Path.Combine(root, "runtime", "python-3.12.14"), Path.Combine(package.Tools, "py"), cancel);
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
