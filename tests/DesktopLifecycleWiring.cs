using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopLifecycleWiring
{
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    static void Context() { SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); }
    static void Pump(Task task)
    {
        Stopwatch elapsed = Stopwatch.StartNew();
        while (!task.IsCompleted && elapsed.ElapsedMilliseconds < 5000) { Application.DoEvents(); Thread.Sleep(1); }
        Assert(task.IsCompleted, "Fixture action did not settle"); task.GetAwaiter().GetResult();
    }
    static void Layout(Control control)
    {
        typeof(Control).GetMethod("CreateControl", BindingFlags.NonPublic | BindingFlags.Instance, null, new[] { typeof(bool) }, null).Invoke(control, new object[] { true });
        control.PerformLayout();
    }
    static IEnumerable<Control> Tree(Control root) { yield return root; foreach (Control child in root.Controls) foreach (Control item in Tree(child)) yield return item; }
    static void Click(Button button) { typeof(Button).GetMethod("OnClick", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(button, new object[] { EventArgs.Empty }); Application.DoEvents(); }
    static Dictionary<string, object> Report(string action)
    {
        return new Dictionary<string, object> { { "ok", false }, { "message", "Нужна подготовка" },
            { "checks", new Dictionary<string, object>() }, { "blockers", new object[] { new Dictionary<string, object> {
                { "title", "Нужна подготовка" }, { "detail", "Проверь комплект" }, { "action", action }, { "action_label", "Исправить" } } } } };
    }
    static Dictionary<string, object> Success() { return new Dictionary<string, object> { { "ok", true }, { "message", "Шлюз восстановлен" } }; }
    static void TimeoutCases()
    {
        foreach (string action in new[] { "start", "test-profile" }) Assert(Bridge.TimeoutMilliseconds(action) == 330000, "Start/profile budget must cover the backend wait");
        Assert(Bridge.TimeoutMilliseconds("stop") == 510000, "Stop budget must cover graceful shutdown");
        Assert(Bridge.TimeoutMilliseconds("repair-gateway") == 180000, "Gateway recovery budget changed");
        Assert(Bridge.TimeoutMilliseconds("recover-session") >= 150000, "Session recovery must cover graceful gateway stop and ownership checks");
        Assert(Bridge.TimeoutMilliseconds("prepare-host") == 210000, "Host preparation budget must cover its bounded helper wait");
        Assert(Bridge.TimeoutMilliseconds("diagnose") == 30000, "Ordinary checks must remain bounded");
    }
    static void MappingCases()
    {
        List<string> calls = new List<string>();
        using (MainForm form = new MainForm(false, (action, payload) => {
            calls.Add(action);
            return Task.FromResult(action == "list-profiles" ? new Dictionary<string, object> { { "ok", true }, { "profiles", new object[0] } } : Report("retry"));
        }))
        {
            Layout(form); form.DisplayReport(Report("connection")); Context();
            Click((Button)typeof(MainForm).GetField("nextAction", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form));
            Assert(form.ActivePage == AppPage.Connection, "connection blocker must select the connection page");
            Assert(calls.SequenceEqual(new[] { "list-profiles" }), "connection blocker must load profiles rather than repeat diagnostics");
        }
        calls.Clear();
        using (MainForm form = new MainForm(false, (action, payload) => { calls.Add(action); return Task.FromResult(action == "repair-gateway" ? Success() : Report("retry")); }))
        {
            Layout(form); form.DisplayReport(Report("repair_gateway")); Context();
            Click((Button)typeof(MainForm).GetField("nextAction", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form));
            Assert(calls.SequenceEqual(new[] { "repair-gateway", "diagnose" }), "Gateway blocker must recover then refresh diagnostics");
            calls.Clear(); Context(); Pump(form.NavigateAsync(AppPage.Diagnostics)); calls.Clear();
            Click(form.RepairGatewayButton);
            Assert(calls.SequenceEqual(new[] { "repair-gateway", "diagnose" }), "Diagnostics recovery button must invoke the same real action");
        }
        calls.Clear();
        using (MainForm form = new MainForm(false, (action, payload) => { calls.Add(action); return Task.FromResult(action == "recover-session" ? Success() : Report("retry")); }))
        {
            Layout(form); form.DisplayReport(Report("recover_session")); Context();
            Click((Button)typeof(MainForm).GetField("nextAction", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form));
            Assert(calls.SequenceEqual(new[] { "recover-session", "diagnose" }), "Interrupted session must use explicit recovery then refresh");
        }
    }
    static void BusyFailureCases()
    {
        List<string> calls = new List<string>();
        TaskCompletionSource<Dictionary<string, object>> pending = new TaskCompletionSource<Dictionary<string, object>>();
        using (MainForm form = new MainForm(false, (action, payload) => { calls.Add(action); return pending.Task; }))
        {
            Layout(form); form.DisplayReport(Report("repair_gateway")); Context(); Task repair = form.RunAction("repair-gateway", false);
            Assert(!repair.IsCompleted && !form.RepairGatewayButton.Enabled, "Pending recovery must remain busy");
            Pump(form.RunAction("start", false)); Assert(calls.Count == 1, "Recovery must share the single-flight gate");
            pending.SetResult(Bridge.Failure("Нет архива восстановления шлюза")); Pump(repair);
            Assert(form.StatusText.Contains("Нет архива"), "Recovery package failure must remain visible");
            Assert(calls.Count == 1, "Failed recovery must not be overwritten by a refresh");
        }
        using (MainForm form = new MainForm(false, (action, payload) => Task.FromResult(Bridge.Unknown("Исход восстановления неизвестен", "timeout"))))
        {
            Layout(form); Context(); Pump(form.RunAction("repair-gateway", false));
            Assert(!form.RepairGatewayButton.Enabled, "Unknown recovery outcome must prevent duplicate mutation");
        }
    }
    static void StartupTimeoutCase()
    {
        string receipt = "logs/runtime-launch-fixture/receipt.json";
        Dictionary<string,object> current = Report("retry");
        current["runtime"] = new Dictionary<string,object> { { "work", "running" }, { "gateway", "running" } };
        current["capabilities"] = new Dictionary<string,object> { { "stop", true } };
        current["startup"] = new Dictionary<string,object> { { "receipt", receipt }, { "phase", "starting" }, { "supervisor_active", true } };
        int reads = 0;
        using (MainForm form = new MainForm(false, (action, payload) => {
            if (action == "diagnose" || action == "status") { reads++; return Task.FromResult(current); }
            var reply = Bridge.Unknown("Запуск ещё продолжается", "startup_timeout"); reply["receipt"] = receipt;
            return Task.FromResult(reply);
        }))
        {
            Layout(form); form.DisplayReport(Report("retry")); Context(); Pump(form.RunAction("start", false));
            Assert(reads == 1, "An uncertain start must refresh diagnostics immediately");
            Assert(!form.StartButton.Enabled && !form.SettingsButton.Enabled, "An uncertain start must hold the mutation gate");
            current["startup"] = new Dictionary<string,object> { { "receipt", receipt }, { "phase", "ready" }, { "supervisor_active", true } };
            typeof(MainForm).GetField("lifecycleEnabled", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            Context(); Pump(form.RefreshRuntimeAsync());
            Assert(form.StopButton.Enabled && !form.StartButton.Enabled, "A confirmed receipt must restore Stop for the running session");
            Assert(form.SettingsButton.Enabled, "A confirmed receipt must release the other UI controls too");
        }
    }
    static void LayoutCases()
    {
        foreach (float scale in new[] { 1f, 2f })
        using (MainForm form = new MainForm(false, (action, payload) => Report("retry")))
        {
            form.ClientSize = new Size(1040, 660); Layout(form);
            var fonts = Tree(form).Select(control => new { Control = control, Font = control.Font }).ToArray();
            foreach (var item in fonts) item.Control.Font = new Font(item.Font.FontFamily, item.Font.SizeInPoints * scale, item.Font.Style);
            Context(); Pump(form.NavigateAsync(AppPage.Diagnostics)); Layout(form);
            foreach (Button button in new[] { form.RepairGatewayButton, form.PrepareButton, form.PrepareHostButton })
            {
                Point point = button.Location;
                for (Control parent = button.Parent; parent != form; parent = parent.Parent) point.Offset(parent.Location);
                Assert(form.ClientRectangle.Contains(new Rectangle(point, button.Size)), "Diagnostic action is outside fixed viewport at scale " + scale + ": " + button.Text);
                Assert(button.Parent.ClientRectangle.Contains(button.Bounds), "Diagnostic action clipped by its parent at scale " + scale + ": " + button.Text);
                Rectangle bounds = button.Bounds;
                for (Control parent = button.Parent; parent != form; parent = parent.Parent)
                {
                    Assert(parent.ClientRectangle.Contains(bounds), "Diagnostic action clipped by an ancestor at scale " + scale + ": " + button.Text);
                    bounds.Offset(parent.Location);
                }
                Assert(button.Width >= button.GetPreferredSize(Size.Empty).Width, "Diagnostic caption clipped: " + button.Text);
            }
            Assert(form.ClientSize == new Size(1040, 660), "Layout check must not expand the viewport");
        }
    }
    static void ReconnectCases()
    {
        string[][] cases = {
            new[] { "stopped", "starting", "Шлюз запускается", "Windows: выключена. Шлюз: запускается." },
            new[] { "restarting", "running", "Windows перезагружается", "Windows: перезагружается. Шлюз: работает." },
            new[] { "running", "reconnecting", "Windows работает", "Windows: работает. Шлюз: восстанавливает связь." },
            new[] { "stopped", "connection-failed", "Ошибка подключения шлюза", "Windows: выключена. Шлюз: ошибка подключения." },
            new[] { "control-unavailable-guest-running", "running", "Windows работает без управления", "Windows: работает без управления. Шлюз: работает." },
            new[] { "stopped", "stopped", "Готово к запуску Windows", "Windows: выключена. Шлюз: выключен." }
        };
        foreach (string[] item in cases)
        using (MainForm form = new MainForm(false, (action, payload) => Task.FromResult(Success())))
        {
            Layout(form);
            Dictionary<string, object> report = Report("retry"); report["ok"] = true;
            report["runtime"] = new Dictionary<string, object> { { "work", item[0] }, { "gateway", item[1] } };
            report["capabilities"] = new Dictionary<string, object> { { "stop", true } };
            report["network_warning"] = item[1] == "reconnecting" ? "Связь восстанавливается. Windows продолжает работать." : "";
            form.DisplayReport(report);
            Assert(form.StatusText == item[2], "Actual work/gateway states must drive the heading: " + item[0] + "/" + item[1] + "; got " + form.StatusText);
            Label summary = (Label)typeof(MainForm).GetField("nextStep", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form);
            Assert(summary.Text == item[3], "Visible home summary must describe both actual VM states; got " + summary.Text);
            Assert(form.StopButton.Enabled, "Stop capability must keep the action available");
            Assert(form.StartButton.Enabled == (item[0] == "stopped" && item[1] == "stopped"), "Actual activity, rather than Stop capability, must gate Start");
            if (item[1] == "reconnecting") Assert(Tree(form).OfType<Label>().Any(control => control.Text.Contains("Связь восстанавливается")), "Reconnect explanation must remain visible in the home content");
        }
    }
    [STAThread]
    static int Main()
    {
        try { Application.EnableVisualStyles(); TimeoutCases(); MappingCases(); BusyFailureCases(); StartupTimeoutCase(); ReconnectCases(); LayoutCases(); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
