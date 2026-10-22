using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class DesktopLayoutRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control nested in Tree(child))yield return nested;}
    static void Layout(Control root){typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(root,new object[]{true});root.PerformLayout();}
    static void Pump(Task task){Stopwatch clock=Stopwatch.StartNew();while(!task.IsCompleted&&clock.ElapsedMilliseconds<10000){Application.DoEvents();Thread.Sleep(5);}Assert(task.IsCompleted,"Layout backend timed out");task.GetAwaiter().GetResult();}
    static void TextFits(Control control,string scenario)
    {
        Size preferred=control.GetPreferredSize(new Size(control.Width,0));
        Assert(control.Height>=preferred.Height,scenario+": clipped caption '"+control.Text+"' actual="+control.Size+" preferred="+preferred);
        Assert(control.Parent.ClientRectangle.Contains(control.Bounds),scenario+": caption outside parent '"+control.Text+"'");
    }
    // Home drops secondary parts on purpose when large text would not fit; those are not on screen.
    static bool Shown(Control control){for(Control item=control;item!=null&&!(item is Form);item=item.Parent)if(!(bool)typeof(Control).GetMethod("GetState",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(item,new object[]{2}))return false;return true;}
    static void Inside(Control control,Form form,string scenario)
    {
        Point point=control.Location;for(Control parent=control.Parent;parent!=form;parent=parent.Parent)point.Offset(parent.Location);
        Assert(form.ClientRectangle.Contains(new Rectangle(point,control.Size)),scenario+": outside fixed viewport '"+control.Text+"'");
    }
    static Dictionary<string,object> Report()
    {
        return new Dictionary<string,object>{{"ok",false},{"message","Не хватает свободной памяти"},
            {"settings",new Dictionary<string,object>{{"work_memory_mib",3072},{"gateway_memory_mib",768},{"vcpus",2},{"lan_port",32861}}},
            {"checks",new Dictionary<string,object>{{"memory",new Dictionary<string,object>{{"passed",false},{"title","Не хватает свободной памяти"},{"detail","Нужно 4864 МиБ; доступно 4437 МиБ."},{"action","settings"},{"action_label","Настроить память"}}}}}};
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Directory.CreateDirectory(args[0]);
            float nativeX=0,nativeY=0;
            foreach(float factor in new[]{1f,1.25f,1.5f,2f})
            using(MainForm form=new MainForm(false,(action,payload)=>action=="get-settings"?new Dictionary<string,object>{{"ok",true},{"settings",Report()["settings"]}}:Report()))
            {
                form.ClientSize=new Size(1040,660);Layout(form);form.DisplayReport(Report());Size viewport=form.ClientSize;
                using(Graphics graphics=form.CreateGraphics()){nativeX=graphics.DpiX;nativeY=graphics.DpiY;}
                var fonts=Tree(form).Select(item=>new{Control=item,Font=item.Font}).ToArray();
                foreach(var item in fonts)item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*factor,item.Font.Style);
                Layout(form);string scenario="font-only "+factor+" at unchanged client "+viewport;
                Assert(!Tree(form).OfType<Label>().Any(item=>item.Text=="Windows Claude"),"Application name must stay in the native title bar without a repeated content heading");
                foreach(string name in new[]{"Главная","Подключение","Настройки","Диагностика"})
                {
                    Button nav=Tree(form).OfType<Button>().Single(item=>item.Text==name);Inside(nav,form,scenario);
                    Assert(nav.Width-54>=TextRenderer.MeasureText(nav.Text,nav.Font).Width,scenario+": clipped navigation "+name);
                }
                Control home=(Control)typeof(MainForm).GetField("overview",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(form);
                if(factor==2f)using(Bitmap image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(Path.Combine(args[0],"ui-layout-home-font-200.png"));}
                foreach(Label label in Tree(home).OfType<Label>().Where(item=>!String.IsNullOrEmpty(item.Text)&&Shown(item))){TextFits(label,scenario);Inside(label,form,scenario);}
                Assert(!(home is ScrollableControl)||!((ScrollableControl)home).AutoScroll,"Home must not use scrolling");
                foreach(Control item in Tree(form))Assert(item.Text.IndexOfAny(new[]{'\u2022','\u00b7','\u2219'})<0,"Decorative separators are not allowed");
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());Pump(form.NavigateAsync(AppPage.Settings));Layout(form);
                Inside(form.SettingsPage.SaveButton,form,scenario);
                Assert(form.ClientSize==viewport,"Font-only validation must not grow the window");
                if(factor==1.25f||factor==2f)using(Bitmap image=new Bitmap(form.Width,form.Height))
                {form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(Path.Combine(args[0],"ui-layout-settings-font-"+(int)(factor*100)+".png"));}
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());Pump(form.NavigateAsync(AppPage.Diagnostics));Layout(form);
                foreach(Button button in new[]{form.PrepareButton,form.PrepareHostButton})
                {Assert(button.Parent.ClientRectangle.Contains(button.Bounds),scenario+": clipped preparation action");Assert(button.Width-54>=TextRenderer.MeasureText(button.Text,button.Font).Width||button==form.PrepareHostButton,scenario+": clipped preparation caption");}
            }
            File.WriteAllText(Path.Combine(args[0],"ui-dpi-evidence.json"),new JavaScriptSerializer().Serialize(new Dictionary<string,object>{{"native_dpi_x",nativeX},{"native_dpi_y",nativeY},{"client_width",1040},{"client_height",660},{"synthetic_font_scales",new[]{1,1.25,1.5,2}},{"monitor_settings_changed",false},{"visible_window_shown",false}}));
            Console.WriteLine("PASS: native DPI "+nativeX+"x"+nativeY+"; unchanged 1040x660 client; synthetic font-only 100/125/150/200%; Home does not scroll; preparation captions and pinned Save fit.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
