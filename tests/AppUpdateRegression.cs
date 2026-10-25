using System;
using System.IO;
using System.Threading;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using WindowsClaude.Launcher;

internal static class AppUpdateRegression
{
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
    static void RecoveryPaths()
    {
        string original = Path.Combine(Path.GetTempPath(), "recovery-" + Guid.NewGuid().ToString("N"));
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
    static int Main(string[] args)
    {
        try
        {
            Assert(!AppUpdate.Newer("99999999999999999999999.1", "2026.10.24.13"), "Overflow must be invalid, not throw");
            Assert(AppUpdate.Newer("2026.10.25.1", "2026.10.24.13"), "New date ordering");
            Assert(!AppUpdate.Newer("2026.10.24.13.0", "2026.10.24.13"), "Trailing zero equality");
            if (args.Length == 0) return 0;
            if (args[1] == "recovery") { RecoveryPaths(); return 0; }
            Environment.SetEnvironmentVariable("POCKET_WINDOWS_UPDATE_API", args[0] + "/release");
            var result = AppUpdate.CheckAsync("2026.10.24.13").GetAwaiter().GetResult();
            if (args[1] == "unavailable") { Assert(result.Offer == null && result.Message.Contains("пакет"), "Missing asset must be explained"); return 0; }
            Assert(result.Offer != null && result.Offer.Id == "2026.10.25.1", "Expected complete update offer");
            var cancel = new CancellationTokenSource();
            if (args[1] == "cancel") cancel.Cancel();
            try
            {
                var package = AppUpdate.DownloadAsync(result.Offer, delegate(int percent) { Assert(percent >= 0 && percent <= 100, "progress range"); }, cancel.Token).GetAwaiter().GetResult();
                Assert(args[1] == "valid", "Bad/cancelled download must not succeed");
                Assert(File.ReadAllText(package.Zip) == "test package", "Downloaded bytes");
                AppUpdate.Discard(package);
            }
            catch (OperationCanceledException) { Assert(args[1] == "cancel", "Unexpected cancellation"); }
            catch (InvalidDataException) { Assert(args[1] == "corrupt" || args[1] == "size", "Unexpected validation failure"); }
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
