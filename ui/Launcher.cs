using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Bridge
{
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
        return value.ContainsKey("ok") && value["ok"] is bool && (bool)value["ok"];
    }

    internal static string Message(Dictionary<string, object> value)
    {
        return value.ContainsKey("message") ? Convert.ToString(value["message"]) : "Готово.";
    }

    internal static Dictionary<string, object> Run(string root, string action, object payload)
    {
        string python = Path.Combine(root, "runtime", "python-3.12.14", "python.exe");
        string backend = Path.Combine(root, "scripts", "portable_app.py");
        if (!File.Exists(python) || !File.Exists(backend))
            return Failure("Не найден bundled runtime. Полностью распакуйте комплект рядом с Launcher.exe.");
        string[] args = { "-X", "utf8", "-I", "-S", "-B", backend, "--root", root, "--action", action };
        ProcessStartInfo info = new ProcessStartInfo(python, String.Join(" ", args.Select(Quote)));
        info.WorkingDirectory = root;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        using (Process process = Process.Start(info))
        {
            if (payload != null)
            {
                byte[] body = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(payload));
                process.StandardInput.BaseStream.Write(body, 0, body.Length);
                process.StandardInput.BaseStream.Flush();
            }
            process.StandardInput.Close();
            Task<string> stdout = process.StandardOutput.ReadToEndAsync();
            Task<string> stderr = process.StandardError.ReadToEndAsync();
            int timeout = action == "start" ? 220000 : 30000;
            if (!process.WaitForExit(timeout))
                return Failure("Ответ не подтверждён вовремя. Проверьте logs; VM принудительно не закрывались.");
            Task.WaitAll(stdout, stderr);
            try
            {
                return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(stdout.Result.Trim());
            }
            catch (InvalidOperationException)
            {
                return Failure("Backend не вернул корректный ответ. Проверьте целостность комплекта.");
            }
            catch (ArgumentException)
            {
                return Failure("Backend не вернул корректный ответ. Проверьте целостность комплекта.");
            }
        }
    }
}

internal sealed class SettingsDialog : Form
{
    private readonly Dictionary<string, NumericUpDown> fields = new Dictionary<string, NumericUpDown>();
    internal Dictionary<string, object> Value
    {
        get
        {
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema", 1 } };
            foreach (KeyValuePair<string, NumericUpDown> field in fields)
                result.Add(field.Key, Decimal.ToInt32(field.Value.Value));
            return result;
        }
    }

    internal SettingsDialog(Dictionary<string, object> settings)
    {
        Text = "Настройки следующего запуска";
        Font = new Font("Segoe UI", 10);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(480, 285);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        TableLayoutPanel table = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 2, RowCount = 6 };
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 65));
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 35));
        string[] keys = { "work_memory_mib", "gateway_memory_mib", "vcpus", "lan_port" };
        string[] labels = { "RAM Windows, МиБ", "RAM gateway, МиБ", "vCPU каждой VM", "Порт внутренней LAN" };
        int[] minimum = { 1024, 256, 1, 1024 };
        int[] maximum = { 32768, 4096, 32, 65535 };
        for (int index = 0; index < keys.Length; index++)
        {
            table.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
            table.Controls.Add(new Label { Text = labels[index], Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, index);
            NumericUpDown input = new NumericUpDown { Minimum = minimum[index], Maximum = maximum[index], Dock = DockStyle.Fill };
            input.Value = Convert.ToDecimal(settings[keys[index]]);
            fields.Add(keys[index], input);
            table.Controls.Add(input, 1, index);
        }
        Label note = new Label { Text = "Сначала штатно завершите VM. Сеть и защита основной Windows не меняются.", Dock = DockStyle.Fill, AutoSize = true };
        table.Controls.Add(note, 0, 4);
        table.SetColumnSpan(note, 2);
        FlowLayoutPanel actions = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft };
        Button save = new Button { Text = "Сохранить", AutoSize = true, DialogResult = DialogResult.OK };
        Button cancel = new Button { Text = "Отмена", AutoSize = true, DialogResult = DialogResult.Cancel };
        actions.Controls.Add(save);
        actions.Controls.Add(cancel);
        table.Controls.Add(actions, 0, 5);
        table.SetColumnSpan(actions, 2);
        AcceptButton = save;
        CancelButton = cancel;
        Controls.Add(table);
    }
}

internal sealed class MainForm : Form
{
    internal readonly Button StartButton = new Button { Text = "Запустить", AutoSize = true };
    internal readonly Button SettingsButton = new Button { Text = "Настройки", AutoSize = true };
    internal readonly Button DiagnosticsButton = new Button { Text = "Диагностика", AutoSize = true };
    private readonly Label status = new Label { Text = "Проверка комплекта…", AutoSize = true, Dock = DockStyle.Fill };
    private readonly TextBox details = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
    private bool ready;
    internal string StatusText { get { return status.Text; } }

