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
        ((RoundedButton)editor.SaveButton).Hint = null; editor.CancelButton.Text = "Отмена"; editor.CancelButton.DialogResult = DialogResult.Cancel;
        AcceptButton = editor.SaveButton; CancelButton = editor.CancelButton; Controls.Add(editor);
    }
}

internal enum AppPage { Home, Connection, Settings, Diagnostics, Help }

internal sealed partial class MainForm : Form
{
    private string root = Bridge.Root;
    private static readonly Color Ink = NativeStyle.Ink;
    private static readonly Color Muted = NativeStyle.Muted;
    private static readonly Color Blue = NativeStyle.Accent;
    internal readonly Button StartButton = MakeButton("Запустить Windows", true);
    internal readonly Button OpenDesktopButton = MakeButton("Открыть Windows", true);
    internal readonly Button HelpButton = MakeButton("Справка", false);
    private readonly Panel helpPage = new Panel { Dock = DockStyle.Fill, BackColor = NativeStyle.Background };
    private readonly Button copyReportButton = MakeButton("Скопировать отчёт", false);
    private readonly ToolTip tips = new ToolTip { AutoPopDelay = 12000, InitialDelay = 400, ReshowDelay = 150, ShowAlways = true };
    private readonly Label diagnosticSummary = new Label { AutoSize = true, Font = new Font("Segoe UI",13,FontStyle.Bold), Text = "Проверка ПК и комплекта", Margin = new Padding(0,0,0,2) };
    private readonly Label diagnosticScope = new Label { AutoSize = true, ForeColor = NativeStyle.Muted, Margin = new Padding(0,2,0,0), Font = new Font("Segoe UI",9),
        Text = "ПК и комплект проверяются без интернета, сервер проверяется в разделе «Подключение»." };
    internal readonly Button SettingsButton = MakeButton("Настройки", false);
    internal readonly Button DiagnosticsButton = MakeButton("Диагностика", false);
    internal readonly Button PrepareButton = MakeButton("Восстановить Python и QEMU", false);
    private readonly Label status = new Label { Text = "Проверяем готовность", AutoSize = true, Font = new Font("Segoe UI", 13, FontStyle.Bold), ForeColor = Ink };
    // Kept as the plain-text representation for backend integration and accessibility checks.
    private readonly TextBox details = new TextBox { Multiline = true, ReadOnly = true, Visible = false };
    private readonly Label nextStep = new Label { AutoSize = true, ForeColor = Muted };
    private readonly Label resources = new Label { AutoSize = true, Font = new Font("Segoe UI", 11), ForeColor = Ink };
    private readonly Pill stateChip = new Pill { AutoSize = true, Font = new Font("Segoe UI",9.5f), Padding = new Padding(26,5,12,5), BackColor = Color.FromArgb(229,229,234), ForeColor = Color.FromArgb(58,58,60), Dot = Color.FromArgb(142,142,147), Text = "Состояние уточняется" };
    private readonly Pill[] stepLabels = new[]{"Шлюз","VPN","Windows","Файлы ПК"}.Select(name => new Pill { Text = name, AutoSize = false, Height = 40, Padding = new Padding(44,0,8,0), TextAlign = ContentAlignment.MiddleLeft, Font = new Font("Segoe UI",9.5f), BackColor = Color.FromArgb(245,245,248), ForeColor = NativeStyle.Muted, AccessibleName = "Шаг запуска: " + name }).ToArray();
    private readonly Panel statusIcon = new Panel { Size = new Size(56,56), Margin = new Padding(0,0,20,0), Anchor = AnchorStyles.Left, BackColor = Color.White };
    private FlowLayoutPanel homeKeys;
    private readonly Label[] cardValues = Enumerable.Range(0,3).Select(index => new Label { AutoSize = true, Font = new Font("Segoe UI",11.5f,FontStyle.Bold), ForeColor = NativeStyle.Ink, Text = "—", Margin = new Padding(0,0,0,2) }).ToArray();
    private readonly Label[] cardDetails = Enumerable.Range(0,3).Select(index => new Label { AutoSize = true, Font = new Font("Segoe UI",9.5f), ForeColor = NativeStyle.Muted, Text = "", Margin = Padding.Empty }).ToArray();
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
    // Checks as a grid of small cards; it scrolls only as a last resort with very large text.
    private readonly FlowLayoutPanel checkRows = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, FlowDirection = FlowDirection.LeftToRight, WrapContents = true, Padding = Padding.Empty, Margin = Padding.Empty };
    private TableLayoutPanel diagnosticsLayout;
    private RoundedPanel diagnosticsCard;
    private readonly List<TableLayoutPanel> recoveryItems = new List<TableLayoutPanel>();
    private TableLayoutPanel recoveryActions;
    private bool recoveryInRow = true;
    private int diagnosticsFailed = -1;
    private bool fittingDiagnostics;
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
        ClientSize = new Size(960, 640);
        MinimumSize = new Size(900, 620);
        StartPosition = FormStartPosition.CenterScreen;
        // As in the panel mockups: a grey sidebar (name, sections with their keys, help at the bottom) and the page beside it.
        TableLayoutPanel frame = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = Padding.Empty, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
        frame.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute,220)); frame.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); frame.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        TableLayoutPanel nav = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 7, Margin = Padding.Empty, Padding = new Padding(14,20,14,16), BackColor = NativeStyle.Sidebar };
        nav.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for (int index = 0; index < 7; index++) nav.RowStyles.Add(index == 5 ? new RowStyle(SizeType.Percent,100) : new RowStyle(SizeType.AutoSize));
        Label brand = new Label { Text = "Windows Claude", AutoSize = false, Height = 52, Dock = DockStyle.Fill, Font = new Font("Segoe UI",11.5f,FontStyle.Bold), ForeColor = Ink,
            Padding = new Padding(50,0,0,14), TextAlign = ContentAlignment.MiddleLeft, Margin = Padding.Empty, BackColor = NativeStyle.Sidebar };
        brand.Paint += delegate(object sender, PaintEventArgs args) {
            args.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            Rectangle mark = new Rectangle(10,(brand.Height-14-30)/2,30,30);
            using (System.Drawing.Drawing2D.GraphicsPath path = NativeStyle.Rounded(mark,8)) using (Brush fill = new SolidBrush(Blue)) args.Graphics.FillPath(fill,path);
            AppIcons.Draw(args.Graphics,AppGlyph.Monitor,Rectangle.Inflate(mark,-6,-6),Color.White);
        };
        nav.Controls.Add(brand,0,0);
        Button[] buttons = { overviewButton, ConnectionButton, SettingsButton, DiagnosticsButton, HelpButton };
        AppPage[] order = { AppPage.Home, AppPage.Connection, AppPage.Settings, AppPage.Diagnostics, AppPage.Help };
        AppGlyph[] glyphs = { AppGlyph.Home, AppGlyph.Globe, AppGlyph.Sliders, AppGlyph.Pulse, AppGlyph.Help };
        string[] hints = { "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4", "F1" };
        for (int index = 0; index < order.Length; index++)
        {
            AppPage target = order[index]; RoundedButton button = (RoundedButton)buttons[index];
            button.Segment = true; button.Nav = true; button.Hint = hints[index]; button.Glyph = glyphs[index];
            button.AutoSize = false; button.Dock = DockStyle.Fill; button.MinimumSize = new Size(0,40); button.Height = 40;
            button.Font = new Font("Segoe UI",10); button.Margin = new Padding(0,0,0,4); button.Padding = new Padding(12,0,12,0);
            button.Click += async delegate { await NavigateAsync(target); }; navigation.Add(target,button);
            nav.Controls.Add(button,0,index == 4 ? 6 : index + 1);
        }
        TableLayoutPanel content = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, Padding = new Padding(32,26,32,14) };
        content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        content.RowStyles.Add(new RowStyle(SizeType.Percent,100)); content.RowStyles.Add(new RowStyle(SizeType.AutoSize)); content.RowStyles.Add(new RowStyle(SizeType.Absolute,4));
        content.Controls.Add(pages,0,0); operation.Margin = new Padding(0,6,0,0);
        frame.Controls.Add(nav,0,0); frame.Controls.Add(content,1,0);
        pageControls.Add(AppPage.Home,overview); pageControls.Add(AppPage.Connection,ConnectionPage); pageControls.Add(AppPage.Settings,SettingsPage); pageControls.Add(AppPage.Diagnostics,diagnostics); pageControls.Add(AppPage.Help,helpPage);
        foreach (Control page in pageControls.Values) { page.Dock = DockStyle.Fill; pages.Controls.Add(page); }
        TableLayoutPanel help = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Margin = Padding.Empty };
        help.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); help.RowStyles.Add(new RowStyle(SizeType.AutoSize)); help.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        help.Controls.Add(NativeStyle.PageHeader(help,"Справка"),0,0); help.Controls.Add(HelpGrid(),0,1); helpPage.Controls.Add(help);
        // Home as in the mockup: title with the state chip, a status card (icon, text, actions, then four steps),
        // three info cards that open their pages, and the keys along the bottom.
        TableLayoutPanel home = homeLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Margin = Padding.Empty };
        home.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for (int index = 0; index < 5; index++) home.RowStyles.Add(index == 3 ? new RowStyle(SizeType.Percent,100) : new RowStyle(SizeType.AutoSize));
        home.Controls.Add(NativeStyle.PageHeader(home,"Главная",stateChip),0,0);
        RoundedPanel summary = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(24,20,24,20), Margin = new Padding(0,0,0,14), BackColor = Color.White };
        TableLayoutPanel summaryRows = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 3, RowCount = 2, Margin = Padding.Empty, BackColor = Color.White };
        summaryRows.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); summaryRows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); summaryRows.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        summaryRows.RowStyles.Add(new RowStyle(SizeType.AutoSize)); summaryRows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        statusIcon.Paint += delegate(object sender, PaintEventArgs args) {
            args.Graphics.Clear(Color.White); args.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            bool running = workState == "running";
            using (System.Drawing.Drawing2D.GraphicsPath path = NativeStyle.Rounded(new Rectangle(0,0,statusIcon.Width-1,statusIcon.Height-1),16))
            using (Brush fill = new SolidBrush(running ? Color.FromArgb(223,243,228) : Color.FromArgb(232,242,251))) args.Graphics.FillPath(fill,path);
            AppIcons.Draw(args.Graphics,AppGlyph.Monitor,new Rectangle(14,14,statusIcon.Width-28,statusIcon.Height-28),running ? Color.FromArgb(46,158,79) : Blue);
        };
        summaryRows.Controls.Add(statusIcon,0,0);
        TableLayoutPanel texts = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 1, RowCount = 5, Margin = new Padding(0,0,16,0), BackColor = Color.White };
        texts.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); for(int index=0;index<5;index++)texts.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        status.Font = new Font("Segoe UI",14,FontStyle.Bold); status.Margin = new Padding(0,0,0,4); nextStep.Margin = new Padding(0,0,0,2); sessionState.Margin = new Padding(0,0,0,2);
        network.Font = new Font("Segoe UI",9); network.Margin = new Padding(0,2,0,0);
        foreach(Label label in new Label[]{status,nextStep,sessionState,network}) { label.Anchor = AnchorStyles.Left; label.TextAlign = ContentAlignment.MiddleLeft; }
        texts.Controls.Add(status,0,0); texts.Controls.Add(nextStep,0,1); texts.Controls.Add(sessionState,0,2); texts.Controls.Add(network,0,3);
        nextAction.Anchor = AnchorStyles.Left; nextAction.Padding = new Padding(10,7,10,7); nextAction.Margin = new Padding(0,8,0,0); texts.Controls.Add(nextAction,0,4);
        summaryRows.Controls.Add(texts,1,0);
        FlowLayoutPanel launch = new FlowLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = false, Anchor = AnchorStyles.Right, Margin = Padding.Empty, BackColor = Color.White };
        foreach(Button button in new[]{StartButton,OpenDesktopButton}) { ((RoundedButton)button).Glyph = AppGlyph.Play; button.Padding = new Padding(18,0,22,0); button.MinimumSize = new Size(0,48); button.Margin = new Padding(10,0,0,0); }
        ((RoundedButton)StopButton).Danger = true; StopButton.Padding = new Padding(18,0,18,0); StopButton.MinimumSize = new Size(0,48); StopButton.Margin = Padding.Empty;
        launch.Controls.Add(StopButton); launch.Controls.Add(StartButton); launch.Controls.Add(OpenDesktopButton); summaryRows.Controls.Add(launch,2,0);
        TableLayoutPanel steps = homeSteps = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 4, RowCount = 1, Margin = new Padding(0,20,0,0), BackColor = Color.White };
        for (int index = 0; index < 4; index++) { steps.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,25)); stepLabels[index].Dock = DockStyle.Fill; stepLabels[index].Margin = new Padding(index == 0 ? 0 : 5,0,index == 3 ? 0 : 5,0); steps.Controls.Add(stepLabels[index],index,0); }
        steps.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        summaryRows.Controls.Add(steps,0,1); summaryRows.SetColumnSpan(steps,3);
        summary.Controls.Add(summaryRows); home.Controls.Add(summary,0,1);
        TableLayoutPanel cards = homeCards = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty };
        AppPage[] cardPages = { AppPage.Connection, AppPage.Settings, AppPage.Settings };
        for(int index=0;index<3;index++)
        {
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,33.33f));
            RoundedPanel card = new RoundedPanel { Dock = DockStyle.Fill, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = Color.White, Padding = new Padding(18,14,18,14), Margin = new Padding(index==0?0:7,0,index==2?0:7,0), Cursor = Cursors.Hand };
            TableLayoutPanel lines = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, BackColor = Color.White };
            lines.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); for(int row=0;row<3;row++)lines.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            lines.Controls.Add(new Label { Text = new[]{"ПОДКЛЮЧЕНИЕ","РЕСУРСЫ И ЭКРАН","ФАЙЛЫ ПК"}[index], AutoSize = true, Font = new Font("Segoe UI",8.5f), ForeColor = Muted, Margin = new Padding(0,0,0,4) },0,0);
            lines.Controls.Add(cardValues[index],0,1); lines.Controls.Add(cardDetails[index],0,2);
            card.Controls.Add(lines); cards.Controls.Add(card,index,0);
            AppPage target = cardPages[index]; int section = index;
            EventHandler open = async delegate { if (target == AppPage.Settings) SettingsPage.ShowSection(section == 2 ? 2 : 0); await NavigateAsync(target); };
            card.Click += open; foreach (Control child in new Control[]{lines}.Concat(lines.Controls.Cast<Control>())) { child.Click += open; child.Cursor = Cursors.Hand; }
            lines.SizeChanged += delegate { foreach(Label label in lines.Controls.OfType<Label>()) label.MaximumSize = new Size(Math.Max(60,lines.ClientSize.Width),0); };
        }
        cards.RowStyles.Add(new RowStyle(SizeType.AutoSize)); home.Controls.Add(cards,0,2);
        FlowLayoutPanel keys = homeKeys = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true, Margin = new Padding(0,8,0,0) };
        foreach (string[] key in new[]{ new[]{"Ctrl+Enter","запустить"}, new[]{"Ctrl+Alt+F","полный экран"}, new[]{"Ctrl+Alt+G","освободить мышь"}, new[]{"F1","справка"} })
        {
            keys.Controls.Add(new Pill { Text = key[0], AutoSize = true, Radius = 6, Font = new Font("Segoe UI",9), BackColor = Color.FromArgb(229,229,234), ForeColor = Color.FromArgb(58,58,60), Padding = new Padding(6,2,6,2), Margin = new Padding(0,0,6,0) });
            keys.Controls.Add(new Label { Text = key[1], AutoSize = true, Font = new Font("Segoe UI",9.5f), ForeColor = Muted, Margin = new Padding(0,2,20,0) });
        }
        home.Controls.Add(keys,0,4);
        overview.Controls.Add(home);
        // Diagnostics: the outcome in one card, the checks as a grid, then recovery actions that say what they do.
        TableLayoutPanel diagnosticLayout = diagnosticsLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Margin = Padding.Empty };
        diagnosticLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for (int index = 0; index < 5; index++) diagnosticLayout.RowStyles.Add(index == 2 ? new RowStyle(SizeType.Percent,100) : new RowStyle(SizeType.AutoSize));
        ((RoundedButton)refreshButton).Glyph = AppGlyph.Diagnostics; ((RoundedButton)refreshButton).Hint = "F5";
        foreach (Button button in new[] {refreshButton,copyReportButton,PrepareHostButton,PrepareButton,RepairGatewayButton}) button.Padding = new Padding(10,7,10,7);
        diagnosticLayout.Controls.Add(NativeStyle.PageHeader(diagnosticLayout,"Диагностика",copyReportButton,refreshButton),0,0);
        RoundedPanel outcome = diagnosticsCard = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = Color.White, Padding = new Padding(64,14,18,14), Margin = new Padding(0,0,0,12) };
        TableLayoutPanel outcomeLines = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, BackColor = Color.White };
        outcomeLines.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); for (int index = 0; index < 3; index++) outcomeLines.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        checkedAt.Margin = Padding.Empty; outcomeLines.Controls.Add(diagnosticSummary,0,0); outcomeLines.Controls.Add(checkedAt,0,1); outcomeLines.Controls.Add(diagnosticScope,0,2);
        outcome.Controls.Add(outcomeLines);
        outcome.Paint += delegate(object sender, PaintEventArgs args) { int size = Math.Min(32, outcome.Height - 20); NativeStyle.StatusMark(args.Graphics, new Rectangle(20,(outcome.Height-size)/2,size,size), diagnosticsFailed < 0 ? (bool?)null : diagnosticsFailed == 0); };
        diagnosticLayout.Controls.Add(outcome,0,1); diagnosticLayout.Controls.Add(checkRows,0,2);
        TableLayoutPanel recovery = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0,8,0,0) };
        recovery.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); recovery.RowStyles.Add(new RowStyle(SizeType.AutoSize)); recovery.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        FlowLayoutPanel recoveryTitle = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Dock = DockStyle.Top, Margin = new Padding(0,0,0,6) };
        recoveryTitle.Controls.Add(new Label { Text = "Восстановление", AutoSize = true, Font = new Font("Segoe UI",10.5f,FontStyle.Bold), Margin = new Padding(0,0,10,0) });
        preparationHint.Text = "Нужно, только если проверка нашла проблему. Работает без интернета."; preparationHint.Margin = new Padding(0,2,0,0); recoveryTitle.Controls.Add(preparationHint);
        TableLayoutPanel actions = recoveryActions = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Top, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty };
        for (int index = 0; index < 3; index++) actions.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        for (int index = 0; index < 3; index++) actions.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        string[] captions = { "Из локальных файлов комплекта", "Подключения и Windows сохранятся", "Включит виртуализацию, нужны права администратора" };
        Button[] repairs = { PrepareButton, RepairGatewayButton, PrepareHostButton };
        for (int index = 0; index < repairs.Length; index++)
        {
            TableLayoutPanel item = new TableLayoutPanel { AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0,0,10,6) };
            Button button = repairs[index]; button.Margin = Padding.Empty; button.Dock = DockStyle.Top;
            Label caption = new Label { Text = captions[index], AutoSize = true, ForeColor = Muted, Font = new Font("Segoe UI",9), Margin = new Padding(6,4,0,0) };
            recoveryItems.Add(item); item.Controls.Add(button,0,0); item.Controls.Add(caption,0,1); actions.Controls.Add(item,index,0);
        }
        recovery.Controls.Add(recoveryTitle,0,0); recovery.Controls.Add(actions,0,1);
        diagnosticLayout.Controls.Add(recovery,0,3); reportLocation.Margin = new Padding(0,4,0,0); diagnosticLayout.Controls.Add(reportLocation,0,4); diagnostics.Controls.Add(diagnosticLayout);
        operation.Height=operation.Font.Height+4;operation.FontChanged+=delegate{operation.Height=operation.Font.Height+4;};
        content.Controls.Add(operation,0,1); content.Controls.Add(progress,0,2); Controls.Add(frame);
        overview.Layout += delegate { FitHome(); }; homeLayout.Layout += delegate { FitHome(); };
        overview.Resize += delegate {
            // The middle column wraps: card width minus padding, the icon and the actions.
            int width = Math.Max(120,overview.ClientSize.Width - summary.Padding.Horizontal - statusIcon.Width - statusIcon.Margin.Horizontal - launch.PreferredSize.Width - texts.Margin.Horizontal);
            foreach (Label label in new[] {status,nextStep,sessionState,network}) label.MaximumSize = new Size(width,0);
            resources.MaximumSize = new Size(overview.ClientSize.Width,0);
            FitHome();
        };
        diagnosticLayout.SizeChanged += delegate {
            foreach(Label label in new[]{diagnosticScope,diagnosticSummary,checkedAt})label.MaximumSize = new Size(Math.Max(100,diagnosticLayout.Width-90),0);
            preparationHint.MaximumSize = reportLocation.MaximumSize = new Size(Math.Max(100,diagnosticLayout.Width),0);
        };
        checkRows.SizeChanged += delegate { FitCheckRows(); }; checkRows.ClientSizeChanged += delegate { FitCheckRows(); }; checkRows.FontChanged += delegate { FitCheckRows(); };
        diagnosticLayout.Layout += delegate { FitDiagnostics(); };
        StartButton.Enabled = false; StopButton.Enabled = false; nextAction.Visible = false;
        StartButton.Click += async delegate { await RunAction("start",false); };
        OpenDesktopButton.Click += async delegate { await RunAction("show-desktop",false); };
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

    private TableLayoutPanel homeLayout, homeCards, homeSteps;
    private bool fittingHome;
    // Large text on a small window: drop the secondary parts first, never the status or the main action.
    private void FitHome()
    {
        if (homeLayout == null || fittingHome || overview.ClientSize.Height < 50) return;
        fittingHome = true;
        try
        {
            // Row heights read inside a layout pass are stale, so measure preferred sizes instead.
            Control[] optional = {homeKeys, homeCards, homeSteps};
            foreach (Control item in optional) item.Visible = true;
            int width = homeLayout.ClientSize.Width;
            // Visible reads false while the window itself is hidden, so track what was dropped here.
            HashSet<Control> dropped = new HashSet<Control>();
            Func<int> needed = () => homeLayout.Controls.Cast<Control>().Where(item => !dropped.Contains(item))
                .Sum(item => item.GetPreferredSize(new Size(width - item.Margin.Horizontal,0)).Height + item.Margin.Vertical);
            foreach (Control item in optional)
            {
                if (needed() <= homeLayout.ClientSize.Height) break;
                item.Visible = false; dropped.Add(item);
            }
        }
        finally { fittingHome = false; }
    }

    private void UpdateHomeCards(Dictionary<string,object> report,Dictionary<string,object> settings,Dictionary<string,object> displaySettings,string filesState)
    {
        bool running = workState == "running", starting = ActiveVmState(workState) && !running;
        stateChip.Text = running ? "Windows работает" : starting ? "Windows " + VmState(workState) : workState == "stopped" ? "Windows выключена" : "Состояние уточняется";
        stateChip.BackColor = running ? Color.FromArgb(223,243,228) : starting ? Color.FromArgb(232,242,251) : Color.FromArgb(229,229,234);
        stateChip.ForeColor = running ? Color.FromArgb(30,107,52) : starting ? Color.FromArgb(28,78,120) : Color.FromArgb(58,58,60);
        stateChip.Dot = running ? Color.FromArgb(46,158,79) : starting ? Blue : Color.FromArgb(142,142,147); stateChip.Invalidate(); statusIcon.Invalidate();
        bool[] done = { gatewayState == "running", Flag(report,"network_verified"), running, filesState == "ready" };
        bool[] active = { ActiveVmState(gatewayState) && gatewayState != "running", gatewayState == "running" && !done[1], starting, running && filesState != "ready" && filesState != "off" && filesState.Length > 0 };
        for(int index=0;index<4;index++)
        {
            string name = new[]{"Шлюз","VPN","Windows","Файлы ПК"}[index];
            bool off = index == 3 && filesState == "off";
            stepLabels[index].Badge = done[index] ? "✓" : active[index] ? "…" : (index + 1).ToString();
            stepLabels[index].Text = name + (off ? " — выкл." : "");
            stepLabels[index].BackColor = done[index] ? Color.FromArgb(238,248,241) : active[index] ? Color.FromArgb(232,242,251) : Color.FromArgb(245,245,248);
            stepLabels[index].ForeColor = done[index] ? Color.FromArgb(30,107,52) : active[index] ? Color.FromArgb(28,78,120) : NativeStyle.Muted;
        }
        cardValues[0].Text = sessionState.Text.Contains("(") ? sessionState.Text.Substring(0,sessionState.Text.IndexOf(" (")) : "Не выбрано";
        cardDetails[0].Text = !sessionState.Text.Contains("(") ? "Добавь в «Подключении»" : sessionState.Text.Contains("проверено") ? "Проверено" : "Для следующего запуска";
        int memory; string gib = settings != null && Int32.TryParse(TextValue(settings,"work_memory_mib","0"),out memory) && memory > 0 ? ((decimal)memory/1024).ToString("0.#",System.Globalization.CultureInfo.CurrentCulture) + " ГиБ" : "—";
        cardValues[1].Text = gib + ", " + (settings != null ? TextValue(settings,"vcpus","—") : "—") + " проц.";
        string screen = "";
        if (displaySettings != null)
            screen = "Экран " + TextValue(displaySettings,"width","—") + " × " + TextValue(displaySettings,"height","—");
        cardDetails[1].Text = screen;
        if (filesState.Length == 0) { cardValues[2].Text = "Выключены"; cardDetails[2].Text = "Включить в настройках"; }
        FitHome();
    }

    private static string FileSharingState(Dictionary<string,object> report)
    {
        return report != null && report.ContainsKey("host_files") ? TextValue(report["host_files"] as Dictionary<string,object>,"status","") : "";
    }

    // Two columns when there is room for them, one otherwise; cards in a row share a height.
    private bool fittingChecks;
    private void FitCheckRows()
    {
        if (fittingChecks) return;
        fittingChecks = true;
        try
        {
            int available = Math.Max(100, checkRows.ClientSize.Width - 2);
            int columns = available >= checkRows.Font.Height * 28 ? 2 : 1, gap = 10;
            int width = (available - gap * columns) / columns;
            List<Control> cards = checkRows.Controls.Cast<Control>().ToList();
            for (int start = 0; start < cards.Count; start += columns)
            {
                int height = 0;
                for (int index = start; index < Math.Min(cards.Count, start + columns); index++)
                {
                    Control card = cards[index];
                    if (!(card is RoundedPanel)) { ((Label)card).MaximumSize = new Size(available, 0); height = Math.Max(height, card.PreferredSize.Height); continue; }
                    Control text = card.Controls[0];
                    foreach (Label label in text.Controls.OfType<Label>()) label.MaximumSize = new Size(Math.Max(40, width - card.Padding.Horizontal), 0);
                    height = Math.Max(height, text.GetPreferredSize(new Size(Math.Max(40, width - card.Padding.Horizontal), 0)).Height + card.Padding.Vertical);
                }
                for (int index = start; index < Math.Min(cards.Count, start + columns); index++)
                    if (cards[index] is RoundedPanel) cards[index].Size = new Size(width, height);
            }
        }
        finally { fittingChecks = false; }
    }

    // Recovery actions side by side when the window is wide enough, one under another otherwise.
    private void FitDiagnostics()
    {
        if (diagnosticsLayout == null || fittingDiagnostics || diagnosticsLayout.ClientSize.Height < 50) return;
        fittingDiagnostics = true;
        try
        {
            int width = diagnosticsLayout.ClientSize.Width;
            int column = (width - 10 * recoveryItems.Count) / Math.Max(1, recoveryItems.Count);
            foreach (TableLayoutPanel item in recoveryItems) item.Controls[0].MinimumSize = Size.Empty;
            bool even = recoveryItems.All(item => item.Controls[0].PreferredSize.Width <= column);
            int[] own = recoveryItems.Select(item => even ? column : Math.Min(width - 10, Math.Max(item.Controls[0].PreferredSize.Width,
                TextRenderer.MeasureText("Подключения и Windows", item.Controls[1].Font).Width + 12))).ToArray();
            bool inRow = own.Sum() + 10 * own.Length <= width;
            for (int index = 0; index < recoveryItems.Count; index++)
            {
                TableLayoutPanel item = recoveryItems[index]; int size = inRow ? own[index] : Math.Min(width - 10, item.Controls[0].PreferredSize.Width);
                item.Controls[0].MinimumSize = new Size(size, 0); item.Controls[1].MaximumSize = new Size(Math.Max(40, Math.Max(size, own[index]) - 6), 0);
            }
            if (inRow != recoveryInRow)
            {
                recoveryInRow = inRow;
                for (int index = 0; index < recoveryItems.Count; index++) recoveryActions.SetCellPosition(recoveryItems[index], new TableLayoutPanelCellPosition(inRow ? index : 0, inRow ? 0 : index));
            }
        }
        finally { fittingDiagnostics = false; }
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
        diagnosticSummary.Text = checks.Count == 0 ? "Проверка ПК и комплекта" : failedChecks == 0 ? "Проблем не найдено" : "Требует внимания: " + failedChecks + " из " + checks.Count;
        diagnosticsFailed = checks.Count == 0 ? -1 : failedChecks; if (diagnosticsCard != null) diagnosticsCard.Invalidate();
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
            if(blocker==null&&(filesState=="failed"||filesState=="unavailable"))nextStep.Text="Проверь доступность выбранных папок ПК в настройках. Windows подключит их снова при следующем входе.";
            cardValues[2].Text=filesState=="ready"?"Подключены":filesState=="off"?"Выключены":filesState=="unavailable"?"Недоступны":filesState=="failed"?"Нет связи":"Подключатся при входе";
            cardDetails[2].Text=filesDetail;
        }
        UpdateHomeCards(report, settings, displaySettings, filesState);
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
            RoundedPanel card = new RoundedPanel { BackColor = Color.White, Padding = new Padding(44, 10, 12, 10), Margin = new Padding(0, 0, 10, 10), AccessibleName = (passed ? "Готово: " : "Нужно внимание: ") + title };
            TableLayoutPanel text = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Margin = Padding.Empty, BackColor = Color.White };
            text.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); text.RowStyles.Add(new RowStyle(SizeType.AutoSize)); text.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            text.Controls.Add(new Label { Text = title, AutoSize = true, Font = new Font("Segoe UI", 10, FontStyle.Bold), ForeColor = passed ? Ink : Color.FromArgb(165, 63, 45), Margin = new Padding(0, 0, 0, 2) },0,0);
            text.Controls.Add(new Label { Text = detail, AutoSize = true, ForeColor = Muted, Font = new Font("Segoe UI", 9.5f), Margin = Padding.Empty },0,1);
            card.Controls.Add(text);
            card.Paint += delegate(object sender, PaintEventArgs args) { NativeStyle.StatusMark(args.Graphics, new Rectangle(14, 12, 20, 20), passed); };
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
        if (diagnosticsLayout != null) diagnosticsLayout.PerformLayout();
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
        checkedAt.Text = "Проверено в " + DateTime.Now.ToString("HH:mm") + ", ошибки показаны первыми.";
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
            else if (action == "diagnose") checkedAt.Text = completed.ContainsKey("checks") ? "Проверено в " + DateTime.Now.ToString("HH:mm") + ", ошибки показаны первыми." : "Диагностика не обновилась. Повтори проверку.";
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
        if (keys == Keys.F1) { navigation[AppPage.Help].PerformClick(); return true; }
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

    // Help page: four short cards, keys as chips; fits the page without scrolling.
    internal static TableLayoutPanel HelpGrid()
    {
        TableLayoutPanel grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, Margin = Padding.Empty, AccessibleName = "Справка по управлению" };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50)); grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));
        grid.RowStyles.Add(new RowStyle(SizeType.Percent,50)); grid.RowStyles.Add(new RowStyle(SizeType.Percent,50));
        grid.Controls.Add(HelpCard("Клавиши панели", new[]{
            new[]{"Ctrl+1…4","разделы панели"}, new[]{"Ctrl+Enter","запустить или открыть Windows"}, new[]{"Ctrl+S","сохранить"},
            new[]{"Ctrl+N","новое подключение"}, new[]{"F5","обновить состояние"}}, null),0,0);
        grid.Controls.Add(HelpCard("В окне Windows", new[]{
            new[]{"Ctrl+Alt+F","полный экран и обратно"}, new[]{"Ctrl+Alt+G","освободить мышь и клавиатуру"}},
            "Окно можно уменьшать и разворачивать: картинка масштабируется, Windows остаётся в своём разрешении."),1,0);
        grid.Controls.Add(HelpCard("Файлы ПК", null,
            "Режим и папки выбираются в «Настройки → Файлы ПК». Windows подключает папки сама при входе, они видны в Проводнике.\n\nПапка программы и автозагрузка ПК из Windows недоступны."),0,1);
        grid.Controls.Add(HelpCard("Выключение и перенос", null,
            "Выключай кнопкой «Выключить» или из меню «Пуск» в Windows. Закрытие панели Windows не выключает.\n\nДля переноса скопируй всю папку программы после выключения."),1,1);
        return grid;
    }

    private static RoundedPanel HelpCard(string title, string[][] keys, string note)
    {
        RoundedPanel card = new RoundedPanel { Dock = DockStyle.Fill, BackColor = Color.White, Padding = new Padding(16,12,16,12), Margin = new Padding(0,0,10,10) };
        TableLayoutPanel lines = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Margin = Padding.Empty, BackColor = Color.White };
        lines.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); lines.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        int row = 0;
        Label heading = new Label { Text = title, AutoSize = true, Font = new Font("Segoe UI",10.5f,FontStyle.Bold), Margin = new Padding(0,0,0,8) };
        lines.RowStyles.Add(new RowStyle(SizeType.AutoSize)); lines.Controls.Add(heading,0,row); lines.SetColumnSpan(heading,2); row++;
        foreach (string[] key in keys ?? new string[0][])
        {
            lines.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            lines.Controls.Add(new Label { Text = key[0], AutoSize = true, Font = new Font("Segoe UI",9), BackColor = Color.FromArgb(235,235,240), Padding = new Padding(6,2,6,2), Margin = new Padding(0,0,12,8), Anchor = AnchorStyles.Left },0,row);
            lines.Controls.Add(new Label { Text = key[1], AutoSize = true, Margin = new Padding(0,0,0,8), Anchor = AnchorStyles.Left },1,row); row++;
        }
        if (note != null)
        {
            Label text = new Label { Text = note, AutoSize = true, ForeColor = keys == null ? NativeStyle.Ink : NativeStyle.Muted, Font = new Font("Segoe UI", keys == null ? 9.5f : 9), Margin = new Padding(0,keys == null ? 0 : 6,0,0) };
            lines.RowStyles.Add(new RowStyle(SizeType.AutoSize)); lines.Controls.Add(text,0,row); lines.SetColumnSpan(text,2);
            lines.SizeChanged += delegate { text.MaximumSize = new Size(Math.Max(60,lines.ClientSize.Width),0); };
        }
        lines.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        card.Controls.Add(lines);
        return card;
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
