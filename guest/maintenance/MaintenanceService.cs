using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Management;
using System.Security.Cryptography;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

internal sealed class MaintenanceService : ServiceBase
{
    private const string Name = "WindowsClaudeMaintenance";
    private const string Folder = @"C:\ProgramData\WindowsClaudeMaintenance";
    private const string Script = Folder + @"\Compact-Windows.ps1";
    internal const int OutputLimit = 1024 * 1024;
    private readonly string nonce;
    private readonly string scriptHash;
    private readonly object state = new object();
    private volatile bool interrupted;
    private volatile bool finished;
    private bool resultWritten;

    private MaintenanceService(string[] args)
    {
        nonce = args[0].ToLowerInvariant();
        scriptHash = args[1].ToLowerInvariant();
        ServiceName = Name;
        AutoLog = false;
        CanStop = true;
        CanShutdown = true;
    }

    public static int Main(string[] args)
    {
        try { ValidateArguments(args); }
        catch (ArgumentException) { return 2; }
        ServiceBase.Run(new MaintenanceService(args));
        return 0;
    }

    internal static void ValidateArguments(string[] args)
    {
        if (args == null || args.Length != 2 || args[0] == null || args[1] == null
            || !Regex.IsMatch(args[0], @"\A[0-9a-fA-F]{32}\z")
            || !Regex.IsMatch(args[1], @"\A[0-9a-fA-F]{64}\z"))
            throw new ArgumentException("Expected nonce32hex and scriptSHA25664hex only.");
    }

    internal static void RequireGuard(string serial, string expected, string actual)
    {
        if (serial != "WC-MAINTENANCE-COPY" || expected == null || actual == null
            || !Regex.IsMatch(expected, @"\A[0-9a-fA-F]{64}\z")
            || !String.Equals(expected, actual, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Maintenance hardware or script identity does not match.");
    }

    internal static string HashScript(Stream script)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(script)).Replace("-", "").ToLowerInvariant();
    }

