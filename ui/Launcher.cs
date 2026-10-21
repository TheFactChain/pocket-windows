using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Bridge
{
    private sealed class PendingReply : Dictionary<string,object>
    {
        internal readonly Task<Dictionary<string,object>> Settled;
        internal PendingReply(Dictionary<string,object> reply,Task<Dictionary<string,object>> settled) : base(reply) { Settled = settled; }
    }

    internal static Task<Dictionary<string,object>> PendingOperation(Dictionary<string,object> reply)
    {
        PendingReply pending = reply as PendingReply;
        return pending == null ? null : pending.Settled;
    }
    internal static string Root
    {
        get { return Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location); }
    }

    internal static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char letter in value)
        {
            if (letter == '\\') { slashes++; continue; }
            if (letter == '"') result.Append('\\', slashes * 2 + 1).Append('"');
            else result.Append('\\', slashes).Append(letter);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    internal static Dictionary<string, object> Failure(string message)
    {
        return new Dictionary<string, object> { { "ok", false }, { "message", message } };
    }

    internal static bool Ok(Dictionary<string, object> value)
    {
        return value != null && value.ContainsKey("ok") && value["ok"] is bool && (bool)value["ok"];
    }

    internal static string Message(Dictionary<string, object> value)
    {
        return value != null && value.ContainsKey("message") ? Convert.ToString(value["message"]) : Ok(value) ? "Готово." : "Ответ не получен.";
    }

    internal static Dictionary<string, object> Run(string root, string action, object payload)
    {
        return RunAsync(root, action, payload).GetAwaiter().GetResult();
    }

    internal static int TimeoutMilliseconds(string action)
    {
        switch (action)
        {
            case "start":
            case "test-profile": return 330000;
            case "stop": return 510000;
            case "repair-gateway":
            case "recover-session": return 180000;
            case "prepare-host": return 210000;
            default: return 30000;
        }
    }

    internal static async Task<Dictionary<string, object>> RunAsync(string root, string action, object payload)
    {
        if (action == "prepare-components") return await PrepareComponentsAsync(root).ConfigureAwait(false);
        if (action == "prepare-host")
        {
            string script = Path.Combine(root, "scripts", "Prepare-Host.ps1");
            if (!File.Exists(script)) return Failure("Не найден сценарий подготовки этого ПК. Распакуй полный комплект.");
            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe");
            ProcessStartInfo prepare = new ProcessStartInfo(powershell, String.Join(" ", new[] { "-NoProfile", "-NonInteractive", "-File", script, "-Root", root }.Select(Quote)));
            prepare.WorkingDirectory = root;
            Dictionary<string,object> reply = await RunProcessAsync(prepare, TimeoutMilliseconds(action)).ConfigureAwait(false);
            string code = reply.ContainsKey("code") ? Convert.ToString(reply["code"]) : "error";
            if (code == "outcome_unknown") { reply["outcome_unknown"] = true; reply["message"] = "Исход подготовки ПК неизвестен. Изменение компонентов может продолжаться. Не повторяй действие до проверки состояния."; }
            else if (code == "restart_required") { reply["restart_required"] = true; reply["message"] = "Для завершения подготовки перезагрузи ПК самостоятельно."; }
            else if (code == "uac_cancelled") reply["message"] = "Запрос администратора отменён. Подготовка ПК не выполнена.";
            else if (code == "ready") reply["message"] = "Этот ПК готов к аппаратному ускорению виртуальных машин.";
            return reply;
        }
        string python = Path.Combine(root, "runtime", "python-3.12.14", "python.exe");
        string backend = Path.Combine(root, "scripts", "portable_app.py");
        if (!File.Exists(python))
        {
            Dictionary<string, object> failure = Failure("Не найден Python");
            failure["blockers"] = new object[] { new Dictionary<string, object> {
                { "title", "Не хватает компонентов" },
                { "detail", "Python отсутствует. Нажми «Восстановить Python и QEMU»: приложение вернёт файлы из комплекта." },
                { "action", "prepare" }, { "action_label", "Восстановить Python и QEMU" }
            } };
            return failure;
        }
        if (!File.Exists(backend))
            return Failure("Не найдены файлы приложения. Распакуй полный комплект рядом с Launcher.exe.");
        string[] args = { "-X", "utf8", "-I", "-S", "-B", backend, "--root", root, "--action", action };
        ProcessStartInfo info = new ProcessStartInfo(python, String.Join(" ", args.Select(Quote)));
        info.WorkingDirectory = root;
        return await ExecuteProcessAsync(info, TimeoutMilliseconds(action), payload).ConfigureAwait(false);
    }

    internal static Dictionary<string, object> Unknown(string message, string code)
    {
        Dictionary<string, object> failure = Failure(message);
        failure["code"] = code;
        failure["outcome_unknown"] = true;
        return failure;
    }

    internal static Task<Dictionary<string, object>> RunProcessAsync(ProcessStartInfo info, int timeoutMilliseconds)
    {
        return ExecuteProcessAsync(info, timeoutMilliseconds, null);
    }

    private static async Task<Dictionary<string, object>> ExecuteProcessAsync(ProcessStartInfo info, int timeoutMilliseconds, object payload)
    {
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = info.RedirectStandardOutput = info.RedirectStandardError = true;
        info.StandardOutputEncoding = info.StandardErrorEncoding = Encoding.UTF8;
        Process process = new Process { StartInfo = info, EnableRaisingEvents = true };
        TaskCompletionSource<bool> exited = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        process.Exited += delegate { exited.TrySetResult(true); };
        try { process.Start(); }
        catch (Exception error)
        {
            process.Dispose();
            if (!(error is System.ComponentModel.Win32Exception) && !(error is InvalidOperationException) && !(error is IOException)) throw;
            Dictionary<string, object> failure = Failure("Не удалось запустить служебный компонент. Проверь доступ к папке и целостность файлов.");
            failure["code"] = "process_start_failed";
            return failure;
        }
        Task<Dictionary<string, object>> response = SettleResponseAsync(ReadResponseAsync(process, exited.Task, payload), exited.Task);
        if (await Task.WhenAny(response, Task.Delay(timeoutMilliseconds)).ConfigureAwait(false) != response)
        {
            // Keep draining pipes until the child exits; timeout never terminates a supervisor or a VM.
            Task cleanup = response.ContinueWith(completed => { var observed = completed.Exception; process.Dispose(); },
                CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
            return new PendingReply(Unknown("Ответ не получен вовремя. Операция может продолжаться. Дождись завершения служебного процесса и повтори диагностику. VM и служебные процессы не останавливались.", "timeout"),response);
        }
        try
        {
            Dictionary<string,object> reply = await response.ConfigureAwait(false);
            string code = reply.ContainsKey("code") ? Convert.ToString(reply["code"]) : "";
            return new[] {"invalid_response","process_failed","transport_error"}.Contains(code) ? new PendingReply(reply,response) : reply;
        }
        finally { process.Dispose(); }
    }

    private static async Task<Dictionary<string,object>> SettleResponseAsync(Task<Dictionary<string,object>> response,Task exited)
    {
        await Task.WhenAll(response,exited).ConfigureAwait(false);
        return await response.ConfigureAwait(false);
    }

    private static async Task<Dictionary<string, object>> ReadResponseAsync(Process process, Task exited, object payload)
    {
        try
        {
            Task<string> stdout = process.StandardOutput.ReadToEndAsync();
            Task<string> stderr = process.StandardError.ReadToEndAsync();
            if (payload != null)
            {
                byte[] body = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(payload));
                await process.StandardInput.BaseStream.WriteAsync(body, 0, body.Length).ConfigureAwait(false);
                await process.StandardInput.BaseStream.FlushAsync().ConfigureAwait(false);
            }
            process.StandardInput.Close();
            await Task.WhenAll(exited, stdout, stderr).ConfigureAwait(false);
            Dictionary<string, object> result;
            try { result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(stdout.Result.Trim()); }
            catch (ArgumentException) { result = null; }
            catch (InvalidOperationException) { result = null; }
            if (result == null || !result.ContainsKey("ok") || !(result["ok"] is bool))
                return Unknown("Служебный компонент не вернул корректный ответ. Исход операции не подтверждён. Проверь состояние через диагностику.", "invalid_response");
            if (process.ExitCode != 0 && Ok(result))
                return Unknown("Служебный компонент завершился с ошибкой. Исход операции не подтверждён. Проверь состояние через диагностику.", "process_failed");
            return result;
        }
        catch (Exception error)
        {
            if (!(error is IOException) && !(error is InvalidOperationException) && !(error is ObjectDisposedException)) throw;
            return Unknown("Связь со служебным компонентом прервалась. Исход операции неизвестен. Проверь состояние через диагностику.", "transport_error");
        }
    }

    private static async Task<Dictionary<string, object>> PrepareComponentsAsync(string root)
    {
        string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe");
        string script = Path.Combine(root, "scripts", "Prepare-Dependencies.ps1");
        if (!File.Exists(script)) return Failure("Не найден сценарий подготовки. Распакуй полный комплект приложения.");
        ProcessStartInfo info = new ProcessStartInfo(powershell, String.Join(" ", new[] { "-NoProfile", "-NonInteractive", "-File", script, "-Project", root }.Select(Quote)));
        info.WorkingDirectory = root;
        Dictionary<string, object> reply = await RunProcessAsync(info, 180000).ConfigureAwait(false);
        string code = reply.ContainsKey("code") ? Convert.ToString(reply["code"]) : "error";
        Dictionary<string, string> messages = new Dictionary<string, string> {
            { "ready", "Компоненты готовы. Файлы Python и QEMU проверены." },
            { "repaired", "Компоненты восстановлены. Файлов: " + (reply.ContainsKey("repaired_files") ? Convert.ToString(reply["repaired_files"]) : "несколько") + "." },
            { "runtime_busy", "Компоненты заняты работающей VM. Штатно заверши Windows и шлюз, затем повтори подготовку." },
            { "package_missing", "Не найден runtime/components-recovery.zip. Распакуй полный комплект приложения и повтори подготовку." },
            { "invalid_package", "Пакет восстановления повреждён или не соответствует комплекту. Распакуй полный проверенный комплект приложения." },
            { "error", "Подготовка не завершена. Проверь свободное место и доступ к папке. Если ошибка сохранится, распакуй полный комплект." }
        };
        if (messages.ContainsKey(code)) reply["message"] = messages[code];
        else if (code == "invalid_response" || code == "process_failed")
            reply["message"] = "Windows PowerShell не подтвердил подготовку. Проверь разрешение запуска scripts/Prepare-Dependencies.ps1 в свойствах комплекта и политике Windows. Политика выполнения не изменялась; исход подготовки не подтверждён.";
        return reply;
    }

}

