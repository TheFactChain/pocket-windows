using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopAsyncRegression
{
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    static Dictionary<string,object> Report()
    {
        return new Dictionary<string,object> {{"ok",true},{"message","Готово"},{"checks",new Dictionary<string,object> {
            {"runtime",new Dictionary<string,object>{{"passed",true},{"title","Компоненты"},{"detail","Python и QEMU доступны."}}}}}};
    }
    static void Pump(Task task)
    {
        Stopwatch elapsed=Stopwatch.StartNew();
        while(!task.IsCompleted && elapsed.ElapsedMilliseconds<8000) {Application.DoEvents();Thread.Sleep(5);}
        Assert(task.IsCompleted,"UI task timed out"); task.GetAwaiter().GetResult();
    }
    static void Context() {SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());}
    static void MainFormCases()
    {
        using(ManualResetEventSlim release=new ManualResetEventSlim())
        {
            int calls=0;
            using(MainForm form=new MainForm(false,(action,payload)=> {
                Interlocked.Increment(ref calls); release.Wait(); return Report();
            }))
            {
                Context(); Task first=form.RunAction("diagnose",true);
                Task duplicate=form.SaveSettingsAndDiagnose(new Dictionary<string,object>());
                Thread.Sleep(100);
                int observed=calls;
                release.Set(); Pump(Task.WhenAll(first,duplicate));
                Assert(observed==1,"All operations must share one gate; overlapping diagnose/save called backend "+observed+" times");
            }
        }
        using(ManualResetEventSlim release=new ManualResetEventSlim())
        {
            int diagnostics=0;
            MainForm form=new MainForm(false,(action,payload)=> {
                if(action=="diagnose") {Interlocked.Increment(ref diagnostics);return Report();}
                release.Wait();return new Dictionary<string,object>{{"ok",true},{"message","Сохранено"}};
            });
            Context();Task save=form.SaveSettingsAndDiagnose(new Dictionary<string,object>());
            form.Dispose();release.Set();Pump(save);
            Assert(diagnostics==0,"Closing during save must prevent follow-up backend calls");
        }
        using(MainForm form=new MainForm(false,(action,payload)=>Bridge.Failure("Недопустимый lan_port")))
        {
            form.DisplayReport(Report());Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
            Assert(form.StatusText.Contains("lan_port"),"Concrete operation failure must remain visible");
            Assert(form.DiagnosticsText.Contains("Python"),"Operation failure must retain the last readiness checks");
        }
    }
    static void AsyncStateCases()
    {
        int calls=0;
        TaskCompletionSource<Dictionary<string,object>> pending=new TaskCompletionSource<Dictionary<string,object>>();
        using(MainForm form=new MainForm(false,(action,payload)=> {calls++;return pending.Task;}))
        {
            IntPtr handle=form.Handle;
            form.DisplayReport(Report());Context();Task action=form.RunAction("diagnose",true);
            Assert(!action.IsCompleted && !form.StartButton.Enabled,"Pending async operation must leave the UI responsive and busy");
            Assert(form.SettingsPage.Fields.Values.All(field=>!field.Enabled),"Settings fields must be read-only while an operation may reload them");
            bool heartbeat=false;form.BeginInvoke(new Action(()=>heartbeat=true));Application.DoEvents();
            Assert(heartbeat,"Native message loop must remain responsive while the backend is pending");
            Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
            Assert(calls==1,"Async bridge must share the operation gate");
            pending.SetResult(Report());Pump(action);
            Assert(form.StartButton.Enabled,"Completed readiness must restore controls");
        }
        int mutations=0;
        using(MainForm form=new MainForm(false,(action,payload)=> {
            if(action=="diagnose")return Report();
            mutations++;return Bridge.Unknown("Исход операции неизвестен", "timeout");
        }))
        {
            form.DisplayReport(Report());Context();Pump(form.RunAction("start",false));
            Assert(!form.StartButton.Enabled&&!form.PrepareButton.Enabled&&!form.SettingsButton.Enabled,
                "Unknown outcome must block duplicate mutations until diagnostics");
            Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
            Pump(form.RunAction("prepare-components",false));Assert(mutations==1,"Unknown outcome must reject repeated mutations");
            Assert(form.ReadinessReport!=null && form.OperationResult.ContainsKey("outcome_unknown"),"Operation and readiness results must remain separate");
            Context();Pump(form.RunAction("diagnose",true));Assert(!form.StartButton.Enabled,"Readiness checks alone cannot resolve an untracked unknown mutation");
        }
    }
    static void DelayedSaveCase(string root)
    {
        string marker=Path.Combine(root,"delayed-save-finished.txt");int mutations=0;
        using(MainForm form=new MainForm(false,(action,payload)=> {
            if(action=="diagnose")return Task.FromResult(Report());
            mutations++;
            if(mutations>1)return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"message","Saved"}});
            ProcessStartInfo info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--process-probe blocked "+Bridge.Quote(marker));
            info.WorkingDirectory=root;return Bridge.RunProcessAsync(info,50);
        }))
        {
            IntPtr handle=form.Handle;form.DisplayReport(Report());Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
            Assert(mutations==1&&!form.StartButton.Enabled,"Timed-out save must hold the mutation gate");
            Context();Pump(form.RunAction("diagnose",true));
            Assert(!form.StartButton.Enabled,"Diagnose must not release a still-running delayed save");
            Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));
            Assert(mutations==1,"A second save must not reach the backend while the old child is alive");
            File.WriteAllText(marker+".release","release");Stopwatch elapsed=Stopwatch.StartNew();
            while(!File.Exists(marker)&&elapsed.ElapsedMilliseconds<5000){Application.DoEvents();Thread.Sleep(10);}
            Assert(File.Exists(marker),"The old child must finish naturally");
            while(!form.StartButton.Enabled&&elapsed.ElapsedMilliseconds<5000){Context();Pump(form.RunAction("diagnose",true));Application.DoEvents();Thread.Sleep(10);}
            Assert(form.StartButton.Enabled,"Readiness refresh after that exact child exits must reconcile the gate");
            Context();Pump(form.SaveSettingsAndDiagnose(new Dictionary<string,object>()));Assert(mutations==2,"A settled old child must allow the next explicit save");
        }
    }
    static void TransportCases(string root)
    {
        MethodInfo method=typeof(Bridge).GetMethod("RunProcessAsync",BindingFlags.Static|BindingFlags.NonPublic);
        Assert(method!=null,"The asynchronous process transport is missing");
        string marker=Path.Combine(root,"timeout-child-finished.txt");
        ProcessStartInfo info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,
            "--process-probe delayed "+Bridge.Quote(marker));
        info.WorkingDirectory=root;
        Task<Dictionary<string,object>> task=(Task<Dictionary<string,object>>)method.Invoke(null,new object[]{info,50});
        Pump(task);Dictionary<string,object> result=task.Result;
        Assert(!Bridge.Ok(result)&&result.ContainsKey("outcome_unknown")&&(bool)result["outcome_unknown"],"Timeout must report an unknown outcome");
        Stopwatch elapsed=Stopwatch.StartNew();
        while(!File.Exists(marker)&&elapsed.ElapsedMilliseconds<5000)Thread.Sleep(10);
        Assert(File.Exists(marker),"Timeout must not kill the child process");
        foreach(string mode in new[]{"invalid","nonzero","empty","null","untyped"})
        {
            info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--process-probe "+mode+" "+Bridge.Quote(marker));
            info.WorkingDirectory=root;
            task=(Task<Dictionary<string,object>>)method.Invoke(null,new object[]{info,3000});Pump(task);
            Assert(!Bridge.Ok(task.Result),"Invalid process response must fail closed: "+mode);
        }
        info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--process-probe echo "+Bridge.Quote(marker));
        info.WorkingDirectory=root;
        task=(Task<Dictionary<string,object>>)method.Invoke(null,new object[]{info,3000});Pump(task);
        Assert(Bridge.Ok(task.Result),"Valid process reply must roundtrip");
        info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--process-probe pipes "+Bridge.Quote(marker));
        info.WorkingDirectory=root;task=(Task<Dictionary<string,object>>)method.Invoke(null,new object[]{info,3000});Pump(task);
        Assert(Bridge.Ok(task.Result),"Both redirected streams must drain concurrently");
    }
    [STAThread]
    static int Main(string[] args)
    {
        if(args.Length>0&&args[0]=="--process-probe")
        {
            if(args[1]=="delayed") {Thread.Sleep(350);File.WriteAllText(args[2],"finished");}
            if(args[1]=="blocked") {Stopwatch clock=Stopwatch.StartNew();while(!File.Exists(args[2]+".release")&&clock.ElapsedMilliseconds<8000)Thread.Sleep(10);File.WriteAllText(args[2],"finished");}
            if(args[1]=="pipes")Console.Error.Write(new string('x',100000));
            if(args[1]=="untyped")Console.Write("{\"ok\":\"true\"}");
            else if(args[1]=="invalid")Console.Write("not-json");
            else if(args[1]=="null")Console.Write("null");
            else if(args[1]!="empty")Console.Write("{\"ok\":true,\"message\":\"done\"}");
            return args[1]=="nonzero"?2:0;
        }
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            MainFormCases();AsyncStateCases();DelayedSaveCase(args[0]);TransportCases(args[0]);
            Console.WriteLine("PASS: single-flight; close during save; readiness preserved; async process JSON/exit handling; timeout does not terminate child.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
