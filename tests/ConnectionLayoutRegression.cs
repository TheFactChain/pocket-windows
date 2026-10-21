using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

internal static class ConnectionLayoutRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control item in Tree(child))yield return item;}
    static Point Position(Control control,Control root){Point point=Point.Empty;for(Control item=control;item!=root;item=item.Parent)point.Offset(item.Location);return point;}
    static bool VisibleInEditor(Control control,Control editor){for(Control item=control;item!=editor;item=item.Parent)if(!(bool)typeof(Control).GetMethod("GetState",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(item,new object[]{2}))return false;return true;}
    static void Layout(Form form){typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(form,new object[]{true});for(int i=0;i<4;i++){form.PerformLayout();foreach(Control item in Tree(form).Reverse())item.PerformLayout();Application.DoEvents();}}
    static Dictionary<string,object> View(){return new Dictionary<string,object>{{"profile_id","example"},{"name","Мой сервер"},{"protocol","hysteria2"},{"host","proxy.example.com"},{"port",443},{"tls_server_name","tls.example.com"},{"obfs","salamander"},{"auth_present",true},{"needs_credentials",false},{"selected",true},{"verified",false}};}
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            foreach(float scale in new[]{1f,1.25f,1.5f,2f})
            using(Form form=new Form{ClientSize=new Size(740,600),AutoScaleMode=AutoScaleMode.Dpi,AutoScaleDimensions=new SizeF(96,96),BackColor=NativeStyle.Background})
            using(ConnectionEditor editor=new ConnectionEditor())
            {
                form.Controls.Add(editor);Layout(form);form.ClientSize=new Size(740,600);Layout(form);
                var fonts=Tree(editor).Select(item=>new{Control=item,Font=item.Font}).ToArray();foreach(var item in fonts)item.Control.Font=new Font(item.Font.FontFamily,item.Font.SizeInPoints*scale,item.Font.Style);Layout(form);
                Assert(!VisibleInEditor(editor.Profiles,editor),"An empty saved-profile selector must not be shown");
                editor.LoadProfiles(new object[]{View()});Layout(form);
                Rectangle profile=new Rectangle(Position(editor.Profiles,editor),editor.Profiles.Size);
                Rectangle tool=new Rectangle(Position(editor.NewButton,editor),editor.NewButton.Size);
                Assert(profile.Bottom<=tool.Top,"Saved profile selector overlaps action toolbar at scale "+scale+": "+profile+" / "+tool);
                Assert(Tree(editor).OfType<Label>().Any(item=>item.Text.Contains("поставщика")&&item.Text.Contains("вручную")),"Connection must explain provider settings versus importing a link");
                Assert(!String.IsNullOrEmpty(editor.Fields["host"].AccessibleDescription)&&editor.Fields["host"].AccessibleDescription.Contains("proxy.example.com"),"Server field needs an accessible hostname example");
                Assert(editor.Fields["auth"].UseSystemPasswordChar&&editor.Fields["password"].UseSystemPasswordChar,"Secrets must remain masked");
                var rows=(Dictionary<string,Control[]>)typeof(ConnectionEditor).GetField("rows",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(editor);
                foreach(string key in new[]{"name","host","port","auth"})
                {
                    Control caption=rows[key][0],box=rows[key][1];
                    Assert(Position(caption,editor).Y+caption.Height<=Position(box,editor).Y,"Caption must sit above its own field: "+key);
                    Assert(Position(caption,editor).X==Position(box,editor).X,"Caption and field must share a left edge: "+key);
                }
                Assert(rows["name"][1].Height==rows["protocol"][1].Height&&rows["host"][1].Height==rows["port"][1].Height,"All inputs must share the same height");
                Assert(Position(rows["host"][1],editor).Y==Position(rows["port"][1],editor).Y,"Address and port must align on the same row");
                foreach(Button button in new[]{editor.SaveButton,editor.SelectButton,editor.TestButton})Assert(form.ClientRectangle.Contains(new Rectangle(Position(button,form),button.Size)),"Footer clipped at scale "+scale);
                foreach(TextBox field in editor.Fields.Values.Where(item=>VisibleInEditor(item,editor)))Assert(field.Height<=field.Font.Height*2,"Input stretched vertically: "+field.AccessibleName);
                editor.LoadView(View());Layout(form);
                CheckBox advanced=Tree(editor).OfType<CheckBox>().Single(item=>item.Text=="Дополнительные параметры");
                Assert(advanced.Checked&&VisibleInEditor(editor.Fields["tls_server_name"],editor)&&VisibleInEditor(editor.Obfuscation,editor),"Configured advanced options must open automatically");
                advanced.Checked=false;Layout(form);Dictionary<string,object> request;
                Assert(editor.TrySaveRequest(out request),"Collapsing advanced options must preserve a saved secret and valid configuration");
                var saved=(Dictionary<string,object>)request["profile"];
                Assert((string)saved["tls_server_name"]=="tls.example.com"&&(string)saved["obfs"]=="salamander"&&!saved.ContainsKey("credentials"),"Collapsed settings or saved credentials were changed");
                editor.SetBusy(true);Assert(!advanced.Enabled&&!editor.Protocol.Enabled&&!editor.Fields["host"].Enabled,"Busy state must disable advanced and basic editing");editor.SetBusy(false);
                editor.NewProfile();editor.Protocol.SelectedIndex=1;Layout(form);
                Assert(Tree(editor).OfType<Label>().Any(item=>VisibleInEditor(item,editor)&&item.Text.Contains("TCP")&&item.Text.Contains("UDP")),"TCP-only proxy limitation must be explained");
                if(args.Length>0){Directory.CreateDirectory(args[0]);editor.NewProfile();editor.LoadProfiles(new object[0]);Layout(form);using(Bitmap image=new Bitmap(form.ClientSize.Width,form.ClientSize.Height)){editor.DrawToBitmap(image,new Rectangle(Point.Empty,editor.Size));image.Save(Path.Combine(args[0],"connection-font"+(int)(scale*100)+".png"),ImageFormat.Png);}}
            }
            Console.WriteLine("PASS: native production-manifest connection layout100/125/150/200; toolbar separation; field guidance; advanced roundtrip; masked secrets; busy state; protocol limitation.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
