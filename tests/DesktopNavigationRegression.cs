using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopNavigationRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static object Field(object owner,string name){return owner.GetType().GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(owner);}
    static bool OwnVisible(Control control){return (bool)typeof(Control).GetMethod("GetState",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(control,new object[]{2});}
    static bool Shown(Control control){for(Control item=control;item!=null&&!(item is Form);item=item.Parent)if(!OwnVisible(item))return false;return true;}
    static void Context(){SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());}
    static void Finish(Task task)
    {
        Stopwatch elapsed=Stopwatch.StartNew();
        while(!task.IsCompleted&&elapsed.ElapsedMilliseconds<5000){Application.DoEvents();Thread.Sleep(5);}
        Assert(task.IsCompleted,"Pending navigation did not finish");task.GetAwaiter().GetResult();
    }
    static Dictionary<string,object> Snapshot(int memory=4096,int cpus=4)
    {
        return new Dictionary<string,object>{{"ok",true},{"message","Готово"},
            {"settings",new Dictionary<string,object>{{"schema",1},{"work_memory_mib",memory},{"gateway_memory_mib",1536},{"vcpus",cpus},{"lan_port",42345}}},
            {"windows_settings",new Dictionary<string,object>{{"schema",1},{"width",1920},{"height",1080},{"fit",false}}},
            {"viewer_settings",new Dictionary<string,object>{{"schema",1},{"start_fullscreen",false},{"remember_window",false}}},
            {"file_settings",new Dictionary<string,object>{{"schema",1},{"mode","off"},{"folders",new string[0]}}},
            {"host",new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",12288},{"logical_processors",8}}}};
    }
    static Dictionary<string,object> Report(int memory=4096,int cpus=4)
    {
        var reply=Snapshot(memory,cpus);
        reply["checks"]=new Dictionary<string,object>{{"runtime",new Dictionary<string,object>{{"passed",true},{"title","Компоненты"},{"detail","Файлы проверены"}}}};
        return reply;
    }
    static void OnlyPage(MainForm form,AppPage selected)
    {
        var pages=(Dictionary<AppPage,Control>)Field(form,"pageControls");
        Assert(form.ActivePage==selected&&pages.Count(pair=>OwnVisible(pair.Value))==1&&OwnVisible(pages[selected]),
            "Exactly the selected page must be visible after every switch");
    }
    static void ColdReadAndFastNavigation()
    {
        int reads=0;
        var pending=new TaskCompletionSource<Dictionary<string,object>>();
        using(var form=new MainForm(false,(action,payload)=>{
            if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[0]}});
            Assert(action=="get-settings","Navigation must not issue unrelated requests");reads++;return pending.Task;
        }))
        {
            Context();Task first=form.NavigateAsync(AppPage.Settings),duplicate=form.NavigateAsync(AppPage.Settings);
            Assert(reads==1&&!first.IsCompleted&&!duplicate.IsCompleted,"Cold settings navigation must deduplicate pending reads");
            Assert(form.SettingsPage.Loading&&form.SettingsPage.Fields.Values.All(field=>!Shown(field))&&!Shown(form.SettingsPage.SaveButton),
                "Cold loading must show one loading state without empty fields or Save");
            Assert(form.SettingsButton.Enabled&&form.ConnectionButton.Enabled&&form.DiagnosticsButton.Enabled&&form.HelpButton.Enabled,
                "A slow settings read must leave navigation enabled");
            foreach(AppPage page in new[]{AppPage.Home,AppPage.Help,AppPage.Settings,AppPage.Home})
            {
                Task navigation=form.NavigateAsync(page);
                if(page!=AppPage.Settings)Assert(navigation.IsCompleted,"Unrelated pages must switch synchronously during a slow settings read");
                OnlyPage(form,page);
            }
            Assert(form.NavigateAsync(AppPage.Connection).IsCompleted,"An independent profile read must not wait for the settings read");
            OnlyPage(form,AppPage.Connection);Finish(form.NavigateAsync(AppPage.Home));
            pending.SetResult(Snapshot());Finish(Task.WhenAll(first,duplicate));
            OnlyPage(form,AppPage.Home);
            Assert(!form.SettingsPage.Loading&&form.SettingsPage.Fields["work_memory_mib"].Text=="4096"&&form.SettingsPage.Fields["vcpus"].Text=="4",
                "The complete settings page must be populated together without stealing navigation");
            Assert(form.NavigateAsync(AppPage.Settings).IsCompleted&&reads==1,"Cached settings must switch without another read");
            OnlyPage(form,AppPage.Settings);
        }
    }
    static void StartupCacheAndDrafts()
    {
        int requests=0;
        using(var form=new MainForm(false,(action,payload)=>{requests++;return Task.FromResult(Bridge.Failure("Unexpected read"));}))
        {
            form.DisplayReport(Report());
            Assert(!form.SettingsPage.Loading&&form.SettingsPage.Fields["vcpus"].Text=="4", "Startup report must initialize CPU and resources before Settings opens");
            Dictionary<string,object> value;
            Assert(form.SettingsPage.TryGetWindowsValue(out value)&&(int)value["width"]==1920,"Startup report must initialize display settings");
            Assert(form.SettingsPage.TryGetViewerValue(out value)&&!(bool)value["remember_window"],"Startup report must initialize viewer settings");
            Assert(form.SettingsPage.TryGetFileSharingValue(out value)&&(string)value["mode"]=="off","Startup report must initialize file settings");
            form.SettingsPage.Fields["work_memory_mib"].Text="6144";form.SettingsPage.Fields["vcpus"].Text="6";
            form.SettingsPage.StartFullscreen.Checked=true;
            form.SettingsPage.Error.Text="Сохрани или исправь черновик";form.SettingsPage.Error.Visible=true;
            form.DisplayReport(Report(2048,2));
            foreach(AppPage page in new[]{AppPage.Settings,AppPage.Home,AppPage.Diagnostics,AppPage.Help,AppPage.Settings})
            { Assert(form.NavigateAsync(page).IsCompleted,"Cached pages must switch synchronously");OnlyPage(form,page); }
            for(int section=0;section<3;section++)
            {
                form.SettingsPage.ShowSection(section);
                var sections=(List<Control>)Field(form.SettingsPage,"sections");
                Assert(sections.Count(OwnVisible)==1&&OwnVisible(sections[section]),"Settings subpages must never remain overlaid");
            }
            Assert(requests==0,"Cached navigation must reuse startup data");
            Assert(form.SettingsPage.Dirty&&form.SettingsPage.Fields["work_memory_mib"].Text=="6144"&&form.SettingsPage.Fields["vcpus"].Text=="6"&&form.SettingsPage.StartFullscreen.Checked,
                "Reports and section switches must preserve all edited draft values");
            Assert(form.SettingsPage.Error.Text=="Сохрани или исправь черновик"&&OwnVisible(form.SettingsPage.Error),"Navigation and passive refresh must retain the existing error");
        }
    }
    static void LateReplyAfterStartup()
    {
        var pending=new TaskCompletionSource<Dictionary<string,object>>();
        using(var form=new MainForm(false,(action,payload)=>pending.Task))
        {
            Context();Task read=form.NavigateAsync(AppPage.Settings);
            form.DisplayReport(Report(6144,6));form.SettingsPage.Fields["vcpus"].Text="7";
            pending.SetResult(Bridge.Failure("Old read failure"));Finish(read);
            Assert(!form.SettingsPage.Loading&&form.SettingsPage.Fields["work_memory_mib"].Text=="6144"&&form.SettingsPage.Fields["vcpus"].Text=="7",
                "An old reply must not overwrite startup data or a newer draft");
            Assert(!form.SettingsPage.Error.Text.Contains("Old read failure"),"An obsolete failure must not replace current state");
        }
    }
    static void MutationDuringRead()
    {
        int reads=0,mutations=0;
        var pendingRead=new TaskCompletionSource<Dictionary<string,object>>();
        var pendingMutation=new TaskCompletionSource<Dictionary<string,object>>();
        using(var form=new MainForm(false,(action,payload)=>{
            if(action=="get-settings")return ++reads==1?pendingRead.Task:Task.FromResult(Snapshot(8192,8));
            Assert(action=="prepare-components","Unexpected mutation");mutations++;return pendingMutation.Task;
        }))
        {
            Context();Task read=form.NavigateAsync(AppPage.Settings),mutation=form.RunAction("prepare-components",false);
            pendingRead.SetResult(Snapshot(2048,2));Finish(read);
            Assert(form.SettingsPage.Loading&&!form.SettingsPage.HasValues,"A read started before a mutation must not publish stale settings");
            Assert(!form.PrepareButton.Enabled&&!form.SettingsPage.SaveButton.Enabled&&form.SettingsPage.Fields.Values.All(field=>!field.Enabled),
                "Completing an unrelated read must not release the global mutation guard");
            Finish(form.RunAction("prepare-components",false));Assert(mutations==1,"Duplicate mutations must remain blocked");
            Assert(form.NavigateAsync(AppPage.Help).IsCompleted&&form.NavigateAsync(AppPage.Home).IsCompleted,"Navigation must remain available during a mutation");
            OnlyPage(form,AppPage.Home);
            pendingMutation.SetResult(Bridge.Failure("Fixture mutation failure"));Finish(mutation);
            Context();Finish(form.NavigateAsync(AppPage.Settings));
            Assert(reads==2&&form.SettingsPage.Fields["work_memory_mib"].Text=="8192","After a stale read, explicit navigation must allow a fresh read");
            Finish(form.NavigateAsync(AppPage.Help));
            Assert(((Label)Field(form,"operation")).Text.Contains("Fixture mutation failure"),"Changing pages must preserve a concrete mutation failure");
        }
    }
    static void ReadFailureAndRetry()
    {
        int reads=0;
        using(var form=new MainForm(false,(action,payload)=>Task.FromResult(++reads==1?Bridge.Failure("Fixture read failure"):Snapshot())))
        {
            Context();Finish(form.NavigateAsync(AppPage.Settings));
            Assert(form.SettingsPage.Loading&&form.SettingsPage.Error.Text.Contains("Fixture read failure")&&form.SettingsPage.Fields.Values.All(field=>!Shown(field)),
                "A failed cold read must retain a coherent error state without exposing empty controls");
            Finish(form.NavigateAsync(AppPage.Home));Finish(form.NavigateAsync(AppPage.Settings));
            Assert(reads==2&&!form.SettingsPage.Loading&&form.SettingsPage.Fields["vcpus"].Text=="4","Reopening a failed cold page must retry successfully");
        }
    }
    static Dictionary<string,object> ProfileListReply()
    {
        return new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{new Dictionary<string,object>{
            {"profile_id","fixture"},{"name","Сохранённое подключение"},{"protocol","hysteria2"},{"host","example.com"},{"port",443},
            {"selected",true},{"auth_present",true},{"needs_credentials",false}}}}};
    }
    static void DeferredConnectionRead()
    {
        foreach(string action in new[]{"diagnose","prepare-components"})
        {
            int lists=0,operations=0;
            var pending=new TaskCompletionSource<Dictionary<string,object>>();
            using(var form=new MainForm(false,(name,payload)=>{
                if(name=="list-profiles"){lists++;return Task.FromResult(ProfileListReply());}
                Assert(name==action,"Deferred profile read must not issue unrelated operations");operations++;return pending.Task;
            }))
            {
                Context();Task operation=form.RunAction(action,false);
                Task first=form.NavigateAsync(AppPage.Connection),duplicate=form.NavigateAsync(AppPage.Connection);
                Assert(lists==0&&!first.IsCompleted&&!duplicate.IsCompleted,"Cold Connection must deduplicate and wait for the occupied gate");
                Assert(form.ConnectionPage.Loading&&form.ConnectionPage.Fields.Values.All(field=>!Shown(field))&&!Shown(form.ConnectionPage.NewButton),
                    "Deferred Connection must show one loading state rather than an empty editable form");
                Assert(form.NavigateAsync(AppPage.Help).IsCompleted,"Waiting for profiles must leave navigation responsive");
                Finish(form.RunAction(action,false));Assert(operations==1,"Deferred navigation must not release the existing operation gate");
                pending.SetResult(action=="diagnose"?Report():Bridge.Failure("Fixture operation completed"));
                Finish(Task.WhenAll(operation,first,duplicate));
                Assert(lists==1&&!form.ConnectionPage.Loading&&form.ConnectionPage.ProfileId=="fixture",
                    "The first Connection request must load automatically after the gate releases without another tab click");
                OnlyPage(form,AppPage.Help);
                Assert(form.NavigateAsync(AppPage.Connection).IsCompleted&&lists==1,"A loaded Connection must open from cache");
            }
        }
    }
    static void DeferredConnectionDraftAndError()
    {
        int lists=0;
        var pending=new TaskCompletionSource<Dictionary<string,object>>();
        using(var form=new MainForm(false,(action,payload)=>{
            if(action=="diagnose")return pending.Task;
            Assert(action=="list-profiles","Unexpected deferred action");
            return Task.FromResult(++lists==1?Bridge.Failure("Fixture profile read failure"):ProfileListReply());
        }))
        {
            form.ConnectionPage.Fields["host"].Text="draft.example.com";
            Context();Task diagnose=form.RunAction("diagnose",false),read=form.NavigateAsync(AppPage.Connection);
            pending.SetResult(Report());Finish(Task.WhenAll(diagnose,read));
            Assert(lists==1&&form.ConnectionPage.Loading&&form.ConnectionPage.Error.Text.Contains("Fixture profile read failure"),
                "A deferred read failure must leave a coherent error state");
            Finish(form.NavigateAsync(AppPage.Home));Finish(form.NavigateAsync(AppPage.Connection));
            Assert(lists==2&&!form.ConnectionPage.Loading&&form.ConnectionPage.ProfileId==null&&form.ConnectionPage.Dirty
                &&form.ConnectionPage.Fields["host"].Text=="draft.example.com","Retrying the cold profile list must preserve an existing unsaved draft");
        }
    }
    static void CloseDuringDeferredConnection()
    {
        int lists=0;
        var pending=new TaskCompletionSource<Dictionary<string,object>>();
        var form=new MainForm(false,(action,payload)=>{if(action=="list-profiles")lists++;return pending.Task;});
        Context();Task operation=form.RunAction("diagnose",false),read=form.NavigateAsync(AppPage.Connection);
        form.Dispose();Finish(read);
        pending.SetResult(Report());Finish(operation);
        Assert(lists==0,"Closing while Connection waits must not start another backend request");
    }
    static void SettledLayoutAndRetainedChecks()
    {
        var pending=new TaskCompletionSource<Dictionary<string,object>>();
        using(var form=new MainForm(false,(action,payload)=>pending.Task))
        {
            form.DisplayReport(Report());form.PerformLayout();
            var home=(Control)Field(form,"homeLayout");
            var optional=new[]{(Control)Field(form,"homeKeys"),(Control)Field(form,"homeCards"),(Control)Field(form,"homeSteps")};
            MethodInfo fitHome=typeof(MainForm).GetMethod("FitHome",BindingFlags.Instance|BindingFlags.NonPublic);
            MethodInfo fitDiagnostics=typeof(MainForm).GetMethod("FitDiagnostics",BindingFlags.Instance|BindingFlags.NonPublic);
            // Force the nested summary row to overflow without changing visibility merely to measure it.
            optional[2].MinimumSize=new Size(0,1000);
            for(int index=0;index<3;index++)fitHome.Invoke(form,null);
            Assert(!OwnVisible(optional[2]),"A large nested steps row must be dropped while retaining the summary and primary actions");
            Assert(OwnVisible(form.StartButton.Parent)&&OwnVisible(home.Controls[1]),"Fitting Home must retain the status card and its action container");
            int visibilityChanges=0,visibilityLayouts=0;
            foreach(Control item in optional)item.VisibleChanged+=delegate{visibilityChanges++;};
            foreach(Control parent in optional.Select(item=>item.Parent).Distinct())parent.Layout+=(sender,args)=>{
                if(args.AffectedProperty=="Visible"&&optional.Contains(args.AffectedControl))visibilityLayouts++;
            };
            for(int index=0;index<3;index++){home.PerformLayout();fitHome.Invoke(form,null);}
            Assert(visibilityChanges==0&&visibilityLayouts==0,"Repeated settled Home layout must not show then hide the optional parts");

            var items=(List<TableLayoutPanel>)Field(form,"recoveryItems");
            for(int index=0;index<3;index++)fitDiagnostics.Invoke(form,null);
            Size[] minimum=items.Select(item=>item.Controls[0].MinimumSize).ToArray();
            int minimumChanges=0;
            foreach(TableLayoutPanel item in items)item.Layout+=(sender,args)=>{if(args.AffectedProperty=="MinimumSize")minimumChanges++;};
            for(int index=0;index<3;index++)fitDiagnostics.Invoke(form,null);
            Assert(minimumChanges==0&&items.Select(item=>item.Controls[0].MinimumSize).SequenceEqual(minimum),
                "Repeated settled Diagnostics layout must not reset and restore button minimum sizes");

            optional[2].MinimumSize=Size.Empty;form.ClientSize=new Size(960,1000);form.PerformLayout();
            for(int index=0;index<3;index++)fitHome.Invoke(form,null);
            Assert(optional.All(OwnVisible),"Optional Home content must return when the viewport has room");
            var rows=(Control)Field(form,"checkRows");Control[] cards=rows.Controls.Cast<Control>().ToArray();
            var same=Report();same["message"]="Updated status with identical checks";form.DisplayReport(same);
            Assert(rows.Controls.Cast<Control>().SequenceEqual(cards)&&cards.All(card=>!card.IsDisposed),"Identical diagnostics must retain existing cards");
            foreach(string key in new[]{"passed","title","detail"})
            {
                cards=rows.Controls.Cast<Control>().ToArray();var changed=Report();
                var check=(Dictionary<string,object>)((Dictionary<string,object>)changed["checks"])["runtime"];
                check[key]=key=="passed"?(object)false:"Changed "+key;
                form.DisplayReport(changed);
                Assert(cards.All(card=>card.IsDisposed)&&!rows.Controls.Cast<Control>().SequenceEqual(cards),"A changed diagnostics "+key+" must update the rendered cards");
            }

            var pages=(Control)Field(form,"pages");pages.Parent.PerformLayout();Rectangle before=pages.Bounds;
            Assert(form.SettingsPage.RecommendedButton.Enabled,"Loaded host evidence must enable recommendations before an operation");
            Context();Task operation=form.RunAction("diagnose",false);pages.Parent.PerformLayout();
            Assert(pages.Bounds==before&&!form.SettingsPage.RecommendedButton.Enabled,"Busy status must preserve the viewport and disable recommendations through their section");
            pending.SetResult(Report());Finish(operation);pages.Parent.PerformLayout();
            Assert(pages.Bounds==before&&form.SettingsPage.RecommendedButton.Enabled,"Completing an operation must preserve the viewport and restore recommendations");
            var status=(Label)Field(form,"operation");Font original=status.Font;
            using(var enlarged=new Font(original.FontFamily,original.Size+4,original.Style))
            {
                status.Font=enlarged;
                var row=((TableLayoutPanel)status.Parent).RowStyles[1];
                Assert(row.SizeType==SizeType.Absolute&&row.Height>=status.Height+status.Margin.Vertical,"The reserved operation row must adapt to a larger font");
                status.Font=original;
            }
        }
    }
    [STAThread]
    static int Main()
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            ColdReadAndFastNavigation();StartupCacheAndDrafts();LateReplyAfterStartup();MutationDuringRead();ReadFailureAndRetry();
            DeferredConnectionRead();DeferredConnectionDraftAndError();CloseDuringDeferredConnection();
            SettledLayoutAndRetainedChecks();
            Console.WriteLine("PASS: coherent cached navigation, cold loading/dedup, startup hydration, drafts/errors, stale replies and mutation protection");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
