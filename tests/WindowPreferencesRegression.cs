using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class WindowPreferencesRegression
{
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer();
    static string fixtureRoot,statePath;
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static Dictionary<string,object> Viewer(bool remember){return new Dictionary<string,object>{{"schema",1},{"start_fullscreen",false},{"remember_window",remember}};}
    static Dictionary<string,object> State(Rectangle bounds,bool maximized){return new Dictionary<string,object>{{"schema",1},{"x",bounds.X},{"y",bounds.Y},{"width",bounds.Width},{"height",bounds.Height},{"maximized",maximized}};}
    static void Write(Dictionary<string,object> state){Directory.CreateDirectory(Path.GetDirectoryName(statePath));File.WriteAllText(statePath,Json.Serialize(state));}
    static MainForm Form()
    {
        MainForm form=new MainForm(false,(action,payload)=>Task.FromResult(Bridge.Failure("fixture does not run backend")));
        typeof(MainForm).GetField("root",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(form,fixtureRoot);
        form.StartPosition=FormStartPosition.Manual;return form;
    }
    static Rectangle NormalBounds(Dictionary<string,object> value){return new Rectangle((int)value["x"],(int)value["y"],(int)value["width"],(int)value["height"]);}
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            fixtureRoot=Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),"fixture-root");statePath=Path.Combine(fixtureRoot,"config","window-state.json");Directory.CreateDirectory(Path.GetDirectoryName(statePath));
            Rectangle work=Screen.PrimaryScreen.WorkingArea;
            Rectangle saved=new Rectangle(work.X+20,work.Y+20,Math.Min(1050,work.Width-40),Math.Min(700,work.Height-40));
            Write(State(saved,false));
            using(MainForm form=Form())
            {
                form.SettingsPage.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);form.SettingsPage.Fields["work_memory_mib"].Text="4096";
                form.ApplyViewerPreferences(Viewer(true));Assert(form.Bounds==saved,"First valid viewer settings must restore panel bounds");
                Assert(form.SettingsPage.Fields["work_memory_mib"].Text=="4096","Applying viewer preferences must preserve unsaved settings");
                Rectangle moved=saved;moved.Offset(10,10);form.Bounds=moved;form.ApplyViewerPreferences(Viewer(true));Assert(form.Bounds==moved,"Subsequent preference updates must not move the panel");
                form.SavePanelBounds();var written=Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(statePath));Assert(NormalBounds(written)==moved&&written["maximized"] is bool&&!(bool)written["maximized"]&&(int)written["schema"]==1,"Save must persist normal bounds and maximized state");
                Assert(!File.Exists(Path.Combine(fixtureRoot,"config","viewer.json")),"Panel geometry must not overwrite viewer preferences");
                string before=File.ReadAllText(statePath);form.ApplyViewerPreferences(Viewer(false));form.Bounds=saved;form.SavePanelBounds();Assert(File.ReadAllText(statePath)==before,"Disabling remember-window must stop geometry writes");
            }
            Write(State(saved,false));
            using(MainForm form=Form()){Rectangle before=form.Bounds;form.SavePanelBounds();Assert(NormalBounds(Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(statePath)))==saved,"Geometry must not save before preferences load");form.ApplyViewerPreferences(Viewer(false));form.ApplyViewerPreferences(Viewer(true));Assert(form.Bounds==before,"Enabling memory later must not restore old geometry mid-session");}
            foreach(Dictionary<string,object> invalid in new[]{new Dictionary<string,object>{{"schema",1},{"start_fullscreen",false},{"remember_window","true"}},new Dictionary<string,object>{{"schema",2},{"start_fullscreen",false},{"remember_window",true}}})
            using(MainForm form=Form()){Rectangle before=form.Bounds;form.ApplyViewerPreferences(invalid);Assert(form.Bounds==before,"Invalid viewer preferences must not restore bounds");form.ApplyViewerPreferences(Viewer(true));Assert(form.Bounds==saved,"Invalid viewer settings must not consume initial restore");}
            foreach(string invalid in new[]{"{broken", "null", "{\"schema\":1,\"x\":0,\"y\":0,\"width\":\"1050\",\"height\":700,\"maximized\":false}", "{\"schema\":1,\"x\":0,\"y\":0,\"width\":-1,\"height\":700,\"maximized\":false}"})
            {File.WriteAllText(statePath,invalid);using(MainForm form=Form()){Rectangle before=form.Bounds;form.ApplyViewerPreferences(Viewer(true));Assert(form.Bounds==before,"Malformed geometry must preserve defaults");}}
            Write(State(new Rectangle(500000,-500000,16000,16000),false));
            using(MainForm form=Form()){form.ApplyViewerPreferences(Viewer(true));Assert(Screen.FromRectangle(form.Bounds).WorkingArea.Contains(form.Bounds),"Detached monitor or oversized geometry must restore inside an available work area");}
            Write(State(saved,true));
            using(MainForm form=Form())
            {
                form.ApplyViewerPreferences(Viewer(true));Assert(form.WindowState==FormWindowState.Maximized,"Maximized state must restore");form.SavePanelBounds();var written=Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(statePath));Assert((bool)written["maximized"]&&NormalBounds(written)==saved,"Maximized save must preserve normal restore bounds");
                form.WindowState=FormWindowState.Minimized;form.SavePanelBounds();written=Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(statePath));Assert((bool)written["maximized"],"Minimizing a maximized panel must retain its restore state");
                using(FileStream locked=new FileStream(statePath,FileMode.Open,FileAccess.Read,FileShare.None)){form.SavePanelBounds();}
            }
            Write(State(saved,false));using(FileStream locked=new FileStream(statePath,FileMode.Open,FileAccess.Read,FileShare.None))using(MainForm form=Form()){form.ApplyViewerPreferences(Viewer(true));form.SavePanelBounds();}
            Assert(Directory.GetFiles(Path.GetDirectoryName(statePath),"*.tmp").Length==0,"Failed persistence must clean up its temporary file");
            Console.WriteLine("PASS: panel geometry restore-once, remember opt-out, strict JSON, monitor clamping, maximized/minimized roundtrip, draft preservation and IO failures.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
