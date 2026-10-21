using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class DesktopBackendIntegration
{
    private static Dictionary<string, object> Settings(Dictionary<string, object> reply)
    {
        if (!Bridge.Ok(reply)) throw new InvalidOperationException(Bridge.Message(reply));
        return (Dictionary<string, object>)reply["settings"];
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = args[0];
            if (Directory.Exists(Path.Combine(root, "images")) || Directory.Exists(Path.Combine(root, "private")))
                throw new InvalidOperationException("Use a separate QA copy without disks or profiles");
            int[] before = Process.GetProcessesByName("qemu-system-x86_64").Select(p => p.Id).OrderBy(p => p).ToArray();
            Dictionary<string, object> original = Settings(Bridge.Run(root, "get-settings", null));
            Dictionary<string, object> desired = new Dictionary<string, object>(original);
            desired["lan_port"] = 32861;
            Dictionary<string, object> saved = Settings(Bridge.Run(root, "save-settings", desired));
            Dictionary<string, object> loaded = Settings(Bridge.Run(root, "get-settings", null));
            if (Convert.ToInt32(saved["lan_port"]) != 32861 || Convert.ToInt32(loaded["lan_port"]) != 32861)
                throw new InvalidOperationException("Native stdin/settings roundtrip failed");
            string configPath = Path.Combine(root, "config", "runtime.json");
            const string invalidConfig = "{fixture broken public config";
            File.WriteAllText(configPath, invalidConfig);
            Dictionary<string, object> recovery = Bridge.Run(root, "get-settings", null);
            if (!Bridge.Ok(recovery) || !(bool)recovery["recovery_required"] ||
                Convert.ToInt32(Settings(recovery)["lan_port"]) != 31861 || File.ReadAllText(configPath) != invalidConfig)
                throw new InvalidOperationException("Settings recovery is not available without rewriting invalid data");
            using (SettingsDialog dialog = new SettingsDialog(Settings(recovery))) { }
            Settings(Bridge.Run(root, "save-settings", desired));
            if (!Directory.GetFiles(Path.GetDirectoryName(configPath), "runtime.invalid-*.json")
                    .Any(path => File.ReadAllText(path) == invalidConfig))
                throw new InvalidOperationException("Recovered settings lost the invalid original");
            Directory.CreateDirectory(Path.Combine(root, "state"));
            File.WriteAllText(Path.Combine(root, "state", "gateway-network-accepted.json"),
                "{\"native_tests_passed\":true,\"wan_bypass_packets\":0}");
            Dictionary<string, object> report = Bridge.Run(root, "diagnose", null);
            Dictionary<string, object> checks = (Dictionary<string, object>)report["checks"];
            bool runtime = (bool)((Dictionary<string, object>)checks["runtime_files"])["passed"];
            bool engine = (bool)((Dictionary<string, object>)checks["engine_identity"])["passed"];
            bool guests = (bool)((Dictionary<string, object>)checks["guest_files"])["passed"];
            if (!runtime || !engine || guests || Bridge.Ok(report) || (bool)report["private_profile_read"])
                throw new InvalidOperationException("Unexpected preview preflight");
            if ((bool)report["network_verified"] || Convert.ToString(report["network_acceptance_scope"]) != "historical")
                throw new InvalidOperationException("Copied acceptance falsely certifies this QA copy");
            Dictionary<string, object> start = Bridge.Run(root, "start", null);
            if (Bridge.Ok(start)) throw new InvalidOperationException("Unconfigured preview must not start guests");
            int[] after = Process.GetProcessesByName("qemu-system-x86_64").Select(p => p.Id).OrderBy(p => p).ToArray();
            if (!before.SequenceEqual(after)) throw new InvalidOperationException("VM process set changed during backend-only verification");
            using (MainForm form = new MainForm())
            {
                Dictionary<string, object> readyDisplay = new Dictionary<string, object>(report);
                readyDisplay["ok"] = true;
                typeof(MainForm).GetMethod("Display", BindingFlags.Instance | BindingFlags.NonPublic)
                    .Invoke(form, new object[] { readyDisplay });
                Label statusLabel = (Label)typeof(MainForm).GetField("status", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
                TextBox details = (TextBox)typeof(MainForm).GetField("details", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
                if (!details.Text.Contains(Convert.ToString(report["network_warning"])))
                    throw new InvalidOperationException("UI hid the historical network warning");
                Dictionary<string, object> invalid = new Dictionary<string, object>(desired);
                invalid["lan_port"] = 80;
                Task saving = form.SaveSettingsAndDiagnose(invalid);
                Stopwatch deadline = Stopwatch.StartNew();
                while (!saving.IsCompleted && deadline.ElapsedMilliseconds < 15000)
                {
                    Application.DoEvents();
                    Thread.Sleep(10);
                }
                if (!saving.IsCompleted) throw new InvalidOperationException("UI save test timed out");
                saving.GetAwaiter().GetResult();
                if (!form.StatusText.Contains("lan_port"))
                    throw new InvalidOperationException("UI erased the concrete settings-save failure with a later diagnosis");
            }
            Console.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "ok", true }, { "native_json_stdin_settings_roundtrip", true },
                { "bundled_runtime_preflight", true }, { "unconfigured_start_blocked", true },
                { "qemu_process_set_unchanged", true }, { "private_profile_read", false },
                { "actual_guest_network_test", false }, { "settings_failure_remains_visible", true },
                { "invalid_settings_recovery_and_original_preserved", true },
                { "historical_network_warning_visible", true } }));
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1; }
    }
}
