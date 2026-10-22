using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopUsabilityRegression
{
    static void Assert(bool value,string message) { if(!value)throw new Exception(message); }
    static IEnumerable<Control> Tree(Control root) { yield return root; foreach(Control child in root.Controls)foreach(Control item in Tree(child))yield return item; }
    static Rectangle Bounds(Control control,Control parent) { Point point=control.Location;for(Control item=control.Parent;item!=parent;item=item.Parent)point.Offset(item.Location);return new Rectangle(point,control.Size); }
    static Dictionary<string,object> Report(string work)
    {
        return new Dictionary<string,object>{{"ok",true},{"message","Готово к запуску"},
            {"checks",new Dictionary<string,object>{{"runtime_state",new Dictionary<string,object>{{"passed",true},{"title","Состояние Windows"},{"detail","Проверено"}}}}},
            {"runtime",new Dictionary<string,object>{{"work",work},{"gateway",work},{"control_available",true}}},
            {"capabilities",new Dictionary<string,object>{{"stop",work=="running"}}}};
    }
    static void Key(MainForm form,Keys key)
    {
        object[] arguments={new Message(),key};
        bool handled=(bool)typeof(MainForm).GetMethod("ProcessCmdKey",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(form,arguments);
        Assert(handled,"Documented shortcut must be handled: "+key);
        var until=DateTime.UtcNow.AddSeconds(5);
        do { Application.DoEvents(); Thread.Sleep(10); }
        while ((bool)typeof(MainForm).GetField("busy",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form) && DateTime.UtcNow<until);
        Assert(!(bool)typeof(MainForm).GetField("busy",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form),"Keyboard action must settle: "+key);
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            Directory.CreateDirectory(args[0]);var calls=new List<string>();
            Func<string,object,Task<Dictionary<string,object>>> runner=(action,payload)=> {
                calls.Add(action);
                if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[0]}});
                if(action=="get-settings")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"settings",new Dictionary<string,object>{{"schema",1},{"work_memory_mib",4096},{"gateway_memory_mib",1024},{"vcpus",2},{"lan_port",31861}}},{"host",new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",1024},{"logical_processors",8}}}});
                return Task.FromResult(Report("stopped"));
            };
            using(var form=new MainForm(false,runner))
            {
                form.StartPosition=FormStartPosition.Manual;form.Location=new Point(-30000,-30000);form.ShowInTaskbar=false;form.Show();
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                form.ClientSize=new Size(1040,660);form.DisplayReport(Report("stopped"));form.PerformLayout();
                Control home=(Control)typeof(MainForm).GetField("overview",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
                Rectangle start=Bounds(form.StartButton,home);
                Assert(start.Left>home.ClientSize.Width/2,"The primary start action sits on the right of the Home summary, as in the mockup");
                Assert(Tree(home).OfType<Label>().Any(label=>label.Text.Contains("Ctrl+Alt+F"))&&Tree(home).OfType<Label>().Any(label=>label.Text.Contains("Ctrl+Alt+G")),"Home needs visible fullscreen and mouse-release hints");
                Assert(Tree(form).OfType<Button>().Any(button=>button.Text=="Справка"),"Keyboard help must be discoverable without knowing F1");
                using(var image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(Path.Combine(args[0],"home.png"),ImageFormat.Png);}
                Key(form,Keys.Control|Keys.D2);Assert(form.ActivePage==AppPage.Connection,"Ctrl+2 must open Connection");
                Key(form,Keys.Control|Keys.D1);Assert(form.ActivePage==AppPage.Home,"Ctrl+1 must return Home");
                int before=calls.Count;Key(form,Keys.Control|Keys.Enter);Assert(calls.Skip(before).Contains("start"),"Ctrl+Enter must invoke the primary start action");
                form.DisplayReport(Report("running"));Application.DoEvents();
                Button open=Tree(form).OfType<Button>().SingleOrDefault(button=>button.Text=="Открыть Windows");
                Assert(open!=null&&open.Visible&&open.Enabled&&!form.StartButton.Enabled,"Running Windows needs Open instead of another Start");
                before=calls.Count;Key(form,Keys.Control|Keys.Enter);Assert(calls.Skip(before).Contains("show-desktop")&&!calls.Skip(before).Contains("start"),"The running shortcut must open the existing desktop");
                Key(form,Keys.Control|Keys.D4);Assert(form.ActivePage==AppPage.Diagnostics,"Ctrl+4 must open Diagnostics");
                Control diagnostics=(Control)typeof(MainForm).GetField("diagnostics",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
                Control checks=(Control)typeof(MainForm).GetField("checkRows",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
                Assert(Bounds(checks,diagnostics).Top<Bounds(form.PrepareButton,diagnostics).Top,"Diagnostic results must appear before recovery actions");
                Assert(Tree(diagnostics).OfType<Label>().Any(label=>label.Text.Contains("без интернета")),"Diagnostics must explain its offline scope");
                using(var image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(Path.Combine(args[0],"diagnostics.png"),ImageFormat.Png);}
                Key(form,Keys.Control|Keys.D3);
                form.SettingsPage.Fields["work_memory_mib"].Text="5120";
                form.SettingsPage.RecommendedButton.PerformClick();
                Assert(form.SettingsPage.Fields["work_memory_mib"].Text=="5120","Insufficient memory must preserve the resource draft");
                var refreshed=Report("stopped");refreshed["host"]=new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",8192},{"logical_processors",8}};
                form.DisplayReport(refreshed);
                Assert(form.SettingsPage.Fields["work_memory_mib"].Text=="5120","Refreshing host data must preserve the resource draft");
                form.SettingsPage.RecommendedButton.PerformClick();
                Assert(form.SettingsPage.Fields["work_memory_mib"].Text=="6144","Recommendations must use the freshly checked available memory");
            }
            Console.WriteLine("PASS: centered primary action, discoverable help, scoped keyboard actions, reopen existing Windows, results before recovery.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
