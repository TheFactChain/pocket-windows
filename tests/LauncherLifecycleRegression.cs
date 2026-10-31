using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using WindowsClaude.Launcher;

namespace WindowsClaude.Launcher { public partial class App : Application { internal static bool UpdateProbePending; internal static string UpdateProbeNonce; } }

internal static class Bridge
{
    internal static string Root { get { return AppDomain.CurrentDomain.BaseDirectory; } }
    internal const string PythonDir = "runtime\\python-3.12.14";
    internal static readonly List<string> Calls = new List<string>();
    internal static Func<string, object, Task<Dictionary<string, object>>> Handler;
    internal static Task<Dictionary<string, object>> RunAsync(string root, string action, object payload)
    { Calls.Add(action); return Handler(action, payload); }
    internal static Task<Dictionary<string, object>> PendingOperation(Dictionary<string, object> result) { return result.ContainsKey("pending_test") ? (Task<Dictionary<string, object>>)result["pending_test"] : null; }
    internal static bool Ok(Dictionary<string, object> result) { return result != null && result.ContainsKey("ok") && (bool)result["ok"]; }
    internal static string Message(Dictionary<string, object> result) { return result != null && result.ContainsKey("message") ? (string)result["message"] : "Ответ не получен."; }
    internal static Dictionary<string, object> Failure(string message) { return new Dictionary<string, object> { { "ok", false }, { "message", message } }; }
    internal static Dictionary<string, object> Unknown(string message, string code) { var result = Failure(message); result["outcome_unknown"] = true; return result; }
    internal static string Quote(string value) { return "\"" + value + "\""; }
}

