using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopProfileIntegration
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static void Pump(Task task){Stopwatch clock=Stopwatch.StartNew();while(!task.IsCompleted&&clock.ElapsedMilliseconds<15000){Application.DoEvents();Thread.Sleep(5);}Assert(task.IsCompleted,"Real profile backend timed out");task.GetAwaiter().GetResult();}
    static void Context(){SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());}
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            string root=args[0];Assert(!Directory.Exists(Path.Combine(root,"images"))&&!Directory.Exists(Path.Combine(root,"private")),"Use only a disposable QA root without guest disks");
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            using(MainForm form=new MainForm(false,(action,payload)=>Bridge.RunAsync(root,action,payload)))
            {
                Context();Pump(form.NavigateAsync(AppPage.Connection));
                form.ConnectionPage.Fields["name"].Text="Тестовое подключение";form.ConnectionPage.Fields["host"].Text="example.com";form.ConnectionPage.Fields["auth"].Text="profile-fixture-only-secret";
                Context();Task<Dictionary<string,object>> save=form.SaveProfileAsync();Pump(save);Assert(Bridge.Ok(save.Result),"Real save must succeed: "+Bridge.Message(save.Result));
                string id=form.ConnectionPage.ProfileId;Assert(!String.IsNullOrEmpty(id)&&form.ConnectionPage.Fields["auth"].Text=="","Public reply must identify profile and clear secret input");
                form.ConnectionPage.Fields["host"].Text="updated.example.com";Context();save=form.SaveProfileAsync();Pump(save);Assert(Bridge.Ok(save.Result),"Metadata editing must preserve omitted saved credentials");
                Context();Task<Dictionary<string,object>> choose=form.ProfileActionAsync("select-profile",new Dictionary<string,object>{{"profile_id",id}});Pump(choose);
                Dictionary<string,object> view=(Dictionary<string,object>)choose.Result["profile"];
                Assert((bool)view["selected"]&&!(bool)view["verified"]&&Convert.ToString(view["state"])=="pending"&&view["applied_revision_id"]==null,"Selecting must report pending, not connected");
                string publicPath=Path.Combine(root,"public-profile.json");Context();Task<Dictionary<string,object>> export=form.ExportProfileAsync(publicPath,false);Pump(export);
                Assert(Bridge.Ok(export.Result)&&!File.ReadAllText(publicPath).Contains("profile-fixture-only-secret"),"Public export must omit secret");
                string privatePath=Path.Combine(root,"explicit-private-profile.json");Context();export=form.ExportProfileAsync(privatePath,true);Pump(export);
                Assert(Bridge.Ok(export.Result)&&File.ReadAllText(privatePath).Contains("profile-fixture-only-secret"),"Only explicit secret export may contain the fixture password");
                Context();Task<Dictionary<string,object>> imported=form.ImportProfileAsync("hy2://uri-fixture-only-secret@example.net:443");Pump(imported);
                Assert(Bridge.Ok(imported.Result)&&form.ConnectionPage.ProfileId!=id&&form.ConnectionPage.Fields["auth"].Text=="","URI import must validate/store and return only a safe public view");
                Assert(!File.ReadAllText(Path.Combine(root,"profiles","profiles.json")).Contains("profile-fixture-only-secret"),"Persistent profile metadata must not contain plaintext secret");
                Assert(!form.ConnectionPage.TestButton.Enabled,"Unimplemented network testing must not be enabled");
            }
            Console.WriteLine("PASS: native UI to real temporary Python profile backend; DPAPI save; preserve credentials; pending selection; URI import; safe and explicit secret export; no VM or network actions.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