    internal static bool SuccessfulReport(string json, DateTime created, DateTime written, DateTime started)
    {
        if (created < started || written < started || json == null || json.Length > 65536) return false;
        try
        {
            var value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
            object schema, success, stamp;
            DateTime reportStart;
            return value != null && value.TryGetValue("schema", out schema) && schema is int && (int)schema == 1
                && value.TryGetValue("success", out success) && success is bool && (bool)success
                && value.TryGetValue("startedUtc", out stamp) && stamp is string
                && DateTime.TryParse((string)stamp, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out reportStart)
                && reportStart.ToUniversalTime() >= started;
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    internal sealed class OutputBudget { internal int Written; }
    internal static void Drain(Stream input, Stream output, OutputBudget budget)
    {
        byte[] bytes = new byte[4096];
        int count;
        IOException failed = null;
        while ((count = input.Read(bytes, 0, bytes.Length)) != 0)
            lock (budget)
            {
                int keep = Math.Min(count, OutputLimit - budget.Written);
                if (keep > 0 && failed == null)
                    try { output.Write(bytes, 0, keep); budget.Written += keep; }
                    catch (IOException error) { failed = error; }
            }
        if (failed != null) throw new IOException("Output capture failed after draining the process.", failed);
    }

    protected override void OnStart(string[] args)
    {
        // Foreground thread survives OnStop returning; it never abandons a running DISM.
        new Thread(Work) { IsBackground = false }.Start();
    }
    protected override void OnStop() { lock (state) { if (!finished) interrupted = true; } }
    protected override void OnShutdown() { OnStop(); }

    private static void Plain(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Maintenance paths cannot be reparse points.");
    }

    private FileStream Guard()
    {
        foreach (string path in new[] { @"C:\", @"C:\ProgramData", Folder, Script }) Plain(path);
        string serial = null;
        using (var search = new ManagementObjectSearcher("SELECT IdentifyingNumber FROM Win32_ComputerSystemProduct"))
        using (var results = search.Get())
            foreach (ManagementObject item in results)
                using (item) { if (serial != null) throw new ArgumentException("Ambiguous machine identity."); serial = item["IdentifyingNumber"] as string; }
        var script = new FileStream(Script, FileMode.Open, FileAccess.Read, FileShare.Read);
        try
        {
            RequireGuard(serial, scriptHash, HashScript(script));
            return script;
        }
        catch { script.Dispose(); throw; }
    }

    private int RunCleanup(out DateTime started)
    {
        var start = new ProcessStartInfo(@"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + Script + "\" -ConfirmMaintenanceCopy") {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Folder,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true };
        // CreateNew refuses stale runs and linked output files instead of overwriting them.
        using (var stdout = new FileStream(Folder + @"\stdout.log", FileMode.CreateNew, FileAccess.Write, FileShare.Read))
        using (var stderr = new FileStream(Folder + @"\stderr.log", FileMode.CreateNew, FileAccess.Write, FileShare.Read))
        using (var process = new Process { StartInfo = start })
        {
            var budget = new OutputBudget();
            Exception outputError = null;
            started = DateTime.UtcNow;
            process.Start();
            try
            {
                process.StandardInput.Close();
                var output = new Thread(delegate() { try { Drain(process.StandardOutput.BaseStream, stdout, budget); } catch (Exception error) { lock (budget) { outputError = error; } } });
                var errors = new Thread(delegate() { try { Drain(process.StandardError.BaseStream, stderr, budget); } catch (Exception error) { lock (budget) { outputError = error; } } });
                output.Start(); errors.Start();
                process.WaitForExit();
                output.Join(); errors.Join();
                if (outputError != null) throw new IOException("Output capture failed.");
                return process.ExitCode;
            }
            finally { process.WaitForExit(); }
        }
    }

    private static string FindReport(DateTime started)
    {
        foreach (string path in Directory.GetFiles(@"C:\ProgramData", "WindowsClaude-compact-*.json"))
        {
            Plain(path);
            var info = new FileInfo(path);
            if (info.Length <= 65536 && SuccessfulReport(File.ReadAllText(path), info.CreationTimeUtc, info.LastWriteTimeUtc, started))
                return path;
        }
        throw new IOException("Fresh successful maintenance report not found.");
    }

    private static int SystemCommand(string executable, string args)
    {
        using (var process = Process.Start(new ProcessStartInfo(executable, args) {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Folder }))
        { process.WaitForExit(); return process.ExitCode; }
    }

    private void SaveResult(bool success, int exitCode, string report, string error)
    {
        string path = Folder + @"\result.json";
        if (resultWritten) Plain(path);
        var value = new Dictionary<string, object> { { "schema", 1 }, { "nonce", nonce },
            { "script_sha256", scriptHash }, { "success", success }, { "exit_code", exitCode },
            { "report_file", report }, { "error", error } };
        using (var file = new FileStream(path, resultWritten ? FileMode.Truncate : FileMode.CreateNew, FileAccess.Write, FileShare.Read))
        using (var writer = new StreamWriter(file, new UTF8Encoding(false)))
            writer.Write(new JavaScriptSerializer().Serialize(value));
        resultWritten = true;
    }

    private void Work()
    {
        FileStream approved = null;
        bool guarded = false, success = false;
        int exitCode = -1;
        string report = null, error = null, stage = "guard";
        try
        {
            approved = Guard();
            guarded = true;
            stage = "cleanup";
            if (File.Exists(Folder + @"\result.json")) throw new IOException("Maintenance already attempted.");
            DateTime started;
            exitCode = RunCleanup(out started);
            if (exitCode != 0) throw new IOException("Cleanup command failed.");
            stage = "report validation";
            report = FindReport(started);
            success = !interrupted;
            if (interrupted) error = "Maintenance was interrupted by a stop or shutdown request.";
        }
        catch (Exception failure) { error = stage + " failed (" + failure.GetType().Name + ")."; }
        finally
        {
            // A failed identity guard permits no writes, system commands, or shutdown.
            if (guarded)
            {
                lock (state)
                {
                    if (interrupted) { success = false; error = "Maintenance was interrupted by a stop or shutdown request."; }
                    finished = true;
                }
                try { SaveResult(success, exitCode, report, error); } catch { success = false; }
                try
                {
                    if (SystemCommand(@"C:\Windows\System32\sc.exe", "delete " + Name) != 0)
                    {
                        success = false; error = "Service registration deletion failed.";
                        SaveResult(success, exitCode, report, error);
                    }
                }
                catch { try { SaveResult(false, exitCode, report, "Service registration deletion failed."); } catch { } }
                try
                {
                    if (SystemCommand(@"C:\Windows\System32\shutdown.exe", "/s /t 0") != 0)
                        SaveResult(false, exitCode, report, "Normal shutdown request failed.");
                }
                catch { try { SaveResult(false, exitCode, report, "Normal shutdown request failed."); } catch { } }
            }
            if (approved != null) approved.Dispose();
        }
    }
}
