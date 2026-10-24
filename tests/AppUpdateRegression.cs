using System;
using System.IO;
using System.Threading;
using WindowsClaude.Launcher;

internal static class AppUpdateRegression
{
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
    static int Main(string[] args)
    {
        try
        {
            Assert(!AppUpdate.Newer("99999999999999999999999.1", "2026.10.24.13"), "Overflow must be invalid, not throw");
            Assert(AppUpdate.Newer("2026.10.25.1", "2026.10.24.13"), "New date ordering");
            Assert(!AppUpdate.Newer("2026.10.24.13.0", "2026.10.24.13"), "Trailing zero equality");
            if (args.Length == 0) return 0;
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
