// Backend protocol, timeouts and safety rules of the launcher.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

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
    // Mirrors scripts/runtime_config.PYTHON; tests/test_update_audit_fixes.py keeps them equal.
    internal const string PythonDir = "runtime\\python-3.12.14";
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
            case "start": return 600000;
            case "diagnose":
            case "collect-report": return 300000;
            case "test-profile": return 330000;
            case "stop": return 510000;
            case "repair-gateway":
            case "force-stop":
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
            ProcessStartInfo prepare = new ProcessStartInfo(powershell, String.Join(" ", new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, "-Root", root }.Select(Quote)));
            prepare.WorkingDirectory = root;
            Dictionary<string,object> reply = await RunProcessAsync(prepare, TimeoutMilliseconds(action)).ConfigureAwait(false);
            string code = reply.ContainsKey("code") ? Convert.ToString(reply["code"]) : "error";
            if (code == "outcome_unknown") { reply["outcome_unknown"] = true; reply["message"] = "Исход подготовки ПК неизвестен. Изменение компонентов может продолжаться. Не повторяй действие до проверки состояния."; }
            else if (code == "restart_required") { reply["restart_required"] = true; reply["message"] = "Для завершения подготовки перезагрузи ПК самостоятельно."; }
            else if (code == "uac_cancelled") reply["message"] = "Запрос администратора отменён. Подготовка ПК не выполнена.";
            else if (code == "ready") reply["message"] = "Этот ПК готов к аппаратному ускорению виртуальных машин.";
            return reply;
        }
        string python = Path.Combine(root, PythonDir, "python.exe");
        string backend = Path.Combine(root, "scripts", "portable_app.py");
        Dictionary<string, object> problem = KitProblem(root);
        if (problem != null) return problem;
        string[] args = { "-X", "utf8", "-I", "-S", "-B", backend, "--root", root, "--action", action };
        ProcessStartInfo info = new ProcessStartInfo(python, String.Join(" ", args.Select(Quote)));
        info.WorkingDirectory = root;
        return await ExecuteProcessAsync(info, TimeoutMilliseconds(action), payload).ConfigureAwait(false);
    }

    // Python can only explain problems once it runs, so a lone EXE, a partial unpack or a locked file is told apart here.
    internal static Dictionary<string, object> KitProblem(string root)
    {
        if (!Directory.Exists(Path.Combine(root, "runtime")) && !Directory.Exists(Path.Combine(root, "scripts")))
            return Blocked("kit_detached", "Launcher открыт отдельно от папки Pocket Windows",
                "Launcher.exe работает только внутри распакованной папки Pocket Windows. Открой эту папку и запусти Launcher.exe оттуда. Для Рабочего стола создай ярлык, а не переноси сам файл.",
                "retry", "Проверить снова");
        bool recovery = CanRestoreComponents(root);
        foreach (string file in new[] { Path.Combine(root, PythonDir, "python.exe"), Path.Combine(root, "scripts", "portable_app.py") })
        {
            string name = file.Substring(root.Length).TrimStart('\\');
            Exception denied = null;
            try { using (File.OpenRead(file)) { } continue; }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            catch (UnauthorizedAccessException error) { denied = error; }
            catch (IOException error) { denied = error; }
            if (denied != null)
                return Blocked("kit_access", "Нет доступа к файлам программы",
                    "Windows не дала открыть " + name + " (" + denied.GetType().Name + "). Проверь права на папку и повтори.", "retry", "Проверить снова");
            if (file.EndsWith("python.exe", StringComparison.OrdinalIgnoreCase) && recovery)
                return Blocked("runtime_files", "Не хватает Python и QEMU",
                    "Их можно вернуть из резервной копии внутри комплекта. Диск Windows и подключения не меняются.", "prepare", "Восстановить Python и QEMU");
            return Blocked("application_files", "Комплект распакован не полностью",
                "Не найден " + name + ". Распакуй полный ZIP Pocket Windows заново, в новую папку.", "retry", "Проверить снова");
        }
        return null;
    }

    const long MaxEventsBytes = 4 * 1024 * 1024;  // mirrors scripts/runtime_logs.MAX_EVENTS_BYTES
    static readonly object EventLock = new object();

    // One line in logs/events.jsonl, the same history the Python side writes. Never throws.
    internal static void Event(string code, Dictionary<string, object> fields)
    {
        try
        {
            var record = new Dictionary<string, object> {
                { "utc", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fff+00:00") },
                { "mono_ms", Environment.TickCount & int.MaxValue }, { "pid", Process.GetCurrentProcess().Id },
                { "component", "launcher" }, { "code", code } };
            if (fields != null) foreach (var item in fields) if (item.Value != null) record[item.Key] = item.Value;
            byte[] line = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(record) + "\n");
            string folder = Path.Combine(Root, "logs"), path = Path.Combine(folder, "events.jsonl");
            lock (EventLock)
            {
                Directory.CreateDirectory(folder);
                var info = new FileInfo(path);
                if (info.Exists && info.Length + line.Length > MaxEventsBytes)
                {
                    // Another writer may hold the file; a short overrun is fine, unbounded growth is not.
                    // Measure again: another process may have rotated already, and its fresh file must not replace the history.
                    try { string rotated = Path.Combine(folder, "events.1.jsonl"); if (new FileInfo(path).Length + line.Length > MaxEventsBytes) { if (File.Exists(rotated)) File.Delete(rotated); File.Move(path, rotated); } }
                    catch (IOException) { if (info.Length > 2 * MaxEventsBytes) return; }
                    catch (UnauthorizedAccessException) { if (info.Length > 2 * MaxEventsBytes) return; }
                }
                using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
                    stream.Write(line, 0, line.Length);
            }
        }
        catch (Exception) { }
    }

    internal static void Event(string code, string detail)
    {
        Event(code, detail == null ? null : new Dictionary<string, object> { { "detail", detail } });
    }

    // Restoring needs both the script and its archive; offer it only when it can actually run.
    internal static bool CanRestoreComponents(string root)
    {
        return File.Exists(Path.Combine(root, "scripts", "Prepare-Dependencies.ps1")) && File.Exists(Path.Combine(root, "runtime", "components-recovery.zip"));
    }

    static Dictionary<string, object> Blocked(string code, string title, string detail, string action, string label)
    {
        Dictionary<string, object> failure = Failure(title);
        failure["code"] = code;
        failure["blockers"] = new object[] { new Dictionary<string, object> {
            { "code", code }, { "title", title }, { "detail", detail }, { "action", action }, { "action_label", label } } };
        return failure;
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
        ProcessStartInfo info = new ProcessStartInfo(powershell, String.Join(" ", new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, "-Project", root }.Select(Quote)));
        info.WorkingDirectory = root;
        Dictionary<string, object> reply = await RunProcessAsync(info, 180000).ConfigureAwait(false);
        string code = reply.ContainsKey("code") ? Convert.ToString(reply["code"]) : "error";
        Dictionary<string, string> messages = new Dictionary<string, string> {
            { "ready", "Компоненты готовы. Файлы Python и QEMU проверены." },
            { "repaired", "Компоненты восстановлены. Файлов: " + (reply.ContainsKey("repaired_files") ? Convert.ToString(reply["repaired_files"]) : "несколько") + "." },
            { "runtime_busy", "Компоненты заняты работающей Windows. Выключи Windows, затем повтори." },
            { "package_missing", "В комплекте нет резервной копии компонентов. Распакуй полный ZIP Pocket Windows заново." },
            { "invalid_package", "Пакет восстановления повреждён или не соответствует комплекту. Распакуй полный проверенный комплект приложения." },
            { "error", "Подготовка не завершена. Проверь свободное место и доступ к папке. Если ошибка сохранится, распакуй полный комплект." }
        };
        if (messages.ContainsKey(code)) reply["message"] = messages[code];
        else if (code == "invalid_response" || code == "process_failed")
            reply["message"] = "Windows PowerShell не подтвердил подготовку. Проверь целостность комплекта и ограничения Windows. Постоянная политика выполнения не изменялась; исход подготовки не подтверждён.";
        return reply;
    }

}
