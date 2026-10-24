using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace WindowsClaude.Launcher
{
    // Light application updates from GitHub Releases: the app package (no Windows disk) is downloaded,
    // then a copy of the bundled Python installs it after the launcher has exited.
    internal static class AppUpdate
    {
        const string GitHubApi = "https://api.github.com/repos/nekontritce11123-lab/windows-claude-portable/releases/latest";

        // Tests may point the check at a loopback server; any other override is ignored.
        static string Api
        {
            get
            {
                string over = Environment.GetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API");
                return over != null && over.StartsWith("http://127.0.0.1:", StringComparison.Ordinal) ? over : GitHubApi;
            }
        }

        internal sealed class Offer { public string Id, ZipUrl, ShaUrl; public long Bytes; }

        static HttpWebRequest Open(string url)
        {
            ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; // TLS 1.2 on .NET Framework 4.8
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = "PocketWindows-Launcher"; request.Timeout = 15000; request.ReadWriteTimeout = 30000;
            return request;
        }

        // Release ids are dotted numbers such as 2026.10.24.12; a malformed one never counts as newer.
        internal static bool Newer(string candidate, string current)
        {
            var a = Parts(candidate); var b = Parts(current);
            if (a == null || b == null) return false;
            for (int index = 0; index < Math.Max(a.Length, b.Length); index++)
            {
                long x = index < a.Length ? a[index] : 0, y = index < b.Length ? b[index] : 0;
                if (x != y) return x > y;
            }
            return false;
        }

        static long[] Parts(string id)
        {
            if (id == null || !Regex.IsMatch(id, @"\A\d+(\.\d+){1,5}\z")) return null;
            return id.Split('.').Select(part => Int64.Parse(part)).ToArray();
        }

        internal static async Task<Offer> CheckAsync(string currentId)
        {
            try
            {
                string text;
                using (var response = await Open(Api).GetResponseAsync().ConfigureAwait(false))
                using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8)) text = await reader.ReadToEndAsync().ConfigureAwait(false);
                var release = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
                if (release == null || !release.ContainsKey("tag_name") || !release.ContainsKey("assets")) return null;
                string id = Convert.ToString(release["tag_name"]).TrimStart('v');
                if (!Newer(id, currentId)) return null;
                var assets = ((IEnumerable)release["assets"]).Cast<object>().OfType<Dictionary<string, object>>().ToList();
                var zip = assets.FirstOrDefault(item => Convert.ToString(item["name"]).EndsWith("-app.zip", StringComparison.Ordinal));
                if (zip == null) return null;
                var sha = assets.FirstOrDefault(item => Convert.ToString(item["name"]) == Convert.ToString(zip["name"]) + ".sha256");
                if (sha == null) return null;
                return new Offer { Id = id, ZipUrl = Convert.ToString(zip["browser_download_url"]), ShaUrl = Convert.ToString(sha["browser_download_url"]), Bytes = Convert.ToInt64(zip["size"]) };
            }
            catch (Exception) { return null; } // offline, rate limited or a private repository: just no offer
        }

        internal static async Task<string[]> DownloadAsync(Offer offer, Action<int> progress)
        {
            string sha;
            using (var response = await Open(offer.ShaUrl).GetResponseAsync().ConfigureAwait(false))
            using (var reader = new StreamReader(response.GetResponseStream(), Encoding.ASCII)) sha = (await reader.ReadToEndAsync().ConfigureAwait(false)).Trim().Split(' ')[0].ToLowerInvariant();
            if (!Regex.IsMatch(sha, @"\A[a-f0-9]{64}\z")) throw new InvalidDataException("Контрольная сумма обновления повреждена.");
            string folder = Path.Combine(Path.GetTempPath(), "pocket-windows-update"); Directory.CreateDirectory(folder);
            string zip = Path.Combine(folder, "app-" + offer.Id + ".zip");
            if (File.Exists(zip)) File.Delete(zip);
            using (var response = await Open(offer.ZipUrl).GetResponseAsync().ConfigureAwait(false))
            using (var input = response.GetResponseStream())
            using (var output = File.Create(zip))
            {
                var buffer = new byte[1 << 16]; long done = 0; int last = -1, read;
                while ((read = await input.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                {
                    await output.WriteAsync(buffer, 0, read).ConfigureAwait(false); done += read;
                    int percent = offer.Bytes > 0 ? (int)(done * 100 / offer.Bytes) : 0;
                    if (percent != last) { last = percent; progress(Math.Min(percent, 100)); }
                }
            }
            return new[] { zip, sha };
        }

        // The installer runs from a copy of Python outside the application folder, so nothing it replaces is in use.
        internal static void Launch(string root, string zip, string sha)
        {
            root = root.TrimEnd('\\');
            string work = Path.Combine(Path.GetTempPath(), "pocket-windows-updater-" + Guid.NewGuid().ToString("N"));
            string python = Path.Combine(work, "py", "python.exe");
            Copy(Path.Combine(root, "runtime", "python-3.12.14"), Path.Combine(work, "py"));
            string logs = Path.Combine(root, "logs"); Directory.CreateDirectory(logs);
            string script = "@echo off\r\nchcp 65001 >nul\r\nping -n 6 127.0.0.1 >nul\r\n"
                + "\"" + python + "\" -X utf8 -I -B \"" + Path.Combine(root, "scripts", "release_portable.py") + "\" install-app --zip \"" + zip
                + "\" --sha256 " + sha + " --target \"" + root + "\" > \"" + Path.Combine(logs, "update-last.txt") + "\" 2>&1\r\n"
                + "start \"\" \"" + Path.Combine(root, "Launcher.exe") + "\"\r\n";
            string file = Path.Combine(work, "update.cmd");
            File.WriteAllText(file, script, new UTF8Encoding(false));
            Process.Start(new ProcessStartInfo("cmd.exe", "/d /c \"" + file + "\"") { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = work });
        }

        static void Copy(string source, string target)
        {
            Directory.CreateDirectory(target);
            foreach (string file in Directory.GetFiles(source)) File.Copy(file, Path.Combine(target, Path.GetFileName(file)), true);
            foreach (string folder in Directory.GetDirectories(source)) Copy(folder, Path.Combine(target, Path.GetFileName(folder)));
        }

        // What the installer wrote before the launcher came back: the new version, or the reason it failed.
        internal static string TakeReport(string root)
        {
            string path = Path.Combine(root, "logs", "update-last.txt");
            try
            {
                if (!File.Exists(path)) return null;
                string text = File.ReadAllText(path, Encoding.UTF8).Trim(); File.Delete(path);
                var match = Regex.Match(text, "\"release_id\":\\s*\"([0-9.]+)\"");
                if (match.Success) return "Обновлено до версии " + match.Groups[1].Value;
                return text.Length == 0 ? null : "Обновление не удалось, прежняя версия работает.";
            }
            catch (Exception) { return null; }
        }
    }
}
