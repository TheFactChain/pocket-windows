using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

internal static class MaintenanceServiceRegression
{
    private static int checks;
    private static void Assert(bool value, string message)
    { if (!value) throw new Exception(message); checks++; }
    private static void Refused(Action action, string message)
    { try { action(); } catch (ArgumentException) { checks++; return; } throw new Exception(message); }
    private sealed class BrokenOutput : MemoryStream
    { public override void Write(byte[] bytes, int offset, int count) { throw new IOException("fixture full disk"); } }

    public static int Main()
    {
        string nonce = new string('a', 32);
        string hash;
        using (var sha = SHA256.Create())
            hash = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes("fixed script"))).Replace("-", "").ToLowerInvariant();
        MaintenanceService.ValidateArguments(new[] { nonce, hash });
        checks++;
        foreach (string[] bad in new[] {
            new string[0], new[] { nonce }, new[] { nonce, hash, "extra" },
            new[] { nonce + "\n", hash }, new[] { new string('g', 32), hash },
            new[] { nonce, new string('a', 63) }, new[] { nonce, hash + "\n" } })
            Refused(delegate { MaintenanceService.ValidateArguments(bad); }, "Invalid arguments accepted");
        MaintenanceService.RequireGuard("WC-MAINTENANCE-COPY", hash, hash.ToUpperInvariant());
        checks++;
        using (var script = new MemoryStream(Encoding.UTF8.GetBytes("fixed script")))
            Assert(MaintenanceService.HashScript(script) == hash, "Script SHA256 differs");
        using (var changed = new MemoryStream(Encoding.UTF8.GetBytes("changed script")))
            Refused(delegate { MaintenanceService.RequireGuard("WC-MAINTENANCE-COPY", hash, MaintenanceService.HashScript(changed)); }, "Changed script accepted");
        Refused(delegate { MaintenanceService.RequireGuard("HOST", hash, hash); }, "Host accepted");
        Refused(delegate { MaintenanceService.RequireGuard("WC-MAINTENANCE-COPY", hash, new string('0', 64)); }, "Wrong script accepted");
        DateTime started = DateTime.UtcNow.AddMinutes(-1);
        DateTime fresh = started.AddSeconds(1);
        string report = "{\"schema\":1,\"success\":true,\"startedUtc\":\"" + fresh.ToString("o") + "\"}";
        Assert(MaintenanceService.SuccessfulReport(report, fresh, fresh, started), "Fresh successful report rejected");
        Assert(!MaintenanceService.SuccessfulReport(report, started.AddSeconds(-1), fresh, started), "Old file accepted");
        Assert(!MaintenanceService.SuccessfulReport(report.Replace("true", "false"), fresh, fresh, started), "Failed report accepted");
        Assert(!MaintenanceService.SuccessfulReport(report.Replace("true", "\"true\""), fresh, fresh, started), "String success accepted");
        Assert(!MaintenanceService.SuccessfulReport(report.Replace("\"schema\":1", "\"schema\":2"), fresh, fresh, started), "Wrong schema accepted");
        Assert(!MaintenanceService.SuccessfulReport("{}", fresh, fresh, started), "Missing fields accepted");
        Assert(!MaintenanceService.SuccessfulReport("{bad", fresh, fresh, started), "Malformed JSON accepted");
        Assert(!MaintenanceService.SuccessfulReport(report.Replace(fresh.ToString("o"), started.AddSeconds(-1).ToString("o")), fresh, fresh, started), "Old report contents accepted");
        using (var input = new MemoryStream(new byte[MaintenanceService.OutputLimit + 4000]))
        using (var output = new MemoryStream())
        {
            var budget = new MaintenanceService.OutputBudget();
            MaintenanceService.Drain(input, output, budget);
            Assert(output.Length == MaintenanceService.OutputLimit, "Output limit not enforced");
            Assert(input.Position == input.Length, "Output not drained after limit");
            using (var second = new MemoryStream(new byte[4000]))
            using (var secondOutput = new MemoryStream())
            {
                MaintenanceService.Drain(second, secondOutput, budget);
                Assert(secondOutput.Length == 0, "Combined stdout/stderr limit exceeded");
            }
        }
        using (var input = new MemoryStream(new byte[10000]))
        using (var broken = new BrokenOutput())
        {
            bool failed = false;
            try { MaintenanceService.Drain(input, broken, new MaintenanceService.OutputBudget()); }
            catch (IOException) { failed = true; }
            Assert(failed && input.Position == input.Length, "Capture failure abandoned process output");
        }
        Console.WriteLine("Maintenance guards/hash/fresh-report/output: " + checks + " checks PASS; no service execution");
        return 0;
    }
}
