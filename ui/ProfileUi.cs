using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed partial class MainForm
{
    internal async Task<Dictionary<string,object>> ProfileActionAsync(string action,object payload)
    {
        if (action == "test-profile" && !testAvailable) return Bridge.Failure("Проверка подключения пока недоступна.");
        if (!BeginOperation(action)) return Bridge.Failure("Дождись завершения текущей операции.");
        Dictionary<string,object> result = null;
        try
        {
            result = await run(action,payload);
            if (!CanUpdate) return result;
            operationResult = result; operation.Text = Bridge.Message(result);
            if (!Bridge.Ok(result))
            {
                ConnectionPage.Error.Text = Bridge.Message(result);
                if (Flag(result,"outcome_unknown")) TrackUnknownOutcome(result);
                return result;
            }
            ConnectionPage.Error.Text = "";
            if (action == "list-profiles" || action == "get-profile-view") operation.Text = "";
            if (result.ContainsKey("profile")) ConnectionPage.LoadView((Dictionary<string,object>)result["profile"]);
            if (action == "list-profiles")
            {
                profilesLoaded = true;
                ConnectionPage.LoadProfiles(result.ContainsKey("profiles") ? result["profiles"] : null);
                string id = ConnectionPage.PickedId;
                if (id != null)
                {
                    Dictionary<string,object> view = await run("get-profile-view",new Dictionary<string,object>{{"profile_id",id}});
                    if (!CanUpdate) return result;
                    if (Bridge.Ok(view)) ConnectionPage.LoadView((Dictionary<string,object>)view["profile"]);
                    else ConnectionPage.Error.Text = Bridge.Message(view);
                }
            }
            else if (action == "save-profile" || action == "select-profile" || action == "import-profile")
            {
                await RefreshProfileMutationAsync(result);
            }
            else if (action == "test-profile")
                ConnectionPage.State.Text = Flag(result,"verified") ? "Проверка подключения пройдена." : "Проверка подключения не подтверждена. Посмотри диагностику.";
            return result;
        }
        catch (Exception)
        {
            if (result != null && Bridge.Ok(result) && (action == "save-profile" || action == "select-profile" || action == "import-profile"))
            {
                if (CanUpdate) { operationResult = result; operation.Text = Bridge.Message(result); ConnectionPage.Error.Text = "Изменение сохранено, но состояние не обновилось. Повтори диагностику и открой подключение заново."; }
                return result;
            }
            else if (action == "list-profiles" || action == "get-profile-view")
                result = Bridge.Failure("Не удалось загрузить подключения. Проверь компоненты и повтори.");
            else
                result = Bridge.Unknown("Исход действия с подключением неизвестен. Сначала выполни диагностику и проверь список профилей.","profile_operation_failed");
            if (CanUpdate) { operationResult = result; operation.Text = Bridge.Message(result); if (Flag(result,"outcome_unknown")) TrackUnknownOutcome(result); ConnectionPage.Error.Text = Bridge.Message(result); }
            return result;
        }
        finally { if (CanUpdate) Busy(false); }
    }

    private async Task RefreshProfileMutationAsync(Dictionary<string,object> completed)
    {
        string warning = "";
        try
        {
            Dictionary<string,object> list = await run("list-profiles",null);
            if (!CanUpdate) return;
            if (Bridge.Ok(list)) ConnectionPage.LoadProfiles(list["profiles"]);
            else warning = "Изменение сохранено, но список не обновился. Открой подключение заново.";
        }
        catch (Exception) { warning = "Изменение сохранено, но список не обновился. Открой подключение заново."; }
        if (!CanUpdate) return;
        try
        {
            Dictionary<string,object> report = await run("diagnose",null);
            if (!CanUpdate) return;
            if (report != null && report.ContainsKey("checks")) Display(report);
            else warning += " Изменение сохранено, но диагностика не обновилась. Повтори проверку состояния.";
        }
        catch (Exception) { warning += " Изменение сохранено, но диагностика не обновилась. Повтори проверку состояния."; }
        if (!CanUpdate) return;
        operationResult = completed;
        operation.Text = Bridge.Message(completed);
        ConnectionPage.Error.Text = warning.Trim();
    }

    internal async Task<Dictionary<string,object>> SaveProfileAsync()
    {
        Dictionary<string,object> request;
        if (!ConnectionPage.TrySaveRequest(out request)) return Bridge.Failure(ConnectionPage.Error.Text);
        return await ProfileActionAsync("save-profile",request);
    }

    internal Task<Dictionary<string,object>> ImportProfileAsync(string uri)
    {
        return ProfileActionAsync("import-profile",new Dictionary<string,object>{{"uri",uri}});
    }

    internal Task<Dictionary<string,object>> ImportProfileAsync(Dictionary<string,object> profile)
    {
        if (!CanUpdate || busy) return Task.FromResult(Bridge.Failure("Дождись завершения текущей операции."));
        Dictionary<string,object> credentials = profile.ContainsKey("credentials") ? profile["credentials"] as Dictionary<string,object> : null;
        if (TextValue(profile,"protocol","") == "hysteria2" && (credentials == null || String.IsNullOrEmpty(TextValue(credentials,"auth",""))))
        {
            ConnectionPage.LoadImported(profile);
            return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"message","Профиль заполнен в редакторе. Введи пароль и сохрани."}});
        }
        return ProfileActionAsync("import-profile",new Dictionary<string,object>{{"profile",profile}});
    }

    internal Task<Dictionary<string,object>> ExportProfileAsync(string destination,bool includeSecrets = false)
    {
        if (ConnectionPage.ProfileId == null) return Task.FromResult(Bridge.Failure("Сначала выбери сохранённый профиль."));
        return ProfileActionAsync("export-profile",new Dictionary<string,object>{{"profile_id",ConnectionPage.ProfileId},{"destination",destination},{"include_secrets",includeSecrets}});
    }

    private async Task ImportProfileDialogAsync()
    {
        using (ProfileImportDialog dialog = new ProfileImportDialog())
        {
            if (dialog.ShowDialog(this) != DialogResult.OK || !CanUpdate) return;
            if (dialog.JsonProfile != null) await ImportProfileAsync(dialog.JsonProfile);
            else await ImportProfileAsync(dialog.UriText);
        }
    }

    private async Task ExportProfileDialogAsync()
    {
        using (ProfileExportDialog options = new ProfileExportDialog())
        {
            if (options.ShowDialog(this) != DialogResult.OK || !CanUpdate) return;
            using (SaveFileDialog destination = new SaveFileDialog { Title = "Экспорт подключения в новый файл", Filter = "JSON профиль (*.json)|*.json", DefaultExt = "json", AddExtension = true, OverwritePrompt = false, FileName = "connection.json" })
                if (destination.ShowDialog(this) == DialogResult.OK && CanUpdate) await ExportProfileAsync(destination.FileName,options.IncludeSecrets);
        }
    }
}

