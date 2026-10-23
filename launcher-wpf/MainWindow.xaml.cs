using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;
using System.Windows.Threading;
using Wpf.Ui.Controls;
using TextBlock = System.Windows.Controls.TextBlock;
using Forms = System.Windows.Forms;

namespace WindowsClaude.Launcher
{
    public enum AppPage { Home, Connection, Settings, Diagnostics, Help }

    // Same state rules as the WinForms launcher (ui/Launcher.cs): an unconfirmed operation blocks
    // everything except diagnostics, the panel hides to the tray while Windows runs, status polls every 3 s.
    public partial class MainWindow : FluentWindow
    {
        static readonly Brush Green = Hex("#34C759"), GreenSoft = Hex("#E3F7E8"), GreenInk = Hex("#1E7A36"),
            Blue = Hex("#3390EC"), BlueSoft = Hex("#E9F2FD"), BlueInk = Hex("#1C5A9A"),
            Grey = Hex("#8A8A8E"), GreySoft = Hex("#EDEDF0"), Orange = Hex("#FF9500"), Red = Hex("#E53935"), Ink = Hex("#1C1C1E");
        static readonly int[,] Modes = { { 800, 600 }, { 1024, 768 }, { 1280, 720 }, { 1280, 800 }, { 1366, 768 }, { 1440, 900 },
            { 1600, 900 }, { 1680, 1050 }, { 1920, 1080 }, { 1920, 1200 }, { 2560, 1440 } };
        const int DefaultMode = 8;
        static readonly string[] Protocols = { "hysteria2", "socks5", "http", "https" };

        readonly DispatcherTimer lifecycleTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        readonly DispatcherTimer saveTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(700) };
        readonly DispatcherTimer toastTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        readonly Border[] stepCards = new Border[4];
        readonly TextBlock[] stepTexts = new TextBlock[4];
        readonly Ellipse[] stepDots = new Ellipse[4];
        Forms.NotifyIcon trayIcon; Forms.ToolStripMenuItem trayStop;

        Dictionary<string, object> readinessReport, operationResult, settingsHost, originalSettings, originalWindows, originalViewer, originalFiles;
        Task<Dictionary<string, object>> pendingOutcome;
        string pendingStartReceipt, activeOperationAction, workState = "unknown", gatewayState = "unknown", suggestedAction = "retry", reportText = "", displayedChecks;
        bool busy, ready, outcomeUnknown, stopAvailable, testAvailable, runtimeActive, runtimePolling, closing, reallyClose,
            panelHiddenForSession, panelDismissedByUser, settingsLoaded, profilesLoaded, applyingSettings, checksOpen, pulsing;
        int operationRevision;
        readonly List<string> folders = new List<string>();

        // Connection editor state
        string profileId, loadedProtocol, loadedObfs; bool authPresent;
        List<Dictionary<string, object>> profiles = new List<Dictionary<string, object>>();

        static Brush Hex(string value) { var brush = (Brush)new BrushConverter().ConvertFromString(value); brush.Freeze(); return brush; }
        static bool Flag(Dictionary<string, object> item, string key) { return item != null && item.ContainsKey(key) && item[key] is bool && (bool)item[key]; }
        static string Text(Dictionary<string, object> value, string key, string fallback) { return value != null && value.ContainsKey(key) && value[key] != null ? Convert.ToString(value[key], CultureInfo.InvariantCulture) : fallback; }
        static Dictionary<string, object> Child(Dictionary<string, object> value, string key) { return value != null && value.ContainsKey(key) ? value[key] as Dictionary<string, object> : null; }
        static Task<Dictionary<string, object>> Run(string action, object payload) { return Bridge.RunAsync(Bridge.Root, action, payload); }
        static void Show(UIElement element, bool visible) { element.Visibility = visible ? Visibility.Visible : Visibility.Collapsed; }
        static readonly IEasingFunction EaseOut = new CubicEase { EasingMode = EasingMode.EaseOut };

        public MainWindow()
        {
            InitializeComponent();
            string[] steps = { "Шлюз", "VPN", "Windows", "Файлы" };
            for (int index = 0; index < 4; index++)
            {
                stepDots[index] = new Ellipse { Width = 8, Height = 8, Margin = new Thickness(0, 0, 7, 0), VerticalAlignment = VerticalAlignment.Center };
                stepTexts[index] = new TextBlock { Text = steps[index], FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center };
                stepCards[index] = new Border { CornerRadius = new CornerRadius(12), Padding = new Thickness(10, 6, 10, 6), Margin = new Thickness(3, 0, 3, 0),
                    Child = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Center, Children = { stepDots[index], stepTexts[index] } } };
                Steps.Children.Add(stepCards[index]);
            }
            BuildHelp();
            for (int index = 0; index < Modes.GetLength(0); index++) ResolutionBox.Items.Add(Modes[index, 0] + " × " + Modes[index, 1]);
            VersionText.Text = ReleaseInfo.Read(Bridge.Root).DisplayText;

            InputBindings.Add(new KeyBinding(new Command(() => OpenSheet(AppPage.Connection)), Key.D2, ModifierKeys.Control));
            InputBindings.Add(new KeyBinding(new Command(() => OpenSheet(AppPage.Help)), Key.F1, ModifierKeys.None));
            InputBindings.Add(new KeyBinding(new Command(CloseSheet), Key.Escape, ModifierKeys.None));
            InputBindings.Add(new KeyBinding(new Command(() => { var task = RunAction("diagnose", true); }), Key.F5, ModifierKeys.None));
            InputBindings.Add(new KeyBinding(new Command(() => {
                if (OpenDesktopButton.IsVisible && OpenDesktopButton.IsEnabled) OnOpenDesktop(null, null);
                else if (StartButton.IsVisible && StartButton.IsEnabled) OnStart(null, null);
            }), Key.Enter, ModifierKeys.Control));

