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
    static void ListCases()
    {
        int lists=0,gets=0;var current=View("Первый VPN");var other=View("Другой VPN");other["profile_id"]="profile2";other["selected"]=false;
        using(MainForm form=new MainForm(false,(action,payload)=> {
            if(action=="list-profiles"){lists++;return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{current,other}}});}
            if(action=="get-profile-view"){gets++;return Task.FromResult(Bridge.Failure("fixture unavailable"));}
            return Task.FromResult(Bridge.Failure("unexpected action"));
        }))
        {
            IntPtr handle=form.Handle;Context();Pump(form.ProfileActionAsync("list-profiles",null));
            Assert(lists==1&&gets==0,"Listing complete public views must not fetch the selected profile again");
            Assert(form.ConnectionPage.ProfileId=="profile1"&&form.ConnectionPage.Fields["name"].Text=="Первый VPN","First list must open its selected public view");
            form.ConnectionPage.Fields["name"].Text="Несохранённый черновик";Pump(form.ProfileActionAsync("list-profiles",null));
            Assert(form.ConnectionPage.Fields["name"].Text=="Несохранённый черновик","Refreshing the list must preserve an edited saved profile");
            form.ConnectionPage.Profiles.SelectedIndex=1;
            Assert(gets==1&&form.ConnectionPage.PickedId=="profile1"&&form.ConnectionPage.Fields["name"].Text=="Несохранённый черновик","A failed profile preview must restore selection and preserve the loaded draft");
            form.ConnectionPage.NewProfile();form.ConnectionPage.Fields["host"].Text="draft.example.com";Pump(form.ProfileActionAsync("list-profiles",null));
            Assert(form.ConnectionPage.ProfileId==null&&form.ConnectionPage.Fields["host"].Text=="draft.example.com","Refreshing the list must preserve a new unsaved profile");
            Assert(form.ConnectionPage.PickedId==null,"A list refresh must not select a saved entry over an unsaved draft");
        }
    }
    static void RefreshCases()
    {
        foreach(string mutation in new[]{"save-profile","select-profile","import-profile"})
        {
            int writes=0;var updated=View("Обновлённый VPN");var saved=new Dictionary<string,object>{{"ok",true},{"message","Изменение сохранено"},{"profile",updated}};
            var refresh=new TaskCompletionSource<Dictionary<string,object>>();
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{updated}}});
                if(action=="status")return refresh.Task;
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
        foreach(string failure in new[]{"list-throws","status-throws","status-fails"})
        {
            var saved=new Dictionary<string,object>{{"ok",true},{"message","Сохранено"},{"profile",View("Новый VPN")}};
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if((action=="list-profiles"&&failure=="list-throws")||(action=="status"&&failure=="status-throws"))throw new InvalidOperationException("fixture refresh failure");
                if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{View("Новый VPN")}}});
                if(action=="status")return Task.FromResult(Bridge.Failure("Состояние недоступно"));
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
    static int Main(){try{Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);ListCases();RefreshCases();Console.WriteLine("PASS: list uses public views and preserves drafts; profile mutation refreshes Home/readiness within single-flight; refresh errors preserve committed result.");return 0;}catch(Exception error){Console.Error.WriteLine(error);return 1;}}
}