internal sealed class ProfileImportDialog : Form
{
    private readonly TextBox uri = new TextBox { Dock = DockStyle.Fill, UseSystemPasswordChar = true, AccessibleName = "Ссылка подключения" };
    private readonly Label error = new Label { AutoSize = true, ForeColor = Color.Firebrick };
    internal Dictionary<string,object> JsonProfile;
    internal string UriText { get { return uri.Text; } }
    internal ProfileImportDialog()
    {
        Text = "Импорт подключения"; Font = new Font("Segoe UI",10); BackColor = NativeStyle.Background; ClientSize = new Size(560,230);
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = MinimizeBox = false; StartPosition = FormStartPosition.CenterParent;
        TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), ColumnCount = 1, RowCount = 4 };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        root.Controls.Add(new Label { Text = "Вставь ссылку Hysteria2, SOCKS5, HTTP или HTTPS. Она может содержать пароль и поэтому скрыта.", AutoSize = true, Dock = DockStyle.Fill },0,0);
        root.Controls.Add(uri,0,1); root.Controls.Add(error,0,2);
        FlowLayoutPanel actions = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = false };
        Button import = new RoundedButton { Text = "Импортировать", Primary = true, AutoSize = true, Padding = new Padding(14,9,14,9) };
        Button json = new RoundedButton { Text = "Открыть JSON", AutoSize = true, Padding = new Padding(14,9,14,9) };
        Button cancel = new RoundedButton { Text = "Отмена", DialogResult = DialogResult.Cancel, AutoSize = true, Padding = new Padding(14,9,14,9) };
        import.Click += delegate { if (!String.IsNullOrWhiteSpace(uri.Text)) DialogResult = DialogResult.OK; else error.Text = "Вставь ссылку или открой файл JSON."; };
        json.Click += delegate {
            using (OpenFileDialog file = new OpenFileDialog { Filter = "JSON профиль (*.json)|*.json", CheckFileExists = true })
            {
                if (file.ShowDialog(this) != DialogResult.OK) return;
                try
                {
                    if (new FileInfo(file.FileName).Length > 65536) { error.Text = "Файл больше 64 КиБ."; return; }
                    JsonProfile = new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(file.FileName));
                    if (JsonProfile == null) { error.Text = "Нужен объект профиля JSON."; return; }
                    DialogResult = DialogResult.OK;
                }
                catch (Exception) { error.Text = "Не удалось прочитать профиль JSON. Проверь формат и доступ к файлу."; }
            }
        };
        actions.Controls.Add(import);actions.Controls.Add(json);actions.Controls.Add(cancel);root.Controls.Add(actions,0,3);Controls.Add(root);AcceptButton = import;CancelButton = cancel;
    }
}

internal sealed class ProfileExportDialog : Form
{
    private readonly CheckBox secrets = new CheckBox { Text = "Включить пароли", AutoSize = true };
    internal bool IncludeSecrets { get { return secrets.Checked; } }
    internal ProfileExportDialog()
    {
        Text = "Экспорт подключения"; Font = new Font("Segoe UI",10); BackColor = NativeStyle.Background; ClientSize = new Size(510,220);
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = MinimizeBox = false; StartPosition = FormStartPosition.CenterParent;
        TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), ColumnCount = 1, RowCount = 3 };
        root.Controls.Add(secrets,0,0);
        root.Controls.Add(new Label { Text = "По умолчанию сохраняются только настройки. Если включить пароли, они попадут в JSON открытым текстом. Передавай такой файл только доверенному получателю.", AutoSize = true, Dock = DockStyle.Fill, ForeColor = NativeStyle.Muted },0,1);
        FlowLayoutPanel actions = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.RightToLeft };
        Button save = new RoundedButton { Text = "Выбрать файл", Primary = true, AutoSize = true, Padding = new Padding(14,9,14,9), DialogResult = DialogResult.OK };
        Button cancel = new RoundedButton { Text = "Отмена", AutoSize = true, Padding = new Padding(14,9,14,9), DialogResult = DialogResult.Cancel };
        actions.Controls.Add(save);actions.Controls.Add(cancel);root.Controls.Add(actions,0,2);Controls.Add(root);AcceptButton = save;CancelButton = cancel;
    }
}
