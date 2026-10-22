using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class DesktopPagesRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control nested in Tree(child))yield return nested;}
    // The form is never shown, so Control.Visible is false everywhere; read each control's own flag instead.
    static bool Shown(Control control){for(Control item=control;item!=null&&!(item is Form);item=item.Parent)if(!(bool)typeof(Control).GetMethod("GetState",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(item,new object[]{2}))return false;return true;}
    static void Layout(Control root){typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(root,new object[]{true});root.PerformLayout();}
    static void Pump(Task task){Stopwatch elapsed=Stopwatch.StartNew();while(!task.IsCompleted&&elapsed.ElapsedMilliseconds<8000){Application.DoEvents();Thread.Sleep(5);}Assert(task.IsCompleted,"UI action timed out");task.GetAwaiter().GetResult();}
    static void Context(){SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());}
    static Dictionary<string,object> Settings(){return new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",768},{"vcpus",2},{"lan_port",32861}};}
    static Dictionary<string,object> View(){return new Dictionary<string,object>{{"profile_id","profile1"},{"name","Личный VPN"},{"protocol","hysteria2"},{"host","example.com"},{"port",443},{"auth_present",true},{"needs_credentials",false},{"selected",true},{"verified",false},{"state","pending"},{"applied_revision_id",null}};}
    static Dictionary<string,object> Report(){return new Dictionary<string,object>{{"ok",true},{"message","Готово к запуску"},{"settings",Settings()},{"checks",new Dictionary<string,object>{{"runtime",new Dictionary<string,object>{{"passed",true},{"title","Компоненты"},{"detail","Файлы проверены."}}}}},{"network_warning","Сеть текущего сеанса ещё не проверена."},{"connection",new Dictionary<string,object>{{"selected_profile",View()},{"verified",false}}}};}
    static void Inside(Control control,Form form,string label)
    {
        Point point=control.Location;for(Control parent=control.Parent;parent!=form;parent=parent.Parent)point.Offset(parent.Location);
        Assert(form.ClientRectangle.Contains(new Rectangle(point,control.Size)),label+": outside fixed viewport, "+control.Bounds+"; form="+form.ClientRectangle);
        Assert(control.Parent.ClientRectangle.Contains(control.Bounds),label+": clipped by immediate parent; bounds="+control.Bounds+"; parent="+control.Parent.ClientRectangle);
    }
    static void Render(Form form,string path){Layout(form);using(Bitmap image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(path,ImageFormat.Png);}}
    static void SettingsCases()
    {
        Dictionary<string,object> original=Settings();original["gateway_memory_mib"]=1536;original["lan_port"]=42345;
        using(SettingsEditor editor=new SettingsEditor())
        {
            editor.Fields["work_memory_mib"].Text="8192";editor.Fields["vcpus"].Text="4";
            Dictionary<string,object> unloaded;
            Assert(!editor.TryGetValue(out unloaded)&&unloaded==null&&!String.IsNullOrEmpty(editor.Error.Text),"Save must require loaded hidden settings instead of returning a partial object that resets them to defaults");
            editor.LoadValues(original,true);
            Assert(editor.Fields.Keys.OrderBy(key=>key).SequenceEqual(new[]{"vcpus","work_memory_mib"}),"Settings must expose exactly Windows memory and CPU per VM");
            Assert(editor.Fields["vcpus"].AccessibleName=="Процессоры каждой VM","CPU caption must describe its scope per VM");
            Assert(editor.Error.Text.Contains("резервной копии"),"Recovery must explain the original configuration backup");
            editor.Fields["work_memory_mib"].Text="8192";editor.Fields["vcpus"].Text="4";
            Dictionary<string,object> saved;
            Assert(editor.TryGetValue(out saved),"Visible memory and CPU changes must save");
            Assert(saved.Count==5&&(int)saved["schema"]==1&&(int)saved["work_memory_mib"]==8192&&(int)saved["vcpus"]==4,"Save must return the complete schema 1 settings contract");
            Assert((int)saved["gateway_memory_mib"]==1536&&(int)saved["lan_port"]==42345,"Saving must preserve loaded hidden values instead of defaults");
            Assert((int)original["work_memory_mib"]==3072&&(int)original["vcpus"]==2,"Editing must not mutate loaded settings");
            editor.CancelButton.PerformClick();
            Assert(editor.TryGetValue(out saved)&&(int)saved["work_memory_mib"]==3072&&(int)saved["vcpus"]==2&&(int)saved["gateway_memory_mib"]==1536&&(int)saved["lan_port"]==42345,"Cancel must restore visible values and retain hidden values");
            editor.LoadValues(Settings(),false);
            Assert(editor.TryGetValue(out saved)&&(int)saved["gateway_memory_mib"]==768&&(int)saved["lan_port"]==32861,"Reload must replace the preserved settings snapshot");
        }
    }
    static void LayoutCases(string logs)
    {
        foreach(float scale in new[]{1f} /* laid out at 100% only; Windows scales the window */)
        using(MainForm form=new MainForm(false,(action,payload)=>action=="get-settings"?new Dictionary<string,object>{{"ok",true},{"settings",Settings()}}:Report()))
        {
            form.ClientSize=new Size(1040,660);Layout(form);form.DisplayReport(Report());
            Assert(form.StatusText.Contains("не подтверждён"),"File readiness must not claim a running session when runtime is unknown");
            Assert(Tree(form).OfType<LinkLabel>().Any(item=>item.Text.Contains("Личный VPN")&&item.Text.Contains("следующего запуска")),"Home must show the active public profile and pending state");
            Size viewport=form.ClientSize;var fonts=Tree(form).Select(control=>new{Control=control,Font=control.Font}).ToArray();
            foreach(var item in fonts)item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*scale,item.Font.Style);
            Layout(form);Assert(form.ClientSize==viewport,"Font-only validation must not increase the viewport");
            string[] navigation={"Главная","Подключение","Настройки","Диагностика"};
            foreach(string name in navigation){Button button=Tree(form).OfType<Button>().Single(item=>item.Text==name);Inside(button,form,name);Assert(button.Width-54>=TextRenderer.MeasureText(button.Text,button.Font).Width,"Navigation caption clipped: "+name+" scale "+scale+" size="+button.Size+" pref="+button.GetPreferredSize(Size.Empty));}
            Inside(form.StartButton,form,"Start");
            Control home=(Control)typeof(MainForm).GetField("overview",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
            if(scale>1f){Directory.CreateDirectory(logs);Render(form,Path.Combine(logs,"ui-pages-home-font"+(int)(scale*100)+".png"));}
            foreach(Label label in Tree(home).OfType<Label>().Where(item=>!String.IsNullOrEmpty(item.Text)&&Shown(item)))
            {Inside(label,form,"Ready Home label "+label.Text+" scale "+scale);Assert(label.Height>=label.GetPreferredSize(new Size(label.Width,0)).Height,"Ready Home label clipped "+scale+": "+label.Text+" actual="+label.Size+" preferred="+label.GetPreferredSize(new Size(label.Width,0))); }
            Context();Pump(form.NavigateAsync(AppPage.Settings));Layout(form);
            Inside(form.SettingsPage.SaveButton,form,"Settings save at font scale "+scale);
            if(scale==2f)Render(form,Path.Combine(logs,"ui-pages-settings-font200.png"));
            foreach(TextBox field in form.SettingsPage.Fields.Values)
            {
                Inside(field,form,"Settings field "+field.AccessibleName+" scale "+scale);
                Assert(field.Height>=field.PreferredHeight,"Settings text must not be clipped");
            }
            foreach(Label label in Tree(form.SettingsPage).OfType<Label>().Where(item=>!String.IsNullOrEmpty(item.Text)))
                Assert(label.Height>=label.GetPreferredSize(new Size(label.Width,0)).Height,"Settings label clipped: "+label.Text+" scale "+scale);
            if(scale==1f){Directory.CreateDirectory(logs);Render(form,Path.Combine(logs,"ui-pages-settings.png"));}
            form.SettingsPage.Fields["work_memory_mib"].Text="";Dictionary<string,object> invalid;Assert(!form.SettingsPage.TryGetValue(out invalid),"Invalid values must remain editable");Layout(form);
            Inside(form.SettingsPage.SaveButton,form,"Save with inline error "+scale);
            foreach(TextBox field in form.SettingsPage.Fields.Values)Inside(field,form,"Field with inline error "+scale);Assert(form.ClientSize==viewport,"Inline validation must not increase the viewport");
            if(scale==1f){Directory.CreateDirectory(logs);Render(form,Path.Combine(logs,"ui-pages-settings-validation.png"));Context();Pump(form.NavigateAsync(AppPage.Diagnostics));Render(form,Path.Combine(logs,"ui-pages-diagnostics.png"));Context();Pump(form.NavigateAsync(AppPage.Home));Render(form,Path.Combine(logs,"ui-pages-home.png"));}
        }
    }
    static void ProfileCases(string logs)
    {
        List<string> calls=new List<string>();Dictionary<string,object> lastSave=null,lastExport=null;
        TaskCompletionSource<Dictionary<string,object>> pending=null;
        using(MainForm form=new MainForm(false,(action,payload)=> {
            calls.Add(action);
            if(action=="save-profile"){lastSave=(Dictionary<string,object>)payload;if(pending!=null)return pending.Task;}
            if(action=="export-profile")lastExport=(Dictionary<string,object>)payload;
            if(action=="list-profiles")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profiles",new object[]{View()}}});
            if(action=="test-profile")return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"verified",false},{"message","Проверка не подтверждена"}});
            return Task.FromResult(new Dictionary<string,object>{{"ok",true},{"profile",View()},{"message","Сохранено"}});
        }))
        {
            Layout(form);Context();Pump(form.NavigateAsync(AppPage.Connection));
            Assert(form.ConnectionPage.ProfileId=="profile1","Profile list must load the selected public view");
            Assert(form.ConnectionPage.Profiles.Text.Contains("Личный VPN")&&form.ConnectionPage.Protocol.Text=="Hysteria2","Profile and protocol captions must remain visible as native combo values");
            Render(form,Path.Combine(logs,"ui-pages-connection.png"));
            Assert(form.ConnectionPage.Fields["auth"].UseSystemPasswordChar&&form.ConnectionPage.Fields["password"].UseSystemPasswordChar,"Passwords must be masked");
            Assert(form.ConnectionPage.Fields["auth"].Text=="","Saved secrets must not be requested or shown");
            form.ConnectionPage.Fields["host"].Text="draft.example.com";
            Context();Pump(form.NavigateAsync(AppPage.Home));Context();Pump(form.NavigateAsync(AppPage.Connection));
            Assert(form.ConnectionPage.Fields["host"].Text=="draft.example.com","Page navigation must preserve an unsaved connection draft");
            pending=new TaskCompletionSource<Dictionary<string,object>>();Context();Task save=form.SaveProfileAsync();
            Assert(!save.IsCompleted&&!form.ConnectionPage.SaveButton.Enabled,"Delayed save must show a busy state");
            Assert(form.ConnectionPage.Fields.Values.All(field=>!field.Enabled)&&!form.ConnectionPage.Protocol.Enabled&&!form.ConnectionPage.ProxyAuth.Enabled&&!form.ConnectionPage.Obfuscation.Enabled,"All connection editing controls must be disabled until the save/reload finishes");
            Task duplicate=form.ProfileActionAsync("select-profile",new Dictionary<string,object>{{"profile_id","profile1"}});Pump(duplicate);Assert(!calls.Contains("select-profile"),"Profile mutations must share the single-flight gate");
            Dictionary<string,object> profile=(Dictionary<string,object>)lastSave["profile"];Assert(!profile.ContainsKey("credentials"),"Blank existing password must omit credentials and preserve the old secret");
            pending.SetResult(new Dictionary<string,object>{{"ok",true},{"profile",View()},{"message","Сохранено"}});Pump(save);pending=null;
            Assert(form.ConnectionPage.Fields["auth"].Text=="","Successful save must clear the password input");
            Assert(form.ConnectionPage.State.Text.Contains("не проверена"),"Saved or pending must never claim verified connectivity");
            Context();Pump(form.ExportProfileAsync(Path.Combine(Path.GetTempPath(),"profile-public.json"),false));Assert(!(bool)lastExport["include_secrets"],"Export must omit passwords by default");
            Context();Pump(form.ExportProfileAsync(Path.Combine(Path.GetTempPath(),"profile-explicit.json"),true));Assert((bool)lastExport["include_secrets"],"Explicit secret export choice must reach the backend");
            int before=calls.Count;Context();Pump(form.ImportProfileAsync(new Dictionary<string,object>{{"name","Импорт"},{"protocol","hysteria2"},{"host","example.net"},{"port",443}}));
            Assert(calls.Count==before&&form.ConnectionPage.ProfileId==null,"JSON without a HY password must remain an unsaved draft");
            Dictionary<string,object> request;Assert(!form.ConnectionPage.TrySaveRequest(out request),"HY draft needs a password before saving");
            form.ConnectionPage.Fields["auth"].Text="fixture-password";Assert(form.ConnectionPage.TrySaveRequest(out request),"Entering a password must complete the draft");
            form.ConnectionPage.NewProfile();form.ConnectionPage.Protocol.SelectedIndex=1;form.ConnectionPage.Fields["host"].Text="proxy.example.com";
            Assert(form.ConnectionPage.TrySaveRequest(out request)&&((Dictionary<string,object>)((Dictionary<string,object>)request["profile"])["credentials"]).Count==0,"A proxy without authorization must send an explicit empty credentials object");
            form.ConnectionPage.LoadView(View());form.DisplayReport(Report());Assert(!form.ConnectionPage.TestButton.Enabled,"No test capability means no active test button");
            int count=calls.Count;Context();Pump(form.ProfileActionAsync("test-profile",new Dictionary<string,object>{{"profile_id","profile1"}}));Assert(calls.Count==count,"Unavailable test must not call a missing backend action");
        }
        using(MainForm form=new MainForm(false,(action,payload)=>new Dictionary<string,object>{{"ok",true},{"profiles",new object[0]},{"message","Готово"}}))
        {
            Layout(form);Context();Pump(form.NavigateAsync(AppPage.Connection));
            Dictionary<string,object> longView=View();longView["name"]=new string('Я',128);form.ConnectionPage.LoadView(longView);
            form.ConnectionPage.Error.Text="Проверь имя сервера. Вводи адрес без протокола и пути. Некорректные данные не сохранены.";
            form.ClientSize=new Size(1040,660);
            foreach(Button button in new[]{form.ConnectionPage.SaveButton,form.ConnectionPage.SelectButton,form.ConnectionPage.TestButton})Inside(button,form,"Connection footer with a long name and error");
            Render(form,Path.Combine(logs,"ui-pages-connection-long.png"));
            Context();Pump(form.NavigateAsync(AppPage.Diagnostics));form.DisplayReport(Report());Layout(form);
            Render(form,Path.Combine(logs,"ui-pages-diagnostics-long.png"));
            foreach(Button button in new[]{form.PrepareButton,form.RepairGatewayButton,form.PrepareHostButton})Inside(button,form,"Diagnostics recovery actions");
        }
        using(Form help=MainForm.BuildHelp(new Font("Segoe UI",10)))
        {
            Render(help,Path.Combine(logs,"ui-help.png"));
            Assert(!Tree(help).OfType<TextBox>().Any()&&!Tree(help).OfType<ScrollableControl>().Any(item=>item.AutoScroll),"Help must not scroll");
            foreach(Label label in Tree(help).OfType<Label>().Where(item=>item.Text.Length>0))
            {
                Rectangle bounds=label.Bounds;for(Control parent=label.Parent;parent!=help;parent=parent.Parent){Assert(new Rectangle(Point.Empty,parent.ClientSize).Contains(bounds),"Help text clipped: "+label.Text);bounds.Offset(parent.Location);}
                Assert(label.Height>=label.GetPreferredSize(new Size(label.Width,0)).Height,"Help text cut short: "+label.Text);
            }
            Assert(Tree(help).OfType<Label>().Any(item=>item.Text=="Ctrl+Alt+F")&&Tree(help).OfType<Label>().Any(item=>item.Text=="Ctrl+Enter"),"Help must list the keys");
        }
        using(ProfileExportDialog options=new ProfileExportDialog())Assert(!options.IncludeSecrets,"Secret export opt-in must begin unchecked on every dialog");
    }
    [STAThread]
    static int Main(string[] args)
    {
        try{Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);SettingsCases();LayoutCases(args[0]);ProfileCases(args[0]);Console.WriteLine("PASS: four-page navigation; fixed viewport/font-only layout; pinned settings Save; delayed profile single-flight; masked secrets; preserve credentials; JSON draft; export opt-in; capability gates.");return 0;}
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
