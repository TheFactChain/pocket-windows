using System;
using System.Collections.Generic;
using System.Windows.Forms;

internal static class RuntimeStatusRegression
{
    static void Assert(bool value,string message) { if(!value)throw new Exception(message); }
    static Dictionary<string,object> Check(bool passed,string title) { return new Dictionary<string,object>{{"passed",passed},{"title",title},{"detail",title}}; }
    static Dictionary<string,object> Report(bool full,bool engineOk,string work)
    {
        var checks=new Dictionary<string,object>{{"runtime_state",Check(true,"Сеанс")}};
        if(full) { checks["engine_identity"]=Check(engineOk,"Проверка компонентов"); checks["gateway_package"]=Check(true,"Шлюз"); }
        return new Dictionary<string,object>{{"report_kind",full?"preflight":"runtime_status"},{"ok",!full||engineOk},
            {"message","Состояние обновлено"},{"checked_utc",full?"full-check":"poll"},{"checks",checks},
            {"runtime",new Dictionary<string,object>{{"work",work},{"gateway",work},{"control_available",true}}},
            {"already_running",work=="running"},{"network_verified",work=="running"},
            {"capabilities",new Dictionary<string,object>{{"stop",work=="running"},{"test_profile",false}}}};
    }
    [STAThread]
    static int Main()
    {
        try
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            using(var form=new MainForm(false,(action,payload)=>Report(true,true,"stopped")))
            {
                form.DisplayReport(Report(true,false,"running"));
                form.DisplayReport(Report(false,true,"stopped"));
                var checks=(Dictionary<string,object>)form.ReadinessReport["checks"];
                Assert(checks.ContainsKey("engine_identity"),"A quiet status must retain the full component result");
                Assert(!(bool)((Dictionary<string,object>)checks["engine_identity"])["passed"]&&!form.StartButton.Enabled,"A quiet poll must never clear an integrity failure");
                Assert((string)form.ReadinessReport["checked_utc"]=="full-check","Polling must retain the time of the full check");
                Assert(!form.StopButton.Visible,"A stopped status must still update lifecycle controls");
                form.DisplayReport(Report(true,true,"stopped"));
                Assert(form.StartButton.Enabled,"A successful full check must restore start");
                form.DisplayReport(Report(false,true,"running"));
                Assert(!form.StartButton.Enabled&&form.StopButton.Enabled,"A live status must retain stop and prevent duplicate start");
                form.DisplayReport(Report(false,true,"stopped"));
                Assert(form.StartButton.Enabled,"A confirmed shutdown must reuse the complete readiness check");
            }
            using(var form=new MainForm(false,(action,payload)=>Report(false,true,"stopped")))
            {
                form.DisplayReport(Report(false,true,"stopped"));
                Assert(!form.StartButton.Enabled,"A lightweight status alone must not certify installation readiness");
            }
            Console.WriteLine("PASS: lightweight status preserves integrity checks/time, updates lifecycle and cannot replace a full check."); return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
