using System;
using System.Collections.Generic;
using System.Collections;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopUiRegression
{
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    static Dictionary<string, object> FailureReport()
    {
        return new Dictionary<string, object> {
            { "ok", false }, { "message", "Запуск заблокирован: откройте диагностику." },
            { "settings", new Dictionary<string, object> {{"work_memory_mib",3072},{"gateway_memory_mib",768},{"vcpus",2},{"lan_port",32861}} },
            { "checks", new Dictionary<string, object> {
                { "runtime_files", new Dictionary<string, object> {{"passed",true},{"title","Компоненты приложения"},{"detail","Python и QEMU доступны."}} },
                { "memory", new Dictionary<string, object> {{"passed",false},{"title","Недостаточно свободной памяти"},{"detail","Свободно 4153 МиБ; требуется 4864 МиБ. Закройте тяжёлые приложения или уменьшите RAM Windows."},{"action","settings"},{"action_label","Изменить настройки"}} }
            } },
            { "network_warning", "Проверка VPN относится к исходному комплекту. Сеть этой копии ещё не проверена." }
        };
    }
    static void Pump(Task task)
    {
        for (int count=0; !task.IsCompleted && count<500; count++) { Application.DoEvents(); Thread.Sleep(10); }
        Assert(task.IsCompleted,"Async UI action timed out"); task.GetAwaiter().GetResult();
    }
    static void LayoutTree(Control control)
    {
        typeof(Control).GetMethod("CreateControl", BindingFlags.Instance | BindingFlags.NonPublic,
            null, new[]{typeof(bool)}, null).Invoke(control, new object[]{true});
        control.PerformLayout();
    }
    static bool SaveDialog(SettingsDialog dialog)
    {
        MethodInfo save = typeof(SettingsDialog).GetMethod("TrySave", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert(save != null, "Settings must validate entered values before closing");
        return (bool)save.Invoke(dialog, null);
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Dictionary<string,object> host = Bridge.Run(args[1], "prepare-host", null);
            Assert(Bridge.Ok(host) && (bool)host["restart_required"] && Convert.ToString(host["checked_root"]) == args[1],
                "Host preparation bridge must work without Python and preserve Unicode root arguments");
            Dictionary<string,object> repaired = Bridge.Run(args[1], "prepare-components", null);
            Assert(Bridge.Ok(repaired) && Convert.ToInt32(repaired["repaired_files"]) == 1,
                "Native bridge must restore dependencies without Python: " + Bridge.Message(repaired));
            Assert(Bridge.Message(repaired).Contains("восстановлены"), "Preparation result must be readable in Russian");
            Dictionary<string,object> verified = Bridge.Run(args[1], "prepare-components", null);
            Assert(Bridge.Ok(verified) && Convert.ToInt32(verified["repaired_files"]) == 0,
                "Repeated preparation must keep valid files");
            using (MainForm form = new MainForm(false, (action,payload) => action == "save-settings" ? Bridge.Failure("Недопустимый lan_port: 80") : FailureReport()))
            {
                LayoutTree(form);
                form.DisplayReport(FailureReport());
                Button overview = (Button)typeof(MainForm).GetField("overviewButton", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
                Assert(overview.AccessibilityObject.Role == AccessibleRole.PageTab &&
                    (overview.AccessibilityObject.State & AccessibleStates.Selected) != 0,
                    "Screen readers must identify the selected Overview tab");
                Assert(form.StatusText.Contains("памяти"),"Home must expose memory blocker without diagnostics");
                Assert(!form.StartButton.Enabled && form.PrepareButton.Enabled,"Missing prerequisites must not disable preparation");
                Assert(form.ResourceText.Contains("3072") && form.ResourceText.Contains("Процессоры каждой VM: 2") && !form.ResourceText.Contains("VPN") && !form.ResourceText.Contains("768"),"Home must show Windows memory and CPU per VM without gateway memory");
                Directory.CreateDirectory(args[0]);
                LayoutTree(form);
                using (Bitmap image = new Bitmap(form.Width,form.Height)) { form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size)); image.Save(Path.Combine(args[0],"ui-redesign-home.png")); }
                Dictionary<string,object> ready = FailureReport();
                ready["ok"] = true;
                ready["message"] = "Всё готово к запуску Windows.";
                ((Dictionary<string,object>)((Dictionary<string,object>)ready["checks"])["memory"])["passed"] = true;
                form.DisplayReport(ready);
                Assert(form.StartButton.Enabled, "Ready state must enable the primary action");
                LayoutTree(form);
                using (Bitmap image = new Bitmap(form.Width,form.Height)) { form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size)); image.Save(Path.Combine(args[0],"ui-redesign-ready.png")); }
                form.DisplayReport(FailureReport());
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Task diagnose = form.RunAction("diagnose", true);
                Assert(form.DiagnosticsSelected,"Diagnostics must become visible immediately");
                Assert(form.DiagnosticsButton.Enabled && !form.PrepareButton.Enabled,"Long-running action must keep navigation available while disabling duplicate mutations");
                Pump(diagnose);
                Assert(form.DiagnosticsButton.AccessibilityObject.Role == AccessibleRole.PageTab &&
                    (form.DiagnosticsButton.AccessibilityObject.State & AccessibleStates.Selected) != 0 &&
                    (overview.AccessibilityObject.State & AccessibleStates.Selected) == 0,
                    "Screen readers must follow selection when Diagnostics opens");
                Assert(form.DiagnosticsText.Contains("4153") && form.DiagnosticsText.IndexOf("4153") < form.DiagnosticsText.IndexOf("Python"),"Failed checks must be first");
                Assert(form.CheckedAtText.Contains("Проверено"),"Completed diagnostics need timestamp");
                LayoutTree(form);
                using (Bitmap image = new Bitmap(form.Width,form.Height)) { form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size)); image.Save(Path.Combine(args[0],"ui-redesign-diagnostics.png")); }
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
                Assert(form.StatusText.Contains("lan_port"),"Settings-save error must remain visible");
                Assert(form.DiagnosticsText.Contains("Проверка VPN"),"Historical VPN warning must survive action-only failures");
                form.DisplayReport(Bridge.Failure("Не найден Python. Нажми «Восстановить Python и QEMU»."));
                Assert(form.PrepareButton.Enabled && !form.StartButton.Enabled,"Preparation must work with missing Python");
                form.Size=form.MinimumSize; LayoutTree(form);
                foreach (Button button in new[]{form.SettingsButton,form.DiagnosticsButton,form.PrepareButton})
                    Assert(button.Parent.ClientRectangle.Contains(button.Bounds),"Primary control clipped at minimum size: "+button.Text+" bounds="+button.Bounds+" parent="+button.Parent.ClientRectangle);
                FlowLayoutPanel rows = (FlowLayoutPanel)typeof(MainForm).GetField("checkRows", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form);
                Assert(!rows.HorizontalScroll.Visible,"Diagnostics must wrap at minimum width without horizontal scrolling: viewport=" + rows.ClientSize + "; content=" + rows.DisplayRectangle + "; child=" + rows.Controls[0].Bounds);
                using (Bitmap image = new Bitmap(form.Width,form.Height)) { form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size)); image.Save(Path.Combine(args[0],"ui-redesign-minimum.png")); }
            }
            using (MainForm form = new MainForm(false, (action,payload) => {
                if (action == "prepare-components") return Bridge.Failure("Пакет восстановления отсутствует: components-recovery.zip");
                throw new InvalidOperationException("Broken Python must not overwrite preparation failure");
            }))
            {
                LayoutTree(form);
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Pump(form.RunAction("prepare-components",false));
                Assert(form.StatusText.Contains("components-recovery.zip"),"Diagnosis must not erase preparation error");
            }
            using (SettingsDialog dialog = new SettingsDialog((Dictionary<string, object>)FailureReport()["settings"]))
            {
                LayoutTree(dialog);
                Assert(Convert.ToInt32(dialog.Value["work_memory_mib"]) == 3072, "Styled settings dialog must retain model values");
                using (Bitmap image = new Bitmap(dialog.Width, dialog.Height))
                {
                    dialog.DrawToBitmap(image, new Rectangle(Point.Empty, dialog.Size));
                    image.Save(Path.Combine(args[0], "ui-redesign-settings.png"));
                }
            }
            string[,] invalidInputs = {
                {"work_memory_mib", ""}, {"work_memory_mib", "abc"},
                {"work_memory_mib", "1"}, {"work_memory_mib", "32769"},
                {"work_memory_mib", "2147483648"},
                {"vcpus", "0"}, {"vcpus", "33"}
            };
            for (int index = 0; index < invalidInputs.GetLength(0); index++)
            {
                Dictionary<string,object> original = (Dictionary<string, object>)FailureReport()["settings"];
                using (SettingsDialog dialog = new SettingsDialog(original))
                {
                    LayoutTree(dialog);
                    IDictionary inputs = (IDictionary)typeof(SettingsDialog).GetField("fields", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(dialog);
                    Control input = (Control)inputs[invalidInputs[index,0]];
                    input.Text = invalidInputs[index,1];
                    Assert(!SaveDialog(dialog) && dialog.DialogResult != DialogResult.OK,
                        "Invalid settings must keep the dialog open: " + invalidInputs[index,0] + "=" + invalidInputs[index,1]);
                    Label error = (Label)typeof(SettingsDialog).GetField("validationError", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(dialog);
                    Assert(!String.IsNullOrEmpty(error.Text), "Invalid settings need a visible explanation");
                    input.Text = Convert.ToString(original[invalidInputs[index,0]]);
                    Assert(SaveDialog(dialog) && dialog.DialogResult == DialogResult.OK,
                        "Correcting a field must allow saving");
                    Assert(Convert.ToInt32(dialog.Value[invalidInputs[index,0]]) == Convert.ToInt32(original[invalidInputs[index,0]]),
                        "Save must return the entered integer");
                    Assert(Convert.ToInt32(original["work_memory_mib"]) == 3072, "Editing must not mutate the original settings");
                }
            }
            using (SettingsDialog dialog = new SettingsDialog((Dictionary<string, object>)FailureReport()["settings"]))
            {
                IDictionary inputs = (IDictionary)typeof(SettingsDialog).GetField("fields", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(dialog);
                string[] keys = {"work_memory_mib", "vcpus"};
                int[] values = {2048, 4};
                for (int index=0; index<keys.Length; index++) ((Control)inputs[keys[index]]).Text = values[index].ToString();
                Assert(SaveDialog(dialog), "Valid changed settings must save");
                Assert((int)dialog.Value["gateway_memory_mib"] == 768 && (int)dialog.Value["lan_port"] == 32861, "Dialog save must preserve hidden settings");
                for (int index=0; index<keys.Length; index++) Assert(dialog.Value[keys[index]] is int && (int)dialog.Value[keys[index]] == values[index], "Settings contract must preserve integer values");
            }
            List<string> actions = new List<string>();
            using (MainForm form = new MainForm(false, (action,payload) => {
                actions.Add(action);
                return action == "prepare-components" ? new Dictionary<string,object> {{"ok",true},{"message","Компоненты восстановлены."}} : FailureReport();
            }))
            {
                LayoutTree(form);
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Pump(form.RunAction("prepare-components", false));
                Assert(actions.SequenceEqual(new[]{"prepare-components","diagnose"}),"Successful preparation must refresh readiness");
                Assert(form.StatusText.Contains("памяти") && !form.StartButton.Enabled,"Preparation must preserve other startup gates");
            }
            Console.WriteLine("PASS: native recovery without Python; repeat preparation; memory summary; diagnostic navigation and order; timestamp; resource values; settings/preparation error persistence; minimum layout; offscreen rendering.");
            return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error.ToString()); return 1; }
    }
}
