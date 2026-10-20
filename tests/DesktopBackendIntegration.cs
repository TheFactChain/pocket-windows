using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
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
            int[] before = Process.GetProcessesByName("qemu-system-x86_64").Select(p => p.Id).OrderBy(p => p).ToArray();
            Dictionary<string, object> original = Settings(Bridge.Run(root, "get-settings", null));
            Dictionary<string, object> desired = new Dictionary<string, object>(original);
            desired["lan_port"] = 32861;
            Dictionary<string, object> saved = Settings(Bridge.Run(root, "save-settings", desired));
            Dictionary<string, object> loaded = Settings(Bridge.Run(root, "get-settings", null));
            if (Convert.ToInt32(saved["lan_port"]) != 32861 || Convert.ToInt32(loaded["lan_port"]) != 32861)
                throw new InvalidOperationException("Native stdin/settings roundtrip failed");
            Dictionary<string, object> report = Bridge.Run(root, "diagnose", null);
            Dictionary<string, object> checks = (Dictionary<string, object>)report["checks"];
            bool runtime = (bool)((Dictionary<string, object>)checks["runtime_files"])["passed"];
            bool engine = (bool)((Dictionary<string, object>)checks["engine_identity"])["passed"];
            bool guests = (bool)((Dictionary<string, object>)checks["guest_files"])["passed"];
            if (!runtime || !engine || guests || Bridge.Ok(report) || (bool)report["private_profile_read"])
                throw new InvalidOperationException("Unexpected preview preflight");
            Dictionary<string, object> start = Bridge.Run(root, "start", null);
            if (Bridge.Ok(start)) throw new InvalidOperationException("Unconfigured preview must not start guests");
            int[] after = Process.GetProcessesByName("qemu-system-x86_64").Select(p => p.Id).OrderBy(p => p).ToArray();
            if (!before.SequenceEqual(after)) throw new InvalidOperationException("VM process set changed during backend-only verification");
            using (MainForm form = new MainForm())
            {
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
                { "actual_guest_network_test", false }, { "settings_failure_remains_visible", true } }));
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1; }
    }
}
