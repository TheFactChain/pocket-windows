using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopShellRegression
{
    static void Assert(bool value,string message) { if(!value) throw new Exception(message); }
    static IEnumerable<Control> Tree(Control parent) { foreach(Control child in parent.Controls) { yield return child; foreach(Control item in Tree(child)) yield return item; } }
    static void Pump(Task task) { DateTime end=DateTime.UtcNow.AddSeconds(10); while(!task.IsCompleted&&DateTime.UtcNow<end) { Application.DoEvents(); Thread.Sleep(5); } Assert(task.IsCompleted,"UI operation did not complete."); task.GetAwaiter().GetResult(); Application.DoEvents(); }
    static Dictionary<string,object> Settings() { return new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",768},{"vcpus",2},{"lan_port",33861}}; }
    static Rectangle Bounds(Control control,Form form) { Point point=Point.Empty; for(Control item=control;item!=form;item=item.Parent) point.Offset(item.Location); return new Rectangle(point,control.Size); }
    static void Render(MainForm form,string[] args,string name)
    {
        if(args.Length==0) return;
        Directory.CreateDirectory(args[0]);
        using(Bitmap image=new Bitmap(form.Width,form.Height)) { form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size)); image.Save(Path.Combine(args[0],name+"-hosted.png")); }
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            TaskCompletionSource<Dictionary<string,object>> pending=null;
            bool failDiagnosis=false;
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if(action=="get-settings") return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"settings",Settings()}});
                if(action=="list-profiles") return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[0]},{"message","Подключения загружены."}});
                if(action=="diagnose")
                {
                    if(failDiagnosis) throw new InvalidOperationException("Diagnosis fixture failed.");
                    return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"settings",Settings()},{"checks",new Dictionary<string,object>{{"runtime",new Dictionary<string,object>{{"passed",true},{"title","Компоненты"},{"detail","Файлы на месте."}}}}}});
                }
                if(pending!=null) return pending.Task;
                return Task.FromResult(Bridge.Failure("Проверка не выполнена."));
            }))
            {
                form.StartPosition=FormStartPosition.Manual; form.Location=new Point(-30000,-30000); form.ShowInTaskbar=false;
                form.Show(); Application.DoEvents();
                Assert(!Tree((Control)typeof(MainForm).GetField("overview",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form)).OfType<Label>().Any(label=>label.Text.Contains("Закрытие панели")),"Home should not contain the permanent close-panel footnote.");
                Label operation=(Label)typeof(MainForm).GetField("operation",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Pump(form.NavigateAsync(AppPage.Settings));
                Assert(!operation.Visible,"Loading settings must not leave a global idle status.");
                foreach(TextBox field in form.SettingsPage.Fields.Values.Where(field=>field.Visible))
                    Assert(field.Parent.Parent.Height<=field.PreferredHeight+32,"Hosted settings fields must not stretch after the Form is shown.");
                Render(form,args,"settings");
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Pump(form.NavigateAsync(AppPage.Connection));
                Assert(!operation.Visible,"Loading profiles must not leave a global idle status.");
                Render(form,args,"connection");
                form.ConnectionPage.LoadProfiles(new object[]{new Dictionary<string,object>{{"name","Тестовое подключение"},{"profile_id","fixture"},{"selected",true}}});
                Application.DoEvents(); form.PerformLayout();
                Rectangle picker=Bounds(form.ConnectionPage.ProfileList,form);
                Assert(form.ConnectionPage.ProfileList.Visible,"Saved profiles must remain selectable.");
                foreach(Button button in new[]{form.ConnectionPage.NewButton,form.ConnectionPage.ImportButton,form.ConnectionPage.ExportButton})
                    Assert(!picker.IntersectsWith(Bounds(button,form)),"Hosted profile picker overlaps toolbar: "+button.Text);
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Pump(form.RunAction("diagnose",true));
                Assert(!operation.Visible,"Successful diagnosis should not leave an idle global status.");
                failDiagnosis=true; SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Pump(form.RunAction("diagnose",true));
                Assert(operation.Visible&&operation.Text.Contains("диагност"),"A failed repeat diagnosis must be visible beside the previous check cards.");
                failDiagnosis=false;
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Pump(form.NavigateAsync(AppPage.Home));
                form.DisplayReport(new Dictionary<string,object>{{"ok",false},{"checks",new Dictionary<string,object>{{"whpx",new Dictionary<string,object>{{"passed",false},{"title","Нужна виртуализация"},{"action","windows_features"},{"action_label","Подготовить ПК"}}}}}});
                ((Button)typeof(MainForm).GetField("nextAction",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form)).PerformClick();
                Assert(operation.Visible&&operation.Text.Contains("Подготовить этот ПК"),"The host-preparation instruction must be shown after navigation.");
                pending=new TaskCompletionSource<Dictionary<string,object>>();
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext()); Task test=form.RunAction("prepare-components",false);
                Assert(operation.Visible,"An active operation must remain visible.");
                pending.SetResult(Bridge.Unknown("Исход восстановления неизвестен.","operation_failed")); Pump(test);
                Assert(operation.Visible&&!form.StartButton.Enabled,"An unknown mutation outcome must stay visible and keep launch blocked.");
            }
            Console.WriteLine("PASS: no repeated sidebar title/closure footnote; no idle loading status; active and uncertain operations retained."); return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
