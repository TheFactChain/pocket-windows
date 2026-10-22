using System;
using System.Collections.Generic;
using System.Drawing;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopPanelLifecycleRegression
{
    static void Assert(bool value,string message) { if(!value) throw new Exception(message); }
    static void Pump(Task task) { DateTime end=DateTime.UtcNow.AddSeconds(8); while(!task.IsCompleted&&DateTime.UtcNow<end) { Application.DoEvents(); Thread.Sleep(5); } Assert(task.IsCompleted,"UI operation timed out."); task.GetAwaiter().GetResult(); Application.DoEvents(); }
    static MethodInfo Method(string name) { MethodInfo method=typeof(MainForm).GetMethod(name,BindingFlags.Instance|BindingFlags.NonPublic); Assert(method!=null,"Panel lifecycle method is missing: "+name); return method; }
    static Task Refresh(MainForm form) { return (Task)Method("RefreshRuntimeAsync").Invoke(form,null); }
    static NotifyIcon Tray(MainForm form) { return (NotifyIcon)typeof(MainForm).GetField("trayIcon",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form); }
    static void Monitor(MainForm form)
    {
        form.StartPosition=FormStartPosition.Manual; form.Location=new Point(-30000,-30000); form.ShowInTaskbar=false; form.Show();
        Method("StartLifecycleMonitoring").Invoke(form,null);
        ((System.Windows.Forms.Timer)typeof(MainForm).GetField("lifecycleTimer",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form)).Interval=60000;
        SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
    }
    static Dictionary<string,object> Report(string work,string gateway,bool ok=true)
    {
        return new Dictionary<string,object>{{"ok",ok},{"message",ok?"Состояние проверено":"Windows завершилась нештатно; шлюз сохранён"},
            {"runtime",new Dictionary<string,object>{{"work",work},{"gateway",gateway},{"control_available",true}}},
            {"network_verified",work=="running"},{"capabilities",new Dictionary<string,object>{{"stop",work!="stopped"||gateway!="stopped"},{"test_profile",false}}},
            {"checks",new Dictionary<string,object>{{"runtime",new Dictionary<string,object>{{"passed",ok},{"title",ok?"Компоненты":"Сеанс требует проверки"},{"detail","Тестовый ответ"}}}}}};
    }
    static void ManualCloseStatusCases()
    {
        foreach(string scenario in new[]{"failed","unconfirmed","failed-read","exception","stopped"})
        {
            Dictionary<string,object> current=Report("running","running");bool failRead=false;
            using(var form=new MainForm(false,(action,payload)=>{
                Assert(action=="status","Manual close must not issue a mutation");
                if(failRead)throw new InvalidOperationException("Fixture status failure");
                return Task.FromResult(current);
            }))
            {
                Monitor(form);form.DisplayReport(current);form.Close();
                Assert(!form.Visible&&!form.IsDisposed,"Manual close must keep the running session in the background");
                if(scenario=="exception")failRead=true;
                else if(scenario=="failed-read")current=Bridge.Failure("Fixture rejected status");
                else if(scenario=="stopped")current=Report("stopped","stopped");
                else current["windows_display"]=new Dictionary<string,object>{{"status",scenario}};
                Pump(Refresh(form));
                Assert(!form.Visible&&!form.IsDisposed&&Tray(form).Visible,"Manual close must survive status "+scenario+" and retain its tray entry");
                if(scenario=="stopped")Assert(form.StartButton.Enabled,"A hidden panel must still process a confirmed shutdown");
                if(scenario=="exception"||scenario=="failed-read")Assert(form.StatusText.Contains("не подтверждено"),"A hidden panel must still retain its status error");
                Tray(form).ContextMenuStrip.Items[0].PerformClick();
                Assert(form.Visible,"Explicit tray reopen must remain available after "+scenario);
            }
        }
    }
    static void PendingStatusBeforeManualClose()
    {
        foreach(bool throws in new[]{false,true})
        {
            int reads=0;var pending=new TaskCompletionSource<Dictionary<string,object>>();
            using(var form=new MainForm(false,(action,payload)=>{Assert(action=="status","Unexpected operation during pending status");reads++;return pending.Task;}))
            {
                Monitor(form);form.DisplayReport(Report("running","running"));
                Task read=Refresh(form);Assert(reads==1&&!read.IsCompleted,"The status request must already be pending before X");
                form.Close();
                if(throws)pending.SetException(new InvalidOperationException("Fixture late status failure"));
                else pending.SetResult(Report("stopped","running",false));
                Pump(read);
                Assert(!form.Visible&&!form.IsDisposed&&Tray(form).Visible,"A late status error must respect X even when the request began before it");
                Assert(throws?form.StatusText.Contains("не подтверждено"):form.StopButton.Enabled&&!form.StartButton.Enabled,
                    "The late reply must still update the hidden panel's state");
                Tray(form).ContextMenuStrip.Items[0].PerformClick();Assert(form.Visible,"The tray must reopen a panel after a late status error");
            }
        }
    }
    static void PendingStartAfterManualClose()
    {
        foreach(bool succeeds in new[]{false,true})
        {
            var pending=new TaskCompletionSource<Dictionary<string,object>>();
            Dictionary<string,object> current=Report("running","running");int starts=0;
            using(var form=new MainForm(false,(action,payload)=>{
                if(action=="start"){starts++;return pending.Task;}
                Assert(action=="diagnose"||action=="status","Unexpected follow-up after pending start");return Task.FromResult(current);
            }))
            {
                Monitor(form);Task start=form.RunAction("start",false);form.Close();
                Assert(!form.Visible&&!form.IsDisposed&&!start.IsCompleted,"X during pending start must preserve the background operation");
                pending.SetResult(succeeds?new Dictionary<string,object>{{"ok",true},{"message","Готово"}}:Bridge.Failure("Запуск отклонён."));
                Pump(start);
                Assert(starts==1&&!form.Visible&&!form.IsDisposed&&Tray(form).Visible,"A start completion after X must not reopen or dispose the panel");
                current=succeeds?Report("running","running"):Report("stopped","stopped");
                if(succeeds)current["windows_display"]=new Dictionary<string,object>{{"status","unconfirmed"}};
                Pump(Refresh(form));
                Assert(!form.Visible&&Tray(form).Visible,"A follow-up poll must not erase manual dismissal after start success or failure");
                Tray(form).ContextMenuStrip.Items[0].PerformClick();Assert(form.Visible,"Explicit tray reopen must work after pending start completion");
            }
        }
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Dictionary<string,object> current=Report("running","running"); int reads=0,mutations=0;
            TaskCompletionSource<Dictionary<string,object>> pendingRead=null,pendingMutation=null;
            bool failRead=false;
            using(MainForm form=new MainForm(false,(action,payload)=> {
                if(action=="diagnose"||action=="status") { reads++; if(failRead)throw new InvalidOperationException("Read failed"); return pendingRead==null?Task.FromResult(current):pendingRead.Task; }
                mutations++; return pendingMutation==null?Task.FromResult(new Dictionary<string,object>{{"ok",true},{"message","Готово"}}):pendingMutation.Task;
            }))
            {
                Monitor(form); form.DisplayReport(Report("stopped","stopped"));
                Pump(form.RunAction("start",false));
                Assert(!form.Visible,"Confirmed start must hide the panel.");
                NotifyIcon tray=Tray(form);
                Assert(tray.Visible,"Hidden panel must have a tray entry.");
                Assert(tray.ContextMenuStrip.Items[0].Text=="Открыть панель"&&tray.ContextMenuStrip.Items[1].Text=="Выключить Windows","Tray actions must be clearly named.");
                tray.ContextMenuStrip.Items[0].PerformClick(); Assert(form.Visible,"Tray action must reopen the panel.");
                form.Close(); Assert(!form.IsDisposed&&!form.Visible,"Closing the active panel must keep its lifecycle monitor alive.");
                current=Report("stopped","stopped"); Pump(Refresh(form));
                Assert(!form.Visible&&!form.IsDisposed&&form.StartButton.Enabled&&tray.Visible,"Guest shutdown must update readiness without undoing manual close.");
                tray.ContextMenuStrip.Items[0].PerformClick();
                Assert(form.Visible&&form.StartButton.Enabled&&!tray.Visible,"Explicit tray reopen after shutdown must allow a new start.");
                current=Report("running","running"); Pump(form.RunAction("start",false));
                current=Report("running","running");
                current["windows_settings"]=new Dictionary<string,object>{{"schema",1},{"width",1920},{"height",1080}};
                current["windows_display"]=new Dictionary<string,object>{{"status","failed"},{"receipt",new Dictionary<string,object>{{"current",new Dictionary<string,object>{{"width",1280},{"height",720}}}}}};
                Pump(Refresh(form));
                Assert(form.Visible&&!form.StartButton.Enabled,"A failed guest display change must restore the panel without starting a second Windows.");
                Label detail=(Label)typeof(MainForm).GetField("nextStep",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form);
                Assert(detail.Text.Contains("не применила"),"Guest display failure must have a visible explanation.");
                current["ok"]=false;
                current["blockers"]=new object[]{new Dictionary<string,object>{{"title","Ошибка подключения"},{"detail","Проверь пароль сервера"},{"action","connection"},{"action_label","Подключение"}}};
                form.DisplayReport(current);
                Assert(detail.Text=="Проверь пароль сервера","Display feedback must not overwrite an unrelated blocker.");
                current=Report("running","running"); Pump(form.RunAction("start",false));
                current=Report("stopped","running",false); Pump(Refresh(form));
                Assert(form.Visible&&form.StopButton.Enabled&&!form.StartButton.Enabled,"An orphan gateway/error must restore the panel without enabling another start.");
                current=Report("running","running"); form.DisplayReport(current);
                pendingRead=new TaskCompletionSource<Dictionary<string,object>>(); int before=reads;
                Task read=Refresh(form); Pump(Refresh(form)); Assert(reads==before+1,"Polling must be single-flight.");
                pendingMutation=new TaskCompletionSource<Dictionary<string,object>>(); Task mutation=form.RunAction("stop",false);
                Pump(Refresh(form)); Assert(reads==before+1,"Polling must not run during a foreground operation.");
                pendingRead.SetResult(Report("stopped","stopped")); Pump(read); pendingRead=null;
                Assert(!form.StartButton.Enabled,"A stale poll must not release a foreground operation.");
                current=Report("stopped","stopped"); pendingMutation.SetResult(new Dictionary<string,object>{{"ok",true},{"message","Выключено"}}); Pump(mutation); pendingMutation=null;
                Assert(form.StartButton.Enabled,"A completed stop must leave the panel usable.");
                current=Report("running","running"); Pump(form.RunAction("start",false)); failRead=true; Pump(Refresh(form)); failRead=false;
                Assert(form.Visible&&form.StatusText.Contains("не подтверждено"),"A failed quiet refresh must restore the panel with an explicit state error.");
                current=Report("stopped","stopped"); int beforeStop=mutations; tray.ContextMenuStrip.Items[1].PerformClick(); Application.DoEvents();
                Assert(mutations==beforeStop+1&&form.StartButton.Enabled,"Tray shutdown must use the existing stop operation and refresh readiness.");
                form.Close(); Assert(form.IsDisposed,"Closing a stopped panel must exit normally.");
            }
            ManualCloseStatusCases();PendingStatusBeforeManualClose();PendingStartAfterManualClose();
            Console.WriteLine("PASS: manual close survives status/start completions; tray reopen/stop; automatic hide still returns on real failures; quiet poll single-flight/busy/stale guards; stopped panel exits."); return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
