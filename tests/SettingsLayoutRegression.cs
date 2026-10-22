using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

internal static class SettingsLayoutRegression
{
    static void Assert(bool value,string message) { if(!value) throw new Exception(message); }
    static IEnumerable<Control> Tree(Control root) { yield return root; foreach(Control child in root.Controls) foreach(Control item in Tree(child)) yield return item; }
    static bool OwnVisible(Control control) { return (bool)typeof(Control).GetMethod("GetState",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(control,new object[]{2}); }
    static void Layout(Control root)
    {
        typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(root,new object[]{true});
        for(int index=0;index<3;index++) foreach(Control item in Tree(root).Reverse()) item.PerformLayout();
    }
    static void Inside(Control control,Form form,string message)
    {
        Point origin=Point.Empty; for(Control item=control;item!=form;item=item.Parent) origin.Offset(item.Location);
        Assert(form.ClientRectangle.Contains(new Rectangle(origin,control.Size)),message+": outside viewport");
        Assert(control.Parent.ClientRectangle.Contains(control.Bounds),message+": clipped by parent");
    }
    static void ViewerAndHostCases()
    {
        using(SettingsEditor editor=new SettingsEditor())
        {
            CheckBox fullscreen=editor.StartFullscreen,remember=editor.RememberWindow;
            Button recommend=editor.RecommendedButton;
            Dictionary<string,object> viewer;
            Assert(!editor.TryGetViewerValue(out viewer),"Viewer settings must load before save");
            editor.LoadViewerValues(null);Assert(!fullscreen.Checked&&remember.Checked,"Viewer defaults must preserve windowed launch and remember placement");
            fullscreen.Checked=true;remember.Checked=false;Assert(editor.TryGetViewerValue(out viewer),"Viewer settings must save after loading");
            Assert(viewer.Count==3&&(int)viewer["schema"]==1&&(bool)viewer["start_fullscreen"]&&!(bool)viewer["remember_window"],"Viewer save must contain only its own schema");
            editor.CancelButton.PerformClick();Assert(!fullscreen.Checked&&remember.Checked,"Cancel must restore loaded viewer defaults");
            editor.LoadViewerValues(new Dictionary<string,object>{{"schema",1},{"start_fullscreen",true},{"remember_window",false}});fullscreen.Checked=false;remember.Checked=true;editor.CancelButton.PerformClick();Assert(fullscreen.Checked&&!remember.Checked,"Cancel must restore explicit viewer preferences");
            editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
            Assert(!recommend.Enabled,"Recommendations must wait for host resource evidence");
            editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",10000},{"logical_processors",12}});recommend.PerformClick();
            Dictionary<string,object> runtime;Assert(editor.TryGetValue(out runtime)&&(int)runtime["work_memory_mib"]==7424&&(int)runtime["vcpus"]==4,"Recommendation must reserve gateway plus 1 GiB and round RAM down to 256 MiB");
            Assert((int)runtime["gateway_memory_mib"]==1536&&(int)runtime["lan_port"]==42345,"Recommendations must preserve hidden runtime values");
            Assert(Tree(editor).OfType<Label>().Any(label=>label.Text.Contains("7,25")||label.Text.Contains("7.25")),"Windows memory needs a compact GiB value");
            editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",8192},{"ram_available_mib",2048},{"logical_processors",1}});recommend.PerformClick();
            Assert(editor.Fields["work_memory_mib"].Text=="7424"&&editor.Fields["vcpus"].Text=="4"&&!String.IsNullOrEmpty(editor.Error.Text),"Insufficient RAM must explain the problem and preserve both fields");
            editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",8192},{"ram_available_mib",8192},{"logical_processors",1}});recommend.PerformClick();Assert(editor.TryGetValue(out runtime)&&(int)runtime["work_memory_mib"]==4096&&(int)runtime["vcpus"]==1,"Recommendation must cap at half host RAM and retain at least one CPU");
            editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",65536},{"ram_available_mib",64000},{"logical_processors",3}});recommend.PerformClick();Assert(editor.TryGetValue(out runtime)&&(int)runtime["work_memory_mib"]==8192&&(int)runtime["vcpus"]==1,"Recommendations must cap RAM at 8 GiB and round CPU division down");
            editor.CancelButton.PerformClick();Assert(editor.TryGetValue(out runtime)&&(int)runtime["work_memory_mib"]==3072&&(int)runtime["vcpus"]==2,"Cancel must restore loaded resource values after recommendations");
            editor.LoadHostValues(null);Assert(!recommend.Enabled,"Missing host evidence must disable recommendations");
            Assert(!editor.Dirty,"Loaded settings must not report unsaved changes");
            editor.MemorySlider.Value=4096;Assert(editor.Fields["work_memory_mib"].Text=="4096"&&editor.Dirty,"The memory slider must edit the memory value and mark the draft unsaved");
            editor.Fields["work_memory_mib"].Text="5120";Assert(editor.MemorySlider.Value==5120,"A typed memory value must move the slider");
            editor.CancelButton.PerformClick();Assert(!editor.Dirty&&editor.MemorySlider.Value==3072,"Cancel must clear the unsaved-changes note and restore the slider");
        }
    }
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            ViewerAndHostCases();
            Assert(typeof(SettingsEditor).GetField("Resolution",BindingFlags.Instance|BindingFlags.NonPublic)!=null,"Settings must expose a desktop-resolution selector.");
            Assert(typeof(SettingsEditor).GetMethod("LoadWindowsValues",BindingFlags.Instance|BindingFlags.NonPublic)!=null,"Windows display settings must load separately from runtime resources.");
            using(SettingsEditor editor=new SettingsEditor())
            {
                Dictionary<string,object> value;
                Assert(!editor.TryGetWindowsValue(out value),"Windows settings must be loaded before save.");
                editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
                editor.LoadWindowsValues(new Dictionary<string,object>{{"schema",1},{"width",1366},{"height",768},{"fit",false}},new Dictionary<string,object>{{"virtual_bytes",85899345920L}});
                Assert(editor.Resolution.SelectedIndex==4&&editor.Resolution.Items.Count==11,"Only the agreed standard desktop-resolution presets may be selected.");
                editor.Resolution.SelectedIndex=8;
                Assert(editor.TryGetWindowsValue(out value)&&value.Count==4&&(int)value["schema"]==1&&(int)value["width"]==1920&&(int)value["height"]==1080&&!(bool)value["fit"],"Windows display save must return only its own schema.");
                editor.CancelButton.PerformClick();Assert(editor.TryGetWindowsValue(out value)&&(int)value["width"]==1366,"Cancel must restore loaded Windows resolution.");
                editor.Resolution.SelectedIndex=-1;Assert(!editor.TryGetWindowsValue(out value),"An unselected resolution must not be saved.");
                editor.LoadWindowsValues(new Dictionary<string,object>{{"schema",1},{"width",1234},{"height",567},{"fit",false}});
                Assert(editor.Resolution.SelectedIndex==-1&&!editor.TryGetWindowsValue(out value),"Unsupported saved modes must remain an explicit error, not silently become a preset.");
                editor.LoadWindowsValues(null);Assert(editor.Resolution.SelectedIndex==8&&editor.TryGetWindowsValue(out value)&&!(bool)value["fit"]&&(int)value["width"]==1920&&(int)value["height"]==1080,"Default Windows desktop is the standard 1920x1080.");
                editor.LoadWindowsValues(new Dictionary<string,object>{{"schema",1},{"width",1280},{"height",720},{"fit",true}});Assert(editor.Resolution.SelectedIndex==8&&editor.TryGetWindowsValue(out value)&&!(bool)value["fit"]&&(int)value["width"]==1920,"An old 'as the PC screen' setting opens as 1920x1080 and saves as a fixed size.");
                Assert(!Enumerable.Range(0,editor.Resolution.Items.Count).Any(item=>Convert.ToString(editor.Resolution.Items[item]).Contains("ПК")),"The 'as the PC screen' choice is gone.");
                Assert(editor.TryGetValue(out value)&&value.Count==5&&(int)value["gateway_memory_mib"]==1536&&(int)value["lan_port"]==42345&&(int)value["vcpus"]==2,"Adding Windows display settings must not change the runtime contract.");
                Assert(!editor.Fields.ContainsKey("disk")&&Tree(editor).OfType<Label>().Any(label=>label.Text=="80 ГиБ"),"Windows disk capacity must remain read-only.");
            }
            foreach(float scale in new[]{1f} /* laid out at 100% only; Windows scales the window */)
            foreach(Size viewport in new[]{new Size(676,560),new Size(616,540)})
            using(Form host=new Form{ClientSize=viewport,AutoScaleMode=AutoScaleMode.None})
            {
                SettingsEditor editor=new SettingsEditor();host.Controls.Add(editor);
                editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
                editor.LoadWindowsValues(null,new Dictionary<string,object>{{"virtual_bytes",85899345920L}});
                editor.LoadFileSharingValues(new Dictionary<string,object>{{"schema",1},{"mode","selected"},{"folders",new[]{@"C:\Work",@"D:\Photos"}}});
                editor.LoadViewerValues(null);editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",10000},{"logical_processors",12}});
                var fonts=Tree(editor).Select(control=>new{Control=control,Font=control.Font}).ToArray();
                foreach(var item in fonts) item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*scale,item.Font.Style);
                Layout(host);
                if(scale==1f&&viewport.Width==676)Assert(Tree(editor).OfType<Label>().Single(label=>label.Text.StartsWith("Рекомендуется")).Width>=250,"Host hint must use the space left beside Recommendations");
                if(scale==1f&&viewport.Width==676) using(Graphics graphics=host.CreateGraphics()) Console.WriteLine("Native render DPI: "+graphics.DpiX+"x"+graphics.DpiY);
                if(args.Length>0)
                {
                    Directory.CreateDirectory(args[0]);
                    using(Bitmap image=new Bitmap(editor.Width,editor.Height)){editor.DrawToBitmap(image,new Rectangle(Point.Empty,editor.Size));image.Save(Path.Combine(args[0],"settings-font"+(int)(scale*100)+"-"+viewport.Width+".png"),ImageFormat.Png);}
                }
                foreach(TextBox field in new[]{editor.Fields["vcpus"]})
                {
                    Control box=field.Parent.Parent;
                    Assert(box.Height<=field.PreferredHeight+32,"Settings input background stretched: "+box.Height+" vs text "+field.PreferredHeight);
                    Assert(field.Height>=field.PreferredHeight,"Settings text clipped");Inside(field,host,"Settings input");
                }
                Inside(editor.MemorySlider,host,"Memory slider");Inside(editor.SaveButton,host,"Save");Inside(editor.CancelButton,host,"Cancel");
                Assert(editor.RecommendedButton.Width>=editor.RecommendedButton.GetPreferredSize(Size.Empty).Width,"Recommendations button text clipped at scale "+scale);
                Panel pages=Tree(editor).OfType<Panel>().Single(panel=>panel.AutoScroll);
                for(int section=0;section<3;section++)
                {
                    editor.ShowSection(section);Layout(host);
                    // Each section fits the window without a scroll bar at ordinary text sizes.
                    if(scale<=1.5f&&(viewport.Width==676||scale==1f))Assert(!pages.VerticalScroll.Visible,"Settings section "+section+" needs a scroll bar at scale "+scale+" in "+viewport+": content "+pages.DisplayRectangle.Height+" > visible "+pages.ClientSize.Height);
                }
                editor.ShowSection(1);Layout(host);
                foreach(CheckBox option in new[]{editor.StartFullscreen,editor.RememberWindow})Assert(option.Height>=TextRenderer.MeasureText(option.Text,option.Font,new Size(Math.Max(1,option.Width-32),0),TextFormatFlags.WordBreak).Height,"Viewer preference must show its complete text at scale "+scale);
                Assert(editor.Resolution.Height>=TextRenderer.MeasureText("1920 × 1080",editor.Resolution.Font).Height,"Resolution text must not be clipped at scale "+scale);
                Assert(editor.Resolution.Parent.ClientRectangle.Contains(editor.Resolution.Bounds),"Resolution selector must remain inside its rounded field: "+editor.Resolution.Bounds+" parent="+editor.Resolution.Parent.ClientRectangle+" scale="+scale);
                Panel scroll=Tree(editor).OfType<Panel>().Single(panel=>panel.AutoScroll);
                scroll.ScrollControlIntoView(editor.RememberWindow);Layout(host);Inside(editor.RememberWindow,host,"Remember placement");
                foreach(CheckBox option in new[]{editor.StartFullscreen,editor.RememberWindow})Assert(option.Height>=option.GetPreferredSize(new Size(option.Width,0)).Height,"Viewer preference text clipped at scale "+scale);
                if(args.Length>0)using(Bitmap image=new Bitmap(editor.Width,editor.Height)){editor.DrawToBitmap(image,new Rectangle(Point.Empty,editor.Size));image.Save(Path.Combine(args[0],"settings-options-font"+(int)(scale*100)+"-"+viewport.Width+".png"),ImageFormat.Png);}
                editor.ShowSection(2);Layout(host);
                ComboBox fileMode=Tree(editor).OfType<ComboBox>().Single(item=>item.AccessibleName=="Доступ к файлам ПК");
                foreach(Button mode in Tree(editor).OfType<Button>().Where(button=>new[]{"Выключено","Выбранные папки","Все диски ПК"}.Contains(button.Text)))
                {scroll.ScrollControlIntoView(mode);Layout(host);Inside(mode,host,"File sharing mode button");Assert(mode.Height>=TextRenderer.MeasureText(mode.Text,mode.Font).Height,"File mode text clipped at scale "+scale);}
                ListBox list=Tree(editor).OfType<ListBox>().Single();Assert(list.Height>=list.ItemHeight*3,"Folder list must show three whole rows at scale "+scale);
                Button addFolder=Tree(editor).OfType<Button>().Single(button=>button.Text=="Добавить папку");
                scroll.ScrollControlIntoView(addFolder);Layout(host);Inside(addFolder,host,"Add folder");
                foreach(Button action in Tree(addFolder.Parent).OfType<Button>())Assert(action.Width>=action.GetPreferredSize(Size.Empty).Width&&action.Height>=action.GetPreferredSize(Size.Empty).Height,"Folder action clipped at scale "+scale);
                Label fileNotice=Tree(editor).OfType<Label>().Single(label=>label.Text.StartsWith("Запись из Windows"));
                scroll.ScrollControlIntoView(fileNotice);Layout(host);Inside(fileNotice,host,"File sharing notice");Inside(editor.SaveButton,host,"Save while file sharing visible");Inside(editor.CancelButton,host,"Cancel while file sharing visible");
                if(args.Length>0)using(Bitmap image=new Bitmap(editor.Width,editor.Height)){editor.DrawToBitmap(image,new Rectangle(Point.Empty,editor.Size));image.Save(Path.Combine(args[0],"settings-files-font"+(int)(scale*100)+"-"+viewport.Width+".png"),ImageFormat.Png);}
                scroll.AutoScrollPosition=Point.Empty;Layout(host);
                Dictionary<string,object> value;
                Assert(editor.TryGetValue(out value)&&(int)value["gateway_memory_mib"]==1536&&(int)value["lan_port"]==42345,"Hidden loaded settings changed");
                editor.Fields["work_memory_mib"].Text="";Assert(!editor.TryGetValue(out value),"Empty memory accepted");Layout(host);Inside(editor.SaveButton,host,"Save after validation");
                foreach(Label label in Tree(editor).OfType<Label>().Where(item=>!String.IsNullOrEmpty(item.Text)&&OwnVisible(item)))
                    Assert(label.Height>=label.GetPreferredSize(new Size(label.Width,0)).Height,"Settings label clipped: "+label.Text);
            }
            Console.WriteLine("PASS: viewer defaults/save/cancel; host recommendations/caps/low-memory/hidden-values; compact resources/resolution, separate Windows schema, allowed modes/default/cancel/unsupported validation, read-only disk, fixed viewports/font100-200, pinned Save and hidden runtime preservation.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