internal static class LauncherLifecycleRegression
{
    static int checks;
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); checks++; }
    static List<Button> Buttons(DependencyObject node)
    {
        var result = new List<Button>();
        foreach (object child in LogicalTreeHelper.GetChildren(node))
            if (child is Button) result.Add((Button)child); else if (child is DependencyObject) result.AddRange(Buttons((DependencyObject)child));
        return result;
    }
    static object Field(object obj, string name) { return obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public).GetValue(obj); }
    static void Set(object obj, string name, object value) { obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic).SetValue(obj, value); }
    static object Call(object obj, string name, params object[] args) { return obj.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly).Invoke(obj, args); }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindow(string name, string title);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    static void Pump(Func<bool> done)
    {
        var frame = new DispatcherFrame(); var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(5) };
        DateTime until = DateTime.UtcNow.AddSeconds(10);
        timer.Tick += delegate { if (done() || DateTime.UtcNow >= until) frame.Continue = false; };
        timer.Start(); Dispatcher.PushFrame(frame); timer.Stop(); Assert(done(), "async operation settled");
    }
    static Dictionary<string, object> Report(string work = "stopped", string gateway = "stopped", bool recovery = false, bool force = false)
    {
        return new Dictionary<string, object> { { "ok", !recovery }, { "message", "Готово" },
            { "checks", new Dictionary<string, object>() }, { "runtime", new Dictionary<string, object> { { "work", work }, { "gateway", gateway } } },
            { "capabilities", new Dictionary<string, object> { { "stop", work != "stopped" || gateway != "stopped" }, { "recover_session", recovery }, { "force_stop", force } } },
            { "settings", new Dictionary<string, object> { { "work_memory_mib", 4096 }, { "vcpus", 2 } } } };
    }
    static MainWindow Window()
    {
        Bridge.Calls.Clear(); Bridge.Handler = (action, payload) => Task.FromResult(Report());
        var window = new MainWindow(); Call(window, "Display", Report()); return window;
    }
    static Dictionary<string, object> SetupReport(bool broken = false, bool runtimeFailure = false)
    {
        var report = Report(runtimeFailure ? "failed" : "stopped", runtimeFailure ? "running" : "stopped", runtimeFailure);
        report["ok"] = false;
        var connection = new Dictionary<string, object> { { "passed", false }, { "title", "Подключение не выбрано" },
            { "detail", "Добавь или выбери подключение для запуска Windows." }, { "action", "connection" }, { "action_label", "Добавить подключение" } };
        if (!broken) connection["setup_required"] = true;
        var checks = new Dictionary<string, object> { { "connection", connection },
            { "runtime_files", new Dictionary<string, object> { { "passed", true }, { "title", "Компоненты" } } } };
        var blockers = new List<Dictionary<string, object>> { connection };
        if (runtimeFailure)
        {
            var failure = new Dictionary<string, object> { { "passed", false }, { "title", "Ошибка сеанса" },
                { "detail", "Нужно завершить прерванный сеанс." }, { "action", "recover_session" }, { "action_label", "Завершить прерванный сеанс" } };
            checks["runtime_state"] = failure; blockers.Add(failure);
        }
        report["checks"] = checks; report["blockers"] = blockers.ToArray(); return report;
    }
    [STAThread] static int Main()
    {
        try
        {
            var app = new App(); app.InitializeComponent();
            App.UpdateProbePending = true;
            Bridge.Calls.Clear();
            var gated = (Task<Dictionary<string, object>>)typeof(MainWindow).GetMethod("Run", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { "start", null });
            Assert(!Bridge.Ok(gated.Result) && Bridge.Calls.Count == 0, "Probe blocks every ordinary backend command");
            var probeWindow = new MainWindow();
            var probeClosing = new CancelEventArgs(); Call(probeWindow, "OnClosing", probeWindow, probeClosing);
            Assert(!probeClosing.Cancel && (bool)Field(probeWindow, "closing") && Bridge.Calls.Count == 0, "Probe exits without settings or VM actions");
            App.UpdateProbePending = false;
            var brokenWindow = new MainWindow();
            Bridge.Handler = (action, payload) => Task.FromResult(Bridge.Unknown("Python did not return a response", "invalid_response"));
            var brokenDiagnosis = (Task<bool>)Call(brokenWindow, "RunAction", "diagnose", false, null); Pump(() => brokenDiagnosis.IsCompleted);
            Set(brokenWindow, "updateOffer", new AppUpdate.Offer { Id = "2026.10.27.1", V2 = true }); Call(brokenWindow, "ShowUpdateOffer");
            Assert(!(bool)Field(brokenWindow, "outcomeUnknown") && ((Button)Field(brokenWindow, "InstallUpdateButton")).IsEnabled, "Broken Python during initial diagnosis does not block independent update bootstrap");
            var window = Window();
            var allFiles = Report();
            allFiles["file_settings"] = new Dictionary<string, object> { { "mode", "all" }, { "folders", new string[0] } };
            Call(window, "LoadSettings", allFiles);
            ((System.Windows.Controls.RadioButton)Field(window, "FilesSelected")).IsChecked = true;
            ((System.Windows.Controls.RadioButton)Field(window, "FilesSelected")).RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent));
            Call(window, "OnStart", null, new RoutedEventArgs());
            Pump(() => !(bool)Field(window, "startPending"));
            Assert(!Bridge.Calls.Contains("start") && !Bridge.Calls.Contains("save-settings"), "Empty selected folders never starts with saved all-disks access");
            Assert(((TextBlock)Field(window, "SettingsNote")).Text.Contains("папку"), "Empty selected folders explains how to continue");
            ((List<string>)Field(window, "folders")).Add(@"C:\Shared");
            Call(window, "ScheduleSave");
            object selectedSettings = null;
            Bridge.Handler = (action, payload) => { if (action == "save-settings") selectedSettings = payload; return Task.FromResult(Report()); };
            Call(window, "OnStart", null, new RoutedEventArgs());
            Pump(() => Bridge.Calls.Contains("start"));
            Assert((string)((Dictionary<string, object>)((Dictionary<string, object>)selectedSettings)["files"])["mode"] == "selected", "Adding a folder saves selected mode before start");
            window = Window();
            Call(window, "LoadProfileView", new Dictionary<string, object> {
                { "profile_id", new string('a', 32) }, { "protocol", "hysteria2" }, { "host", "saved.example" },
                { "port", 443 }, { "name", "Saved" }, { "auth_present", true } });
            Set(window, "testAvailable", true);
            ((TextBox)Field(window, "ProfileHost")).Text = "draft.example";
            ((TextBox)Field(window, "ProfilePort")).Text = "8443";
            ((PasswordBox)Field(window, "ProfileAuth")).Password = "draft-password";
            object testedDraft = null;
            Bridge.Handler = (action, payload) => { if (action == "test-profile") testedDraft = payload; return Task.FromResult(Report()); };
            Call(window, "OnTestProfile", null, new RoutedEventArgs());
            Pump(() => testedDraft != null);
            var testedRequest = (Dictionary<string, object>)testedDraft;
            Assert(testedRequest.ContainsKey("profile"), "Testing the editor sends current fields, not only a saved profile id");
            var testedProfile = (Dictionary<string, object>)testedRequest["profile"];
            Assert((string)testedProfile["host"] == "draft.example" && (int)testedProfile["port"] == 8443, "Test uses edited host and port");
            Assert((string)((Dictionary<string, object>)testedProfile["credentials"])["auth"] == "draft-password", "Test uses edited credentials");
            Assert(!Bridge.Calls.Contains("save-profile") && !Bridge.Calls.Contains("select-profile"), "Testing never saves or selects the draft");
            Bridge.Calls.Clear(); ((TextBox)Field(window, "ProfilePort")).Text = "invalid";
            Call(window, "OnTestProfile", null, new RoutedEventArgs());
            Assert(!Bridge.Calls.Contains("test-profile"), "Invalid draft does not test the older saved profile");
            Call(window, "NewProfile");
            Assert(((Button)Field(window, "ExportButton")).Visibility == Visibility.Collapsed, "New draft cannot export a nonexistent saved profile");
            Assert(((Button)Field(window, "TestProfileButton")).IsEnabled, "Draft test does not require a selected saved profile");
            Assert(((Button)Field(window, "DeleteProfileButton")).Visibility == Visibility.Collapsed, "New draft cannot delete a nonexistent saved profile");
            window = Window();
            string savedId = new string('b', 32);
            var savedRow = new Dictionary<string, object> { { "profile_id", savedId }, { "protocol", "hysteria2" }, { "host", "old.example" }, { "name", "Коннект 1" }, { "selected", true } };
            Call(window, "LoadProfileList", (object)new object[] { savedRow });
            Assert(((List<Dictionary<string, object>>)Field(window, "profiles")).Count == 1, "Fixture list has the saved profile");
            Call(window, "LoadProfileView", savedRow); Call(window, "ShowEditor", "Изменить подключение");
            Assert(((Button)Field(window, "DeleteProfileButton")).Visibility == Visibility.Visible, "Saved profile offers deletion in its editor");
            bool runningAsked = false;
            var refuse = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) };
            refuse.Tick += delegate { var dialog = FindWindow("#32770", "Удалить подключение"); if (dialog != IntPtr.Zero) { runningAsked = true; PostMessage(dialog, 0x111, new IntPtr(7), IntPtr.Zero); refuse.Stop(); } };
            Set(window, "runtimeActive", true); refuse.Start(); Call(window, "OnDeleteProfile", null, new RoutedEventArgs()); refuse.Stop(); Set(window, "runtimeActive", false);
            Assert(!runningAsked && !Bridge.Calls.Contains("delete-profile"), "Selected profile of a running session is not deleted");
            Assert(((TextBlock)Field(window, "EditorError")).Text.Contains("Выключи Windows"), "Refused deletion explains how to continue: " + ((TextBlock)Field(window, "EditorError")).Text);
            Call(window, "EditorErrorText", "");
            object deleted = null;
            Bridge.Handler = (action, payload) => { if (action == "delete-profile") deleted = payload; return Task.FromResult(action == "list-profiles"
                ? new Dictionary<string, object> { { "ok", true }, { "profiles", new object[0] } } : Report()); };
            foreach (int answer in new[] { 7, 6 })
            {
                bool asked = false;
                var reply = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) };
                reply.Tick += delegate { var dialog = FindWindow("#32770", "Удалить подключение"); if (dialog != IntPtr.Zero) { asked = (SendMessage(dialog, 0x400, IntPtr.Zero, IntPtr.Zero).ToInt64() & 0xffff) == 7; PostMessage(dialog, 0x111, new IntPtr(answer), IntPtr.Zero); reply.Stop(); } };
                reply.Start(); Call(window, "OnDeleteProfile", null, new RoutedEventArgs()); reply.Stop();
                Assert(asked, "Deletion asks first and defaults to No");
                if (answer == 7) Assert(deleted == null && !Bridge.Calls.Contains("delete-profile"), "No keeps the profile");
            }
            Pump(() => deleted != null && Field(window, "profileId") == null && !(bool)Field(window, "busy"));
            Assert((string)((Dictionary<string, object>)deleted)["profile_id"] == savedId, "Yes deletes exactly the edited profile");
            Assert(Bridge.Calls.IndexOf("list-profiles") > Bridge.Calls.IndexOf("delete-profile") && ((List<Dictionary<string, object>>)Field(window, "profiles")).Count == 0, "Deletion reloads the saved list");
            Assert(Field(window, "profileId") == null, "Editor forgets the deleted profile");
            window = Window();
            ((TextBox)Field(window, "MemoryInput")).Text = Decimal.MaxValue.ToString(CultureInfo.InvariantCulture);
            Call(window, "StepMemory", 0.5m);
            decimal steppedMemory;
            Assert(MainWindow.TryGiB(((TextBox)Field(window, "MemoryInput")).Text, out steppedMemory) && steppedMemory == 32m, "Memory increment clamps before decimal arithmetic");
            ((TextBox)Field(window, "MemoryInput")).Text = Decimal.MaxValue.ToString(CultureInfo.InvariantCulture);
            Call(window, "StepMemory", -0.5m);
            Assert(MainWindow.TryGiB(((TextBox)Field(window, "MemoryInput")).Text, out steppedMemory) && steppedMemory == 31.5m, "Memory decrement handles the largest decimal without overflow");
            ((TextBox)Field(window, "CpuInput")).Text = Int32.MaxValue.ToString(CultureInfo.InvariantCulture);
            Call(window, "StepCpu", 1);
            Assert(((TextBox)Field(window, "CpuInput")).Text == "32", "CPU increment clamps before integer arithmetic");
            window = Window();
            foreach (string failureCode in new[] { "runtime_error", "supervisor_exited" })
            {
                window = Window();
                var genericStartup = Bridge.Failure("Supervisor завершился во время запуска.\nlogs/receipt.json");
                genericStartup["code"] = failureCode; genericStartup["receipt"] = "logs/runtime-launch-fixture/receipt.json";
                genericStartup["outcome_unknown"] = true;
                Bridge.Handler = (action, payload) => Task.FromResult(action == "start" ? genericStartup : Report());
                var failedStart = (Task<bool>)Call(window, "RunAction", "start", false, null);
                Pump(() => failedStart.IsCompleted);
                string explanation = ((TextBlock)Field(window, "OperationText")).Text;
                Assert(!explanation.Contains("сервер") && !explanation.Contains("подключению"), "Generic supervisor failure never claims the proxy failed");
                Assert(explanation.Contains("Проверка"), "Generic startup failure directs to evidence in diagnostics");
                Assert(((TextBlock)Field(window, "ChecksHeroTitle")).Text.StartsWith("Нужно исправить"), "Diagnostics shows the failed launch even when every preflight check passed");
                Assert(!explanation.Contains("не запустилась") && !((TextBlock)Field(window, "StatusText")).Text.Contains("не запустилась"), "An unknown outcome never claims Windows did not start");
            }
            window = Window();
            var exactFailure = Bridge.Failure("Не удалось открыть диск Windows."); exactFailure["code"] = "disk_failed";
            Bridge.Handler = (action, payload) => Task.FromResult(action == "start" ? exactFailure : Report());
            var exactStart = (Task<bool>)Call(window, "RunAction", "start", false, null);
            Pump(() => exactStart.IsCompleted);
            Assert(((TextBlock)Field(window, "OperationText")).Text.Contains("диск Windows"), "A specific failure keeps its actual cause");
            window = Window();
            Set(window, "updateOffer", new AppUpdate.Offer { Id = "2026.10.27.1", Repair = true });
            Call(window, "ShowUpdateOffer");
            Assert(((Border)Field(window, "UpdatePill")).Visibility == Visibility.Collapsed && ((Button)Field(window, "InstallUpdateButton")).Visibility == Visibility.Visible, "Current-version repair is available without a new-version notification");
            Assert(Buttons((DependencyObject)Field(window, "UpdatesPane")).Count == 2 && ((Button)Field(window, "InstallUpdateButton")).Content.ToString() == "Обновить", "Updates has exactly Check and Update actions");
            Assert(typeof(MainWindow).GetField("UpdatePackageText", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public) == null, "Updates has no technical package details control");
            Call(window, "UpdateBytesProgress", new Dictionary<string, object> { { "phase", "downloading" }, { "done_bytes", 123 }, { "total_bytes", 456 } });
            Assert(!((TextBlock)Field(window, "UpdateStatusText")).Text.Contains("123") && !((TextBlock)Field(window, "UpdateStatusText")).Text.Contains("байт"), "Progress uses a short human status instead of byte counts");
            using (var updateCancellation = new System.Threading.CancellationTokenSource())
            {
                Set(window, "updating", true); Set(window, "updateCancel", updateCancellation); Call(window, "ShowUpdateOffer");
                Assert(((Button)Field(window, "InstallUpdateButton")).Content.ToString() == "Отменить" && !((Button)Field(window, "CheckUpdatesButton")).IsEnabled, "Update becomes Cancel while Check is disabled");
                Call(window, "OnUpdate", null, null); Assert(updateCancellation.IsCancellationRequested, "The same Update action cancels work");
                Set(window, "updating", false); Set(window, "updateCancel", null);
            }
            Set(window, "updateOffer", null); Call(window, "ShowUpdateOffer");
            Call(window, "Busy", true);
            Assert(!((StackPanel)Field(window, "SettingsBody")).IsEnabled, "settings disabled while saving/busy");
            Call(window, "Busy", false);
            var save = new TaskCompletionSource<Dictionary<string, object>>();
            Bridge.Handler = (action, payload) => action == "save-settings" ? save.Task : Task.FromResult(Report());
            ((TextBox)Field(window, "MemoryInput")).Text = "6";
            Call(window, "ScheduleSave"); Call(window, "OnStart", null, new RoutedEventArgs());
            Assert(Bridge.Calls.Count == 1 && Bridge.Calls[0] == "save-settings", "start awaits dirty save");
            var saved = Report(); saved["settings"] = new Dictionary<string, object> { { "work_memory_mib", 6144 }, { "vcpus", 2 } }; save.SetResult(saved);
            Pump(() => Bridge.Calls.Contains("start"));
            Assert(Bridge.Calls.FindAll(value => value == "start").Count == 1, "start runs once after save");
            window = Window();
            ((TextBox)Field(window, "MemoryInput")).Text = "6";
            var earlierSave = new TaskCompletionSource<Dictionary<string, object>>();
            object lastSettings = null;
            Bridge.Handler = (action, payload) => { if (action == "save-settings") { lastSettings = payload; return earlierSave.Task; } return Task.FromResult(Report()); };
            var saving = (Task<bool>)Call(window, "SaveSettingsAsync");
            Call(window, "OnStart", null, new RoutedEventArgs());
            Assert(!Bridge.Calls.Contains("start") && Bridge.Calls.Count == 1, "start joins the save already in progress");
            ((TextBox)Field(window, "MemoryInput")).Text = "8";
            var latest = Report(); latest["settings"] = new Dictionary<string, object> { { "work_memory_mib", 8192 }, { "vcpus", 2 } };
            Bridge.Handler = (action, payload) => { if (action == "save-settings") lastSettings = payload; return Task.FromResult(latest); };
            earlierSave.SetResult(saved);
            Pump(() => Bridge.Calls.Contains("start"));
            Assert(((TextBox)Field(window, "MemoryInput")).Text == "8", "old save never overwrites newer input");
            Assert(Convert.ToInt32(((Dictionary<string, object>)lastSettings)["work_memory_mib"]) == 8192, "latest edits are saved before start");
            window = Window();
            ((TextBox)Field(window, "MemoryInput")).Text = "bad"; Call(window, "ScheduleSave");
            var args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
            Assert(args.Cancel && !(bool)Field(window, "closing"), "invalid settings prevent closing");
            Pump(() => !(bool)Field(window, "closePending"));
            Assert(((TextBox)Field(window, "MemoryInput")).Text == "bad", "failed save preserves input");
            Assert(((TextBlock)Field(window, "SettingsNote")).Text.Length > 0, "failed save remains actionable");
            window = Window();
            ((TextBox)Field(window, "MemoryInput")).Text = "6";
            var failedSave = new TaskCompletionSource<Dictionary<string, object>>();
            Bridge.Handler = (action, payload) => failedSave.Task;
            args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
            Pump(() => Bridge.Calls.Contains("save-settings"));
            Assert(args.Cancel && !(bool)Field(window, "closing"), "close waits for persistence");
            failedSave.SetResult(Bridge.Failure("settings file locked"));
            Pump(() => !(bool)Field(window, "closePending"));
            Call(window, "Display", Report());
            Assert(!(bool)Field(window, "closing") && ((TextBox)Field(window, "MemoryInput")).Text == "6", "failed persistence keeps the panel and input");
            Assert(((TextBlock)Field(window, "SettingsNote")).Text == "settings file locked", "refresh preserves save error");
            foreach (bool closeAgain in new[] { false, true })
            {
                window = Window(); window.ShowActivated = false; window.ShowInTaskbar = false;
                window.Left = -30000; window.Top = -30000; window.Show();
                Pump(() => Bridge.Calls.Contains("diagnose") && !(bool)Field(window, "busy"));
                Bridge.Calls.Clear();
                var delayedCloseSave = new TaskCompletionSource<Dictionary<string, object>>();
                Bridge.Handler = (action, payload) => action == "save-settings" ? delayedCloseSave.Task : Task.FromResult(Report());
                ((TextBox)Field(window, "MemoryInput")).Text = "6"; Call(window, "ScheduleSave");
                args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
                Pump(() => Bridge.Calls.Contains("save-settings"));
                window.ShowExistingPanel();
                if (closeAgain) Call(window, "OnClosing", window, new CancelEventArgs());
                delayedCloseSave.SetResult(saved);
                Pump(() => !(bool)Field(window, "closePending"));
                Assert((bool)Field(window, "closing") == closeAgain, "latest open/close request wins while settings save is pending");
                Assert(closeAgain || window.IsVisible, "repeat startup keeps the saved panel open");
                if (!closeAgain) { Call(window, "FinishClosing"); window.Close(); }
            }
            window = Window(); window.ShowActivated = false; window.ShowInTaskbar = false;
            window.Left = -30000; window.Top = -30000; window.Show();
            Pump(() => Bridge.Calls.Contains("diagnose") && !(bool)Field(window, "busy"));
            using (var cancelUpdate = new System.Threading.CancellationTokenSource())
            {
                Set(window, "updating", true); Set(window, "updateCancel", cancelUpdate);
                args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
                Assert(args.Cancel && cancelUpdate.IsCancellationRequested && (bool)Field(window, "closeAfterUpdateCancel"), "close waits for update cancellation");
                window.ShowExistingPanel();
                Assert(!(bool)Field(window, "closeAfterUpdateCancel") && cancelUpdate.IsCancellationRequested, "repeat startup cancels closing but keeps the update cancelled");
                Call(window, "FinishClosing"); window.Close();
            }
            window = Window();
            Call(window, "Display", Report("running", "running"));
            var stop = new TaskCompletionSource<Dictionary<string, object>>();
            Bridge.Handler = (action, payload) => action == "stop" ? stop.Task : Task.FromResult(Report());
            var first = (Task<bool>)Call(window, "StopSessionAsync", true);
            var second = (Task<bool>)Call(window, "StopSessionAsync", false);
            Assert(Bridge.Calls.FindAll(value => value == "stop").Count == 1, "stop requests share an operation");
            Assert(!(bool)Field(window, "closing"), "tray exit waits for stop");
            stop.SetResult(Bridge.Failure("shutdown_unconfirmed: original cause"));
            Pump(() => first.IsCompleted && second.IsCompleted);
            Assert(!first.Result && !(bool)Field(window, "closing"), "failed stop cannot exit even if subsequent report is stopped");
            Assert(((string)Field(window, "reportText")).Contains("original cause"), "diagnostics preserve stop failure");
            Call(window, "Display", Report());
            Assert(((string)Field(window, "reportText")).Contains("original cause"), "poll cannot erase the original failure");
            Call(window, "Display", Report("running", "running"));
            Bridge.Handler = (action, payload) => Task.FromResult(Report());
            var success = (Task<bool>)Call(window, "StopSessionAsync", true);
            Pump(() => success.IsCompleted && (bool)Field(window, "closing"));
            Assert(success.Result, "tray exits after confirmed stop");
            window = Window();
            var recovery = Report("failed", "running", true, true);
            var token = new Dictionary<string, object> { { "session_nonce", "current" }, { "generation", "1" }, { "process", new Dictionary<string, object> { { "pid", 123 }, { "created_filetime", 456 }, { "image", "qemu.exe" } } } };
            recovery["force_stop"] = token;
            Call(window, "Display", recovery);
            Assert(((TextBlock)Field(window, "StatusText")).Text == "Windows завершилась с ошибкой", "recovery heading");
            Assert(((Button)Field(window, "NextActionButton")).Content.ToString() == "Завершить прерванный сеанс", "recovery action");
            Assert(((Button)Field(window, "ForceStopButton")).Visibility == Visibility.Visible, "force is a separate available action");
            var items = (TrayMenu.Item[])Call(window, "TrayItems");
            Assert(items.Length == 3 && items[2].Text == "Выключить Windows и выйти", "tray offers three explicit actions");
            Set(window, "outcomeUnknown", true);
            items = (TrayMenu.Item[])Call(window, "TrayItems");
            Assert(items[1].Enabled && items[2].Enabled, "recovery remains available from tray for unconfirmed session");
            Set(window, "outcomeUnknown", false);
            var cancel = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) };
            bool cancelled = false, defaultNo = false;
            cancel.Tick += delegate { var dialog = FindWindow("#32770", "Принудительно выключить Windows"); if (dialog != IntPtr.Zero) { cancelled = true; defaultNo = (SendMessage(dialog, 0x400, IntPtr.Zero, IntPtr.Zero).ToInt64() & 0xffff) == 7; PostMessage(dialog, 0x111, new IntPtr(7), IntPtr.Zero); cancel.Stop(); } };
            cancel.Start(); Call(window, "OnForceStop", null, new RoutedEventArgs()); cancel.Stop();
            Assert(cancelled && !Bridge.Calls.Contains("force-stop"), "No cancels force stop without backend action");
            Assert(defaultNo, "destructive confirmation defaults to No");
            object captured = null;
            Bridge.Handler = (action, payload) => { if (action == "force-stop") captured = payload; return Task.FromResult(Report()); };
            var accept = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) };
            var latestToken = new Dictionary<string, object>(token); latestToken["generation"] = "2";
            accept.Tick += delegate { var dialog = FindWindow("#32770", "Принудительно выключить Windows"); if (dialog != IntPtr.Zero) { recovery["force_stop"] = latestToken; Call(window, "Display", recovery); PostMessage(dialog, 0x111, new IntPtr(6), IntPtr.Zero); accept.Stop(); } };
            accept.Start(); Call(window, "OnForceStop", null, new RoutedEventArgs()); accept.Stop();
            Pump(() => captured != null && !(bool)Field(window, "busy"));
            var body = (Dictionary<string, object>)captured;
            Assert((bool)body["confirmed"] && Object.ReferenceEquals(body["session"], latestToken), "Yes sends exactly the latest reported session");
            Call(window, "Display", recovery);
            foreach (double scale in new[] { 1.0, 1.25, 1.5, 2.0 })
            {
                var root = (FrameworkElement)window.Content;
                root.Measure(new Size(744, 418)); root.Arrange(new Rect(0, 0, 744, 418)); root.UpdateLayout();
                var card = (FrameworkElement)Field(window, "LaunchCard");
                foreach (string name in new[] { "StatusText", "NextStepText", "NextActionButton", "ForceStopButton" })
                {
                    var part = (FrameworkElement)Field(window, name);
                    var bounds = part.TransformToAncestor(card).TransformBounds(new Rect(part.RenderSize));
                    Assert(bounds.Top >= 0 && bounds.Bottom <= card.ActualHeight + 1 && bounds.Left >= 0 && bounds.Right <= card.ActualWidth + 1, name + " fits at DPI " + scale);
                    for (DependencyObject parent = part; parent != card; parent = VisualTreeHelper.GetParent(parent)) Assert(!(parent is Viewbox), name + " text never scales");
                }
                var image = new RenderTargetBitmap((int)(744 * scale), (int)(418 * scale), 96 * scale, 96 * scale, PixelFormats.Pbgra32);
                image.Render(root);
                Assert(image.PixelWidth == (int)(744 * scale), "rendered DPI geometry");
            }
            window = Window(); Call(window, "Display", recovery);
            Bridge.Handler = (action, payload) => Task.FromResult(Report());
            var recovered = (Task<bool>)Call(window, "StopSessionAsync", false); Pump(() => recovered.IsCompleted);
            Assert(Bridge.Calls.Contains("recover-session") && !Bridge.Calls.Contains("stop"), "stop routes failed session to recovery");
            window = Window(); Call(window, "Display", recovery);
            var capabilityChanged = Report("failed", "running", true, false);
            Bridge.Handler = (action, payload) => Task.FromResult(capabilityChanged);
            var poll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => poll.IsCompleted);
            Assert(((Button)Field(window, "ForceStopButton")).Visibility == Visibility.Collapsed, "capability-only poll hides force action");
            window = Window(); Call(window, "Display", Report("running", "running"));
            var pending = new TaskCompletionSource<Dictionary<string, object>>();
            var unknown = Bridge.Unknown("stop still pending", "timeout"); unknown["pending_test"] = pending.Task;
            Bridge.Handler = (action, payload) => Task.FromResult(action == "stop" ? unknown : Report());
            var unknownStop = (Task<bool>)Call(window, "StopSessionAsync", true); Pump(() => unknownStop.IsCompleted);
            Assert(!unknownStop.Result && !(bool)Field(window, "closing"), "pending stop never exits");
            var duplicate = (Task<bool>)Call(window, "StopSessionAsync", true); Pump(() => duplicate.IsCompleted);
            Assert(Bridge.Calls.FindAll(value => value == "stop").Count == 1, "pending backend stop cannot be repeated");
            Bridge.Calls.Clear();
            window = new MainWindow();
            Bridge.Handler = (action, payload) => Task.FromResult(Bridge.Failure("Python missing"));
            var initialCheck = (Task<bool>)Call(window, "RunAction", "diagnose", false, null);
            Pump(() => initialCheck.IsCompleted);
            Assert((string)Field(window, "workState") == "unknown" && Field(window, "readinessReport") == null, "initial diagnosis failed before observing a session");
            args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
            Pump(() => !(bool)Field(window, "closePending"));
            Assert((bool)Field(window, "closing"), "initial backend failure must not trap an unopened session");
            window = Window();
            ((TextBox)Field(window, "MemoryInput")).Text = "6";
            Set(window, "updating", true); Set(window, "updateHandedOff", true);
            Call(window, "Busy", true);
            var lifecycle = (DispatcherTimer)Field(window, "lifecycleTimer");
            var saveTimer = (DispatcherTimer)Field(window, "saveTimer");
            lifecycle.Start(); saveTimer.Start();
            var tray = new System.Windows.Forms.NotifyIcon();
            Set(window, "trayIcon", tray);
            args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
            Assert(!args.Cancel && (bool)Field(window, "closing"), "updater handoff exits despite busy state");
            window.ShowExistingPanel();
            Assert((bool)Field(window, "closing") && !window.IsVisible, "repeat startup never reopens a panel handed to the updater");
            Assert(!lifecycle.IsEnabled && !saveTimer.IsEnabled && Field(window, "trayIcon") == null, "handoff clears timers and tray");
            Assert(!Bridge.Calls.Contains("save-settings"), "handoff never saves settings again");
            foreach (string settledAction in new[] { "stop", "recover-session" })
            {
                window = Window();
                Call(window, "Display", settledAction == "stop" ? Report("running", "running") : Report("failed", "running", true));
                Bridge.Handler = (action, payload) => action == "diagnose"
                    ? Task.FromException<Dictionary<string, object>>(new System.IO.IOException("status refresh unavailable"))
                    : Task.FromResult(new Dictionary<string, object> { { "ok", true }, { "message", "Session stopped successfully" } });
                var settled = (Task<bool>)Call(window, "StopSessionAsync", false); Pump(() => settled.IsCompleted);
                Assert(!(bool)Field(window, "outcomeUnknown"), settledAction + " success remains settled when diagnosis throws");
                Assert(((string)Field(window, "reportText")).Contains("status refresh unavailable"), "refresh failure remains in diagnostics");
                Bridge.Handler = (action, payload) => Task.FromResult(Report());
                var diagnosed = (Task<bool>)Call(window, "RunAction", "diagnose", false, null); Pump(() => diagnosed.IsCompleted);
                Assert(!(bool)Field(window, "outcomeUnknown") && ((Button)Field(window, "StartButton")).IsEnabled, "later diagnosis restores launch after " + settledAction);
                var stopped = window.GetType().GetProperty("SessionStopped", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(window, null);
                Assert((bool)stopped, "later diagnosis permits stopped-session close");
            }
            window = Window(); Call(window, "Display", Report("running", "running"));
            Bridge.Handler = (action, payload) => Task.FromResult(action == "stop" ? Bridge.Unknown("transport outcome unknown", "transport_error") : Report());
            var unconfirmed = (Task<bool>)Call(window, "StopSessionAsync", false); Pump(() => unconfirmed.IsCompleted);
            Assert((bool)Field(window, "outcomeUnknown") && !((Button)Field(window, "StartButton")).IsEnabled, "a genuinely unconfirmed backend remains fail closed");
            Bridge.Calls.Clear(); window = new MainWindow();
            var unreadableRuntime = Report("unknown", "unknown"); unreadableRuntime["ok"] = false;
            unreadableRuntime["checks"] = new Dictionary<string, object> { { "runtime_state", new Dictionary<string, object> {
                { "passed", false }, { "title", "Состояние VM не удалось прочитать" }, { "detail", "Access denied" }, { "action", "retry" } } } };
            unreadableRuntime["capabilities"] = new Dictionary<string, object> { { "stop", false } };
            Bridge.Handler = (action, payload) => Task.FromResult(unreadableRuntime);
            var fullCheck = (Task<bool>)Call(window, "RunAction", "diagnose", false, null); Pump(() => fullCheck.IsCompleted);
            Assert(Field(window, "readinessReport") != null && !(bool)Field(window, "runtimeActive") && !(bool)Field(window, "startAttempted"), "full preflight cannot observe a runtime before launch");
            args = new CancelEventArgs(); Call(window, "OnClosing", window, args);
            Pump(() => !(bool)Field(window, "closePending"));
            Assert((bool)Field(window, "closing"), "unreadable runtime in full preflight does not trap an unopened panel");
            window = Window(); var setup = SetupReport(); Call(window, "Display", setup);
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "Нужна настройка", "empty connection is setup, not a counted problem");
            Assert(((TextBlock)Field(window, "ChecksHeroTitle")).Text == "Нужна настройка", "empty connection never says needs fixing");
            Assert(!((Button)Field(window, "StartButton")).IsEnabled && !(bool)Field(window, "ready"), "setup still blocks Windows launch");
            Assert(((Button)Field(window, "RefreshButton")).Content.ToString() == "Добавить подключение", "diagnostics offers connection setup directly");
            Assert(((TextBlock)Field(window, "NextStepText")).Text == "Добавь или выбери подключение для запуска Windows.", "setup explains the next step directly");
            Assert(!((string)Field(window, "reportText")).Contains("Нужно исправить: Подключение"), "copied diagnostics also distinguish setup from errors");
            var setupRow = (DockPanel)((StackPanel)Field(window, "CheckCards")).Children[0];
            Assert(((Wpf.Ui.Controls.SymbolIcon)((Border)setupRow.Children[0]).Child).Symbol != Wpf.Ui.Controls.SymbolRegular.Warning16, "setup card is neutral, without warning icon");
            Set(window, "profilesLoaded", true); Bridge.Calls.Clear();
            ((Button)Field(window, "RefreshButton")).RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent));
            Assert((AppPage)Field(window, "currentPage") == AppPage.Connection && Bridge.Calls.Count == 0, "diagnostic setup action opens existing connection route");
            Bridge.Handler = (action, payload) => Task.FromResult(setup);
            foreach (System.Windows.Input.KeyBinding binding in window.InputBindings)
                if (binding.Key == System.Windows.Input.Key.F5) binding.Command.Execute(null);
            Assert(Bridge.Calls.Contains("diagnose") && (AppPage)Field(window, "currentPage") == AppPage.Diagnostics, "F5 still refreshes diagnostics when setup is needed");
            Call(window, "Display", SetupReport(true));
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема", "broken connection without setup marker remains an error");
            var errorRow = (DockPanel)((StackPanel)Field(window, "CheckCards")).Children[0];
            Assert(((Wpf.Ui.Controls.SymbolIcon)((Border)errorRow.Children[0]).Child).Symbol == Wpf.Ui.Controls.SymbolRegular.Warning16, "changing only setup marker refreshes warning style");
            Assert(((Button)Field(window, "RefreshButton")).Content.ToString() == "Проверить снова", "real connection error keeps diagnostic refresh");
            foreach (bool toSetup in new[] { true, false })
            {
                var previous = SetupReport(toSetup); previous["message"] = "Подключение";
                var polled = SetupReport(!toSetup); polled["message"] = "Подключение"; polled["report_kind"] = "runtime_status";
                ((Dictionary<string, object>)((Dictionary<string, object>)previous["checks"])["connection"])["title"] = "Подключение";
                ((Dictionary<string, object>)((Dictionary<string, object>)polled["checks"])["connection"])["title"] = "Подключение";
                ((Dictionary<string, object>)previous["capabilities"])["test_profile"] = false;
                ((Dictionary<string, object>)polled["capabilities"])["test_profile"] = false;
                Call(window, "Display", previous);
                var mergedPoll = (Dictionary<string, object>)Call(window, "MergeRuntimeStatus", polled);
                Assert(Bridge.Message(previous) == Bridge.Message(mergedPoll) && Bridge.Ok(previous) == Bridge.Ok(mergedPoll), "poll fixture keeps message and readiness unchanged");
                var json = new System.Web.Script.Serialization.JavaScriptSerializer();
                Assert(json.Serialize(previous["capabilities"]) == json.Serialize(mergedPoll["capabilities"]), "poll fixture keeps capabilities unchanged");
                Set(window, "panelHiddenForSession", true); Set(window, "panelDismissedByUser", true);
                Bridge.Handler = (action, payload) => Task.FromResult(polled);
                var setupPoll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => setupPoll.IsCompleted);
                Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == (toSetup ? "Нужна настройка" : "1 проблема"), "runtime poll applies setup marker transition " + toSetup);
                Assert(((Button)Field(window, "RefreshButton")).Content.ToString() == (toSetup ? "Добавить подключение" : "Проверить снова"), "runtime poll updates diagnostic action " + toSetup);
                var polledRow = (DockPanel)((StackPanel)Field(window, "CheckCards")).Children[0];
                Assert((((Wpf.Ui.Controls.SymbolIcon)((Border)polledRow.Children[0]).Child).Symbol == Wpf.Ui.Controls.SymbolRegular.Warning16) != toSetup, "runtime poll updates setup/error style " + toSetup);
                Set(window, "panelHiddenForSession", false);
            }
            Call(window, "Display", SetupReport(false, true));
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема" && ((TextBlock)Field(window, "ChecksHeroTitle")).Text == "Нужно исправить: 1", "mixed setup and runtime failure count only the real error");
            Assert(((TextBlock)Field(window, "StatusText")).Text == "Windows завершилась с ошибкой", "setup never masks recovery status");
            Assert(((Button)Field(window, "RefreshButton")).Content.ToString() == "Добавить подключение" && ((Button)Field(window, "NextActionButton")).Content.ToString() == "Завершить прерванный сеанс", "mixed state preserves setup route and recovery action");
            var mixed = SetupReport();
            var components = new Dictionary<string, object> { { "passed", false }, { "title", "Не хватает компонентов" },
                { "detail", "Восстанови компоненты из комплекта." }, { "action", "prepare" }, { "action_label", "Восстановить компоненты" } };
            ((Dictionary<string, object>)mixed["checks"])["runtime_files"] = components;
            mixed["blockers"] = new object[] { ((Dictionary<string, object>)mixed["checks"])["connection"], components };
            Call(window, "Display", mixed);
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема" && ((TextBlock)Field(window, "StatusText")).Text == "Не хватает компонентов", "real failure takes priority even when setup blocker arrives first");
            Assert(((Button)Field(window, "NextActionButton")).Content.ToString() == "Восстановить компоненты" && ((Button)Field(window, "RefreshButton")).Content.ToString() == "Добавить подключение", "repair and connection setup paths remain available together");
            Call(window, "Display", setup); Call(window, "Display", Bridge.Failure("Shutdown was not confirmed"));
            Assert(((TextBlock)Field(window, "StatusText")).Text != "Подключение не выбрано" && ((string)Field(window, "reportText")).Contains("Shutdown was not confirmed"), "setup never replaces a shutdown failure");
            var partialFolders = Report("running", "running");
            partialFolders["host_files"] = new Dictionary<string, object> { { "status", "partial" }, { "share_count", 1 },
                { "unavailable", new object[] {
                    new Dictionary<string, object> { { "label", "Archive на ПК" }, { "status", "unavailable" } },
                    new Dictionary<string, object> { { "label", "Startup на ПК" }, { "status", "protected" } } } } };
            Call(window, "Display", partialFolders);
            Assert(((TextBlock)Field(window, "NextStepText")).Text.Contains("Часть папок ПК недоступна"), "partial file sharing never claims all folders are ready");
            Assert(((string)Field(window, "reportText")).Contains("Archive на ПК: папка недоступна") && ((string)Field(window, "reportText")).Contains("Startup на ПК: защищённая папка"), "file sharing diagnostics names missing folders and gives their reasons");
            var folderCards = (StackPanel)Field(window, "CheckCards");
            Assert(folderCards.Children.Count > 0 && folderCards.Children[0] is DockPanel, "missing folders have a visible warning card");
            var folderWarning = (StackPanel)((DockPanel)folderCards.Children[0]).Children[1];
            Assert(((TextBlock)folderWarning.Children[1]).Text.Contains("Startup на ПК: защищённая папка"), "visible diagnostics explains each missing folder");
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема" && (bool)Field(window, "ready"), "partial folder warning is visible without blocking VM startup");
            var stable = Report("running", "running");
            Call(window, "Display", stable);
            Bridge.Handler = (action, payload) => Task.FromResult(stable);
            window.ShowExistingPanel();
            Pump(() => window.IsVisible && !(bool)Field(window, "busy"));
            ((DispatcherTimer)Field(window, "lifecycleTimer")).Stop();
            Call(window, "Display", stable);
            string stableHeading = ((TextBlock)Field(window, "StatusText")).Text;
            Bridge.Handler = (action, payload) => Task.FromResult(Bridge.Failure("temporary status failure"));
            var failedPoll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => failedPoll.IsCompleted);
            Assert(((TextBlock)Field(window, "StatusText")).Text == "Состояние Windows не подтверждено", "a failed poll shows its temporary warning: " + ((TextBlock)Field(window, "StatusText")).Text);
            Bridge.Handler = (action, payload) => Task.FromResult(stable);
            var restoredPoll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => restoredPoll.IsCompleted);
            Assert(((TextBlock)Field(window, "StatusText")).Text == stableHeading, "an unchanged successful poll clears the temporary failure warning");
            var reconnectWarning = Report("running", "reconnecting");
            reconnectWarning["ok"] = false;
            reconnectWarning["checks"] = new Dictionary<string, object> { { "runtime_files", new Dictionary<string, object> {
                { "passed", false }, { "title", "Компоненты требуют проверки" }, { "detail", "fixture component failure" } } } };
            reconnectWarning["network_warning"] = "Связь восстанавливается. Интернет Windows временно закрыт.";
            Call(window, "Display", reconnectWarning);
            var warningText = (TextBlock)Field(window, "NetworkText");
            Assert(warningText.Visibility == Visibility.Visible && warningText.Text == (string)reconnectWarning["network_warning"], "the actual network warning stays visible alongside an unrelated failed check");
            var changedWarning = new Dictionary<string, object>(reconnectWarning);
            changedWarning["report_kind"] = "runtime_status";
            changedWarning["network_warning"] = "Проверка связи задерживается. Текущий доступ не подтверждён.";
            Bridge.Handler = (action, payload) => Task.FromResult(changedWarning);
            var warningPoll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => warningPoll.IsCompleted);
            Assert(warningText.Text == (string)changedWarning["network_warning"], "a changed warning redraws even when readiness and runtime states stay the same");
            var serverStopped = Report("running", "running");
            serverStopped["host_files"] = new Dictionary<string, object> { { "status", "failed" }, { "code", "FILE_SERVER_STOPPED" }, { "share_count", 1 } };
            Call(window, "Display", serverStopped);
            string serverAction = ((TextBlock)Field(window, "NextStepText")).Text;
            Assert(serverAction.Contains("выключи") && serverAction.Contains("панели") && serverAction.Contains("запусти заново"), "a stopped host file server asks for a full safe session restart");
            Assert(!serverAction.Contains("следующем входе") && ((string)Field(window, "reportText")).Contains("Файловый сервер остановился"), "file server failure never promises recovery by guest logon alone");
            var fileRecovery = Report("running", "running");
            var recoveryFields = new Dictionary<string, object> { { "status", "ready" }, { "share_count", 1 },
                { "recovery_required", true }, { "recovery_log", "logs/host-files-recovery-fixture.jsonl" } };
            fileRecovery["host_files"] = recoveryFields;
            Call(window, "Display", fileRecovery);
            Assert(((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема" && (bool)Field(window, "ready"), "retained file recovery becomes visible without blocking Windows");
            Assert(((string)Field(window, "reportText")).Contains("logs/host-files-recovery-fixture.jsonl") && ((TextBlock)Field(window, "NextStepText")).Text.IndexOf("журнал",StringComparison.OrdinalIgnoreCase)>=0, "file recovery shows its journal and an actionable next step: "+((TextBlock)Field(window,"NextStepText")).Text);
            recoveryFields["recovery_log_failed"] = true;
            Call(window, "Display", fileRecovery);
            Assert(((string)Field(window, "reportText")).Contains("Не удалось записать журнал"), "failed durability never claims the recovery journal was saved");
            recoveryFields["recovery_channel_failed"] = true;
            recoveryFields["status"] = "failed"; recoveryFields["code"] = "RECOVERY_CHANNEL_FAILED";
            Call(window, "Display", fileRecovery);
            Assert(((TextBlock)Field(window, "NextStepText")).Text.Contains("выключи") && !((string)Field(window, "reportText")).Contains("Файловый сервер остановился"), "a broken recovery channel asks for a safe restart without inventing process death");
            var clipboardFailed = Report("running", "running");
            clipboardFailed["clipboard"] = new Dictionary<string, object> { { "status", "failed" }, { "code", "CLIPBOARD_START_FAILED" } };
            Call(window, "Display", clipboardFailed);
            Assert(((TextBlock)Field(window, "ClipboardDetail")).Text.Contains("не готов") && ((TextBlock)Field(window, "DiagnosticSummary")).Text == "1 проблема", "native clipboard startup failure is visible");
            Assert((bool)Field(window, "ready") && ((string)Field(window, "reportText")).Contains("CLIPBOARD_START_FAILED"), "clipboard failure keeps Windows available and reports a safe reason");
            var clipboardReady = Report("running", "running");
            clipboardReady["report_kind"] = "runtime_status";
            clipboardReady["clipboard"] = new Dictionary<string, object> { { "status", "ready" }, { "code", "CLIPBOARD_READY" }, { "last_transfer_utc", "2026-10-27T00:12:34+00:00" } };
            Bridge.Handler = (action, payload) => Task.FromResult(clipboardReady);
            var clipboardPoll = (Task)Call(window, "RefreshRuntimeAsync"); Pump(() => clipboardPoll.IsCompleted);
            Assert(((TextBlock)Field(window, "ClipboardDetail")).Text.Contains("передача подтверждена"), "clipboard status alone refreshes the visible last confirmed transfer");
            foreach (string protocol in new[] { "hysteria2", "http", "https", "socks5" })
            {
                var capabilities = Report("running", "running");
                capabilities["connection"] = new Dictionary<string, object> { { "selected_profile", new Dictionary<string, object> {
                    { "name", "Fixture" }, { "protocol", protocol } } } };
                Call(window, "Display", capabilities);
                string detail = ((TextBlock)Field(window, "ConnectionDetail")).Text;
                Assert(detail.Contains("только IPv4") && detail.Contains(protocol == "hysteria2" ? "TCP и UDP" : "Только TCP"), "selected connection shows the actual UDP and external IPv4 capabilities: " + protocol);
            }
            Assert(((TextBlock)Field(window, "FilesLimits")).Text.Contains("жёсткими ссылками"), "file sharing exposes the retained hardlink limitation");
            var oversizedClipboard = Report("running", "running");
            oversizedClipboard["clipboard"] = new Dictionary<string, object> { { "status", "unconfirmed" }, { "code", "GUEST_CLIPBOARD_TEXT_TOO_LARGE" } };
            Call(window, "Display", oversizedClipboard);
            string copyAdvice = ((TextBlock)Field(window, "ClipboardDetail")).Text;
            Assert(copyAdvice.Contains("слишком большой") && copyAdvice.Contains("файлом") && !copyAdvice.Contains("перезапусти"), "over-limit text has actionable advice without requiring a Windows restart");
            Console.WriteLine("PASS launcher lifecycle: " + checks + " checks"); return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
