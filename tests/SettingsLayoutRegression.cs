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
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            Assert(typeof(SettingsEditor).GetField("Resolution",BindingFlags.Instance|BindingFlags.NonPublic)!=null,"Settings must expose a desktop-resolution selector.");
            Assert(typeof(SettingsEditor).GetMethod("LoadWindowsValues",BindingFlags.Instance|BindingFlags.NonPublic)!=null,"Windows display settings must load separately from runtime resources.");
            using(SettingsEditor editor=new SettingsEditor())
            {
                Dictionary<string,object> value;
                Assert(!editor.TryGetWindowsValue(out value),"Windows settings must be loaded before save.");
                editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
                editor.LoadWindowsValues(new Dictionary<string,object>{{"schema",1},{"width",1366},{"height",768}},new Dictionary<string,object>{{"virtual_bytes",85899345920L}});
                Assert(editor.Resolution.SelectedIndex==4&&editor.Resolution.Items.Count==7,"Only the agreed desktop-resolution presets may be selected.");
                editor.Resolution.SelectedIndex=6;
                Assert(editor.TryGetWindowsValue(out value)&&value.Count==3&&(int)value["schema"]==1&&(int)value["width"]==1920&&(int)value["height"]==1080,"Windows display save must return only its own schema.");
                editor.CancelButton.PerformClick();Assert(editor.TryGetWindowsValue(out value)&&(int)value["width"]==1366,"Cancel must restore loaded Windows resolution.");
                editor.Resolution.SelectedIndex=-1;Assert(!editor.TryGetWindowsValue(out value),"An unselected resolution must not be saved.");
                editor.LoadWindowsValues(new Dictionary<string,object>{{"schema",1},{"width",1234},{"height",567}});
                Assert(editor.Resolution.SelectedIndex==-1&&!editor.TryGetWindowsValue(out value),"Unsupported saved modes must remain an explicit error, not silently become a preset.");
                editor.LoadWindowsValues(null);Assert(editor.TryGetWindowsValue(out value)&&(int)value["width"]==1280&&(int)value["height"]==720,"Default Windows desktop must be 1280x720.");
                Assert(editor.TryGetValue(out value)&&value.Count==5&&(int)value["gateway_memory_mib"]==1536&&(int)value["lan_port"]==42345&&(int)value["vcpus"]==2,"Adding Windows display settings must not change the runtime contract.");
                Assert(!editor.Fields.ContainsKey("disk")&&Tree(editor).OfType<Label>().Any(label=>label.Text=="80 ГиБ"),"Windows disk capacity must remain read-only.");
            }
            foreach(float scale in new[]{1f,1.25f,1.5f,2f})
            foreach(Size viewport in new[]{new Size(710,540),new Size(650,440)})
            using(Form host=new Form{ClientSize=viewport,AutoScaleMode=AutoScaleMode.None})
            {
                SettingsEditor editor=new SettingsEditor();host.Controls.Add(editor);
                editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",3072},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
                editor.LoadWindowsValues(null,new Dictionary<string,object>{{"virtual_bytes",85899345920L}});
                var fonts=Tree(editor).Select(control=>new{Control=control,Font=control.Font}).ToArray();
                foreach(var item in fonts) item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*scale,item.Font.Style);
                Layout(host);
                if(scale==1f&&viewport.Width==710) using(Graphics graphics=host.CreateGraphics()) Console.WriteLine("Native render DPI: "+graphics.DpiX+"x"+graphics.DpiY);
                if(args.Length>0)
                {
                    Directory.CreateDirectory(args[0]);
                    using(Bitmap image=new Bitmap(editor.Width,editor.Height)){editor.DrawToBitmap(image,new Rectangle(Point.Empty,editor.Size));image.Save(Path.Combine(args[0],"settings-font"+(int)(scale*100)+"-"+viewport.Width+".png"),ImageFormat.Png);}
                }
                foreach(TextBox field in editor.Fields.Values)
                {
                    Control box=field.Parent.Parent;
                    Assert(box.Height<=field.PreferredHeight+32,"Settings input background stretched: "+box.Height+" vs text "+field.PreferredHeight);
                    Assert(field.Height>=field.PreferredHeight,"Settings text clipped");Inside(field,host,"Settings input");
                }
                Inside(editor.SaveButton,host,"Save");Inside(editor.CancelButton,host,"Cancel");
                Assert(editor.Resolution.Height>=TextRenderer.MeasureText("1920 × 1080",editor.Resolution.Font).Height,"Resolution text must not be clipped at scale "+scale);
                Assert(editor.Resolution.Parent.ClientRectangle.Contains(editor.Resolution.Bounds),"Resolution selector must remain inside its rounded field: "+editor.Resolution.Bounds+" parent="+editor.Resolution.Parent.ClientRectangle+" scale="+scale);
                Dictionary<string,object> value;
                Assert(editor.TryGetValue(out value)&&(int)value["gateway_memory_mib"]==1536&&(int)value["lan_port"]==42345,"Hidden loaded settings changed");
                editor.Fields["work_memory_mib"].Text="";Assert(!editor.TryGetValue(out value),"Empty memory accepted");Layout(host);Inside(editor.SaveButton,host,"Save after validation");
                foreach(Label label in Tree(editor).OfType<Label>().Where(item=>!String.IsNullOrEmpty(item.Text)))
                    Assert(label.Height>=label.GetPreferredSize(new Size(label.Width,0)).Height,"Settings label clipped: "+label.Text);
            }
            Console.WriteLine("PASS: compact resources/resolution, separate Windows schema, allowed modes/default/cancel/unsupported validation, read-only disk, fixed viewports/font100-200, pinned Save and hidden runtime preservation.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
