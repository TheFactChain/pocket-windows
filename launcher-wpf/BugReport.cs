// Problem report: the draft that survives closing, a package built without Python, and delivery to the support receiver.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal static class BugReport
{
    // support/receiver.py behind Caddy on sunimo.ru (support/caddy-route.txt).
    internal const string ReceiverUrl = "https://sunimo.ru/pocket-support";
    const long MaxUploadBytes = 20 * 1024 * 1024;  // mirrors scripts/bug_report.MAX_REPORT_BYTES

    static string Draft(string root) { return Path.Combine(root, "logs", "reports", "draft.json"); }

    internal static Dictionary<string, object> LoadDraft(string root)
    {
        try { return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Draft(root))); }
        catch (Exception) { return null; }
    }

    internal static void SaveDraft(string root, Dictionary<string, object> draft)
    {
        try { Directory.CreateDirectory(Path.GetDirectoryName(Draft(root))); File.WriteAllText(Draft(root), new JavaScriptSerializer().Serialize(draft)); }
        catch (Exception) { }  // losing a draft write must not interrupt typing
    }

    internal static void ClearDraft(string root) { try { File.Delete(Draft(root)); } catch (Exception) { } }

    internal static string NewId() { return Guid.NewGuid().ToString("N"); }

    // Without Python only the description, the shared history and launcher logs go in; the reason is stated inside.
    internal static string MinimalZip(string root, string id, string what, string steps, string contact, string reason)
    {
        string folder = Path.Combine(root, "logs", "reports", id), path = Path.Combine(folder, "report.zip");
        Directory.CreateDirectory(folder);
        var replace = new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>(root, "<Pocket Windows>") };
        foreach (var name in new[] { "USERPROFILE", "USERNAME", "COMPUTERNAME" })
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (!String.IsNullOrEmpty(value) && value.Length >= 3) replace.Add(new KeyValuePair<string, string>(value, name == "USERPROFILE" ? "%USERPROFILE%" : name == "USERNAME" ? "<user>" : "<pc>"));
        }
        Func<string, string> clean = text =>
        {
            foreach (var pair in replace.OrderByDescending(item => item.Key.Length))
                foreach (var variant in new[] { pair.Key, pair.Key.Replace('\\', '/'), pair.Key.Replace("\\", "\\\\") })
                    text = System.Text.RegularExpressions.Regex.Replace(text, System.Text.RegularExpressions.Regex.Escape(variant), pair.Value.Replace("$", "$$"), System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            return text;
        };
        var sources = new List<Dictionary<string, object>>();
        string partial = path + ".partial";
        if (File.Exists(partial)) File.Delete(partial);
        using (var archive = ZipFile.Open(partial, ZipArchiveMode.Create))
        {
            Action<string, string> add = (name, text) =>
            {
                using (var writer = new StreamWriter(archive.CreateEntry(name).Open(), new UTF8Encoding(false))) writer.Write(text);
            };
            var json = new JavaScriptSerializer();
            add("report.json", clean(json.Serialize(new Dictionary<string, object> { { "report_id", id }, { "what", what }, { "steps", steps }, { "contact", contact }, { "created_utc", DateTime.UtcNow.ToString("o") } })));
            foreach (var name in new[] { "events.jsonl", "events.1.jsonl", "launcher-error.log", "launcher-ui.log", "update-details.txt" })
            {
                string source = Path.Combine(root, "logs", name);
                try
                {
                    using (var stream = new FileStream(source, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    {
                        if (stream.Length > 4 * 1024 * 1024) stream.Seek(-4 * 1024 * 1024, SeekOrigin.End);
                        add("logs/" + name, clean(new StreamReader(stream, Encoding.UTF8).ReadToEnd()));
                    }
                    sources.Add(new Dictionary<string, object> { { "source", "logs/" + name }, { "status", "ok" } });
                }
                catch (FileNotFoundException) { sources.Add(new Dictionary<string, object> { { "source", "logs/" + name }, { "status", "missing" } }); }
                catch (UnauthorizedAccessException) { sources.Add(new Dictionary<string, object> { { "source", "logs/" + name }, { "status", "denied" } }); }
                catch (IOException error) { sources.Add(new Dictionary<string, object> { { "source", "logs/" + name }, { "status", "error" }, { "reason", error.GetType().Name } }); }
            }
            sources.Add(new Dictionary<string, object> { { "source", "diagnostics" }, { "status", "unavailable" }, { "reason", reason } });
            add("manifest.json", json.Serialize(new Dictionary<string, object> { { "schema", 1 }, { "report_id", id }, { "minimal", true }, { "sources", sources } }));
        }
        if (File.Exists(path)) File.Delete(path);
        File.Move(partial, path);
        return path;
    }

    // One report id is one card: a retry after a network error sends the same id and the receiver answers with the existing card.
    internal static async Task<Dictionary<string, object>> SendAsync(string id, string zip)
    {
        var info = new FileInfo(zip);
        if (info.Length > MaxUploadBytes) throw new InvalidDataException("Архив больше 20 МБ.");
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
        var request = (HttpWebRequest)WebRequest.Create(ReceiverUrl.TrimEnd('/') + "/v1/reports");
        request.Method = "POST"; request.ContentType = "application/zip"; request.Timeout = 120000; request.ReadWriteTimeout = 120000;
        request.Headers["X-Report-Id"] = id; request.UserAgent = "PocketWindows-Launcher";
        request.ContentLength = info.Length;
        // Timeout covers only synchronous calls; a stalled server must not leave the window waiting forever.
        var upload = Upload(request, zip);
        if (await Task.WhenAny(upload, Task.Delay(180000)).ConfigureAwait(false) != upload)
        {
            request.Abort();
            throw new WebException("Сервер поддержки не ответил вовремя.", WebExceptionStatus.Timeout);
        }
        return await upload.ConfigureAwait(false);
    }

    static async Task<Dictionary<string, object>> Upload(HttpWebRequest request, string zip)
    {
        using (var body = await request.GetRequestStreamAsync().ConfigureAwait(false))
        using (var file = File.OpenRead(zip)) await file.CopyToAsync(body).ConfigureAwait(false);
        using (var response = (HttpWebResponse)await request.GetResponseAsync().ConfigureAwait(false))
        using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
        {
            string text = await reader.ReadToEndAsync().ConfigureAwait(false);
            if (text.Length > 65536) throw new InvalidDataException("Слишком длинный ответ сервера.");
            return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(text);
        }
    }
}
