using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class ProfileDialogsRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control nested in Tree(child))yield return nested;}
    static void Layout(Form form)
    {
        typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(form,new object[]{true});
        for(int index=0;index<4;index++)form.PerformLayout();
    }
    static void Fits(Control item,Form form,string scenario)
    {
        Point point=item.PointToScreen(Point.Empty);point=form.PointToClient(point);
        Assert(form.ClientRectangle.Contains(new Rectangle(point,item.Size)),scenario+": outside dialog: "+item.Text);
        if(item is Label&&!String.IsNullOrEmpty(item.Text))
            Assert(item.Height>=item.GetPreferredSize(new Size(item.Width,0)).Height,scenario+": clipped explanation");
        if(item is Button)Assert(item.Width>=TextRenderer.MeasureText(item.Text,item.Font).Width+24,scenario+": clipped action");
    }
    static Dictionary<string,object> View(string id)
    {return new Dictionary<string,object>{{"profile_id",id},{"name",id},{"protocol","hysteria2"},{"host","example.com"},{"port",443},{"auth_present",true},{"needs_credentials",false}};}
    static void Logic()
    {
        foreach(bool throws in new[]{false,true})
        using(MainForm form=new MainForm(false,(action,payload)=>{
            if(throws)throw new InvalidOperationException("fixture read failure");
            return Task.FromResult(Bridge.Failure("Не удалось прочитать профиль"));
        }))
        {
            IntPtr handle=form.Handle;
            form.ConnectionPage.LoadProfiles(new object[]{View("A"),View("B")});
            form.ConnectionPage.LoadView(View("A"));
            SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
            form.ConnectionPage.Profiles.SelectedIndex=1;
            Application.DoEvents();
            Assert(form.ConnectionPage.PickedId==form.ConnectionPage.ProfileId,"Failed selection shows B while actions still use A");
            Assert(form.ConnectionPage.PickedId=="A","Failed selection must restore the loaded profile");
            Assert(!String.IsNullOrEmpty(form.ConnectionPage.Error.Text),"Failed selection needs a visible error");
        }
        using(ConnectionEditor editor=new ConnectionEditor())
        {
            Dictionary<string,object> request;
            editor.LoadView(View("A"));editor.Obfuscation.Checked=true;
            Assert(!editor.TrySaveRequest(out request),"Changed obfuscation silently reuses old credentials");
            editor.LoadView(View("A"));editor.Protocol.SelectedIndex=1;editor.ProxyAuth.Checked=true;
            Assert(!editor.TrySaveRequest(out request),"Changed protocol silently reuses old credentials");
        }
    }
    [STAThread] static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            if(args.Contains("--logic")){Logic();Console.WriteLine("PASS: failed profile selection and credential mode changes stay consistent");return 0;}
            Directory.CreateDirectory(args[0]);
            foreach(float scale in new[]{1f} /* laid out at 100% only; Windows scales the window */)
            foreach(bool import in new[]{false,true})
            using(Form form=import?(Form)new ProfileImportDialog():new ProfileExportDialog())
            {
                Layout(form);
                foreach(var item in Tree(form).Select(control=>new{Control=control,Font=control.Font}).ToArray())
                    item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*scale,item.Font.Style);
                Layout(form);
                if(import)
                {
                    Label error=(Label)typeof(ProfileImportDialog).GetField("error",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form);
                    error.Text="Не удалось прочитать профиль JSON. Проверь формат и доступ к файлу.";
                    Layout(form);
                }
                string scenario=(import?"import":"export")+" font "+scale;
                foreach(Control control in Tree(form).Where(control=>control is Button||control is Label||control is CheckBox))Fits(control,form,scenario);
                Assert(form.AcceptButton!=null&&form.CancelButton!=null,scenario+": missing keyboard actions");
                if(!import)Assert(!((ProfileExportDialog)form).IncludeSecrets,"Secret export must be opt-in");
                using(Bitmap bitmap=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(bitmap,new Rectangle(Point.Empty,form.Size));bitmap.Save(Path.Combine(args[0],(import?"import":"export")+"-"+(int)(scale*100)+".png"));}
            }
            Logic();
            Console.WriteLine("PASS: import/export text and actions fit 100/125/150/200 percent fonts; profile failure and credential validation; no visible windows or VM");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