    internal MainForm()
    {
        Text = "Windows Claude Portable";
        Font = new Font("Segoe UI", 10);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(660, 380);
        MinimumSize = new Size(600, 330);
        StartPosition = FormStartPosition.CenterScreen;
        TableLayoutPanel table = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(18), ColumnCount = 1, RowCount = 5 };
        table.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
        table.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        table.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
        table.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        table.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));
        table.Controls.Add(new Label { Text = "Windows Claude", Font = new Font("Segoe UI", 18, FontStyle.Bold), Dock = DockStyle.Fill }, 0, 0);
        table.Controls.Add(status, 0, 1);
        FlowLayoutPanel buttons = new FlowLayoutPanel { Dock = DockStyle.Fill };
        foreach (Button button in new[] { StartButton, SettingsButton, DiagnosticsButton }) buttons.Controls.Add(button);
        table.Controls.Add(buttons, 0, 2);
        table.Controls.Add(details, 0, 3);
        table.Controls.Add(new Label { Text = "Закрытие панели не останавливает VM. Завершайте гостевую Windows штатно.", Dock = DockStyle.Fill, AutoSize = true }, 0, 4);
        Controls.Add(table);
        StartButton.Enabled = false;
        StartButton.Click += async delegate { await Action("start"); };
        DiagnosticsButton.Click += async delegate { await Action("diagnose"); };
        SettingsButton.Click += Settings;
        Shown += async delegate { await Action("diagnose"); };
    }

    private void Busy(bool value)
    {
        StartButton.Enabled = !value && ready;
        SettingsButton.Enabled = DiagnosticsButton.Enabled = !value;
        UseWaitCursor = value;
    }

    private void Display(Dictionary<string, object> result)
    {
        status.Text = Bridge.Message(result);
        status.ForeColor = Bridge.Ok(result) ? Color.DarkGreen : Color.DarkRed;
        List<string> lines = new List<string>();
        if (result.ContainsKey("checks"))
        {
            Dictionary<string, object> checks = (Dictionary<string, object>)result["checks"];
            foreach (KeyValuePair<string, object> item in checks)
            {
                Dictionary<string, object> check = (Dictionary<string, object>)item.Value;
                lines.Add(((bool)check["passed"] ? "✓ " : "× ") + Convert.ToString(check["detail"]));
            }
            ready = Bridge.Ok(result);
        }
        else lines.Add(Bridge.Message(result));
        details.Text = String.Join(Environment.NewLine + Environment.NewLine, lines);
    }

    private async Task Action(string action)
    {
        Busy(true);
        status.Text = action == "start" ? "Запуск gateway и Windows… Подтвердите запрос UAC." : "Проверка prerequisites и файлов…";
        try
        {
            Dictionary<string, object> result = await Task.Run(() => Bridge.Run(Bridge.Root, action, null));
            if (!IsDisposed) Display(result);
        }
        catch (Exception)
        {
            if (!IsDisposed) Display(Bridge.Failure("Не удалось выполнить действие. Проверьте доступ к папке и целостность runtime."));
        }
        finally { if (!IsDisposed) Busy(false); }
    }

    private async void Settings(object sender, EventArgs args)
    {
        Busy(true);
        try
        {
            Dictionary<string, object> current = await Task.Run(() => Bridge.Run(Bridge.Root, "get-settings", null));
            if (IsDisposed) return;
            if (!Bridge.Ok(current)) { Display(current); return; }
            using (SettingsDialog dialog = new SettingsDialog((Dictionary<string, object>)current["settings"]))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                Dictionary<string, object> value = dialog.Value;
                await SaveSettingsAndDiagnose(value);
            }
        }
        catch (Exception)
        {
            if (!IsDisposed) Display(Bridge.Failure("Настройки не подтверждены. Проверьте доступ к папке."));
        }
        finally { if (!IsDisposed) Busy(false); }
    }

    internal async Task SaveSettingsAndDiagnose(Dictionary<string, object> value)
    {
        Dictionary<string, object> saved = await Task.Run(() => Bridge.Run(Bridge.Root, "save-settings", value));
        if (!IsDisposed) Display(saved);
        if (!Bridge.Ok(saved)) return;
        Dictionary<string, object> check = await Task.Run(() => Bridge.Run(Bridge.Root, "diagnose", null));
        if (!IsDisposed) Display(check);
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
            buttons = new[] { form.StartButton.Text, form.SettingsButton.Text, form.DiagnosticsButton.Text };
            checks.Add("ui-controls", !form.StartButton.Enabled && buttons.Distinct().Count() == 3);
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