internal sealed class SettingsDialog : Form
{
    private readonly SettingsEditor editor = new SettingsEditor();
    private readonly Dictionary<string, TextBox> fields;
    private readonly Label validationError;
    internal Dictionary<string, object> Value
    {
        get { Dictionary<string, object> value; if (!editor.TryGetValue(out value)) throw new InvalidOperationException("Проверь настройки."); return value; }
    }
    internal bool TrySave() { Dictionary<string, object> value; if (!editor.TryGetValue(out value)) { DialogResult = DialogResult.None; return false; } DialogResult = DialogResult.OK; return true; }
    internal SettingsDialog(Dictionary<string, object> settings)
    {
        Text = "Настройки"; Font = new Font("Segoe UI", 10); BackColor = NativeStyle.Background;
        AutoScaleDimensions = new SizeF(96, 96); AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(590, 450); FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = MinimizeBox = false; StartPosition = FormStartPosition.CenterParent;
        fields = editor.Fields; validationError = editor.Error; editor.LoadValues(settings, false);
        editor.SaveButton.Click += delegate { TrySave(); };
        editor.CancelButton.Text = "Отмена"; editor.CancelButton.DialogResult = DialogResult.Cancel;
        AcceptButton = editor.SaveButton; CancelButton = editor.CancelButton; Controls.Add(editor);
    }
}

internal enum AppPage { Home, Connection, Settings, Diagnostics }

