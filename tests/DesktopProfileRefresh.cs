using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopProfileRefresh
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static void Context(){SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());}
    static void Pump(Task task){Stopwatch clock=Stopwatch.StartNew();while(!task.IsCompleted&&clock.ElapsedMilliseconds<8000){Application.DoEvents();Thread.Sleep(5);}Assert(task.IsCompleted,"Profile refresh timed out");task.GetAwaiter().GetResult();}
    static Dictionary<string,object> View(string name){return new Dictionary<string,object>{{"profile_id","profile1"},{"name",name},{"protocol","hysteria2"},{"host","example.com"},{"port",443},{"auth_present",true},{"needs_credentials",false},{"selected",true}};}
    static Dictionary<string,object> Report(Dictionary<string,object> view){return new Dictionary<string,object>{{"ok",true},{"checks",new Dictionary<string,object>()},{"connection",new Dictionary<string,object>{{"selected_profile",view},{"verified",false}}}};}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control nested in Tree(child))yield return nested;}
    static void RefreshCases()
    {
        foreach(string mutation in new[]{"save-profile","select-profile","import-profile"})
        {
            int writes=0;var updated=View("Обновлённый VPN");var saved=new Dictionary<string,object>{{"ok",true},{"message","Изменение сохранено"},{"profile",updated}};
            var refresh=new TaskCompletionSource<Dictionary<string,object>>();
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{updated}}});
                if(action=="diagnose")return refresh.Task;
                writes++;return Task.FromResult(saved);
            }))
            {
                IntPtr handle=form.Handle;form.DisplayReport(Report(View("Прежний VPN")));Context();
                var task=form.ProfileActionAsync(mutation,new Dictionary<string,object>());
                Assert(!task.IsCompleted,"Profile mutation must keep its single-flight gate through readiness refresh: "+mutation);
                Pump(form.ProfileActionAsync("select-profile",new Dictionary<string,object>()));Assert(writes==1,"Readiness refresh must not release the mutation gate");
                refresh.SetResult(Report(updated));Pump(task);
                Assert(Object.ReferenceEquals(task.Result,saved)&&Object.ReferenceEquals(form.OperationResult,saved),"Readiness refresh must retain the original mutation result");
                var connection=(Dictionary<string,object>)form.ReadinessReport["connection"];
                Assert(Convert.ToString(((Dictionary<string,object>)connection["selected_profile"])["name"])=="Обновлённый VPN","Readiness must use the newly selected profile");
                Assert(Tree(form).OfType<LinkLabel>().Any(link=>link.Text.Contains("Обновлённый VPN")),"Home must show the refreshed public connection");
            }
        }
        foreach(string failure in new[]{"list-throws","diagnose-throws","diagnose-fails"})
        {
            var saved=new Dictionary<string,object>{{"ok",true},{"message","Сохранено"},{"profile",View("Новый VPN")}};
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if((action=="list-profiles"&&failure=="list-throws")||(action=="diagnose"&&failure=="diagnose-throws"))throw new InvalidOperationException("fixture refresh failure");
                if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{View("Новый VPN")}}});
                if(action=="diagnose")return Task.FromResult(Bridge.Failure("Диагностика недоступна"));
                return Task.FromResult(saved);
            }))
            {
                IntPtr handle=form.Handle;var previous=Report(View("Прежний VPN"));form.DisplayReport(previous);Context();var task=form.ProfileActionAsync("save-profile",new Dictionary<string,object>());Pump(task);
                Assert(Object.ReferenceEquals(task.Result,saved)&&Object.ReferenceEquals(form.OperationResult,saved),"A refresh failure must not turn a committed save into failure: "+failure);
                Assert(Object.ReferenceEquals(form.ReadinessReport,previous),"Failed refresh must retain the last readiness evidence");
                Assert(!String.IsNullOrEmpty(form.ConnectionPage.Error.Text),"Saved-but-stale state needs an actionable refresh warning");
            }
        }
    }
    [STAThread]
    static int Main(){try{Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);RefreshCases();Console.WriteLine("PASS: profile mutation refreshes Home/readiness within single-flight; refresh errors preserve committed result.");return 0;}catch(Exception error){Console.Error.WriteLine(error);return 1;}}
}
