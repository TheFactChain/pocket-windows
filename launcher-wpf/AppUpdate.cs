using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
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
        internal sealed class Offer { public string Id, ZipUrl, ShaUrl; public long Bytes; }
        internal sealed class CheckResult { public Offer Offer; public string Title, Message; }
        internal sealed class Package { public string Zip, Sha, Id, Work, Tools, Root; }

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

        internal static async Task<CheckResult> CheckAsync(string currentId)
        {
            if (Parts(currentId) == null) return new CheckResult { Title = "Версия не определена", Message = "У этой копии не указана версия выпуска. Обновление доступно в готовом комплекте." };
            string text = await ReadText(Api, 1024 * 1024, CancellationToken.None).ConfigureAwait(false);
            var release = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
            if (release == null || !release.ContainsKey("tag_name") || !release.ContainsKey("assets"))
                throw new InvalidDataException("Сервер вернул непонятные сведения о выпуске.");
            string id = Convert.ToString(release["tag_name"]).TrimStart('v');
            if (Parts(id) == null) throw new InvalidDataException("Номер выпуска на сервере повреждён.");
            if (!Newer(id, currentId)) return new CheckResult { Title = "Установлена последняя версия", Message = "Версия " + currentId + ". Новых обновлений пока нет." };
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
            return new CheckResult { Offer = new Offer { Id = id, ZipUrl = zipUrl, ShaUrl = shaUrl, Bytes = bytes }, Title = "Доступно обновление", Message = "Можно установить новую версию. Личные данные сохранятся." };
        }

        internal static async Task<Package> DownloadAsync(Offer offer, Action<int> progress, CancellationToken cancel)
        {
            Package package = null;
            try
            {
                cancel.ThrowIfCancellationRequested();
                if (!AssetUrl(offer.ZipUrl) || !AssetUrl(offer.ShaUrl) || offer.Bytes <= 0 || offer.Bytes > MaxPackage) throw new InvalidDataException("Пакет не прошёл проверку.");
                string sha = (await ReadText(offer.ShaUrl, 4096, cancel).ConfigureAwait(false)).Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
                if (sha == null || !Regex.IsMatch(sha, @"\A[a-fA-F0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма обновления повреждена.");
                string work = Path.Combine(Path.GetTempPath(), "pocket-windows-updater-" + Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(work);
                package = new Package { Work = work, Zip = Path.Combine(work, "app.zip"), Sha = sha.ToLowerInvariant(), Id = offer.Id };
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

        internal static async Task PrepareAsync(string root, Package package, CancellationToken cancel)
        {
            package.Root = Path.GetFullPath(root).TrimEnd('\\');
            package.Tools = Path.Combine(package.Root, "state", "app-updater-" + Guid.NewGuid().ToString("N"));
            await Task.Run(delegate
            {
                Copy(Path.Combine(root, "runtime", "python-3.12.14"), Path.Combine(package.Tools, "py"), cancel);
                Copy(Path.Combine(root, "scripts"), Path.Combine(package.Tools, "scripts"), cancel);
            }, cancel).ConfigureAwait(false);
            var info = Command(package.Tools, "check-app", "--zip", package.Zip, "--sha256", package.Sha, "--release-id", package.Id);
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
            File.WriteAllText(Path.Combine(state, "update-recovery.json"), new JavaScriptSerializer().Serialize(new Dictionary<string, object> { { "schema", 2 }, { "work", Path.GetFileName(package.Tools) } }), Encoding.UTF8);
            RunInstaller(root, package.Tools, new[] { "--zip", package.Zip, "--sha256", package.Sha, "--release-id", package.Id });
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
            try { if (Directory.Exists(package.Work) && IsWork(package.Work)) Directory.Delete(package.Work, true); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
            try { if (package.Tools != null && Directory.Exists(package.Tools) && IsTools(package.Root, package.Tools)) Directory.Delete(package.Tools, true); }
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