internal sealed partial class MainForm : Form
{
    private string root = Bridge.Root;
    private static readonly Color Ink = NativeStyle.Ink;
    private static readonly Color Muted = NativeStyle.Muted;
    private static readonly Color Blue = NativeStyle.Accent;
    internal readonly Button StartButton = MakeButton("Запустить Windows", true);
    internal readonly Button OpenDesktopButton = MakeButton("Открыть Windows", true);
    internal readonly Button HelpButton = MakeButton("Справка и клавиши", false);
    private readonly Button copyReportButton = MakeButton("Скопировать отчёт", false);
    private readonly ToolTip tips = new ToolTip { AutoPopDelay = 12000, InitialDelay = 400, ReshowDelay = 150, ShowAlways = true };
    private readonly Label keyboardHint = new Label { AutoSize = true, ForeColor = NativeStyle.Muted, Font = new Font("Segoe UI",9),
        Text = "Полный экран в Windows: Ctrl+Alt+F\nМышь и клавиатура: Ctrl+Alt+G", TextAlign = ContentAlignment.MiddleCenter };
    private readonly Label diagnosticSummary = new Label { AutoSize = true, Font = new Font("Segoe UI",12,FontStyle.Bold), Text = "Проверка ПК и комплекта", Margin = new Padding(0,0,0,6) };
    private readonly Label diagnosticScope = new Label { AutoSize = true, ForeColor = NativeStyle.Muted, Margin = new Padding(0,0,0,12),
        Text = "ПК и комплект проверяются без интернета. Проверка сервера доступна в разделе «Подключение»." };
    internal readonly Button SettingsButton = MakeButton("Настройки", false);
    internal readonly Button DiagnosticsButton = MakeButton("Диагностика", false);
    internal readonly Button PrepareButton = MakeButton("Восстановить Python и QEMU", false);
    private readonly Label status = new Label { Text = "Проверяем готовность", AutoSize = true, Font = new Font("Segoe UI", 13, FontStyle.Bold), ForeColor = Ink };
    // Kept as the plain-text representation for backend integration and accessibility checks.
    private readonly TextBox details = new TextBox { Multiline = true, ReadOnly = true, Visible = false };
    private readonly Label nextStep = new Label { AutoSize = true, ForeColor = Muted };
    private readonly Label resources = new Label { AutoSize = true, Font = new Font("Segoe UI", 11), ForeColor = Ink };
    private readonly Label network = new Label { AutoSize = true, ForeColor = Color.FromArgb(130, 100, 32) };
    private readonly Label checkedAt = new Label { AutoSize = true, ForeColor = Muted, Text = "Диагностика ещё не выполнена" };
    private readonly Label reportLocation = new Label { AutoSize = true, ForeColor = Muted, Font = new Font("Segoe UI", 9) };
    private readonly Label operation = new Label { AutoSize = false, AutoEllipsis = true, Dock = DockStyle.Fill, ForeColor = Ink, Font = new Font("Segoe UI",9), Visible = false };
    private readonly Label preparationHint = new Label { AutoSize = true, ForeColor = NativeStyle.Muted,
        Font = new Font("Segoe UI", 9.5f), Margin = new Padding(0, 0, 0, 12),
        Text = "Проверит файлы, необходимые для запуска Windows, и восстановит недостающие или повреждённые. Интернет не нужен." };
    private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Fill, Style = ProgressBarStyle.Marquee, MarqueeAnimationSpeed = 25, Visible = false, Height = 4 };
    private readonly Panel pages = new Panel { Dock = DockStyle.Fill, BackColor = NativeStyle.Background };
    private readonly Panel overview = new Panel { Dock = DockStyle.Fill, BackColor = NativeStyle.Background };
    private readonly Panel diagnostics = new Panel { Dock = DockStyle.Fill, BackColor = NativeStyle.Background };
    private readonly RoundedButton overviewButton = new RoundedButton { Text = "Главная", Segment = true, Selected = true, AutoSize = true, Padding = new Padding(14,10,14,10), Margin = new Padding(0,0,0,4), Glyph = AppGlyph.Home };
    internal readonly Button ConnectionButton = MakeButton("Подключение", false);
    internal readonly Button StopButton = MakeButton("Выключить", false);
    internal readonly Button PrepareHostButton = MakeButton("Подготовить этот ПК", false);
    internal readonly Button RepairGatewayButton = MakeButton("Восстановить шлюз", false);
    private readonly Button refreshButton = MakeButton("Проверить снова", false);
    private readonly LinkLabel sessionState = new LinkLabel { Font = new Font("Segoe UI",9), AutoSize = true, ForeColor = NativeStyle.Muted, LinkColor = NativeStyle.Accent, ActiveLinkColor = NativeStyle.Accent, LinkBehavior = LinkBehavior.HoverUnderline };
    internal readonly SettingsEditor SettingsPage = new SettingsEditor();
    internal readonly ConnectionEditor ConnectionPage = new ConnectionEditor();
    private readonly Dictionary<AppPage,Control> pageControls = new Dictionary<AppPage,Control>();
    private readonly Dictionary<AppPage,RoundedButton> navigation = new Dictionary<AppPage,RoundedButton>();
    private bool settingsLoaded;
    private bool displaySettingsLoaded;
    private bool viewerSettingsLoaded;
    private Dictionary<string,object> settingsHost;
    private bool profilesLoaded;
    private bool stopAvailable;
    private bool runtimeActive;
    private string workState = "unknown";
    private string gatewayState = "unknown";
    private bool testAvailable;
    internal AppPage ActivePage { get; private set; }
    private bool showingDiagnostics;
    private readonly FlowLayoutPanel checkRows = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new Padding(0, 0, 12, 0) };
    private readonly Button nextAction = MakeButton("Изменить настройки", false);
    private readonly Func<string, object, Task<Dictionary<string, object>>> run;
    private Dictionary<string, object> readinessReport;
    private Dictionary<string, object> operationResult;
    private bool outcomeUnknown;
    private Task<Dictionary<string,object>> pendingOutcome;
    private string pendingStartReceipt;
    private string activeOperationAction;
    private bool closing;
    private readonly System.Windows.Forms.Timer lifecycleTimer = new System.Windows.Forms.Timer { Interval = 3000 };
    private NotifyIcon trayIcon;
    private ToolStripMenuItem trayStop;
    private bool lifecycleEnabled;
    private bool runtimePolling;
    private bool panelHiddenForSession;
    private int operationRevision;
    internal Dictionary<string, object> ReadinessReport { get { return readinessReport; } }
    internal Dictionary<string, object> OperationResult { get { return operationResult; } }
    private string suggestedAction = "retry";
    private bool ready;
    private bool busy;
    internal string StatusText { get { return status.Text; } }
    internal string ResourceText { get { return resources.Text; } }
    internal string DiagnosticsText { get { return details.Text; } }
    internal string CheckedAtText { get { return checkedAt.Text; } }
    internal bool DiagnosticsSelected { get { return showingDiagnostics; } }

    private static Button MakeButton(string text, bool primary)
    {
        return new RoundedButton { Text = text, Primary = primary, AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(20, 12, 20, 12),
            MinimumSize = new Size(120, 46), Margin = new Padding(0, 0, 10, 0), BackColor = Color.White,
            Font = new Font("Segoe UI", 10.5f, primary ? FontStyle.Bold : FontStyle.Regular) };
    }

    private void SelectPage(bool showDiagnostics) { SelectPage(showDiagnostics ? AppPage.Diagnostics : AppPage.Home); }
    private void SelectPage(AppPage page)
    {
        ActivePage = page; showingDiagnostics = page == AppPage.Diagnostics;
        if (!busy && !outcomeUnknown) operation.Visible = false;
        foreach (KeyValuePair<AppPage,Control> item in pageControls) item.Value.Visible = item.Key == page;
        pageControls[page].BringToFront();
        foreach (KeyValuePair<AppPage,RoundedButton> item in navigation) item.Value.Selected = item.Key == page;
    }

    internal async Task NavigateAsync(AppPage page)
    {
        if (!CanUpdate) return;
        SelectPage(page);
        if (page == AppPage.Diagnostics) { await RunAction("diagnose", true); return; }
        if (page == AppPage.Connection) { if (!profilesLoaded) await ProfileActionAsync("list-profiles", null); return; }
        if (page != AppPage.Settings || settingsLoaded || !BeginOperation("get-settings")) return;
        try
        {
            Dictionary<string,object> reply = await run("get-settings", null);
            if (!CanUpdate) return;
            if (!Bridge.Ok(reply)) { operation.Text = SettingsPage.Error.Text = Bridge.Message(reply); SettingsPage.Error.Visible = true; return; }
            SettingsPage.LoadValues((Dictionary<string,object>)reply["settings"], Flag(reply,"recovery_required"));
            if (reply.ContainsKey("windows_settings"))
            {
                SettingsPage.LoadWindowsValues((Dictionary<string,object>)reply["windows_settings"],
                    reply.ContainsKey("windows_disk") ? (Dictionary<string,object>)reply["windows_disk"] : null);
                displaySettingsLoaded = true;
            }
            if (reply.ContainsKey("viewer_settings"))
            {
                SettingsPage.LoadViewerValues((Dictionary<string,object>)reply["viewer_settings"]);
                viewerSettingsLoaded = true;
            }
            SettingsPage.LoadFileSharingValues(reply.ContainsKey("file_settings") ? reply["file_settings"] as Dictionary<string,object> : null);
            if (reply.ContainsKey("host")) settingsHost = reply["host"] as Dictionary<string,object>;
            SettingsPage.LoadHostValues(settingsHost);
            settingsLoaded = true; operation.Text = "";
        }
        catch (Exception) { if (CanUpdate) { operation.Text = SettingsPage.Error.Text = "Не удалось загрузить настройки. Повтори после проверки компонентов."; SettingsPage.Error.Visible = true; } }
        finally { if (CanUpdate) Busy(false); }
    }

    internal MainForm() : this(true, (Func<string, object, Task<Dictionary<string, object>>>)null) { }

    internal MainForm(bool diagnoseOnShow, Func<string, object, Dictionary<string, object>> runner)
        : this(diagnoseOnShow, runner == null ? null : new Func<string, object, Task<Dictionary<string, object>>>((action, payload) => Task.Run(() => runner(action, payload)))) { }

    internal MainForm(bool diagnoseOnShow, Func<string, object, Task<Dictionary<string, object>>> runner)
    {
        run = runner ?? ((action, payload) => Bridge.RunAsync(Bridge.Root, action, payload));
        Text = "Windows Claude Portable";
        Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location);
        FormClosing += delegate(object sender,FormClosingEventArgs args) {
            SavePanelBounds();
            if (lifecycleEnabled && args.CloseReason == CloseReason.UserClosing
                && (runtimeActive || outcomeUnknown || (busy && activeOperationAction == "start")))
            { args.Cancel = true; HidePanelForSession(); return; }
            closing = true;
        };
        Disposed += delegate {
            closing = true; lifecycleTimer.Stop(); lifecycleTimer.Dispose();
            tips.Dispose();
            if (trayIcon != null)
            {
                NotifyIcon icon = trayIcon; trayIcon = null;
                ContextMenuStrip menu = icon.ContextMenuStrip;
                icon.Visible = false; icon.Dispose(); if (menu != null) menu.Dispose();
            }
        };
        Font = new Font("Segoe UI", 10);
        ForeColor = Ink;
        BackColor = NativeStyle.Background;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(980, 680);
        MinimumSize = new Size(900, 620);
        StartPosition = FormStartPosition.CenterScreen;
        TableLayoutPanel frame = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(22,16,22,12), ColumnCount = 1, RowCount = 4, Margin = Padding.Empty };
        frame.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        frame.RowStyles.Add(new RowStyle(SizeType.AutoSize)); frame.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        frame.RowStyles.Add(new RowStyle(SizeType.AutoSize)); frame.RowStyles.Add(new RowStyle(SizeType.Absolute,4));
        TableLayoutPanel body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute,190)); body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); body.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        FlowLayoutPanel nav = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Margin = new Padding(0,0,18,0) };
        Button[] buttons = { overviewButton, ConnectionButton, SettingsButton, DiagnosticsButton };
        AppPage[] order = { AppPage.Home, AppPage.Connection, AppPage.Settings, AppPage.Diagnostics };
        AppGlyph[] glyphs = { AppGlyph.Home, AppGlyph.Network, AppGlyph.Settings, AppGlyph.Diagnostics };
        for (int index = 0; index < order.Length; index++)
        {
            AppPage target = order[index]; RoundedButton button = (RoundedButton)buttons[index];
            button.Segment = true; button.Glyph = glyphs[index]; button.MinimumSize = new Size(174,42);
            button.Margin = new Padding(0,0,0,5); button.Padding = new Padding(14,9,14,9);
            button.Click += async delegate { await NavigateAsync(target); }; navigation.Add(target,button); nav.Controls.Add(button);
            button.FontChanged += delegate { body.ColumnStyles[0].Width = navigation.Values.Max(item => item.GetPreferredSize(Size.Empty).Width) + nav.Margin.Horizontal; };
        }
        body.Layout += delegate {
            int width = navigation.Values.Max(item => item.GetPreferredSize(Size.Empty).Width);
            body.ColumnStyles[0].Width = width + nav.Margin.Horizontal;
            operation.Margin = new Padding(width + nav.Margin.Horizontal,6,0,0);
            foreach (RoundedButton item in navigation.Values) { item.AutoSize = false; item.Size = new Size(width,item.GetPreferredSize(Size.Empty).Height); }
        };
        body.Controls.Add(nav,0,0); body.Controls.Add(pages,1,0); frame.Controls.Add(body,0,1);
        pageControls.Add(AppPage.Home,overview); pageControls.Add(AppPage.Connection,ConnectionPage); pageControls.Add(AppPage.Settings,SettingsPage); pageControls.Add(AppPage.Diagnostics,diagnostics);
        foreach (Control page in pageControls.Values) { page.Dock = DockStyle.Fill; pages.Controls.Add(page); }
        HelpButton.Font = new Font("Segoe UI",9); HelpButton.MinimumSize = Size.Empty; HelpButton.Padding = new Padding(10,8,10,8); HelpButton.Margin = new Padding(0,18,0,0); nav.Controls.Add(HelpButton);
        TableLayoutPanel home = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, Margin = Padding.Empty };
        home.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        home.RowStyles.Add(new RowStyle(SizeType.Percent,50));
        for (int index = 0; index < 4; index++) home.RowStyles.Add(new RowStyle(SizeType.AutoSize)); home.RowStyles.Add(new RowStyle(SizeType.Percent,50));
        RoundedPanel summary = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(20,12,20,12), Margin = new Padding(0,0,0,6), BackColor = Color.White };
        TableLayoutPanel summaryRows = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 5, Margin = Padding.Empty };
        summaryRows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); for(int index=0;index<5;index++)summaryRows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        status.Font = new Font("Segoe UI",13,FontStyle.Bold); status.Margin = new Padding(0,0,0,3); nextStep.Margin = new Padding(0,0,0,3); sessionState.Margin = new Padding(0,0,0,6);
        foreach(Label label in new[]{status,nextStep,sessionState}) { label.Anchor = AnchorStyles.None; label.TextAlign = ContentAlignment.MiddleCenter; }
        summaryRows.Controls.Add(status,0,0); summaryRows.Controls.Add(nextStep,0,1); summaryRows.Controls.Add(sessionState,0,2);
        FlowLayoutPanel launch = new FlowLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true, Anchor = AnchorStyles.None, Margin = Padding.Empty };
        foreach(Button button in new[]{StartButton,OpenDesktopButton}) { ((RoundedButton)button).Glyph = AppGlyph.Play; button.Padding = new Padding(24,10,24,10); button.MinimumSize = new Size(260,58); button.Margin = new Padding(4); }
        StopButton.Padding = new Padding(14,9,14,9); StopButton.Margin = new Padding(4); launch.Controls.Add(StartButton); launch.Controls.Add(OpenDesktopButton); launch.Controls.Add(StopButton); summaryRows.Controls.Add(launch,0,3);
        nextAction.Anchor = AnchorStyles.None; nextAction.Padding = new Padding(10,7,10,7); nextAction.Margin = new Padding(0,10,0,0); summaryRows.Controls.Add(nextAction,0,4); summary.Controls.Add(summaryRows); home.Controls.Add(summary,0,1);
        resources.Font = new Font("Segoe UI",9.5f); resources.Margin = new Padding(0,0,0,4); resources.Anchor = AnchorStyles.None; resources.TextAlign = ContentAlignment.MiddleCenter; home.Controls.Add(resources,0,2);
        network.Font = new Font("Segoe UI",9); network.ForeColor = Muted; network.Margin = new Padding(2,0,2,10); network.Anchor = AnchorStyles.None; network.TextAlign = ContentAlignment.MiddleCenter; home.Controls.Add(network,0,3);
        keyboardHint.Anchor = AnchorStyles.None; keyboardHint.Margin = Padding.Empty; home.Controls.Add(keyboardHint,0,4);
        overview.Controls.Add(home);
        TableLayoutPanel diagnosticLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 7, Margin = Padding.Empty };
        diagnosticLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        diagnosticLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); diagnosticLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); diagnosticLayout.RowStyles.Add(new RowStyle(SizeType.Percent,100)); for(int index=0;index<4;index++)diagnosticLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        FlowLayoutPanel tools = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = true, Margin = new Padding(0,0,0,8) };
        ((RoundedButton)PrepareButton).Glyph = ((RoundedButton)RepairGatewayButton).Glyph = AppGlyph.Repair; ((RoundedButton)refreshButton).Glyph = AppGlyph.Diagnostics;
        foreach (Button button in new[] {refreshButton,copyReportButton,PrepareHostButton,PrepareButton,RepairGatewayButton}) { button.Padding = new Padding(10,7,10,7); tools.Controls.Add(button); }
        preparationHint.Text = "Восстановление использует локальные файлы. Подготовка ПК запросит права администратора.";
        diagnosticLayout.Controls.Add(diagnosticSummary,0,0); diagnosticLayout.Controls.Add(diagnosticScope,0,1); diagnosticLayout.Controls.Add(checkRows,0,2); diagnosticLayout.Controls.Add(tools,0,3);
        diagnosticLayout.Controls.Add(preparationHint,0,4); diagnosticLayout.Controls.Add(checkedAt,0,5); diagnosticLayout.Controls.Add(reportLocation,0,6); diagnostics.Controls.Add(diagnosticLayout);
        operation.Height=operation.Font.Height+4;operation.FontChanged+=delegate{operation.Height=operation.Font.Height+4;};
        frame.Controls.Add(operation,0,2); frame.Controls.Add(progress,0,3); Controls.Add(frame);
        overview.Resize += delegate {
            int width = Math.Max(100,overview.ClientSize.Width-38);
            foreach (Label label in new[] {status,nextStep,sessionState,resources,network,keyboardHint}) label.MaximumSize = new Size(width,0);
            launch.MaximumSize = new Size(width,0);
        };
        diagnosticLayout.SizeChanged += delegate { foreach(Label label in new[]{preparationHint,diagnosticScope,diagnosticSummary})label.MaximumSize = new Size(Math.Max(100,diagnosticLayout.Width),0); };
        checkRows.SizeChanged += delegate { FitCheckRows(); };
        StartButton.Enabled = false; StopButton.Enabled = false; nextAction.Visible = false;
        StartButton.Click += async delegate { await RunAction("start",false); };
        OpenDesktopButton.Click += async delegate { await RunAction("show-desktop",false); };
        HelpButton.Click += delegate { ShowKeyboardHelp(); };
        copyReportButton.Click += delegate { try { Clipboard.SetText(details.Text); } catch(Exception) { operation.Text = "Не удалось скопировать отчёт. Попробуй ещё раз."; operation.Visible = true; } };
        tips.SetToolTip(StartButton,"Запустить Windows — Ctrl+Enter"); tips.SetToolTip(OpenDesktopButton,"Открыть уже запущенную Windows — Ctrl+Enter");
        tips.SetToolTip(StopButton,"Штатно выключить Windows и дождаться завершения."); tips.SetToolTip(refreshButton,"Проверить ПК и комплект — F5"); tips.SetToolTip(HelpButton,"Справка по управлению — F1");
        for(int index=0;index<buttons.Length;index++)tips.SetToolTip(buttons[index],buttons[index].Text+" — Ctrl+"+(index+1));
        StopButton.Click += async delegate { if (stopAvailable) await RunAction("stop",false); };
        PrepareButton.Click += async delegate { await RunAction("prepare-components",false); };
        PrepareHostButton.Click += async delegate { await RunAction("prepare-host",false); };
        RepairGatewayButton.Click += async delegate { await RunAction("repair-gateway",false); };
        refreshButton.Click += async delegate { await RunAction("diagnose",true); };
        sessionState.LinkClicked += async delegate { await NavigateAsync(AppPage.Connection); };
        SettingsPage.SaveButton.Click += async delegate { Dictionary<string,object> value; if (SettingsPage.TryGetValue(out value)) await SaveSettingsAndDiagnose(value); };
        ConnectionPage.ProfilePicked += async id => { await ProfileActionAsync("get-profile-view",new Dictionary<string,object>{{"profile_id",id}}); };
        ConnectionPage.SaveButton.Click += async delegate { await SaveProfileAsync(); };
        ConnectionPage.SelectButton.Click += async delegate { await ProfileActionAsync("select-profile",new Dictionary<string,object>{{"profile_id",ConnectionPage.ProfileId}}); };
        ConnectionPage.TestButton.Click += async delegate { if (testAvailable) await ProfileActionAsync("test-profile",new Dictionary<string,object>{{"profile_id",ConnectionPage.ProfileId}}); };
        ConnectionPage.ImportButton.Click += async delegate { await ImportProfileDialogAsync(); };
        ConnectionPage.ExportButton.Click += async delegate { await ExportProfileDialogAsync(); };
        nextAction.Click += async delegate {
            if (suggestedAction == "settings") await NavigateAsync(AppPage.Settings);
            else if (suggestedAction == "connection") await NavigateAsync(AppPage.Connection);
            else if (suggestedAction == "repair_gateway") await RunAction("repair-gateway",false);
            else if (suggestedAction == "recover_session") await RunAction("recover-session",false);
            else if (suggestedAction == "windows_features") { SelectPage(AppPage.Diagnostics); operation.Text = "Нажми «Подготовить этот ПК». Изменение компонентов Windows потребует UAC; перезагрузка выполняется вручную."; operation.Visible = true; }
            else await RunAction(suggestedAction == "prepare" ? "prepare-components" : "diagnose",suggestedAction != "prepare");
        };
        SelectPage(AppPage.Home); Busy(false);
        if (diagnoseOnShow) Shown += async delegate { StartLifecycleMonitoring(); await RunAction("diagnose",false); };
    }

    internal void StartLifecycleMonitoring()
    {
        if (lifecycleEnabled || !CanUpdate) return;
        lifecycleEnabled = true;
        ContextMenuStrip menu = new ContextMenuStrip();
        ToolStripMenuItem open = new ToolStripMenuItem("Открыть панель");
        trayStop = new ToolStripMenuItem("Выключить Windows");
        open.Click += delegate { ShowPanel(false); };
        trayStop.Click += async delegate { ShowPanel(false); if (stopAvailable) await RunAction("stop",false); };
        menu.Items.Add(open); menu.Items.Add(trayStop);
        trayIcon = new NotifyIcon { Icon = Icon ?? SystemIcons.Application, Text = "Windows Claude Portable", ContextMenuStrip = menu };
        trayIcon.DoubleClick += delegate { ShowPanel(false); };
        lifecycleTimer.Tick += async delegate { await RefreshRuntimeAsync(); };
        lifecycleTimer.Start(); UpdateTray();
    }

    private void UpdateTray()
    {
        if (trayIcon == null || !CanUpdate) return;
        trayIcon.Visible = runtimeActive || panelHiddenForSession;
        trayStop.Enabled = stopAvailable && !busy && !outcomeUnknown;
    }

    private void HidePanelForSession()
    {
        if (!CanUpdate) return;
        panelHiddenForSession = true; UpdateTray(); Hide();
    }

    private void ShowPanel(bool home)
    {
        if (!CanUpdate) return;
        bool wasHidden = !Visible;
        panelHiddenForSession = false;
        if (home && wasHidden) SelectPage(AppPage.Home);
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Show(); Activate(); UpdateTray();
    }

    private void RuntimeRefreshFailed(string message)
    {
        ShowPanel(true);
        status.Text = "Состояние Windows не подтверждено";
        nextStep.Text = message; operation.Text = message; operation.Visible = true;
        checkedAt.Text = "Состояние не обновилось. Повтори диагностику.";
    }

    internal async Task RefreshRuntimeAsync()
    {
        if (!lifecycleEnabled || !CanUpdate || busy || runtimePolling
            || (!runtimeActive && !panelHiddenForSession && !outcomeUnknown)) return;
        runtimePolling = true;
        int revision = operationRevision;
        try
        {
            Dictionary<string,object> report = await run("status",null);
            if (!CanUpdate || busy || revision != operationRevision) return;
            if (report == null || !report.ContainsKey("checks")) { RuntimeRefreshFailed(Bridge.Message(report)); return; }
            report = MergeRuntimeStatus(report);
            Dictionary<string,object> state = report.ContainsKey("runtime") ? report["runtime"] as Dictionary<string,object> : null;
            bool changed = readinessReport == null || state == null || pendingStartReceipt != null
                || workState != TextValue(state,"work","unknown") || gatewayState != TextValue(state,"gateway","unknown")
                || Bridge.Ok(report) != Bridge.Ok(readinessReport)
                || Flag(report,"network_verified") != Flag(readinessReport,"network_verified")
                || DisplayState(report) != DisplayState(readinessReport)
                || FileSharingState(report) != FileSharingState(readinessReport)
                || TextValue(report,"message","") != TextValue(readinessReport,"message","")
                || (pendingOutcome != null && pendingOutcome.IsCompleted);
            if (changed) { Display(report); Busy(false); }
            bool workActive = workState == "running" || workState == "starting" || workState == "restarting" || workState == "stopping";
            if (panelHiddenForSession && (!workActive || !Bridge.Ok(report) || outcomeUnknown
                || DisplayState(report) == "failed" || DisplayState(report) == "unconfirmed")) ShowPanel(true);
            UpdateTray();
        }
        catch (Exception)
        {
            if (CanUpdate && !busy && revision == operationRevision)
                RuntimeRefreshFailed("Не удалось обновить состояние. Повтори диагностику.");
        }
        finally { runtimePolling = false; }
    }

    private static string DisplayState(Dictionary<string,object> report)
    {
        if (report == null || !report.ContainsKey("windows_display")) return "";
        var display = report["windows_display"] as Dictionary<string,object>;
        return TextValue(display,"status","");
    }

    private static string FileSharingState(Dictionary<string,object> report)
    {
        return report != null && report.ContainsKey("host_files") ? TextValue(report["host_files"] as Dictionary<string,object>,"status","") : "";
    }

    private void FitCheckRows()
    {
        int width = Math.Max(100, checkRows.ClientSize.Width - 34);
        foreach (Control card in checkRows.Controls)
        {
            card.MinimumSize = Size.Empty;
            card.MaximumSize = new Size(width, 0);
            card.MinimumSize = new Size(width, 0);
            card.Width = width;
            foreach (Label label in card.Controls.OfType<Label>()) label.MaximumSize = new Size(width - 42, 0);
        }
    }

    private void Busy(bool value)
    {
        busy = value;
        StartButton.Enabled = !value && ready && !outcomeUnknown && !runtimeActive;
        OpenDesktopButton.Visible = workState == "running";
        OpenDesktopButton.Enabled = !value && !outcomeUnknown && workState == "running";
        copyReportButton.Enabled = !value && !String.IsNullOrEmpty(details.Text);
        SettingsButton.Enabled = PrepareButton.Enabled = RepairGatewayButton.Enabled = !value && !outcomeUnknown;
        ConnectionButton.Enabled = DiagnosticsButton.Enabled = overviewButton.Enabled = refreshButton.Enabled = nextAction.Enabled = sessionState.Enabled = !value;
        PrepareHostButton.Enabled = !value && !outcomeUnknown && File.Exists(Path.Combine(Bridge.Root,"scripts","Prepare-Host.ps1"));
        StopButton.Enabled = !value && !outcomeUnknown && stopAvailable;
        StopButton.Visible = stopAvailable;
        StartButton.Visible = ready && !outcomeUnknown && !runtimeActive;
        SettingsPage.SaveButton.Enabled = !value && !outcomeUnknown && settingsLoaded;
        SettingsPage.CancelButton.Enabled = !value && !outcomeUnknown && settingsLoaded;
        foreach (TextBox field in SettingsPage.Fields.Values) field.Enabled = !value && !outcomeUnknown;
        SettingsPage.Resolution.Enabled = !value && !outcomeUnknown;
        SettingsPage.StartFullscreen.Enabled = SettingsPage.RememberWindow.Enabled = !value && !outcomeUnknown && viewerSettingsLoaded;
        SettingsPage.LoadHostValues(settingsHost);
        if (value || outcomeUnknown) SettingsPage.RecommendedButton.Enabled = false;
        ConnectionPage.SetBusy(value || outcomeUnknown);
        progress.Visible = value;
        operation.Visible = value || outcomeUnknown || !String.IsNullOrEmpty(operation.Text);
        UseWaitCursor = value;
        UpdateTray();
    }

    private static string VmState(string state, bool gateway = false)
    {
        switch (state) { case "stopped": return gateway ? "выключен" : "выключена"; case "starting": return "запускается"; case "restarting": return "перезагружается"; case "running": return "работает"; case "reconnecting": return "восстанавливает связь"; case "connection-failed": return "ошибка подключения"; case "control-unavailable-guest-running": return "работает без управления"; case "shutdown-unconfirmed": return "выключение не подтверждено"; case "failed": return "ошибка сеанса"; case "stopping": return "выключается"; default: return "состояние неизвестно"; }
    }

    private static bool ActiveVmState(string state)
    {
        return state == "starting" || state == "restarting" || state == "running" || state == "reconnecting"
            || state == "stopping" || state == "shutdown-unconfirmed" || state == "connection-failed" || state == "control-unavailable-guest-running" || state == "failed";
    }

    private static string RuntimeHeading(string work, string gateway)
    {
        if (ActiveVmState(work))
            return work == "failed" || work == "connection-failed" ? "Сеанс Windows требует проверки" : "Windows " + VmState(work);
        if (ActiveVmState(gateway))
            return gateway == "connection-failed" ? "Ошибка подключения шлюза" : gateway == "failed" ? "Сеанс шлюза требует проверки" : "Шлюз " + VmState(gateway, true);
        return work == "stopped" && gateway == "stopped" ? "Готово к запуску Windows" : "Сеанс Windows не подтверждён";
    }

    private static bool Flag(Dictionary<string,object> item,string key) { return item != null && item.ContainsKey(key) && item[key] is bool && (bool)item[key]; }

    private static string TextValue(Dictionary<string, object> value, string key, string fallback)
    {
        return value.ContainsKey(key) && value[key] != null ? Convert.ToString(value[key]) : fallback;
    }

    private List<Dictionary<string, object>> Checks(Dictionary<string, object> result)
    {
        List<Dictionary<string, object>> checks = new List<Dictionary<string, object>>();
        Dictionary<string, object> source = result.ContainsKey("checks") ? result["checks"] as Dictionary<string, object> : null;
        if (source != null)
            foreach (KeyValuePair<string, object> item in source)
            {
                Dictionary<string, object> check = item.Value as Dictionary<string, object>;
                if (check == null) continue;
                Dictionary<string, object> row = new Dictionary<string, object>(check);
                if (!row.ContainsKey("title")) row["title"] = item.Key;
                checks.Add(row);
            }
        return checks.OrderBy(item => item.ContainsKey("passed") && item["passed"] is bool && (bool)item["passed"]).ToList();
    }

    internal void DisplayReport(Dictionary<string, object> result) { Display(result); }

    private Dictionary<string,object> MergeRuntimeStatus(Dictionary<string,object> update)
    {
        if (TextValue(update,"report_kind","") != "runtime_status") return update;
        bool hasFullCheck = readinessReport != null && TextValue(readinessReport,"report_kind","preflight") == "preflight";
        var result = readinessReport == null ? new Dictionary<string,object>() : new Dictionary<string,object>(readinessReport);
        foreach (var item in update)
            if (item.Key != "checks" && item.Key != "blockers" && item.Key != "ok" && item.Key != "message"
                && item.Key != "checked_utc" && item.Key != "gateway_version" && item.Key != "report_kind") result[item.Key] = item.Value;
        var checks = result.ContainsKey("checks") && result["checks"] is Dictionary<string,object>
            ? new Dictionary<string,object>((Dictionary<string,object>)result["checks"]) : new Dictionary<string,object>();
        var liveChecks = update["checks"] as Dictionary<string,object>;
        if (liveChecks != null) foreach (var item in liveChecks) checks[item.Key] = item.Value;
        if (!hasFullCheck) checks["full_check"] = new Dictionary<string,object>{{"passed",false},{"title","Нужна проверка комплекта"},
            {"detail","Открой диагностику и проверь готовность перед запуском Windows."},{"action","retry"},{"action_label","Проверить комплект"}};
        var blockers = new List<Dictionary<string,object>>();
        foreach (var item in checks)
        {
            var check = item.Value as Dictionary<string,object>;
            if (check == null || Flag(check,"passed")) continue;
            var blocker = new Dictionary<string,object>(check); blocker["code"] = item.Key; blockers.Add(blocker);
        }
        result["checks"] = checks; result["blockers"] = blockers.ToArray(); result["ok"] = blockers.Count == 0;
        result["message"] = blockers.Count == 0 ? Bridge.Message(update) : TextValue(blockers[0],"title","Нужна проверка");
        result["report_kind"] = hasFullCheck ? "preflight" : "runtime_status";
        var capabilities = result.ContainsKey("capabilities") ? result["capabilities"] as Dictionary<string,object> : null;
        if (capabilities != null)
        {
            capabilities = new Dictionary<string,object>(capabilities);
            var gateway = checks.ContainsKey("gateway_package") ? checks["gateway_package"] as Dictionary<string,object> : null;
            var runtime = checks.ContainsKey("runtime_state") ? checks["runtime_state"] as Dictionary<string,object> : null;
            capabilities["test_profile"] = hasFullCheck && Flag(gateway,"passed") && Flag(runtime,"passed") && !Flag(capabilities,"stop");
            result["capabilities"] = capabilities;
        }
        return result;
    }

    private void Display(Dictionary<string, object> result)
    {
        result = MergeRuntimeStatus(result);
        bool isReadiness = result.ContainsKey("checks");
        if (isReadiness)
        {
            readinessReport = result;
            if (result.ContainsKey("host"))
            {
                settingsHost = result["host"] as Dictionary<string,object>;
                SettingsPage.LoadHostValues(settingsHost);
            }
            if (result.ContainsKey("viewer_settings")) ApplyViewerPreferences(result["viewer_settings"] as Dictionary<string,object>);
            ready = Bridge.Ok(result);
            if (pendingOutcome != null && pendingOutcome.IsCompleted) { outcomeUnknown = false; pendingOutcome = null; }
            Dictionary<string,object> startup = result.ContainsKey("startup") ? result["startup"] as Dictionary<string,object> : null;
            if (pendingStartReceipt != null && startup != null && TextValue(startup,"receipt","") == pendingStartReceipt
                && (TextValue(startup,"phase","") == "ready"
                    || (!Flag(startup,"supervisor_active") && (TextValue(startup,"phase","") == "stopped" || TextValue(startup,"phase","") == "error"))))
            { outcomeUnknown = false; pendingStartReceipt = null; }
            Dictionary<string,object> capabilities = result.ContainsKey("capabilities") ? result["capabilities"] as Dictionary<string,object> : null;
            stopAvailable = Flag(capabilities,"stop"); testAvailable = Flag(capabilities,"test_profile");
            ConnectionPage.SetTestAvailable(testAvailable);
            Dictionary<string,object> runtime = result.ContainsKey("runtime") ? result["runtime"] as Dictionary<string,object> : null;
            workState = runtime == null ? "unknown" : TextValue(runtime,"work","unknown");
            gatewayState = runtime == null ? "unknown" : TextValue(runtime,"gateway","unknown");
            runtimeActive = ActiveVmState(workState) || ActiveVmState(gatewayState);
            Dictionary<string,object> connection = result.ContainsKey("connection") ? result["connection"] as Dictionary<string,object> : null;
            Dictionary<string,object> selected = connection != null && connection.ContainsKey("selected_profile") ? connection["selected_profile"] as Dictionary<string,object> : null;
            if (selected == null)
            {
                sessionState.Text = "Добавить подключение"; sessionState.LinkArea = new LinkArea(0,sessionState.Text.Length);
                sessionState.AccessibleName = "Добавить подключение. Открыть страницу подключения.";
            }
            else
            {
                string name = TextValue(selected,"name","Профиль"); string shortName = name.Length > 18 ? name.Substring(0,18) + "…" : name;
                string protocol = TextValue(selected,"protocol","");
                if (protocol == "hysteria2") protocol = "Hysteria2"; else protocol = protocol.ToUpperInvariant();
                string heading = shortName + " (" + protocol + ")";
                string connectionState = Flag(connection,"verified") ? "Подключение проверено" : "Выбрано для следующего запуска";
                sessionState.Text = heading + ". " + connectionState;
                sessionState.LinkArea = new LinkArea(0,heading.Length); sessionState.AccessibleName = name + ". " + connectionState;
            }
        }
        else
        {
            operationResult = result;
            if (result.ContainsKey("outcome_unknown") && result["outcome_unknown"] is bool && (bool)result["outcome_unknown"])
                TrackUnknownOutcome(result);
        }
        Dictionary<string, object> report = readinessReport ?? result;
        List<Dictionary<string, object>> checks = Checks(report);
        int failedChecks = checks.Count(item => !Flag(item,"passed"));
        diagnosticSummary.Text = checks.Count == 0 ? "Проверка ПК и комплекта" : "Проверено: " + checks.Count + (failedChecks == 0 ? ". Проблем не найдено." : ". Требует внимания: " + failedChecks + ".");
        Dictionary<string, object> blocker = null;
        if (!Bridge.Ok(result))
        {
            System.Collections.IEnumerable blockers = result.ContainsKey("blockers") ? result["blockers"] as System.Collections.IEnumerable : null;
            if (blockers != null) blocker = blockers.Cast<object>().OfType<Dictionary<string, object>>().FirstOrDefault();
            if (blocker == null && isReadiness) blocker = checks.FirstOrDefault(item => !item.ContainsKey("passed") || !(item["passed"] is bool) || !(bool)item["passed"]);
        }
        status.Text = blocker == null ? Bridge.Message(result) : TextValue(blocker, "title", Bridge.Message(result));
        status.ForeColor = Ink;
        if (isReadiness && Bridge.Ok(result)) status.Text = RuntimeHeading(workState, gatewayState);
        nextStep.Text = blocker == null ? (Bridge.Ok(result) ? "Шлюз запустится перед Windows." : "Подробности доступны в разделе диагностики ниже.") : TextValue(blocker, "detail", Bridge.Message(result));
        if (isReadiness && Bridge.Ok(result)) nextStep.Text = workState == "unknown" && gatewayState == "unknown" ? "Готовность файлов подтверждена." :
            "Windows: " + VmState(workState) + ". Шлюз: " + VmState(gatewayState, true) + ".";
        suggestedAction = blocker == null ? "retry" : TextValue(blocker, "action", "retry");
        nextAction.Text = blocker == null ? "Повторить диагностику" : TextValue(blocker, "action_label", "Повторить диагностику");
        nextAction.Visible = !Bridge.Ok(result);
        Dictionary<string, object> settings = report.ContainsKey("settings") ? report["settings"] as Dictionary<string, object> : null;
        if (settings != null)
            resources.Text = "Windows: " + TextValue(settings, "work_memory_mib", "—") + " МиБ" + Environment.NewLine + "Процессоры каждой VM: " + TextValue(settings, "vcpus", "—");
        else if (String.IsNullOrEmpty(resources.Text)) resources.Text = "Параметры появятся после проверки комплекта.";
        Dictionary<string,object> displaySettings = report.ContainsKey("windows_settings") ? report["windows_settings"] as Dictionary<string,object> : null;
        if (displaySettings != null)
        {
            string size = TextValue(displaySettings,"width","—") + " × " + TextValue(displaySettings,"height","—");
            string state = DisplayState(report);
            string detail = state == "applied" ? "применено" : state == "not_running" ? "при следующем запуске" : state == "failed" ? "не применилось" : "ожидаем подтверждения Windows";
            resources.Text += Environment.NewLine + "Разрешение: " + size + " — " + detail;
            if (state == "failed" || state == "unconfirmed")
            {
                var display = report["windows_display"] as Dictionary<string,object>;
                var receipt = display != null && display.ContainsKey("receipt") ? display["receipt"] as Dictionary<string,object> : null;
                if (blocker == null)
                    nextStep.Text = state == "failed" ? "Windows не применила выбранное разрешение. Выбери другой размер в настройках после выключения." : "Windows ещё не подтвердила разрешение. Проверь подготовку гостевой системы.";
                if (receipt != null && receipt.ContainsKey("current"))
                {
                    var current = receipt["current"] as Dictionary<string,object>;
                    resources.Text += Environment.NewLine + "Сейчас в Windows: " + TextValue(current,"width","—") + " × " + TextValue(current,"height","—");
                }
            }
        }
        string filesState=FileSharingState(report);
        if(filesState.Length>0)
        {
            string filesDetail=filesState=="ready"?"подключены":filesState=="off"?"выключены":filesState=="unavailable"?"папки недоступны":filesState=="failed"?"соединение прервано":filesState=="connecting"?"подключаются":"ожидаем подключения Windows";
            object skipped;var filesReport=report["host_files"] as Dictionary<string,object>;
            if(filesReport!=null&&filesReport.TryGetValue("unavailable",out skipped)&&skipped is System.Collections.ICollection&&((System.Collections.ICollection)skipped).Count>0)
                filesDetail+="; пропущено папок: "+((System.Collections.ICollection)skipped).Count;
            resources.Text+=Environment.NewLine+"Файлы ПК: "+filesDetail;
            if(blocker==null&&filesState=="pending")nextStep.Text="Для первого подключения файлов открой в Windows диск WCPFILES и запусти RUNFILES.CMD.";
            if(blocker==null&&(filesState=="failed"||filesState=="unavailable"))nextStep.Text="Проверь доступность выбранных папок ПК. Повторное подключение можно выполнить через RUNFILES.CMD на диске WCPFILES в Windows.";
        }
        string warning = TextValue(report, "network_warning", network.Text);
        network.Text = warning;
        network.Visible = warning.Length > 0;
        checkRows.SuspendLayout();
        while (checkRows.Controls.Count > 0) { Control old = checkRows.Controls[0]; checkRows.Controls.RemoveAt(0); old.Dispose(); }
        List<string> lines = new List<string>();
        foreach (Dictionary<string, object> item in checks)
        {
            bool passed = item.ContainsKey("passed") && item["passed"] is bool && (bool)item["passed"];
            string title = TextValue(item, "title", "Проверка");
            string detail = TextValue(item, "detail", "");
            lines.Add((passed ? "Готово: " : "Нужно исправить: ") + title + Environment.NewLine + detail);
            FlowLayoutPanel card = new RoundedFlowPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new Padding(20, 16, 20, 18), Margin = new Padding(0, 0, 0, 10), BackColor = Color.White };
            card.Controls.Add(new Label { Text = (passed ? "Готово: " : "Нужно внимание: ") + title, AutoSize = true, Font = new Font("Segoe UI", 10, FontStyle.Bold), ForeColor = passed ? Ink : Color.FromArgb(165, 63, 45), Margin = new Padding(0, 0, 0, 6) });
            card.Controls.Add(new Label { Text = detail, AutoSize = true, ForeColor = Muted, Margin = Padding.Empty });
            checkRows.Controls.Add(card);
        }
        if (checks.Count == 0)
        {
            lines.Add(Bridge.Message(result));
            checkRows.Controls.Add(new Label { Text = Bridge.Message(result), AutoSize = true, Padding = new Padding(12), ForeColor = Ink });
        }
        if (warning.Length > 0) lines.Add(warning);
        string reportPath = TextValue(report, "diagnostic_report", "");
        if (reportPath.Length > 0) lines.Add("Отчёт: " + reportPath);
        reportLocation.Text = reportPath.Length > 0 ? "Отчёт сохранён: " + reportPath : TextValue(report, "diagnostic_report_error", "");
        details.Text = String.Join(Environment.NewLine + Environment.NewLine, lines);
        details.SelectionStart = 0;
        FitCheckRows();
        checkRows.ResumeLayout(true);
        checkRows.AutoScrollPosition = Point.Empty;
        if (checkRows.Controls.Count > 0) checkRows.ScrollControlIntoView(checkRows.Controls[0]);
        if (!isReadiness && !Bridge.Ok(result) && checks.Count > 0) lines.Insert(0, Bridge.Message(result));
        details.Text = String.Join(Environment.NewLine + Environment.NewLine, lines);
        StartButton.Enabled = !busy && ready && !outcomeUnknown && !runtimeActive;
        OpenDesktopButton.Visible = workState == "running";
        OpenDesktopButton.Enabled = !busy && !outcomeUnknown && workState == "running";
        StopButton.Enabled = !busy && !outcomeUnknown && stopAvailable;
        StartButton.Visible = ready && !outcomeUnknown && !runtimeActive;
        StopButton.Visible = stopAvailable;
        ((RoundedButton)nextAction).Primary = !ready || outcomeUnknown;
    }

    private bool CanUpdate { get { return !closing && !IsDisposed && !Disposing; } }

    private bool BeginOperation(string action)
    {
        if (!CanUpdate || busy) return false;
        if (outcomeUnknown && action != "diagnose" && action != "recover-session")
        {
            operation.Text = "Предыдущая операция ещё не подтверждена. Сначала выполни диагностику и проверь состояние.";
            return false;
        }
        activeOperationAction = action;
        operationRevision++;
        Busy(true);
        switch (action)
        {
            case "start": operation.Text = "Запускаем шлюз и Windows…"; break;
            case "stop": operation.Text = "Завершаем работу Windows и шлюза… Дождись штатного выключения."; break;
            case "repair-gateway": operation.Text = "Восстанавливаем шлюз из комплекта…"; break;
            case "recover-session": operation.Text = "Восстанавливаем прерванный сеанс… Диск Windows и подключения сохраняются."; break;
            case "prepare-host": operation.Text = "Готовим этот ПК… Подтверди запрос UAC."; break;
            case "prepare-components": operation.Text = "Восстанавливаем Python и QEMU…"; break;
            case "save-settings": operation.Text = "Сохраняем настройки…"; break;
            case "get-settings": operation.Text = "Загружаем настройки…"; break;
            case "list-profiles": case "get-profile-view": operation.Text = "Загружаем подключения…"; break;
            case "save-profile": case "import-profile": operation.Text = "Сохраняем подключение…"; break;
            case "select-profile": operation.Text = "Выбираем подключение…"; break;
            case "test-profile": operation.Text = "Проверяем подключение…"; break;
            case "export-profile": operation.Text = "Экспортируем подключение…"; break;
            default: operation.Text = "Проверяем готовность к запуску…"; break;
        }
        return true;
    }

    private void TrackUnknownOutcome(Dictionary<string,object> result)
    {
        if (activeOperationAction == "diagnose" || activeOperationAction == "get-settings"
                || activeOperationAction == "list-profiles" || activeOperationAction == "get-profile-view") return;
        outcomeUnknown = true;
        if (pendingOutcome == null) pendingOutcome = Bridge.PendingOperation(result);
        if (activeOperationAction == "start" && result.ContainsKey("receipt")) pendingStartReceipt = TextValue(result,"receipt","");
    }

    private void FailedAction(string action, Dictionary<string, object> completed)
    {
        if (!CanUpdate) return;
        if (completed != null && Bridge.Ok(completed))
            Display(Bridge.Unknown(Bridge.Message(completed) + " Не удалось обновить диагностику. Проверь состояние повторно.", "diagnose_failed"));
        else if (action == "diagnose" || action == "get-settings")
            Display(Bridge.Failure("Не удалось выполнить диагностику. Проверь доступ к папке и целостность компонентов."));
        else
            Display(Bridge.Unknown("Действие не подтверждено. Исход операции неизвестен. Проверь состояние через диагностику.", "operation_failed"));
        operation.Text = Bridge.Message(operationResult);
        if (action == "diagnose") checkedAt.Text = "Диагностика не обновилась. Повтори проверку.";
    }

    private async Task RefreshAfterAsync(Dictionary<string, object> completed)
    {
        if (!CanUpdate || (!Bridge.Ok(completed) && !Flag(completed,"outcome_unknown"))) return;
        Dictionary<string, object> check = await run("diagnose", null);
        if (!CanUpdate) return;
        Display(check);
        operation.Text = Bridge.Message(completed);
        checkedAt.Text = "Проверено " + DateTime.Now.ToString("HH:mm:ss") + ". Ошибки показаны первыми.";
    }

    internal async Task RunAction(string action, bool selectDiagnostics)
    {
        if (!BeginOperation(action)) return;
        if (selectDiagnostics) SelectPage(true);
        Dictionary<string, object> completed = null;
        try
        {
            completed = await run(action, null);
            if (!CanUpdate) return;
            if (action == "recover-session" && Bridge.Ok(completed)) { outcomeUnknown = false; pendingOutcome = null; pendingStartReceipt = null; }
            Display(completed);
            operation.Text = action == "diagnose" && completed.ContainsKey("checks") ? "" : Bridge.Message(completed);
            if (action == "prepare-components" || action == "prepare-host" || action == "repair-gateway" || action == "recover-session" || action == "stop" || action == "start") await RefreshAfterAsync(completed);
            if (action == "prepare-host" && Flag(completed,"restart_required")) operation.Text = "Подготовка завершена. Перезагрузи ПК самостоятельно, затем выполни диагностику. Автоматическая перезагрузка не выполнялась.";
            else if (action == "diagnose") checkedAt.Text = completed.ContainsKey("checks") ? "Проверено " + DateTime.Now.ToString("HH:mm:ss") + ". Ошибки показаны первыми." : "Диагностика не обновилась. Повтори проверку.";
        }
        catch (Exception) { FailedAction(action, completed); }
        finally
        {
            if (CanUpdate)
            {
                Busy(false);
                if (lifecycleEnabled && action == "start" && Bridge.Ok(completed)
                    && ready && workState == "running" && !outcomeUnknown) HidePanelForSession();
                else if (lifecycleEnabled && panelHiddenForSession
                    && (workState == "stopped" || workState == "failed" || outcomeUnknown
                        || (action == "start" && !Bridge.Ok(completed)))) ShowPanel(true);
                UpdateTray();
            }
        }
    }

    internal async Task SaveSettingsAndDiagnose(Dictionary<string, object> value)
    {
        value = new Dictionary<string,object>(value);
        if (displaySettingsLoaded)
        {
            Dictionary<string,object> display;
            if (!SettingsPage.TryGetWindowsValue(out display)) return;
            value["windows"] = display;
        }
        if (viewerSettingsLoaded)
        {
            Dictionary<string,object> viewer;
            if (!SettingsPage.TryGetViewerValue(out viewer)) return;
            value["viewer"] = viewer;
        }
        Dictionary<string,object> files;
        if (!SettingsPage.TryGetFileSharingValue(out files)) return;
        value["files"] = files;
        if (!BeginOperation("save-settings")) return;
        Dictionary<string, object> completed = null;
        try
        {
            completed = await run("save-settings", value);
            if (!CanUpdate) return;
            Display(completed);
            SettingsPage.Error.Text = Bridge.Ok(completed) ? "" : Bridge.Message(completed);
            SettingsPage.Error.Visible = !Bridge.Ok(completed);
            if (Bridge.Ok(completed))
            {
                settingsLoaded = true;
                SettingsPage.LoadValues(completed.ContainsKey("settings") ? (Dictionary<string,object>)completed["settings"] : value,false);
                SettingsPage.LoadFileSharingValues(completed.ContainsKey("file_settings") ? completed["file_settings"] as Dictionary<string,object> : files);
                if (completed.ContainsKey("windows_settings"))
                    SettingsPage.LoadWindowsValues((Dictionary<string,object>)completed["windows_settings"],
                        completed.ContainsKey("windows_disk") ? (Dictionary<string,object>)completed["windows_disk"] : null);
                if (completed.ContainsKey("viewer_settings"))
                {
                    var viewer = (Dictionary<string,object>)completed["viewer_settings"];
                    SettingsPage.LoadViewerValues(viewer); ApplyViewerPreferences(viewer);
                }
            }
            await RefreshAfterAsync(completed);
        }
        catch (Exception) { FailedAction("save-settings", completed); if (CanUpdate) { SettingsPage.Error.Text = Bridge.Message(operationResult); SettingsPage.Error.Visible = true; } }
        finally { if (CanUpdate) Busy(false); }
    }

    protected override bool ProcessCmdKey(ref Message message,Keys keys)
    {
        if (keys == Keys.F1) { ShowKeyboardHelp(); return true; }
        if ((keys & Keys.Modifiers) == Keys.Control && (int)(keys & Keys.KeyCode) >= (int)Keys.D1 && (int)(keys & Keys.KeyCode) <= (int)Keys.D4)
        { navigation[(AppPage)((int)(keys & Keys.KeyCode)-(int)Keys.D1)].PerformClick(); return true; }
        if (keys == (Keys.Control|Keys.Enter) && ActivePage == AppPage.Home)
        { if (OpenDesktopButton.Enabled) OpenDesktopButton.PerformClick(); else if (StartButton.Enabled) StartButton.PerformClick(); return true; }
        if (keys == (Keys.Control|Keys.S))
        { if (ActivePage == AppPage.Connection) ConnectionPage.SaveButton.PerformClick(); else if (ActivePage == AppPage.Settings) SettingsPage.SaveButton.PerformClick(); else return base.ProcessCmdKey(ref message,keys); return true; }
        if (keys == (Keys.Control|Keys.N) && ActivePage == AppPage.Connection) { ConnectionPage.NewButton.PerformClick(); return true; }
        if (keys == (Keys.Control|Keys.I) && ActivePage == AppPage.Connection) { ConnectionPage.ImportButton.PerformClick(); return true; }
        if (keys == Keys.F5) { if (!busy) RefreshFromKeyboard(); return true; }
        return base.ProcessCmdKey(ref message,keys);
    }

    private async void RefreshFromKeyboard()
    {
        if (ActivePage == AppPage.Connection) await ProfileActionAsync("list-profiles",null);
        else await RunAction("diagnose",ActivePage == AppPage.Diagnostics);
    }

    private void ShowKeyboardHelp()
    {
        using(var dialog = new Form { Text = "Справка и клавиши", Font = Font, BackColor = NativeStyle.Background,
            ClientSize = new Size(580,430), MinimumSize = new Size(430,350), StartPosition = FormStartPosition.CenterParent, ShowInTaskbar = false, MinimizeBox = false, MaximizeBox = false })
        {
            var content = new TextBox { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill, BorderStyle = BorderStyle.None,
                BackColor = NativeStyle.Background, ScrollBars = ScrollBars.Vertical, AccessibleName = "Справка по управлению", Text = String.Join(Environment.NewLine,new[]{
                    "В панели программы", "Ctrl+1…4 — открыть раздел", "Ctrl+Enter — запустить или открыть Windows",
                    "Ctrl+S — сохранить подключение или настройки", "Ctrl+N — новое подключение", "Ctrl+I — импорт подключения",
                    "F5 — обновить состояние или список подключений", "F1 — открыть эту справку", "", "В окне виртуальной Windows",
                    "Ctrl+Alt+F — включить или выключить полный экран", "Ctrl+Alt+G — освободить мышь и клавиатуру", "",
                    "Файлы ПК в Windows", "Режим и папки выбираются в настройках и применяются при следующем запуске Windows.",
                    "Первый раз открой в Windows диск WCPFILES, запусти RUNFILES.CMD и подтверди запрос администратора.",
                    "Папка программы, её служебные данные и автозагрузка ПК из Windows недоступны.", "",
                    "Выключай Windows кнопкой «Выключить» или через меню питания внутри Windows.", "Закрытие работающей панели оставляет Windows в работе и сворачивает панель в область уведомлений."}) };
            var close = new RoundedButton { Text = "Понятно", Primary = true, DialogResult = DialogResult.OK, AutoSize = true, Dock = DockStyle.Bottom, Padding = new Padding(12,8,12,8) };
            dialog.Padding = new Padding(22); dialog.Controls.Add(content); dialog.Controls.Add(close); dialog.AcceptButton = close; dialog.CancelButton = close; dialog.ShowDialog(this);
        }
    }

}