            saveTimer.Tick += async delegate { if (busy) return; saveTimer.Stop(); await SaveSettingsAsync(); };
            toastTimer.Tick += delegate { toastTimer.Stop(); if (!busy) HideToast(); };
            ProtocolBox.SelectedIndex = 0; NewProfile(); Show(FoldersRow, false);
            Busy(false);
            Loaded += async delegate { StartLifecycle(); await RunAction("diagnose", false); };
            Closing += OnClosing;
        }

        sealed class Command : ICommand
        {
            readonly Action action; public Command(Action action) { this.action = action; }
            public event EventHandler CanExecuteChanged { add { } remove { } }
            public bool CanExecute(object parameter) { return true; }
            public void Execute(object parameter) { action(); }
        }

        static Border KeyCap(string text)
        {
            return new Border { Background = GreySoft, CornerRadius = new CornerRadius(7), Padding = new Thickness(8, 3, 8, 3), VerticalAlignment = VerticalAlignment.Center,
                Child = new TextBlock { Text = text, FontSize = 12.5, Foreground = Ink } };
        }

        void BuildHelp()
        {
            var keys = new[] { new[] { "Ctrl+Enter", "Запустить или открыть Windows" }, new[] { "Ctrl+Alt+F", "Полный экран и обратно" },
                new[] { "Ctrl+Alt+G", "Освободить мышь" }, new[] { "Ctrl+2", "Подключение" }, new[] { "F5", "Проверить снова" }, new[] { "Esc", "Закрыть лист" } };
            for (int index = 0; index < keys.Length; index++)
            {
                if (index > 0) HelpRows.Children.Add(new Border { Style = (Style)FindResource("Separator"), Margin = new Thickness(16, 0, 0, 0) });
                var row = new DockPanel { Margin = new Thickness(16, 13, 16, 13) };
                var cap = KeyCap(keys[index][0]); DockPanel.SetDock(cap, Dock.Right); row.Children.Add(cap);
                row.Children.Add(new TextBlock { Text = keys[index][1], Style = (Style)FindResource("RowText") });
                HelpRows.Children.Add(row);
            }
        }

        // ---------- Sheets and navigation ----------
        void Navigate(AppPage page)
        {
            if (page == AppPage.Connection || page == AppPage.Help) { OpenSheet(page); return; }
            CloseSheet();
            if (page == AppPage.Settings) SettingsGroup.BringIntoView();
            if (page == AppPage.Diagnostics) { SetChecksOpen(true); Dispatcher.BeginInvoke(new Action(() => ChecksPanel.BringIntoView()), DispatcherPriority.Loaded); }
            if (page == AppPage.Home) MainScroll.ScrollToTop();
        }

        void OpenSheet(AppPage page)
        {
            Show(ConnectionSheet, page == AppPage.Connection); Show(EditorSheet, false); Show(HelpSheet, page == AppPage.Help); Show(SheetBack, false);
            SheetTitle.Text = page == AppPage.Help ? "Клавиши" : "Подключение";
            if (page == AppPage.Connection && !profilesLoaded) { var task = LoadProfilesAsync(); }
            if (SheetHost.Visibility == Visibility.Visible) return;
            Show(SheetHost, true);
            SheetDim.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(200)));
            Sheet.UpdateLayout();
            ((TranslateTransform)Sheet.RenderTransform).BeginAnimation(TranslateTransform.YProperty,
                new DoubleAnimation(Math.Max(300, Sheet.ActualHeight), 0, TimeSpan.FromMilliseconds(280)) { EasingFunction = EaseOut });
            if (page == AppPage.Connection) LinkInput.Focus();
        }

        void ShowEditor(string title)
        {
            Show(ConnectionSheet, false); Show(EditorSheet, true); Show(SheetBack, true); SheetTitle.Text = title;
            var shift = new TranslateTransform(); EditorSheet.RenderTransform = shift;
            EditorSheet.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)));
            shift.BeginAnimation(TranslateTransform.XProperty, new DoubleAnimation(40, 0, TimeSpan.FromMilliseconds(220)) { EasingFunction = EaseOut });
        }

        void CloseSheet()
        {
            if (SheetHost.Visibility != Visibility.Visible) return;
            var slide = new DoubleAnimation(0, Math.Max(300, Sheet.ActualHeight), TimeSpan.FromMilliseconds(220)) { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseIn } };
            slide.Completed += delegate { Show(SheetHost, false); };
            SheetDim.BeginAnimation(OpacityProperty, new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(220)));
            ((TranslateTransform)Sheet.RenderTransform).BeginAnimation(TranslateTransform.YProperty, slide);
        }

        void OnCloseSheet(object sender, RoutedEventArgs e) { CloseSheet(); }
        void OnDimClick(object sender, MouseButtonEventArgs e) { CloseSheet(); }
        void OnSheetBack(object sender, RoutedEventArgs e) { OpenSheet(AppPage.Connection); ConnectionSheet.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180))); }
        void OnConnectionRow(object sender, RoutedEventArgs e) { OpenSheet(AppPage.Connection); }
        void OnHelp(object sender, RoutedEventArgs e) { OpenSheet(AppPage.Help); }
        void OnDiagnosticsRow(object sender, RoutedEventArgs e) { SetChecksOpen(!checksOpen); if (readinessReport == null && !busy) { var task = RunAction("diagnose", false); } }

        void SetChecksOpen(bool open)
        {
            if (checksOpen == open) return;
            checksOpen = open; Show(ChecksPanel, open);
            ((RotateTransform)DiagnosticChevron.RenderTransform).BeginAnimation(RotateTransform.AngleProperty, new DoubleAnimation(open ? 180 : 0, TimeSpan.FromMilliseconds(200)) { EasingFunction = EaseOut });
            if (open) ChecksPanel.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(220)));
        }

        // ---------- Toast ----------
        void SetOperation(string text)
        {
            OperationText.Text = text ?? "";
            if (String.IsNullOrEmpty(text)) { HideToast(); return; }
            Show(ToastClose, !busy);
            if (OperationBar.Visibility != Visibility.Visible || OperationBar.Opacity < 1)
            {
                Show(OperationBar, true);
                OperationBar.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)));
                ((TranslateTransform)OperationBar.RenderTransform).BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(24, 0, TimeSpan.FromMilliseconds(240)) { EasingFunction = EaseOut });
            }
            toastTimer.Stop(); if (!busy) toastTimer.Start();
        }
        void HideToast()
        {
            if (OperationBar.Visibility != Visibility.Visible) return;
            var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(180)); fade.Completed += delegate { if (OperationText.Text.Length == 0 || !busy) Show(OperationBar, false); };
            OperationBar.BeginAnimation(OpacityProperty, fade);
        }
        void OnDismissToast(object sender, RoutedEventArgs e) { toastTimer.Stop(); HideToast(); }

        // ---------- Operations ----------
        bool BeginOperation(string action)
        {
            if (closing || busy) return false;
            if (outcomeUnknown && action != "diagnose" && action != "recover-session")
            {
                SetOperation("Предыдущая операция ещё не подтверждена. Сначала нажми «Проверить снова».");
                return false;
            }
            activeOperationAction = action; operationRevision++;
            Busy(true);
            switch (action)
            {
                case "start": SetOperation("Запускаем шлюз и Windows…"); break;
                case "stop": SetOperation("Выключаем Windows и шлюз… Дождись штатного выключения."); break;
                case "repair-gateway": SetOperation("Восстанавливаем шлюз из комплекта…"); break;
                case "recover-session": SetOperation("Восстанавливаем прерванный сеанс… Диск Windows и подключения сохраняются."); break;
                case "prepare-host": SetOperation("Готовим этот ПК… Подтверди запрос UAC."); break;
                case "prepare-components": SetOperation("Восстанавливаем Python и QEMU…"); break;
                case "save-settings": SetOperation("Сохраняем…"); break;
                case "list-profiles": case "get-profile-view": SetOperation("Загружаем подключения…"); break;
                case "save-profile": case "import-profile": SetOperation("Сохраняем подключение…"); break;
                case "select-profile": SetOperation("Выбираем подключение…"); break;
                case "test-profile": SetOperation("Проверяем подключение… Это займёт до пары минут."); break;
                case "export-profile": SetOperation("Экспортируем подключение…"); break;
                default: SetOperation("Проверяем готовность…"); break;
            }
            return true;
        }

        void Busy(bool value)
        {
            busy = value; Show(BusyRing, value); BusyRing.IsIndeterminate = value; // a hidden indeterminate ring keeps animating
            Show(ToastClose, !value);
            if (!value && OperationText.Text.Length > 0) { toastTimer.Stop(); toastTimer.Start(); }
            foreach (UIElement button in new UIElement[] { RefreshButton, PrepareButton, RepairGatewayButton, NextActionButton, AddLinkButton, SaveProfileButton, ProfileList })
                button.IsEnabled = !value;
            PrepareHostButton.IsEnabled = !value && !runtimeActive;
            TestProfileButton.IsEnabled = !value && testAvailable && profiles.Any(item => Flag(item, "selected"));
            ExportButton.IsEnabled = !value && profiles.Count > 0;
            SettingsBody.IsEnabled = settingsLoaded && !runtimeActive && !outcomeUnknown;
            UpdateLaunchButtons(); UpdateTray(); UpdatePulse();
        }

        void UpdateLaunchButtons()
        {
            StartButton.IsEnabled = !busy && ready && !outcomeUnknown && !runtimeActive;
            Show(StartButton, !runtimeActive && !outcomeUnknown && workState != "running");
            Show(OpenDesktopButton, workState == "running");
            OpenDesktopButton.IsEnabled = !busy && !outcomeUnknown && workState == "running";
            Show(StopButton, stopAvailable);
            StopButton.IsEnabled = !busy && !outcomeUnknown && stopAvailable;
        }

        void UpdatePulse()
        {
            bool active = (busy && (activeOperationAction == "start" || activeOperationAction == "stop")) || workState == "starting" || workState == "stopping" || workState == "restarting";
            if (active == pulsing) return;
            pulsing = active;
            var scale = (ScaleTransform)Pulse.RenderTransform;
            if (!active) { Pulse.BeginAnimation(OpacityProperty, null); scale.BeginAnimation(ScaleTransform.ScaleXProperty, null); scale.BeginAnimation(ScaleTransform.ScaleYProperty, null); Pulse.Opacity = 0; return; }
            var grow = new DoubleAnimation(0.75, 1.15, TimeSpan.FromSeconds(1.4)) { RepeatBehavior = RepeatBehavior.Forever, EasingFunction = EaseOut };
            scale.BeginAnimation(ScaleTransform.ScaleXProperty, grow); scale.BeginAnimation(ScaleTransform.ScaleYProperty, grow);
            Pulse.BeginAnimation(OpacityProperty, new DoubleAnimation(0.35, 0, TimeSpan.FromSeconds(1.4)) { RepeatBehavior = RepeatBehavior.Forever });
        }

        void TrackUnknownOutcome(Dictionary<string, object> result)
        {
            if (activeOperationAction == "diagnose" || activeOperationAction == "get-settings" || activeOperationAction == "list-profiles" || activeOperationAction == "get-profile-view") return;
            outcomeUnknown = true;
            if (pendingOutcome == null) pendingOutcome = Bridge.PendingOperation(result);
            if (activeOperationAction == "start" && result.ContainsKey("receipt")) pendingStartReceipt = Text(result, "receipt", "");
        }

        async Task RunAction(string action, bool selectDiagnostics)
        {
            if (!BeginOperation(action)) return;
            if (selectDiagnostics) Navigate(AppPage.Diagnostics);
            Dictionary<string, object> completed = null;
            try
            {
                completed = await Run(action, null);
                if (closing) return;
                if (action == "recover-session" && Bridge.Ok(completed)) { outcomeUnknown = false; pendingOutcome = null; pendingStartReceipt = null; }
                Display(completed);
                SetOperation(action == "diagnose" && completed.ContainsKey("checks") ? "" : Friendly(Bridge.Message(completed)));
                if (action == "prepare-components" || action == "prepare-host" || action == "repair-gateway" || action == "recover-session" || action == "stop" || action == "start")
                    await RefreshAfterAsync(completed);
                if (action == "prepare-host" && Flag(completed, "restart_required")) SetOperation("Подготовка завершена. Перезагрузи ПК самостоятельно, затем нажми «Проверить снова».");
                else if (action == "diagnose") CheckedAtText.Text = completed.ContainsKey("checks") ? "Проверено в " + DateTime.Now.ToString("HH:mm") : "Проверка не обновилась. Повтори.";
            }
            catch (Exception) { FailedAction(action, completed); }
            finally
            {
                if (!closing)
                {
                    Busy(false);
                    if (action == "start" && Bridge.Ok(completed) && ready && workState == "running" && !outcomeUnknown) HidePanelForSession();
                    else if (panelHiddenForSession && (workState == "stopped" || workState == "failed" || outcomeUnknown || (action == "start" && !Bridge.Ok(completed)))) ShowPanelAutomatically();
                    UpdateTray();
                }
            }
        }

        void FailedAction(string action, Dictionary<string, object> completed)
        {
            if (closing) return;
            if (completed != null && Bridge.Ok(completed)) Display(Bridge.Unknown(Bridge.Message(completed) + " Не удалось обновить проверку. Проверь состояние повторно.", "diagnose_failed"));
            else if (action == "diagnose" || action == "get-settings") Display(Bridge.Failure("Не удалось выполнить проверку. Проверь доступ к папке и целостность компонентов."));
            else Display(Bridge.Unknown("Действие не подтверждено. Исход операции неизвестен. Нажми «Проверить снова».", "operation_failed"));
            SetOperation(Bridge.Message(operationResult));
            if (action == "diagnose") CheckedAtText.Text = "Проверка не обновилась. Повтори.";
        }

        async Task RefreshAfterAsync(Dictionary<string, object> completed)
        {
            if (closing || (!Bridge.Ok(completed) && !Flag(completed, "outcome_unknown"))) return;
            var check = await Run("diagnose", null);
            if (closing) return;
            Display(check);
            SetOperation(Friendly(Bridge.Message(completed)));
            CheckedAtText.Text = "Проверено в " + DateTime.Now.ToString("HH:mm");
        }

        // Supervisor messages are written for logs; the full text stays in the copied report.
        static string Friendly(string message)
        {
            if (message == null) return "";
            if (message.StartsWith("Supervisor", StringComparison.Ordinal) || message.Contains("receipt"))
                return "Windows не запустилась: шлюз не смог подключиться к серверу. Проверь подключение и нажми «Запустить» ещё раз.";
            return message;
        }

        void OnStart(object sender, RoutedEventArgs e) { CommitPendingSettings(); var task = RunAction("start", false); }
        void OnStop(object sender, RoutedEventArgs e) { if (stopAvailable) { var task = RunAction("stop", false); } }
        void OnOpenDesktop(object sender, RoutedEventArgs e) { var task = RunAction("show-desktop", false); }
        void OnRefresh(object sender, RoutedEventArgs e) { var task = RunAction("diagnose", true); }
        void OnPrepare(object sender, RoutedEventArgs e) { var task = RunAction("prepare-components", false); }
        void OnRepairGateway(object sender, RoutedEventArgs e) { var task = RunAction("repair-gateway", false); }
        void OnPrepareHost(object sender, RoutedEventArgs e) { var task = RunAction("prepare-host", false); }
        void OnCopyReport(object sender, RoutedEventArgs e) { try { Clipboard.SetText(reportText); SetOperation("Отчёт скопирован."); } catch (Exception) { SetOperation("Не удалось скопировать отчёт. Попробуй ещё раз."); } }
        void OnNextAction(object sender, RoutedEventArgs e)
        {
            if (suggestedAction == "settings") Navigate(AppPage.Settings);
            else if (suggestedAction == "connection") Navigate(AppPage.Connection);
            else if (suggestedAction == "repair_gateway") { var task = RunAction("repair-gateway", false); }
            else if (suggestedAction == "recover_session") { var task = RunAction("recover-session", false); }
            else if (suggestedAction == "windows_features") { Navigate(AppPage.Diagnostics); SetOperation("Нажми «Подготовить этот ПК» в проверке. Потребуется UAC; перезагрузка выполняется вручную."); }
            else { var task = RunAction(suggestedAction == "prepare" ? "prepare-components" : "diagnose", suggestedAction != "prepare"); }
        }
        void OnShortcut(object sender, RoutedEventArgs e)
        {
            try { SetOperation("Ярлык создан: " + PortableEntry.CreateShortcut(Bridge.Root, Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory))); }
            catch (Exception error) { SetOperation(error.Message); }
        }

        // ---------- Report display ----------
        static bool ActiveVmState(string state)
        {
            return state == "starting" || state == "restarting" || state == "running" || state == "reconnecting" || state == "stopping"
                || state == "shutdown-unconfirmed" || state == "connection-failed" || state == "control-unavailable-guest-running" || state == "failed";
        }

        static string VmState(string state, bool gateway = false)
        {
            switch (state)
            {
                case "stopped": return gateway ? "выключен" : "выключена";
                case "starting": return "запускается";
                case "restarting": return "перезагружается";
                case "running": return "работает";
                case "reconnecting": return "восстанавливает связь";
                case "connection-failed": return "ошибка подключения";
                case "control-unavailable-guest-running": return "работает без управления";
                case "shutdown-unconfirmed": return "выключение не подтверждено";
                case "failed": return "ошибка сеанса";
                case "stopping": return "выключается";
                default: return "состояние неизвестно";
            }
        }

        static string RuntimeHeading(string work, string gateway)
        {
            if (ActiveVmState(work)) return work == "failed" || work == "connection-failed" ? "Сеанс Windows требует проверки" : "Windows " + VmState(work);
            if (ActiveVmState(gateway)) return gateway == "connection-failed" ? "Ошибка подключения шлюза" : gateway == "failed" ? "Сеанс шлюза требует проверки" : "Шлюз " + VmState(gateway, true);
            return "Готово к запуску";
        }

        static string DisplayState(Dictionary<string, object> report) { return Text(Child(report, "windows_display"), "status", ""); }
        static string FileSharingState(Dictionary<string, object> report) { var files = Child(report, "host_files"); return files == null ? "" : Text(files, "status", ""); }

        Dictionary<string, object> MergeRuntimeStatus(Dictionary<string, object> update)
        {
            if (Text(update, "report_kind", "") != "runtime_status") return update;
            bool hasFullCheck = readinessReport != null && Text(readinessReport, "report_kind", "preflight") == "preflight";
            var result = readinessReport == null ? new Dictionary<string, object>() : new Dictionary<string, object>(readinessReport);
            foreach (var item in update)
                if (item.Key != "checks" && item.Key != "blockers" && item.Key != "ok" && item.Key != "message" && item.Key != "checked_utc" && item.Key != "gateway_version" && item.Key != "report_kind") result[item.Key] = item.Value;
            var checks = Child(result, "checks") != null ? new Dictionary<string, object>(Child(result, "checks")) : new Dictionary<string, object>();
            var liveChecks = Child(update, "checks");
            if (liveChecks != null) foreach (var item in liveChecks) checks[item.Key] = item.Value;
            if (!hasFullCheck) checks["full_check"] = new Dictionary<string, object> { { "passed", false }, { "title", "Нужна проверка комплекта" },
                { "detail", "Нажми «Проверить снова» перед запуском Windows." }, { "action", "retry" }, { "action_label", "Проверить комплект" } };
            var blockers = new List<Dictionary<string, object>>();
            foreach (var item in checks)
            {
                var check = item.Value as Dictionary<string, object>;
                if (check == null || Flag(check, "passed")) continue;
                var blocker = new Dictionary<string, object>(check); blocker["code"] = item.Key; blockers.Add(blocker);
            }
            result["checks"] = checks; result["blockers"] = blockers.ToArray(); result["ok"] = blockers.Count == 0;
            result["message"] = blockers.Count == 0 ? Bridge.Message(update) : Text(blockers[0], "title", "Нужна проверка");
            result["report_kind"] = hasFullCheck ? "preflight" : "runtime_status";
            var capabilities = Child(result, "capabilities");
            if (capabilities != null)
            {
                capabilities = new Dictionary<string, object>(capabilities);
                capabilities["test_profile"] = hasFullCheck && Flag(Child(checks, "gateway_package"), "passed") && Flag(Child(checks, "runtime_state"), "passed") && !Flag(capabilities, "stop");
                result["capabilities"] = capabilities;
            }
            return result;
        }

        static List<Dictionary<string, object>> Checks(Dictionary<string, object> result)
        {
            var checks = new List<Dictionary<string, object>>();
            var source = Child(result, "checks");
            if (source != null)
                foreach (var item in source)
                {
                    var check = item.Value as Dictionary<string, object>; if (check == null) continue;
                    var row = new Dictionary<string, object>(check); if (!row.ContainsKey("title")) row["title"] = item.Key; checks.Add(row);
                }
            return checks.OrderBy(item => Flag(item, "passed")).ToList();
        }

        void Display(Dictionary<string, object> result)
        {
            result = MergeRuntimeStatus(result);
            bool isReadiness = result.ContainsKey("checks");
            if (isReadiness)
            {
                readinessReport = result;
                if (!settingsLoaded && result.ContainsKey("settings")) LoadSettings(result);
                if (result.ContainsKey("host")) { settingsHost = Child(result, "host"); UpdateHostHints(); }
                ready = Bridge.Ok(result);
                if (pendingOutcome != null && pendingOutcome.IsCompleted) { outcomeUnknown = false; pendingOutcome = null; }
                var startup = Child(result, "startup");
                if (pendingStartReceipt != null && startup != null && Text(startup, "receipt", "") == pendingStartReceipt
                    && (Text(startup, "phase", "") == "ready" || (!Flag(startup, "supervisor_active") && (Text(startup, "phase", "") == "stopped" || Text(startup, "phase", "") == "error"))))
                { outcomeUnknown = false; pendingStartReceipt = null; }
                var capabilities = Child(result, "capabilities");
                stopAvailable = Flag(capabilities, "stop"); testAvailable = Flag(capabilities, "test_profile");
                var runtime = Child(result, "runtime");
                workState = Text(runtime, "work", "unknown"); gatewayState = Text(runtime, "gateway", "unknown");
                runtimeActive = ActiveVmState(workState) || ActiveVmState(gatewayState);
                var connection = Child(result, "connection");
                var selected = Child(connection, "selected_profile");
                if (selected == null) { ConnectionValue.Text = "Добавить сервер"; ConnectionDetail.Text = "Вставь ссылку от своего VPS"; }
                else
                {
                    string protocol = Text(selected, "protocol", ""); protocol = protocol == "hysteria2" ? "Hysteria2" : protocol.ToUpperInvariant();
                    ConnectionValue.Text = Text(selected, "name", "Профиль");
                    ConnectionDetail.Text = protocol + " · " + (Flag(selected, "needs_credentials") ? "нужно ввести пароль заново" : Flag(connection, "verified") ? "подключено и проверено" : "проверится при запуске");
                }
            }
            else
            {
                operationResult = result;
                if (Flag(result, "outcome_unknown")) TrackUnknownOutcome(result);
            }
            var report = readinessReport ?? result;
            var checks = Checks(report);
            int failed = checks.Count(item => !Flag(item, "passed"));
            DiagnosticSummary.Text = checks.Count == 0 ? "Проверка ПК и комплекта" : failed == 0 ? "Всё в порядке" : "Требует внимания: " + failed;
            DiagnosticGlyph.Symbol = checks.Count > 0 && failed > 0 ? SymbolRegular.ErrorCircle24 : SymbolRegular.Checkmark24;
            DiagnosticBadge.Background = checks.Count == 0 ? Grey : failed == 0 ? Green : Orange;

            Dictionary<string, object> blocker = null;
            if (!Bridge.Ok(result))
            {
                var blockers = result.ContainsKey("blockers") ? result["blockers"] as IEnumerable : null;
                if (blockers != null) blocker = blockers.Cast<object>().OfType<Dictionary<string, object>>().FirstOrDefault();
                if (blocker == null && isReadiness) blocker = checks.FirstOrDefault(item => !Flag(item, "passed"));
            }
            StatusText.Text = blocker == null ? Friendly(Bridge.Message(result)) : Text(blocker, "title", Bridge.Message(result));
            if (blocker == null && !Bridge.Ok(result) && Friendly(Bridge.Message(result)) != Bridge.Message(result)) StatusText.Text = "Windows не запустилась";
            if (isReadiness && Bridge.Ok(result)) StatusText.Text = RuntimeHeading(workState, gatewayState);
            NextStepText.Text = blocker == null ? (Bridge.Ok(result) ? "Сначала поднимется защищённое подключение, затем Windows." : Friendly(Bridge.Message(result))) : Text(blocker, "detail", Bridge.Message(result));
            if (isReadiness && Bridge.Ok(result) && runtimeActive) NextStepText.Text = "Windows: " + VmState(workState) + ". Шлюз: " + VmState(gatewayState, true) + ".";
            suggestedAction = blocker == null ? "retry" : Text(blocker, "action", "retry");
            NextActionButton.Content = blocker == null ? "Проверить снова" : Text(blocker, "action_label", "Проверить снова");
            Show(NextActionButton, !Bridge.Ok(result));

            string displayState = DisplayState(report);
            if (Child(report, "windows_settings") != null && (displayState == "failed" || displayState == "unconfirmed") && blocker == null)
                NextStepText.Text = displayState == "failed" ? "Windows не применила выбранное разрешение. Выбери другое после выключения." : "Windows ещё не подтвердила разрешение.";
            string filesState = FileSharingState(report);
            if (blocker == null && (filesState == "failed" || filesState == "unavailable")) NextStepText.Text = "Проверь доступность выбранных папок ПК. Windows подключит их снова при следующем входе.";
            UpdateHero(report, filesState);
            string warning = runtimeActive ? Text(report, "network_warning", "") : "";
            NetworkText.Text = warning; Show(NetworkText, warning.Length > 0);

            string signature = new JavaScriptSerializer().Serialize(checks.Select(item => new object[] { Flag(item, "passed"), Text(item, "title", ""), Text(item, "detail", "") }).ToArray()) + Bridge.Message(result);
            if (signature != displayedChecks)
            {
                displayedChecks = signature; CheckCards.Children.Clear();
                foreach (var item in checks) AddCheckRow(Flag(item, "passed"), Text(item, "title", "Проверка"), Text(item, "detail", ""));
                if (checks.Count == 0) CheckCards.Children.Add(new TextBlock { Text = Bridge.Message(result), Style = (Style)FindResource("Caption"), Margin = new Thickness(16, 6, 16, 10) });
            }
            var lines = checks.Select(item => (Flag(item, "passed") ? "Готово: " : "Нужно исправить: ") + Text(item, "title", "Проверка") + Environment.NewLine + Text(item, "detail", "")).ToList();
            if (checks.Count == 0) lines.Add(Bridge.Message(result));
            if (!isReadiness && !Bridge.Ok(result)) lines.Insert(0, Bridge.Message(result));
            if (warning.Length > 0) lines.Add(warning);
            string reportPath = Text(report, "diagnostic_report", "");
            if (reportPath.Length > 0) lines.Add("Отчёт: " + reportPath);
            ReportLocation.Text = reportPath.Length > 0 ? "Отчёт: " + reportPath : Text(report, "diagnostic_report_error", "");
            reportText = String.Join(Environment.NewLine + Environment.NewLine, lines);
            Busy(busy);
            SettingsNote.Foreground = Grey;
            SettingsNote.Text = runtimeActive ? "Настройки можно менять, когда Windows выключена." : "Изменения сохраняются сами и применятся при следующем запуске.";
        }

        void AddCheckRow(bool passed, string title, string detail)
        {
            if (CheckCards.Children.Count > 0) CheckCards.Children.Add(new Border { Style = (Style)FindResource("Separator") });
            var row = new DockPanel { Margin = new Thickness(16, 10, 16, 10) };
            var dot = new Border { Width = 24, Height = 24, CornerRadius = new CornerRadius(12), Background = passed ? GreenSoft : Hex("#FFF1DE"), Margin = new Thickness(3, 1, 16, 0), VerticalAlignment = VerticalAlignment.Top,
                Child = new SymbolIcon { Symbol = passed ? SymbolRegular.Checkmark16 : SymbolRegular.Warning16, FontSize = 13, Foreground = passed ? GreenInk : Hex("#B36200") } };
            DockPanel.SetDock(dot, Dock.Left); row.Children.Add(dot);
            row.Children.Add(new StackPanel { Children = {
                new TextBlock { Text = title, FontSize = 14, FontWeight = passed ? FontWeights.Normal : FontWeights.SemiBold, Foreground = Ink },
                new TextBlock { Text = detail, Style = (Style)FindResource("Caption"), Margin = new Thickness(0, 1, 0, 0) } } });
            CheckCards.Children.Add(row);
        }

        void UpdateHero(Dictionary<string, object> report, string filesState)
        {
            bool running = workState == "running", starting = ActiveVmState(workState) && !running;
            bool attention = !ready && readinessReport != null && !runtimeActive;
            StatusIcon.Background = running ? GreenSoft : attention ? Hex("#FFF1DE") : BlueSoft;
            StatusGlyph.Foreground = running ? Green : attention ? Orange : Blue;
            Pulse.Fill = running ? Green : Blue;
            bool[] done = { gatewayState == "running", Flag(report, "network_verified"), running, filesState == "ready" };
            bool[] active = { ActiveVmState(gatewayState) && gatewayState != "running", gatewayState == "running" && !done[1], starting, running && filesState != "ready" && filesState != "off" && filesState.Length > 0 };
            for (int index = 0; index < 4; index++)
            {
                stepDots[index].Fill = done[index] ? Green : active[index] ? Blue : Hex("#C7C7CC");
                stepTexts[index].Foreground = done[index] ? GreenInk : active[index] ? BlueInk : Grey;
                stepCards[index].Background = done[index] ? GreenSoft : active[index] ? BlueSoft : Brushes.White;
                stepTexts[index].Text = new[] { "Шлюз", "VPN", "Windows", "Файлы" }[index] + (index == 3 && filesState == "off" ? " — выкл." : "");
            }
            Show(Steps, runtimeActive || (busy && activeOperationAction == "start"));
        }

        static string FormatGiB(int mib) { return ((decimal)mib / 1024).ToString("0.######", CultureInfo.CurrentCulture); }

        // ---------- Lifecycle, tray, polling ----------
        void StartLifecycle()
        {
            var menu = new Forms.ContextMenuStrip();
            var open = new Forms.ToolStripMenuItem("Открыть панель"); open.Click += delegate { ShowPanel(false); };
            trayStop = new Forms.ToolStripMenuItem("Выключить Windows");
            trayStop.Click += async delegate { ShowPanel(false); if (stopAvailable) await RunAction("stop", false); };
            var quit = new Forms.ToolStripMenuItem("Закрыть панель (Windows продолжит работу)");
            quit.Click += delegate { reallyClose = true; Close(); };
            menu.Items.Add(open); menu.Items.Add(trayStop); menu.Items.Add(quit);
            trayIcon = new Forms.NotifyIcon { Icon = System.Drawing.Icon.ExtractAssociatedIcon(System.Reflection.Assembly.GetExecutingAssembly().Location), Text = "Windows Claude", ContextMenuStrip = menu };
            trayIcon.DoubleClick += delegate { ShowPanel(false); };
            lifecycleTimer.Tick += async delegate { await RefreshRuntimeAsync(); };
            lifecycleTimer.Start(); UpdateTray();
        }

        void UpdateTray()
        {
            if (trayIcon == null || closing) return;
            trayIcon.Visible = runtimeActive || panelHiddenForSession;
            trayStop.Enabled = stopAvailable && !busy && !outcomeUnknown;
        }

        void HidePanelForSession() { panelHiddenForSession = true; UpdateTray(); Hide(); }
        void ShowPanelAutomatically() { if (!panelDismissedByUser) ShowPanel(true); }
        void ShowPanel(bool home)
        {
            bool wasHidden = !IsVisible;
            panelDismissedByUser = false; panelHiddenForSession = false;
            if (home && wasHidden) Navigate(AppPage.Home);
            Show(); if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
            Activate(); UpdateTray();
        }

        void OnClosing(object sender, CancelEventArgs e)
        {
            if (!reallyClose && (runtimeActive || outcomeUnknown || (busy && activeOperationAction == "start")))
            { e.Cancel = true; panelDismissedByUser = true; HidePanelForSession(); return; }
            closing = true; lifecycleTimer.Stop(); saveTimer.Stop();
            if (trayIcon != null) { trayIcon.Visible = false; trayIcon.Dispose(); trayIcon = null; }
        }

        async Task RefreshRuntimeAsync()
        {
            if (closing || busy || runtimePolling || (!runtimeActive && !panelHiddenForSession && !outcomeUnknown)) return;
            runtimePolling = true; int revision = operationRevision;
            try
            {
                var report = await Run("status", null);
                if (closing || busy || revision != operationRevision) return;
                if (report == null || !report.ContainsKey("checks"))
                {
                    ShowPanelAutomatically(); StatusText.Text = "Состояние Windows не подтверждено"; NextStepText.Text = Bridge.Message(report);
                    CheckedAtText.Text = "Состояние не обновилось. Повтори проверку."; return;
                }
                report = MergeRuntimeStatus(report);
                var state = Child(report, "runtime");
                bool changed = readinessReport == null || state == null || pendingStartReceipt != null
                    || workState != Text(state, "work", "unknown") || gatewayState != Text(state, "gateway", "unknown")
                    || Bridge.Ok(report) != Bridge.Ok(readinessReport) || Flag(report, "network_verified") != Flag(readinessReport, "network_verified")
                    || DisplayState(report) != DisplayState(readinessReport) || FileSharingState(report) != FileSharingState(readinessReport)
                    || Text(report, "message", "") != Text(readinessReport, "message", "") || (pendingOutcome != null && pendingOutcome.IsCompleted);
                if (changed) { Display(report); Busy(false); }
                bool workActive = workState == "running" || workState == "starting" || workState == "restarting" || workState == "stopping";
                if (panelHiddenForSession && (!workActive || !Bridge.Ok(report) || outcomeUnknown || DisplayState(report) == "failed" || DisplayState(report) == "unconfirmed")) ShowPanelAutomatically();
                UpdateTray();
            }
            catch (Exception)
            {
                if (!closing && !busy && revision == operationRevision) { ShowPanelAutomatically(); SetOperation("Не удалось обновить состояние. Повтори проверку."); }
            }
            finally { runtimePolling = false; }
        }

        // ---------- Settings: saved automatically ----------
        void LoadSettings(Dictionary<string, object> reply)
        {
            originalSettings = new Dictionary<string, object>(Child(reply, "settings"));
            originalWindows = Child(reply, "windows_settings");
            originalViewer = Child(reply, "viewer_settings");
            originalFiles = Child(reply, "file_settings");
            if (reply.ContainsKey("host")) settingsHost = Child(reply, "host");
            settingsLoaded = true; ResetSettings(); UpdateHostHints(); Busy(busy);
        }

        void ResetSettings()
        {
            applyingSettings = true;
            try
            {
                int memory; Int32.TryParse(Text(originalSettings, "work_memory_mib", "4096"), out memory);
                MemoryInput.Text = FormatGiB(memory); CpuInput.Text = Text(originalSettings, "vcpus", "2");
                int selected = DefaultMode;
                if (originalWindows != null && !Flag(originalWindows, "fit"))
                    for (int index = 0; index < Modes.GetLength(0); index++)
                        if (Text(originalWindows, "width", "") == Modes[index, 0].ToString() && Text(originalWindows, "height", "") == Modes[index, 1].ToString()) selected = index;
                ResolutionBox.SelectedIndex = selected;
                FullscreenSwitch.IsChecked = Flag(originalViewer, "start_fullscreen");
                RememberWindowSwitch.IsChecked = originalViewer == null || !originalViewer.ContainsKey("remember_window") || Flag(originalViewer, "remember_window");
                string mode = Text(originalFiles, "mode", "off");
                folders.Clear();
                var saved = originalFiles != null && originalFiles.ContainsKey("folders") ? originalFiles["folders"] as IEnumerable : null;
                if (saved != null) foreach (var folder in saved) folders.Add(Convert.ToString(folder));
                FilesSwitch.IsChecked = mode != "off"; FilesAll.IsChecked = mode != "selected"; FilesSelected.IsChecked = mode == "selected";
                UpdateFilesRows();
            }
            finally { applyingSettings = false; }
        }

        void UpdateHostHints()
        {
            int total; Int32.TryParse(Text(settingsHost, "ram_total_mib", "0"), out total);
            MemoryHint.Text = total > 0 ? "В этом ПК " + ((decimal)total / 1024).ToString("0", CultureInfo.CurrentCulture) + " ГиБ" : "От 1 до 32";
            string cpus = Text(settingsHost, "logical_processors", "");
            CpuHint.Text = cpus.Length > 0 ? "В этом ПК " + cpus : "От 1 до 32";
        }

        void UpdateFilesRows()
        {
            bool on = FilesSwitch.IsChecked == true;
            Show(FoldersRow, on); Show(FolderPanel, FilesSelected.IsChecked == true); Show(AddFolderButton, FilesSelected.IsChecked == true);
            FilesDetail.Text = !on ? "Выключено" : FilesSelected.IsChecked == true ? (folders.Count == 0 ? "Добавь папку" : "Папок: " + folders.Count) : "Все диски ПК";
            FolderPanel.Children.Clear();
            foreach (string folder in folders)
            {
                var row = new DockPanel { Margin = new Thickness(0, 2, 0, 2) };
                string path = folder;
                var remove = new System.Windows.Controls.Button { Style = (Style)FindResource("TextAction"), Content = "Убрать", Foreground = Red };
                remove.Click += delegate { folders.Remove(path); UpdateFilesRows(); ScheduleSave(); };
                DockPanel.SetDock(remove, Dock.Right); row.Children.Add(remove);
                row.Children.Add(new TextBlock { Text = path, FontSize = 13.5, Foreground = Ink, VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis });
                FolderPanel.Children.Add(row);
            }
        }

        void ScheduleSave() { if (applyingSettings || !settingsLoaded) return; saveTimer.Stop(); saveTimer.Start(); }
        void CommitPendingSettings() { if (saveTimer.IsEnabled) { saveTimer.Stop(); var task = SaveSettingsAsync(); } }
        void OnSettingChanged(object sender, RoutedEventArgs e) { ScheduleSave(); }
        void OnSettingEdited(object sender, KeyboardFocusChangedEventArgs e) { ScheduleSave(); }
        void OnNumberKey(object sender, KeyEventArgs e) { if (e.Key == Key.Enter) { Keyboard.ClearFocus(); ScheduleSave(); } }
        void OnFilesSwitch(object sender, RoutedEventArgs e)
        {
            if (FilesSwitch.IsChecked == true && FilesSelected.IsChecked != true) FilesAll.IsChecked = true;
            UpdateFilesRows(); ScheduleSave();
        }
        void OnFilesMode(object sender, RoutedEventArgs e) { UpdateFilesRows(); if (FilesSelected.IsChecked != true || folders.Count > 0) ScheduleSave(); }
        void OnAddFolder(object sender, RoutedEventArgs e)
        {
            using (var dialog = new Forms.FolderBrowserDialog { Description = "Папка ПК, которая будет видна в Windows" })
                if (dialog.ShowDialog() == Forms.DialogResult.OK && !folders.Any(item => String.Equals(item, dialog.SelectedPath, StringComparison.OrdinalIgnoreCase)))
                { folders.Add(dialog.SelectedPath); UpdateFilesRows(); ScheduleSave(); }
        }

        internal static bool TryGiB(string text, out decimal gib)
        {
            return Decimal.TryParse(text.Trim().Replace(',', '.'), NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out gib);
        }
        void StepMemory(decimal delta)
        {
            decimal gib; if (!TryGiB(MemoryInput.Text, out gib)) gib = 4;
            gib = Math.Min(32, Math.Max(1, Math.Round((gib + delta) * 2, MidpointRounding.AwayFromZero) / 2));
            MemoryInput.Text = gib.ToString("0.#", CultureInfo.CurrentCulture); ScheduleSave();
        }
        void StepCpu(int delta)
        {
            int cpus; if (!Int32.TryParse(CpuInput.Text.Trim(), out cpus)) cpus = 2;
            CpuInput.Text = Math.Min(32, Math.Max(1, cpus + delta)).ToString(CultureInfo.InvariantCulture); ScheduleSave();
        }
        void OnMemoryDown(object sender, RoutedEventArgs e) { StepMemory(-0.5m); }
        void OnMemoryUp(object sender, RoutedEventArgs e) { StepMemory(0.5m); }
        void OnCpuDown(object sender, RoutedEventArgs e) { StepCpu(-1); }
        void OnCpuUp(object sender, RoutedEventArgs e) { StepCpu(1); }

        bool TryGetSettings(out Dictionary<string, object> value, out string error)
        {
            value = null; error = null;
            decimal gib; int original; Int32.TryParse(Text(originalSettings, "work_memory_mib", "0"), out original);
            if (!TryGiB(MemoryInput.Text, out gib) || gib < 1 || gib > 32) { error = "Память — число от 1 до 32 ГиБ, например 6 или 6,5."; return false; }
            // Unchanged text keeps an old exact MiB value such as 5000.
            int mib = MemoryInput.Text.Trim() == FormatGiB(original) ? original : (int)Math.Round(gib * 1024, MidpointRounding.AwayFromZero);
            int cpus;
            if (!Int32.TryParse(CpuInput.Text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out cpus) || cpus < 1 || cpus > 32) { error = "Процессоры — целое число от 1 до 32."; return false; }
            bool filesOn = FilesSwitch.IsChecked == true;
            string mode = !filesOn ? "off" : FilesSelected.IsChecked == true ? "selected" : "all";
            if (mode == "selected" && folders.Count == 0) { error = "Добавь хотя бы одну папку ПК."; return false; }
            int resolution = Math.Max(0, ResolutionBox.SelectedIndex);
            value = new Dictionary<string, object>(originalSettings);
            value["schema"] = 1; value["work_memory_mib"] = mib; value["vcpus"] = cpus;
            value["windows"] = new Dictionary<string, object> { { "schema", 1 }, { "width", Modes[resolution, 0] }, { "height", Modes[resolution, 1] }, { "fit", false } };
            value["viewer"] = new Dictionary<string, object> { { "schema", 1 }, { "start_fullscreen", FullscreenSwitch.IsChecked == true }, { "remember_window", RememberWindowSwitch.IsChecked == true } };
            value["files"] = new Dictionary<string, object> { { "schema", 1 }, { "mode", mode }, { "folders", folders.ToArray() } };
            return true;
        }

        async Task SaveSettingsAsync()
        {
            Dictionary<string, object> value; string error;
            if (!settingsLoaded || runtimeActive) return;
            if (!TryGetSettings(out value, out error)) { SettingsNote.Text = error; SettingsNote.Foreground = Red; return; }
            SettingsNote.Foreground = Grey;
            if (!BeginOperation("save-settings")) { saveTimer.Start(); return; }
            Dictionary<string, object> completed = null;
            try
            {
                completed = await Run("save-settings", value);
                if (closing) return;
                if (Bridge.Ok(completed))
                {
                    settingsLoaded = false; LoadSettings(completed);
                    SetOperation("");
                    var check = await Run("diagnose", null);
                    if (!closing) Display(check);
                    SettingsNote.Text = "Сохранено. Применится при следующем запуске.";
                }
                else { SettingsNote.Text = Bridge.Message(completed); SettingsNote.Foreground = Red; SetOperation(Bridge.Message(completed)); if (Flag(completed, "outcome_unknown")) TrackUnknownOutcome(completed); }
            }
            catch (Exception) { FailedAction("save-settings", completed); }
            finally { if (!closing) Busy(false); }
        }

        // ---------- Connection ----------
        async Task<Dictionary<string, object>> ProfileAction(string action, object payload)
        {
            if (action == "test-profile" && !testAvailable) return Bridge.Failure("Проверка подключения сейчас недоступна.");
            if (!BeginOperation(action)) return Bridge.Failure("Дождись завершения текущей операции.");
            Dictionary<string, object> result = null;
            try
            {
                result = await Run(action, payload);
                if (closing) return result;
                operationResult = result; SetOperation(Bridge.Message(result));
                if (!Bridge.Ok(result)) { ProfileErrorText(Bridge.Message(result)); if (Flag(result, "outcome_unknown")) TrackUnknownOutcome(result); return result; }
                ProfileErrorText("");
                if (action == "list-profiles" || action == "get-profile-view") SetOperation("");
                if (result.ContainsKey("profile")) LoadProfileView(Child(result, "profile"));
                if (action == "list-profiles") { profilesLoaded = true; LoadProfileList(result["profiles"]); }
                else if (action == "save-profile" || action == "select-profile" || action == "import-profile")
                {
                    var list = await Run("list-profiles", null);
                    if (Bridge.Ok(list)) { profilesLoaded = true; LoadProfileList(list["profiles"]); }
                    var status = await Run("status", null);
                    if (status != null && status.ContainsKey("checks")) Display(status);
                    SetOperation(Bridge.Message(result));
                }
                else if (action == "test-profile") ProfileStateText.Text = Flag(result, "verified") ? "Проверка пройдена: сервер отвечает." : "Проверка не подтверждена. Подробности — в «Проверке».";
                return result;
            }
            catch (Exception)
            {
                if (result != null && Bridge.Ok(result)) { ProfileErrorText("Изменение сохранено, но состояние не обновилось. Нажми «Проверить снова»."); return result; }
                result = action == "list-profiles" || action == "get-profile-view" ? Bridge.Failure("Не удалось загрузить подключения. Проверь компоненты и повтори.")
                    : Bridge.Unknown("Исход действия с подключением неизвестен. Сначала нажми «Проверить снова».", "profile_operation_failed");
                if (!closing) { operationResult = result; SetOperation(Bridge.Message(result)); if (Flag(result, "outcome_unknown")) TrackUnknownOutcome(result); ProfileErrorText(Bridge.Message(result)); }
                return result;
            }
            finally { if (!closing) Busy(false); }
        }

        async Task LoadProfilesAsync()
        {
            if (busy) { ProfileErrorText("Дождись завершения текущей операции и открой снова."); return; }
            await ProfileAction("list-profiles", null);
        }

        void LoadProfileList(object source)
        {
            profiles = (source as IEnumerable ?? new object[0]).Cast<object>().OfType<Dictionary<string, object>>().ToList();
            ProfileList.Children.Clear();
            foreach (var item in profiles)
            {
                if (ProfileList.Children.Count > 0) ProfileList.Children.Add(new Border { Style = (Style)FindResource("Separator"), Margin = new Thickness(16, 0, 0, 0) });
                string id = Text(item, "profile_id", ""), protocol = Text(item, "protocol", "");
                protocol = protocol == "hysteria2" ? "Hysteria2" : protocol.ToUpperInvariant();
                bool selected = Flag(item, "selected");
                var row = new DockPanel();
                var edit = new System.Windows.Controls.Button { Style = (Style)FindResource("TextAction"), Content = "Изменить", VerticalAlignment = VerticalAlignment.Center };
                edit.Click += async delegate { var view = await ProfileAction("get-profile-view", new Dictionary<string, object> { { "profile_id", id } }); if (Bridge.Ok(view)) ShowEditor("Изменить подключение"); };
                DockPanel.SetDock(edit, Dock.Right); row.Children.Add(edit);
                var check = new SymbolIcon { Symbol = SymbolRegular.Checkmark24, FontSize = 18, Foreground = Blue, Width = 22, Margin = new Thickness(0, 0, 12, 0), Visibility = selected ? Visibility.Visible : Visibility.Hidden };
                DockPanel.SetDock(check, Dock.Left); row.Children.Add(check);
                row.Children.Add(new StackPanel { VerticalAlignment = VerticalAlignment.Center, Children = {
                    new TextBlock { Text = Text(item, "name", "Профиль"), Style = (Style)FindResource("RowText"), TextTrimming = TextTrimming.CharacterEllipsis },
                    new TextBlock { Text = protocol + " · " + Text(item, "host", "") + (Flag(item, "needs_credentials") ? " · нужен пароль" : ""), Style = (Style)FindResource("Caption"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap } } });
                var button = new System.Windows.Controls.Button { Style = (Style)FindResource("RowNoScale"), Content = row, ToolTip = selected ? "Используется для запуска" : "Использовать для запуска" };
                button.Click += async delegate { if (!selected) await ProfileAction("select-profile", new Dictionary<string, object> { { "profile_id", id } }); };
                ProfileList.Children.Add(button);
            }
            Show(ProfilesTitle, profiles.Count > 0); Show(ProfilesGroup, profiles.Count > 0);
            Busy(busy);
        }

        void LoadProfileView(Dictionary<string, object> view)
        {
            if (view == null) return;
            profileId = Text(view, "profile_id", null);
            loadedProtocol = Text(view, "protocol", "hysteria2"); loadedObfs = Text(view, "obfs", "");
            authPresent = Flag(view, "auth_present") && !Flag(view, "needs_credentials");
            ProfileName.Text = Text(view, "name", ""); ProfileHost.Text = Text(view, "host", ""); ProfilePort.Text = Text(view, "port", "");
            ProfileSni.Text = Text(view, "tls_server_name", "");
            ProtocolBox.SelectedIndex = Math.Max(0, Array.IndexOf(Protocols, loadedProtocol));
            ObfsSwitch.IsChecked = loadedObfs == "salamander";
            ProxyAuthSwitch.IsChecked = loadedProtocol != "hysteria2" && Flag(view, "auth_present");
            ProfileAuth.Password = ProfileObfsPassword.Password = ProfilePassword.Password = ""; ProfileUser.Text = "";
            AuthCaption.Text = authPresent ? "Пароль (сохранён — оставь пустым, чтобы не менять)" : "Пароль";
            EditorErrorText(""); UpdateProfileRows(); Busy(busy);
        }

        void NewProfile()
        {
            profileId = null; loadedProtocol = null; loadedObfs = null; authPresent = false;
            ProfileName.Text = ProfileHost.Text = ProfileSni.Text = ProfileUser.Text = ""; ProfilePort.Text = "443";
            ProfileAuth.Password = ProfileObfsPassword.Password = ProfilePassword.Password = "";
            AuthCaption.Text = "Пароль";
            ProtocolBox.SelectedIndex = 0; ObfsSwitch.IsChecked = false; ProxyAuthSwitch.IsChecked = false;
            EditorErrorText(""); UpdateProfileRows(); Busy(busy);
        }

        void OnManualProfile(object sender, RoutedEventArgs e) { NewProfile(); ShowEditor("Новое подключение"); ProfileHost.Focus(); }
        void OnProtocolChanged(object sender, SelectionChangedEventArgs e) { UpdateProfileRows(); }
        void OnProfileToggle(object sender, RoutedEventArgs e) { UpdateProfileRows(); }
        void UpdateProfileRows()
        {
            if (HysteriaRows == null) return;
            bool hy = ProtocolBox.SelectedIndex == 0;
            Show(HysteriaRows, hy); Show(ProxyRows, !hy); Show(ObfsRow, ObfsSwitch.IsChecked == true);
            Show(ProxyAuthRow, ProxyAuthSwitch.IsChecked == true); Show(SniRow, hy || ProtocolBox.SelectedIndex == 3);
        }
        void ProfileErrorText(string text) { ProfileError.Text = text ?? ""; Show(ProfileError, !String.IsNullOrEmpty(text)); EditorErrorText(text); }
        void EditorErrorText(string text) { EditorError.Text = text ?? ""; Show(EditorError, !String.IsNullOrEmpty(text)); }

        bool CanReuseCredentials
        {
            get { return profileId != null && authPresent && Protocols[Math.Max(0, ProtocolBox.SelectedIndex)] == loadedProtocol && (ObfsSwitch.IsChecked == true ? "salamander" : "") == (loadedObfs ?? ""); }
        }

        bool TryProfileRequest(out Dictionary<string, object> request)
        {
            request = null; int port;
            if (String.IsNullOrWhiteSpace(ProfileHost.Text)) { EditorErrorText("Укажи адрес сервера."); return false; }
            if (!Int32.TryParse(ProfilePort.Text, NumberStyles.None, CultureInfo.InvariantCulture, out port) || port < 1 || port > 65535) { EditorErrorText("Порт — целое число от 1 до 65535."); return false; }
            int kind = Math.Max(0, ProtocolBox.SelectedIndex); bool hy = kind == 0;
            string name = String.IsNullOrWhiteSpace(ProfileName.Text) ? ProfileHost.Text.Trim() : ProfileName.Text.Trim();
            var profile = new Dictionary<string, object> { { "name", name }, { "protocol", Protocols[kind] }, { "host", ProfileHost.Text.Trim() }, { "port", port } };
            if ((hy || kind == 3) && !String.IsNullOrWhiteSpace(ProfileSni.Text)) profile["tls_server_name"] = ProfileSni.Text.Trim();
            bool obfs = ObfsSwitch.IsChecked == true;
            if (hy && obfs) profile["obfs"] = "salamander";
            if (hy)
            {
                if (ProfileAuth.Password.Length > 0)
                {
                    var secret = new Dictionary<string, object> { { "auth", ProfileAuth.Password } };
                    if (obfs) { if (ProfileObfsPassword.Password.Length == 0) { EditorErrorText("Введи пароль маскировки."); return false; } secret["obfs_password"] = ProfileObfsPassword.Password; }
                    profile["credentials"] = secret;
                }
                else if (ProfileObfsPassword.Password.Length > 0) { EditorErrorText("Для смены маскировки введи и основной пароль."); return false; }
                else if (!CanReuseCredentials) { EditorErrorText("Введи пароль. После смены типа или маскировки пароль нужен заново."); return false; }
            }
            else if (ProxyAuthSwitch.IsChecked != true) profile["credentials"] = new Dictionary<string, object>();
            else if (ProfileUser.Text.Length > 0 || ProfilePassword.Password.Length > 0)
            {
                if (ProfileUser.Text.Length == 0 || ProfilePassword.Password.Length == 0) { EditorErrorText("Нужны и логин, и пароль."); return false; }
                profile["credentials"] = new Dictionary<string, object> { { "username", ProfileUser.Text }, { "password", ProfilePassword.Password } };
            }
            else if (!CanReuseCredentials) { EditorErrorText("Введи логин и пароль. После смены типа пароль нужен заново."); return false; }
            request = new Dictionary<string, object> { { "profile", profile } };
            if (profileId != null) request["profile_id"] = profileId;
            EditorErrorText(""); return true;
        }

        // A saved or imported profile becomes the one used for the next start: that is what "add" means here.
        async Task UseProfile(Dictionary<string, object> result)
        {
            var saved = Child(result, "profile");
            if (!Bridge.Ok(result) || saved == null) return;
            if (!Flag(saved, "selected")) result = await ProfileAction("select-profile", new Dictionary<string, object> { { "profile_id", Text(saved, "profile_id", "") } });
            if (Bridge.Ok(result)) { LinkInput.Text = ""; OpenSheet(AppPage.Connection); SetOperation("Готово: «" + Text(saved, "name", "подключение") + "» будет использовано при запуске."); }
        }

        async void OnSaveProfile(object sender, RoutedEventArgs e) { Dictionary<string, object> request; if (TryProfileRequest(out request)) await UseProfile(await ProfileAction("save-profile", request)); }
        async void OnTestProfile(object sender, RoutedEventArgs e)
        {
            var selected = profiles.FirstOrDefault(item => Flag(item, "selected"));
            if (selected != null) await ProfileAction("test-profile", new Dictionary<string, object> { { "profile_id", Text(selected, "profile_id", "") } });
        }
        void OnLinkKey(object sender, KeyEventArgs e) { if (e.Key == Key.Enter) OnAddLink(sender, e); }
        async void OnAddLink(object sender, RoutedEventArgs e)
        {
            string link = LinkInput.Text.Trim();
            if (link.Length == 0) { ProfileErrorText("Вставь ссылку вида hysteria2://пароль@адрес:порт"); LinkInput.Focus(); return; }
            await UseProfile(await ProfileAction("import-profile", new Dictionary<string, object> { { "uri", link } }));
        }

        async void OnImportFile(object sender, RoutedEventArgs e)
        {
            var file = new Microsoft.Win32.OpenFileDialog { Filter = "JSON профиль (*.json)|*.json", CheckFileExists = true };
            if (file.ShowDialog(this) != true) return;
            Dictionary<string, object> profile;
            try
            {
                if (new FileInfo(file.FileName).Length > 65536) { ProfileErrorText("Файл больше 64 КиБ."); return; }
                profile = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(file.FileName));
                if (profile == null) { ProfileErrorText("Нужен объект профиля JSON."); return; }
            }
            catch (Exception) { ProfileErrorText("Не удалось прочитать профиль JSON. Проверь формат и доступ к файлу."); return; }
            if (Text(profile, "protocol", "") == "hysteria2" && String.IsNullOrEmpty(Text(Child(profile, "credentials"), "auth", "")))
            {
                NewProfile(); ProfileName.Text = Text(profile, "name", ""); ProfileHost.Text = Text(profile, "host", ""); ProfilePort.Text = Text(profile, "port", "443");
                ProfileSni.Text = Text(profile, "tls_server_name", ""); ObfsSwitch.IsChecked = Text(profile, "obfs", "") == "salamander"; UpdateProfileRows();
                ShowEditor("Введи пароль"); ProfileAuth.Focus(); return;
            }
            await UseProfile(await ProfileAction("import-profile", new Dictionary<string, object> { { "profile", profile } }));
        }

        async void OnExportProfile(object sender, RoutedEventArgs e)
        {
            var selected = profiles.FirstOrDefault(item => Flag(item, "selected"));
            string id = selected != null ? Text(selected, "profile_id", null) : profileId;
            if (id == null) { ProfileErrorText("Сначала добавь подключение."); return; }
            var secrets = new CheckBox { Content = "Включить пароли", Margin = new Thickness(0, 0, 0, 10) };
            var body = new StackPanel { Width = 400 };
            body.Children.Add(secrets);
            body.Children.Add(new TextBlock { Text = "Без галочки сохраняются только настройки. С галочкой пароли попадут в файл открытым текстом — передавай его только себе или доверенному человеку.", Style = (Style)FindResource("Caption") });
            var dialog = new Wpf.Ui.Controls.MessageBox { Title = "Экспорт подключения", Content = body, PrimaryButtonText = "Выбрать файл", CloseButtonText = "Отмена", Owner = this };
            if (await dialog.ShowDialogAsync() != Wpf.Ui.Controls.MessageBoxResult.Primary) return;
            var file = new Microsoft.Win32.SaveFileDialog { Title = "Экспорт подключения в новый файл", Filter = "JSON профиль (*.json)|*.json", DefaultExt = "json", AddExtension = true, OverwritePrompt = false, FileName = "connection.json" };
            if (file.ShowDialog(this) == true)
                await ProfileAction("export-profile", new Dictionary<string, object> { { "profile_id", id }, { "destination", file.FileName }, { "include_secrets", secrets.IsChecked == true } });
        }
    }
}