internal static class Program
{
    private static void WriteNewJson(string path, object value)
    {
        using (FileStream file = new FileStream(path, FileMode.CreateNew, FileAccess.Write))
        using (StreamWriter writer = new StreamWriter(file, new UTF8Encoding(false)))
            writer.Write(new JavaScriptSerializer().Serialize(value));
    }

    private static int SelfTest(string reportPath)
    {
        Dictionary<string, bool> checks = new Dictionary<string, bool>();
        string root = Bridge.Root;
        checks.Add("relative-root", root == Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
        string[] values = { "with spaces", "кавычка \" и Unicode", "trailing\\", "mixed\\\"tail\\\\" };
        string probePath = Path.Combine(Path.GetDirectoryName(reportPath), "native-argv-probe-" + Guid.NewGuid().ToString("N") + ".json");
        string[] args = new[] { "--args-probe", probePath }.Concat(values).ToArray();
        ProcessStartInfo probe = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, String.Join(" ", args.Select(Bridge.Quote)));
        probe.UseShellExecute = false;
        probe.CreateNoWindow = true;
        using (Process process = Process.Start(probe))
        {
            bool exited = process.WaitForExit(10000);
            checks.Add("native-argument-roundtrip", exited && process.ExitCode == 0 &&
                values.SequenceEqual(new JavaScriptSerializer().Deserialize<string[]>(File.ReadAllText(probePath))));
        }
        if (File.Exists(probePath)) File.Delete(probePath);
        string[] buttons;
        using (MainForm form = new MainForm())
        {
            buttons = new[] { "Главная", form.ConnectionButton.Text, form.SettingsButton.Text, form.DiagnosticsButton.Text };
            checks.Add("ui-controls", !form.StartButton.Enabled && buttons.Distinct().Count() == 4 && form.PrepareButton.Enabled);
        }
        checks.Add("missing-runtime-fails-closed", !Bridge.Ok(Bridge.Run(Path.Combine(root, "missing-runtime"), "get-settings", null)));
        WriteNewJson(reportPath, new Dictionary<string, object> { { "checks", checks }, { "project_root", root }, { "buttons", buttons } });
        return checks.Values.All(value => value) ? 0 : 1;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length >= 2 && args[0] == "--args-probe") { WriteNewJson(args[1], args.Skip(2).ToArray()); return 0; }
            if (args.Length == 2 && args[0] == "--self-test") return SelfTest(args[1]);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
            return 0;
        }
        catch (Exception error)
        {
            if (args.Length > 0) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1; }
            MessageBox.Show("Не удалось открыть панель. Проверьте целостность комплекта и .NET Framework 4.8.", "Windows Claude");
            return 1;
        }
    }
}
